import imaplib
import email
import json
import os
import requests
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:1.7b"

DATABASE_FILE = "database.json"
POLITICAL_SPAM_LABEL = "PotentialPoliticalSpam"

def decode_text(value):
    if not value:
        return ""

    parts = decode_header(value)
    result = []

    for data, charset in parts:
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

            if (
                content_type == "text/plain"
                and "attachment" not in disposition
            ):
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
    """
    Send the email to the local Ollama model and return
    a structured classification.
    """
    body = body[:8000]

    prompt = f"""
You are an email spam classifier.

Determine whether this email is POLITICAL SPAM.

Political spam means unsolicited or mass-distributed email primarily
intended to promote, oppose, fundraise for, or influence opinions about:

- political candidates
- political parties
- elections
- ballot initiatives
- political advocacy organizations
- ideological campaigns
- political fundraising

It also includes emails about politicians, elections, political parties,
or political issues that are sent in a mass-distribution manner.

Do NOT classify the following as political spam:

- legitimate personal correspondence
- receipts
- account alerts
- ordinary commercial spam
- normal transactional emails
- newsletters clearly requested by the user

Return ONLY valid JSON in this exact form:

{{
  "political_spam": true,
  "confidence": 0.0,
  "reason": "short explanation"
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
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0
            },
        },
        timeout=120,
    )

    response.raise_for_status()
    text = response.json()["message"]["content"]

    return json.loads(text)


def load_database():
    """
    Load database.json.

    If it doesn't exist yet, start with an empty list.
    """
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
    """
    Save database.json atomically.
    """
    temp_file = DATABASE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(
            database,
            file,
            indent=2,
            ensure_ascii=False,
        )

    os.replace(temp_file, DATABASE_FILE)


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
    """
    Add one classification result to database.json.
    """

    record = {
        "uid": str(uid),
        "message_id": message_id,
        "from": sender,
        "subject": subject,
        "political_spam": bool(result.get("political_spam", False)),
        "confidence": result.get("confidence", 0.0),
        "reason": result.get("reason", ""),
        "classification_method": classification_method,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    if source_uid is not None:
        record["source_uid"] = str(source_uid)

    database.append(record)
    save_database(database)


def ensure_gmail_label(mail, label):
    """
    Create the Gmail label if it doesn't already exist.
    """

    status, labels = mail.list()

    if status != "OK":
        raise RuntimeError("Unable to retrieve Gmail labels.")

    # LIST output is not especially pleasant to parse, but checking the
    # quoted/unquoted tail is sufficient for this simple custom label.
    label_exists = False

    for item in labels:
        decoded = item.decode("utf-8", errors="replace")
        if label.lower() in decoded.lower():
            label_exists = True
            break

    if not label_exists:
        print(f'Creating Gmail label "{label}"...')

        status, _ = mail.create(label)

        if status != "OK":
            raise RuntimeError(
                f'Unable to create Gmail label "{label}".'
            )


def add_gmail_label(mail, uid, label):
    """
    Add a Gmail label to a specific message using X-GM-LABELS.
    """

    status, _ = mail.uid(
        "STORE",
        str(uid),
        "+X-GM-LABELS",
        f'("{label}")',
    )

    if status != "OK":
        raise RuntimeError(
            f"Unable to add Gmail label to UID {uid}"
        )


def already_processed(database, uid):
    """
    Return True if this mailbox UID is already in database.json.
    """
    uid = str(uid)

    return any(
        str(record.get("uid")) == uid
        for record in database
    )


def extract_raw_message(msg_data):
    """
    Extract the bytes payload returned by IMAP FETCH.
    """
    for item in msg_data:
        if (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[1], bytes)
        ):
            return item[1]

    return None


def fetch_message(mail, uid):
    """
    Fetch a complete email without marking it as read.
    """
    status, msg_data = mail.uid(
        "FETCH",
        str(uid),
        "(BODY.PEEK[])",
    )

    if status != "OK":
        return None

    raw_email = extract_raw_message(msg_data)

    if raw_email is None:
        return None

    return email.message_from_bytes(raw_email)


def imap_quote(value):
    """
    Quote a string for use as one IMAP SEARCH argument.

    Sender addresses should normally contain neither quotes nor backslashes,
    but escaping both makes the search safer.
    """
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def find_unread_from_sender(mail, sender_address):
    """
    Return UIDs for unread INBOX messages whose From field matches
    sender_address.
    """
    if not sender_address:
        return []

    status, data = mail.uid(
        "SEARCH",
        None,
        "UNSEEN",
        "FROM",
        imap_quote(sender_address),
    )

    if status != "OK":
        raise RuntimeError(
            f"Could not search for unread mail from {sender_address}"
        )

    if not data or not data[0]:
        return []

    return [
        uid.decode() if isinstance(uid, bytes) else str(uid)
        for uid in data[0].split()
    ]


def label_and_record_same_sender_messages(
    mail,
    database,
    source_uid,
    sender,
    source_result,
):
    """
    When one message is classified as political spam, find all other unread
    INBOX messages from the same sender, label them, and add them to the DB.

    These messages inherit the source classification instead of invoking the
    LLM again.
    """
    sender_address = parseaddr(sender)[1].strip()

    if not sender_address:
        print(
            "Could not extract a sender email address; "
            "skipping same-sender propagation."
        )
        return

    matching_uids = find_unread_from_sender(
        mail,
        sender_address,
    )

    # Do not process the source message again here.
    matching_uids = [
        uid for uid in matching_uids
        if str(uid) != str(source_uid)
    ]

    if not matching_uids:
        print(
            f"No other unread messages found from {sender_address}"
        )
        return

    print(
        f"Found {len(matching_uids)} other unread message(s) "
        f"from {sender_address}."
    )

    propagated_result = {
        "political_spam": True,
        "confidence": source_result.get("confidence", 0.0),
        "reason": (
            "Automatically classified as political spam because another "
            f"unread message from the same sender ({sender_address}) "
            "was classified as political spam."
        ),
    }

    for uid in matching_uids:
        # If an earlier propagation/classification already handled this UID,
        # do not create a duplicate DB record.
        if already_processed(database, uid):
            print(
                f"  UID {uid}: already in database; "
                "ensuring label is present."
            )

            try:
                add_gmail_label(
                    mail,
                    uid,
                    POLITICAL_SPAM_LABEL,
                )
            except Exception as exc:
                print(
                    f"  UID {uid}: unable to add label: {exc}"
                )

            continue

        msg = fetch_message(mail, uid)

        if msg is None:
            print(
                f"  UID {uid}: unable to fetch message; skipped."
            )
            continue

        related_sender = decode_text(msg.get("From"))
        related_subject = decode_text(msg.get("Subject"))
        related_message_id = msg.get("Message-ID", "")

        # Label first. If Gmail rejects the label operation, don't write a
        # database record claiming that the proactive action succeeded.
        try:
            add_gmail_label(
                mail,
                uid,
                POLITICAL_SPAM_LABEL,
            )
        except Exception as exc:
            print(
                f"  UID {uid}: unable to add label: {exc}"
            )
            continue

        save_result(
            database=database,
            uid=uid,
            message_id=related_message_id,
            sender=related_sender,
            subject=related_subject,
            result=propagated_result,
            classification_method="same_sender_propagation",
            source_uid=source_uid,
        )

        print(
            f'  UID {uid}: labeled "{POLITICAL_SPAM_LABEL}" '
            f"and saved to {DATABASE_FILE}"
        )


def check_mail():
    database = load_database()

    mail = imaplib.IMAP4_SSL("imap.gmail.com")

    try:
        mail.login(
            GMAIL_USER,
            GMAIL_APP_PASSWORD,
        )

        mail.select("INBOX")

        ensure_gmail_label(
            mail,
            POLITICAL_SPAM_LABEL,
        )

        status, data = mail.uid(
            "SEARCH",
            None,
            "UNSEEN",
        )

        if status != "OK":
            raise RuntimeError(
                "Could not search Gmail."
            )

        message_uids = data[0].split()

        print(
            f"Found {len(message_uids)} unread messages."
        )

        for uid_bytes in message_uids:
            uid = (
                uid_bytes.decode()
                if isinstance(uid_bytes, bytes)
                else str(uid_bytes)
            )

            # A message may have been added to the DB earlier in this same
            # run by same-sender propagation.
            if already_processed(database, uid):
                print(
                    f"Skipping already processed UID {uid}"
                )
                continue

            msg = fetch_message(mail, uid)

            if msg is None:
                print(
                    f"Unable to fetch UID {uid}"
                )
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
                result = classify_email(
                    sender,
                    subject,
                    body,
                )

            except Exception as exc:
                print(
                    "Classification failed:",
                    exc,
                )
                continue

            print(
                json.dumps(
                    result,
                    indent=2,
                )
            )

            #
            # Save the source message classification.
            #
            save_result(
                database=database,
                uid=uid,
                message_id=message_id,
                sender=sender,
                subject=subject,
                result=result,
                classification_method="llm",
            )

            print(
                f"Saved result to {DATABASE_FILE}"
            )

            #
            # If this is political spam:
            #
            #   1. Label the current message.
            #   2. Search for every OTHER unread message from the same sender.
            #   3. Label each of those messages.
            #   4. Add each one to database.json.
            #
            # Only after all of that is complete do we continue to the next
            # message in the original unread-message list.
            #
            if result.get("political_spam", False):
                try:
                    add_gmail_label(
                        mail,
                        uid,
                        POLITICAL_SPAM_LABEL,
                    )

                    print(
                        f'Added Gmail label "{POLITICAL_SPAM_LABEL}"'
                    )

                except Exception as exc:
                    print(
                        "Unable to add Gmail label:",
                        exc,
                    )

                try:
                    label_and_record_same_sender_messages(
                        mail=mail,
                        database=database,
                        source_uid=uid,
                        sender=sender,
                        source_result=result,
                    )

                except Exception as exc:
                    print(
                        "Unable to process other messages "
                        f"from the same sender: {exc}"
                    )

    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    check_mail()
