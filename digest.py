
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

import datetime
from datetime import timedelta
import os

import imaplib
import re
import json
from email_utils import decode_text, get_plain_body, LLMClient
from ollama import Client


from typing import List, Tuple


# The Proton credentials are required but remain unchanged from the original script.
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

MODEL = "gpt-oss:20b"










def _fetch_unread_since(days: int = 3) -> List[Tuple[str, email.message.EmailMessage]]:
    """Return unread messages received in the last *days* days.

    The UTC date string is formatted for IMAP ``SINCE`` queries.  We always
    connect via SSL unless the user supplies port 1143, which falls back to a plain
    connection with ``STARTTLS``.
    """
    since_date = (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")

    if PROTON_IMAP_PORT == 993:
        imap = imaplib.IMAP4_SSL(PROTON_IMAP_HOST, PROTON_IMAP_PORT)
    else:
        imap = imaplib.IMAP4(PROTON_IMAP_HOST, PROTON_IMAP_PORT)
        imap.starttls()
    imap.login(PROTON_USER, PROTON_BRIDGE_PASSWORD)
    imap.select("INBOX", readonly=True)

    typ, data = imap.uid("SEARCH", f"SINCE {since_date} UNSEEN")
    if typ != "OK":
        raise RuntimeError("Failed to search for unread messages")

    uids: List[bytes] = data[0].split() if data else []
    results: List[Tuple[str, email.message.EmailMessage]] = []
    for uid_bytes in uids:
        typ, msg_data = imap.uid("FETCH", uid_bytes.decode(), "(RFC822)")
        if typ != "OK":
            continue
        raw_email = b"".join(part[1] for part in msg_data if isinstance(part, tuple))
        results.append((uid_bytes.decode(), email.message_from_bytes(raw_email)))
    imap.logout()
    return results



_KEYWORDS: List[str] = ["urgent", "action required", "meeting", "important"]

def _is_important(msg: email.message.EmailMessage) -> dict:
    """Return the LLM analysis as a structured dict.

    The function builds a concise prompt, sends it to Ollama and returns the parsed
    JSON.  Any parse failure results in an empty dict – callers treat this as not
    important.
    """
    subject = msg.get("Subject", "")
    raw_from = msg.get("From", "")
    sender_addr = email.utils.parseaddr(raw_from)[1]
    date_header = msg.get("Date")
    try:
        dt_obj = email.utils.parsedate_to_datetime(date_header) if date_header else None
        timestamp_iso = dt_obj.isoformat() if dt_obj else ""
    except Exception:
        timestamp_iso = ""

    body_preview = get_plain_body(msg).strip()[:750]

    # Use the shared LLM client to classify importance.
    client = LLMClient(model=MODEL)
    llm_result = client.classify_importance(sender_addr, subject, body_preview)

    if not isinstance(llm_result, dict):
        return {}

    return {
        "sender": sender_addr,
        "subject": subject,
        "timestamp": timestamp_iso,
        "important": llm_result.get("important", False),
        "reason": llm_result.get("reason", ""),
    }







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
        snippet_raw = get_plain_body(current_msg).strip()
        snippet = snippet_raw[:80].replace("\n", " ") + ('…' if len(snippet_raw) > 80 else "")
        body_lines.append(f"- {info['subject']} from {info['sender']}: {snippet}")
    prompt_body = "\\n".join(body_lines)
    prompt = f"Summarise the following important recent emails into one concise paragraph:\\n{prompt_body}"
    
    client = Client()
    response = client.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
    prompt2 = f"Given {response}, advise as to my next three steps."
    response2 = client.chat(model=MODEL, messages=[{"role": "user", "content": prompt2}])
    if hasattr(response2, "message"):
        summary_text = response2.message.content.strip()
    else:
        summary_text = str(response2)


    # Persist data for later display
    digest_payload = {"emails": [info for info, _ in important_items], "summary": summary_text}
    with open("database-digest.json", "w", encoding="utf-8") as f:
        import json as _json
        _json.dump(digest_payload, f, indent=2)
    return summary_text






if __name__ == "__main__":  # pragma: no cover - manual running only
    print(create_digest())
