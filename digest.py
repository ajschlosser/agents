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
import os
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
    since_date = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime("%d-%b-%Y")

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

def _is_important(msg: email.message.EmailMessage) -> bool:
    subject_raw = msg["Subject"] or ""
    body_raw = _get_plain_body(msg)
    text = (subject_raw + "\n" + body_raw).lower()
    return any(keyword in text for keyword in _KEYWORDS)

# ---------------------------------------------------------------------------
# Digest generation ------------------------------------------------------------

def create_digest() -> str:
    """Return a paragraph summarising all important unread emails.

    This function gathers unread, un‑seen Proton Mail messages from the last three days,
    filters those that are considered *important*, and sends a prompt to an Ollama LLM
    (e.g. ``llama3``).  The model returns a concise prose paragraph summarising the
    collected items.

    Returns:
        A single‑paragraph string produced by the LLM, or a default message if no
        important emails were found.
    """
    mails = _fetch_unread_since(days=3)
    important: List[Tuple[str, str, str]] = []
    for uid, msg in mails:
        if not _is_important(msg):
            continue
        sender_raw = msg.get("From", "")
        name, address = email.utils.parseaddr(sender_raw)
        subject = msg.get("Subject", "(no subject)")
        body_text = _get_plain_body(msg).strip()
        snippet = body_text[:80].replace("\n", " ") + ("…" if len(body_text) > 80 else "")
        important.append((subject, address or name, snippet))

    if not important:
        return "No new important emails in the last three days."

    # Build a prompt that lists each email on its own line.
    body_lines = [f"- {s} from {a}: {p}" for s, a, p in important]
    prompt_body = "\n".join(body_lines)
    prompt = f"Summarise the following important recent emails into one concise paragraph:\n{prompt_body}"

    # Call Ollama locally.
    client = Client()
    response = client.chat(model="llama3", messages=[{"role": "user", "content": prompt}])
    # The ChatResponse contains a Message.  Extract the content safely.
    if hasattr(response, "message"):
        return response.message.content.strip()
    # Fallback to string representation for unexpected responses.
    return str(response)


# ---------------------------------------------------------------------------
# Command line entry point -----------------------------------------------------
if __name__ == "__main__":  # pragma: no cover - manual running only
    print(create_digest())
