# digest.py
"""Generate a summary digest of recent important Proton Mail emails.

The script mirrors the Proton Bridge connection used in :mod:`proton` but is
light‑weight: it logs into IMAP, fetches unread messages from the last three
days, heuristically determines which are *important*, and outputs a one‑paragraph
summary that can be printed or written to a file.

Environment variables required:

```
PROTON_USER            # Proton Bridge username/email
PROTON_BRIDGE_PASSWORD # Proton Bridge password/token
PROTON_IMAP_HOST       # optional, defaults to 127.0.0.1
PROTON_IMAP_PORT       # optional, defaults to 1143
```
"""
from __future__ import annotations

import datetime as _dt
import email
import imaplib
import re
import json
import os
import json
from ollama import Client

from typing import List, Tuple

# Environment configuration -----------------------------------------------------
try:
    PROTON_USER = os.environ["PROTON_USER"]
except KeyError:  # pragma: no cover - handled by the developer in tests
    raise RuntimeError("Environment variable PROTON_USER is not set")

try:
    PROTON_BRIDGE_PASSWORD = os.environ["PROTON_BRIDGE_PASSWORD"]
except KeyError:  # pragma: no cover
    raise RuntimeError("Environment variable PROTON_BRIDGE_PASSWORD is not set")

PROTON_IMAP_HOST = os.getenv("PROTON_IMAP_HOST", "127.0.0.1").strip() or "127.0.0.1"
PROTON_IMAP_PORT = int(os.getenv("PROTON_IMAP_PORT", "1143"))

# ---------------------------------------------------------------------------
# Helper utilities --------------------------------------------------------------

def _get_plain_body(msg: email.message.EmailMessage) -> str:
    """Return the first plain‑text part of *msg*, falling back to HTML if needed."""
    # Prefer a plain text part
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # No plain part, look for HTML
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_text = payload.decode(charset, errors="replace")
                    # Strip tags naïvely
                    return re.sub(r"<[^>]+>", "", html_text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            # If the message is pure HTML, strip tags
            return re.sub(r"<[^>]+>", "", text)
    return ""

# ---------------------------------------------------------------------------
# Core logic -------------------------------------------------------------

def _fetch_unread_since(days: int = 3) -> List[Tuple[str, email.message.EmailMessage]]:
    """Fetch unread messages from the last *days* days.

    Returns a list of ``(uid, parsed_msg)`` tuples.
    """
    # Calculate date string in IMAP date format (e.g., 01-Jan-2023)
    since_date = (_dt.datetime.utcnow() - _dt.timedelta(hours=10)).strftime("%d-%b-%Y")

    if PROTON_IMAP_PORT == 993:
        imap = imaplib.IMAP4_SSL(PROTON_IMAP_HOST, PROTON_IMAP_PORT)
    else:
        imap = imaplib.IMAP4(PROTON_IMAP_HOST, PROTON_IMAP_PORT)
        imap.starttls()
    imap.login(PROTON_USER, PROTON_BRIDGE_PASSWORD)
    imap.select("INBOX", readonly=True)

    typ, data = imap.uid("SEARCH", f"SINCE {since_date} UNSEEN")
    if typ != "OK":  # pragma: no cover - unlikely in normal conditions
        raise RuntimeError("Failed to search for unread messages")

    uids = data[0].split()
    results = []
    for uid_bytes in uids:
        typ, msg_data = imap.uid("FETCH", uid_bytes.decode(), "(RFC822)")
        if typ != "OK":  # pragma: no cover
            continue
        raw_email = b"".join(part[1] for part in msg_data if isinstance(part, tuple))
        msg = email.message_from_bytes(raw_email)
        results.append((uid_bytes.decode(), msg))

    imap.logout()
    return results

# ---------------------------------------------------------------------------
# Heuristics to mark important emails -------------------------------------
_KEYWORDS: List[str] = ["urgent", "action required", "meeting", "important"]

def _is_important(msg: email.message.EmailMessage) -> dict:
    """Return a JSON object describing whether the message is important.

    The function asks the LLM to return the analysis as valid JSON in the
    exact form shown below. Any deviation from this shape results in the
    message being marked as not important.
    ```json
    {
        "sender": "email@example.com",
        "subject": "Subject line",
        "timestamp": "2026‑08‑11T12:34:56Z",
        "important": true,
        "reason": "brief explanation"
    }
    ```
    If the LLM returns any other format, an empty dictionary is returned.
    """
    # Basic metadata extraction
    subject = msg.get("Subject", "")
    raw_from = msg.get("From", "")
    sender_addr = email.utils.parseaddr(raw_from)[1]
    date_header = msg.get("Date")
    try:
        dt_obj = email.utils.parsedate_to_datetime(date_header) if date_header else None
        timestamp_iso = dt_obj.isoformat() if dt_obj else ""
    except Exception:
        timestamp_iso = ""

    body_preview = _get_plain_body(msg).strip()[:750]

    prompt = (
        f"Analyze the following email To determine its important.\n"
        f"An email is important if it has an urgent impact on personal or family health, finance, or security.\n"
        f"An email is important ONLY IF it cannot be ignored without grave repercussions for the above.\n"
        f"Examples of important emails: emails from school, from work, from governments, etc.\n"
        f"Examples of unimportant emails: marketing, solicitation, community announcements, pet adoptions, politics, social media, etc.\n"
        f"An email is not important if it can be safely ignored without consequence.\n"
        f"Output a JSON object with these keys:\n"
        f"- sender: {sender_addr}\n"
        f"- subject: {subject}\n"
        f"- timestamp: {timestamp_iso}\n"
        f"- important: true/false indicating if the email is important\n"
        f"- reason: short explanation for the decision\n"
        f"Provide only the JSON object, nothing else.\n"
        f"Email Subject: {subject}\n"
        f"Email Body preview: {body_preview}"
    )
    #print(f"Prompting LLM for importance analysis:\n{prompt}\n")
    client = Client()
    response = client.chat(model="qwen3:4b", messages=[{"role": "user", "content": prompt}])
    content = None
    if hasattr(response, "message"):
        content = response.message.content.strip()
    else:
        # In case of non‑chat responses; unlikely but defensive.
        if hasattr(response, "json"):
            try:
                content = response.json()["content"].strip()
            except Exception:
                pass
    if not content:
        return {}

    try:
        result = json.loads(content)
        # Ensure all required keys are present.
        req_keys = {"sender", "subject", "timestamp", "important", "reason"}

        print(f"LLM analysis result: {result.get('important', False)} - {result.get('reason', '')}")

        if not isinstance(result, dict) or not req_keys.issubset(result):
            return {}
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Digest generation ------------------------------------------------------------

def create_digest() -> str:
    """Return a paragraph summarising all important unread emails.

    The function calls ``_is_important`` which now returns a dictionary
    describing each email. Messages flagged as important are collected,
    then sent to the LLM for a prose summary.
    """
    mails = _fetch_unread_since(days=1)
    important_items: List[Tuple[dict, email.message.EmailMessage]] = []
    for uid, current_msg in mails:
        print(f"Analyzing email UID {uid}...")
        info = _is_important(current_msg)
        if not info.get("important"):
            continue
        important_items.append((info, current_msg))
    
    if not important_items:
        return "No new important emails in the last three days."
    
    body_lines = []
    for info, current_msg in important_items:
        snippet_raw = _get_plain_body(current_msg).strip()
        snippet = snippet_raw[:80].replace("\n", " ") + ('…' if len(snippet_raw) > 80 else "")
        body_lines.append(f"- {info['subject']} from {info['sender']}: {snippet}")
    prompt_body = "\n".join(body_lines)
    prompt = f"Summarise the following important recent emails into one concise paragraph:\n{prompt_body}"
    
    client = Client()
    response = client.chat(model="qwen3:4b", messages=[{"role": "user", "content": prompt}])
    prompt2 = f"Given {response}, advise as to my next three steps."
    response2 = client.chat(model="qwen3:4b", messages=[{"role": "user", "content": prompt2}])
    if hasattr(response2, "message"):
        return response2.message.content.strip()
    return str(response2)




# ---------------------------------------------------------------------------
# Command line entry point -----------------------------------------------------
if __name__ == "__main__":  # pragma: no cover - manual running only
    print(create_digest())
