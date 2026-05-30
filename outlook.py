import html
import os
import re
import pickle
import requests
import msal
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite", "Mail.Send"]
AUTHORITY = "https://login.microsoftonline.com/consumers"

CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID")
TOKEN_FILE = os.getenv("OUTLOOK_TOKEN_FILE", "outlook_token.pickle")
BODY_PREVIEW_CHARS = 800


def _load_token_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            cache.deserialize(pickle.load(f))
    return cache


def _save_token_cache(cache):
    if cache.has_state_changed:
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(cache.serialize(), f)


def _get_access_token(interactive=True):
    cache = _load_token_cache()
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result or "access_token" not in result:
        if not interactive:
            raise RuntimeError("Outlook re-authentication required. Run: python3 -c \"import outlook; outlook.get_session()\"")
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description')}")

    _save_token_cache(cache)
    return result["access_token"]


def get_session(interactive=True):
    """Return a requests.Session with the Bearer token pre-configured."""
    token = _get_access_token(interactive=interactive)
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    return session


def _extract_text(body):
    content = body.get("content", "")
    if body.get("contentType", "").lower() == "html":
        content = re.sub(r"<[^>]+>", " ", content)
        content = html.unescape(content)
        content = re.sub(r"\s+", " ", content).strip()
    return content[:BODY_PREVIEW_CHARS]


def _parse_unsubscribe(headers):
    """Parse List-Unsubscribe headers. Returns dict with url/mailto/one_click or None."""
    unsub_value = None
    one_click = False

    for h in headers:
        name = h.get("name", "").lower()
        value = h.get("value", "")
        if name == "list-unsubscribe":
            unsub_value = value
        elif name == "list-unsubscribe-post":
            one_click = "list-unsubscribe=one-click" in value.lower()

    if not unsub_value:
        return None

    urls = re.findall(r"<([^>]+)>", unsub_value)
    mailto = next((u for u in urls if u.startswith("mailto:")), None)
    http_url = next((u for u in urls if u.startswith("http")), None)

    return {
        "one_click": one_click and bool(http_url),
        "url": http_url,
        "mailto": mailto,
    }


def _send_unsubscribe_email(session, mailto_url):
    """Send an unsubscribe email via Graph API. Returns True on success."""
    parsed = urlparse(mailto_url)
    address = parsed.path
    params = parse_qs(parsed.query)
    subject = params.get("subject", ["unsubscribe"])[0]
    body = params.get("body", [""])[0]

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": address}}],
        },
        "saveToSentItems": False,
    }
    resp = session.post(f"{GRAPH_BASE}/me/sendMail", json=payload)
    return resp.ok


def do_unsubscribe(session, email):
    """
    Attempt to unsubscribe from the sender. Returns (success, method).
    Priority: one-click POST > URL GET > mailto
    """
    unsub = email.get("unsubscribe")
    if not unsub:
        return False, "no_header"

    if unsub["one_click"] and unsub["url"]:
        try:
            resp = requests.post(
                unsub["url"],
                data="List-Unsubscribe=One-Click",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
                allow_redirects=True,
            )
            if resp.ok:
                return True, "one_click"
        except Exception:
            pass

    if unsub["url"]:
        try:
            resp = requests.get(unsub["url"], timeout=15, allow_redirects=True)
            if resp.ok:
                return True, "url_get"
        except Exception:
            pass

    if unsub["mailto"]:
        success = _send_unsubscribe_email(session, unsub["mailto"])
        return success, "mailto"

    return False, "failed"


def get_emails(session, max_emails=50, unread_only=False, min_age_hours=0):
    """Return list of inbox messages as dicts with id/subject/sender/body_preview/unsubscribe."""
    from datetime import datetime, timezone, timedelta
    url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
    params = {
        "$select": "id,subject,from,body,internetMessageHeaders",
        "$top": max_emails,
        "$orderby": "receivedDateTime desc",
    }
    filters = []
    if unread_only:
        filters.append("isRead eq false")
    if min_age_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
        filters.append(f"receivedDateTime le {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if filters:
        params["$filter"] = " and ".join(filters)
    resp = session.get(url, params=params)
    resp.raise_for_status()

    emails = []
    for msg in resp.json().get("value", []):
        sender = msg.get("from", {}).get("emailAddress", {}).get("address", "unknown")
        subject = msg.get("subject") or "(no subject)"
        body_preview = _extract_text(msg.get("body", {}))
        headers = msg.get("internetMessageHeaders") or []
        is_reply = any(h.get("name", "").lower() == "in-reply-to" for h in headers)
        emails.append({
            "id": msg["id"],
            "subject": subject,
            "sender": sender,
            "body_preview": body_preview,
            "unsubscribe": _parse_unsubscribe(headers),
            "is_reply": is_reply,
        })
    return emails


def create_reply_draft(session, original_message_id, body_text):
    """Create a reply draft preserving thread. Returns draft message ID."""
    url = f"{GRAPH_BASE}/me/messages/{original_message_id}/createReply"
    resp = session.post(url, json={})
    resp.raise_for_status()
    draft_id = resp.json()["id"]
    patch_url = f"{GRAPH_BASE}/me/messages/{draft_id}"
    resp = session.patch(patch_url, json={"body": {"contentType": "Text", "content": body_text}})
    resp.raise_for_status()
    return draft_id


def send_draft(session, draft_id):
    """Send a draft message. Returns True on success."""
    url = f"{GRAPH_BASE}/me/messages/{draft_id}/send"
    resp = session.post(url)
    return resp.ok


def move_to_archive(session, message_id):
    """Move message to Archive folder. Returns True on success."""
    url = f"{GRAPH_BASE}/me/messages/{message_id}/move"
    resp = session.post(url, json={"destinationId": "archive"})
    return resp.ok or resp.status_code == 404


def move_to_trash(session, message_id):
    """Move message to Deleted Items (recoverable). Returns True on success."""
    url = f"{GRAPH_BASE}/me/messages/{message_id}/move"
    resp = session.post(url, json={"destinationId": "deleteditems"})
    return resp.ok or resp.status_code == 404  # 404 = already gone, goal achieved


def mark_as_read(session, message_id):
    """Mark message as read. Returns True on success."""
    url = f"{GRAPH_BASE}/me/messages/{message_id}"
    resp = session.patch(url, json={"isRead": True})
    return resp.ok
