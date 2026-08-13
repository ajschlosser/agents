import os
import imaplib
import email
from email.utils import parseaddr
import json
from typing import List

from email_utils import decode_text, get_plain_body, load_database, save_database, already_processed, LLMClient

PROTON_USER = os.environ["PROTON_USER"]
PROTON_BRIDGE_PASSWORD = os.environ["PROTON_BRIDGE_PASSWORD"]
PROTON_IMAP_HOST = os.getenv("PROTON_IMAP_HOST", "127.0.0.1").strip() or "127.0.0.1"
PROTON_IMAP_PORT = int(os.getenv("PROTON_IMAP_PORT", "1143"))
POLITICAL_SPAM_FOLDER = "PotentialPoliticalSpam"
DATABASE_FILE = "database-proton.json"

# IMAP connect using Proton Bridge SSL
imap = imaplib.IMAP4(IMAP_HOST=PROTON_IMAP_HOST, port=PROTON_IMAP_PORT)
imap.starttls()
imap.login(PROTON_USER, PROTON_BRIDGE_PASSWORD)


def ensure_folder(mail):
    status, mailboxes = mail.list()
    if status != "OK":
        raise RuntimeError("Unable to list mailboxes")
    if not any(POLITICAL_SPAM_FOLDER.lower() in m.decode().lower() for m in mailboxes):
        mail.create(POLITICAL_SPAM_FOLDER)


def add_political_folder(mail, uid):
    status, _ = mail.uid("COPY", str(uid), POLITICAL_SPAM_FOLDER)
    if status != "OK":
        raise RuntimeError(f"Could not copy UID {uid} to {POLITICAL_SPAM_FOLDER}")

# Main processing loop
mail = imap
ensure_folder(mail)
status, data = mail.uid("SEARCH", None, "UNSEEN")
uids = data[0].split() if status == "OK" else []
database = load_database(DATABASE_FILE)
for uid_bytes in uids:
    uid = uid_bytes.decode()
    if already_processed(database, uid):
        continue
    typ, msg_data = mail.uid("FETCH", uid, "(RFC822)")
    if typ != "OK":
        continue
    raw_email = b"".join(part[1] for part in msg_data if isinstance(part, tuple))
    msg = email.message_from_bytes(raw_email)
    sender_raw = msg.get("From", "")
    sender = parseaddr(sender_raw)[1]
    subject = decode_text(msg.get("Subject", ""))
    body = get_plain_body(msg)
    client = LLMClient()
    result = client.classify_political_spam(sender, subject, body)
    record = {
        "uid": uid,
        "message_id": msg.get("Message-ID", ""),
        "from": sender,
        "subject": subject,
        "political_spam": bool(result.get("important")),
        "reason": result.get("reason", "")
    }
    database.append(record)
    if record["political_spam"]:
        add_political_folder(mail, uid)
save_database(database, DATABASE_FILE)
print(f"Processed {len(uids)} unread emails.")
ip = imap
ip.logout()
