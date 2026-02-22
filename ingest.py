from gmail import get_service, get_unread_emails, mark_as_read
from evaluate import process_newsletter
import traceback

def ingest():
    print("Checking for new newsletters...")
    service = get_service()
    emails = get_unread_emails()

    if not emails:
        print("No new emails.")
        return

    for subject, body, message_id in emails:
        if not body.strip():
            print(f"  Skipping {subject} — empty body.")
            mark_as_read(service, message_id)
            continue
        print(f"Processing: {subject}")
        process_newsletter(body, source=subject)
        mark_as_read(service, message_id)
        print(f"Done: {subject}")

if __name__ == "__main__":
    try:
        ingest()
    except Exception as e:
        # send yourself an alert email
        from gmail import send_report
        send_report(
            to_address="logan.hartford@outlook.com",
            subject="Startup Scout - Ingest Failed",
            markdown_body=f"ingest.py crashed with the following error:\n\n{traceback.format_exc()}"
        )
        raise