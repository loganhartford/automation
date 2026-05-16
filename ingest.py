from gmail import get_service, get_unread_emails, mark_as_read, send_report
from evaluate import process_newsletter
import anthropic
import traceback

def ingest():
    print("Checking for new newsletters...")
    service = get_service()
    emails = get_unread_emails()

    if not emails:
        print("No new emails.")
        return

    for subject, sender_email, body, message_id in emails:
        if not body.strip():
            print(f"  Skipping {subject} — empty body.")
            mark_as_read(service, message_id)
            continue
        print(f"Processing: {subject} (from {sender_email})")
        process_newsletter(body, source=sender_email)
        mark_as_read(service, message_id)
        print(f"Done: {subject}")

if __name__ == "__main__":
    try:
        ingest()
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e):
            send_report(
                to_address="logan.hartford@outlook.com",
                subject="Startup Scout - Out of API Credits",
                markdown_body="Anthropic API credit balance is too low. Top up at https://console.anthropic.com/settings/billing to resume processing."
            )
            print("Out of Anthropic credits — alert sent.")
        else:
            send_report(
                to_address="logan.hartford@outlook.com",
                subject="Startup Scout - Ingest Failed",
                markdown_body=f"ingest.py crashed with the following error:\n\n{traceback.format_exc()}"
            )
            raise
    except Exception as e:
        send_report(
            to_address="logan.hartford@outlook.com",
            subject="Startup Scout - Ingest Failed",
            markdown_body=f"ingest.py crashed with the following error:\n\n{traceback.format_exc()}"
        )
        raise