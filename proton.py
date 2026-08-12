import email
import imaplib
import json
import os
import requests
import ssl

from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr


try:
    PROTON_USER = os.environ["PROTON_USER"]
except KeyError:
    raise RuntimeError("Environment variable PROTON_USER is not set")

try:
    PROTON_BRIDGE_PASSWORD = os.environ["PROTON_BRIDGE_PASSWORD"]
except KeyError:
    raise RuntimeError("Environment variable PROTON_BRIDGE_PASSWORD is not set")

PROTON_IMAP_HOST = os.getenv("PROTON_IMAP_HOST", "127.0.0.1").strip() or "127.0.0.1"
PROTON_IMAP_PORT = int(os.getenv("PROTON_IMAP_PORT", "1143"))

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:1.7b"

DATABASE_FILE = "database-proton.json"
POLITICAL_SPAM_FOLDER = "PotentialPoliticalSpam"


def decode_text(value):
    if not value:
        return ""
    
    result = []
    for data, charset in decode_header(value):
        if isinstance(data, bytes):
            result.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(data)

    return "".join(result)


def get_body(msg):
    """Extract the plain-text body from an email."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")

    return ""


def classify_email(sender, subject, body):
    body = body[:8000]

    prompt = f"""
You are an email spam classifier.

Determine whether this email is POLITICAL SPAM.

Political spam means unsolicited or mass-distributed email primarily intended
to promote, oppose, fundraise for, or influence opinions about:

- political candidates
- political parties
- elections
- ballot initiatives
- political advocacy organizations
- ideological campaigns
- political fundraising

It also includes emails about politicians, elections, political parties,
or political issues that are sent in a mass-distribution manner.

Do NOT classify:

- personal correspondence
- receipts
- account alerts
- ordinary commercial spam
- transactional emails
- newsletters clearly requested by the user

Return ONLY JSON:

{{
  "political_spam": true,   // whether the email is political spam
  "sender": "email@email.com",     // just the sender's email address in name@domain.com format, nothing else!
  "timestamp": "ISO 8601 timestamp of email at time of sending", // if available, otherwise empty string
  "reason": "short explanation" // why you classified it this way-- say why you DID and not why you didn't
}}

EMAIL:

From: {sender}
Subject: {subject}

{body}
"""

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        },
        timeout=120,
    )
    response.raise_for_status()

    return json.loads(response.json()["message"]["content"])


def load_database():
    print(f"Loading database from {DATABASE_FILE}...")
    if not os.path.exists(DATABASE_FILE):
        return []

    try:
        with open(DATABASE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass

    return []


def save_database(database):
    temp_file = DATABASE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(database, file, indent=2, ensure_ascii=False)

    os.replace(temp_file, DATABASE_FILE)


def already_processed(database, uid):
    uid = str(uid)
    return any(str(record.get("uid")) == uid for record in database)


def save_result(
    database,
    uid,
    message_id,
    sender,
    subject,
    result,
    classification_method="llm",
    source_uid=None,
):
    record = {
        "uid": str(uid),
        "message_id": message_id,
        "from": sender,
        "subject": subject,
        "political_spam": bool(result.get("political_spam", False)),
        "reason": result.get("reason", ""),
        "timestamp": result.get("timestamp", ""),
        "classification_method": classification_method,
        "model": MODEL,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    if source_uid is not None:
        record["source_uid"] = str(source_uid)

    database.append(record)
    save_database(database)


def extract_raw_message(msg_data):
    for item in msg_data:
        if (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[1], bytes)
        ):
            return item[1]

    return None


def fetch_message(mail, uid):
    status, msg_data = mail.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if status != "OK":
        return None

    raw_email = extract_raw_message(msg_data)
    if raw_email is None:
        return None

    return email.message_from_bytes(raw_email)


def ensure_folder(mail, folder):
    """Ensure that the political-spam IMAP folder exists."""
    status, mailboxes = mail.list()
    if status != "OK":
        raise RuntimeError("Unable to list mailboxes")

    for mailbox in mailboxes:
        text = mailbox.decode("utf-8", errors="replace")
        if folder.lower() in text.lower():
            return

    status, _ = mail.create(folder)
    if status != "OK":
        print(f"Unable to create folder {folder}: {status}")
        #raise RuntimeError(f"Unable to create folder {folder}: {status}")


def add_political_folder(mail, uid):
    """Copy a message to the PotentialPoliticalSpam folder."""
    status, _ = mail.uid("COPY", str(uid), "Labels/Housing")
    if status != "OK":
        raise RuntimeError(
            f"Could not copy UID {uid} to {POLITICAL_SPAM_FOLDER}"
        )


def find_unread_from_sender(mail, sender_address):
    if not sender_address:
        return []

    status, data = mail.uid("SEARCH", None, "UNSEEN", "FROM", sender_address)
    if status != "OK":
        raise RuntimeError(f"Could not search unread mail from {sender_address}")

    if not data or not data[0]:
        return []

    return [
        item.decode() if isinstance(item, bytes) else str(item)
        for item in data[0].split()
    ]


def process_same_sender(mail, database, source_uid, sender, source_result):
    sender_address = parseaddr(sender)[1].strip().lower()
    if not sender_address:
        print("Could not extract sender email address; skipping propagation.")
        return

    uids = find_unread_from_sender(mail, sender_address)
    uids = [uid for uid in uids if str(uid) != str(source_uid)]

    if not uids:
        print(f"No other unread messages found from {sender_address}")
        return

    print(f"Found {len(uids)} additional unread messages from {sender_address}")

    inherited_result = {
        "political_spam": True,
        "confidence": source_result.get("confidence", 0.0),
        "reason": (
            "Automatically classified because another unread message from the "
            "same sender was classified as political spam."
        ),
    }

    for uid in uids:
        if already_processed(database, uid):
            print(f"UID {uid}: already processed")
            continue

        msg = fetch_message(mail, uid)
        if msg is None:
            print(f"UID {uid}: unable to fetch message")
            continue

        related_sender = decode_text(msg.get("From"))
        subject = decode_text(msg.get("Subject"))
        message_id = msg.get("Message-ID", "")

        try:
            add_political_folder(mail, uid)
        except Exception as exc:
            print(f"UID {uid}: unable to copy message: {exc}")
            continue

        save_result(
            database=database,
            uid=uid,
            message_id=message_id,
            sender=related_sender,
            subject=subject,
            result=inherited_result,
            classification_method="same_sender_propagation",
            source_uid=source_uid,
        )

        print(f"UID {uid}: copied to {POLITICAL_SPAM_FOLDER} and saved to DB")


def connect_to_proton():
    """
    Connect to Proton Mail Bridge over localhost.

    Bridge uses a locally generated TLS certificate, so certificate validation
    is disabled for this localhost connection.
    """
    print(f"Connecting to Proton Mail Bridge at {PROTON_IMAP_HOST}:{PROTON_IMAP_PORT}...")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    mail = imaplib.IMAP4(PROTON_IMAP_HOST, PROTON_IMAP_PORT)
    mail.starttls(ssl_context=context)
    mail.login(PROTON_USER, PROTON_BRIDGE_PASSWORD)

    print("Connected to Proton Mail Bridge.")
    for mailbox in mail.list()[1]:
        print(repr(mailbox))
    return mail


def check_mail():
    database = load_database()
    mail = connect_to_proton()

    try:
        status, _ = mail.select("INBOX")
        if status != "OK":
            raise RuntimeError("Unable to open INBOX")

        ensure_folder(mail, POLITICAL_SPAM_FOLDER)

        status, data = mail.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("Unable to search inbox")

        uids = data[0].split()
        print(f"Found {len(uids)} unread messages")

        for raw_uid in uids:
            uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)

            if already_processed(database, uid):
                print(f"Skipping already processed UID {uid}")
                continue

            msg = fetch_message(mail, uid)
            if msg is None:
                print(f"Unable to fetch UID {uid}")
                continue

            sender = decode_text(msg.get("From"))
            subject = decode_text(msg.get("Subject"))
            message_id = msg.get("Message-ID", "")
            body = get_body(msg)

            print()
            print("=" * 70)
            print("UID:", uid)
            print("From:", sender)
            print("Subject:", subject)

            try:
                result = classify_email(sender, subject, body)
            except Exception as exc:
                print("Classification failed:", exc)
                continue

            print(json.dumps(result, indent=2))

            save_result(
                database=database,
                uid=uid,
                message_id=message_id,
                sender=sender,
                subject=subject,
                result=result,
                classification_method="llm",
            )

            if not result.get("political_spam", False):
                continue

            try:
                add_political_folder(mail, uid)
                print(f"Copied UID {uid} to {POLITICAL_SPAM_FOLDER}")
            except Exception as exc:
                print("Unable to copy message:", exc)

            process_same_sender(
                mail=mail,
                database=database,
                source_uid=uid,
                sender=sender,
                source_result=result,
            )

    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    print("Starting Proton Mail political spam classification...")
    check_mail()
# TODO: This file was modified for testing purposes only. Do not commit test markers.
