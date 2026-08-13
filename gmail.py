# Copyright (c) 2026 Aaron John Schlosser
#
# MIT License
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR AN
#
import os
import imaplib
import email
from email.utils import parseaddr
import json
from typing import List

from email_utils import decode_text, get_plain_body, load_database, save_database, already_processed, LLMClient

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
POLITICAL_SPAM_LABEL = "PotentialPoliticalSpam"
DATABASE_FILE = "database.json"

# Imap helper
mail = imaplib.IMAP4_SSL("imap.gmail.com")
mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
mail.select("INBOX", readonly=True)

def classify_email(sender: str, subject: str, body: str) -> dict:
    client = LLMClient()
    result = client.classify_political_spam(sender, subject, body)
    return {
        "political_spam": bool(result.get("important")),
        "confidence": 0.0,
        "reason": result.get("reason", "")
    }

# Helper functions for labels and processing similar to original but simplified

def ensure_gmail_label(mail):
    status, labels = mail.list()
    if status != "OK":
        raise RuntimeError("Unable to retrieve Gmail labels.")
    label_exists = any(POLITICAL_SPAM_LABEL.lower() in lbl.decode().lower() for lbl in labels)
    if not label_exists:
        mail.create(POLITICAL_SPAM_LABEL)


def add_gmail_label(mail, uid):
    status, _ = mail.uid("STORE", str(uid), "+X-GM-LABELS", f'("{POLITICAL_SPAM_LABEL}")')
    if status != "OK":
        raise RuntimeError(f"Unable to add Gmail label to UID {uid}")

# Main
database = load_database(DATABASE_FILE)
ensure_gmail_label(mail)
status, data = mail.uid("SEARCH", None, "UNSEEN")
uids = data[0].split() if status == "OK" else []
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
    result = classify_email(sender, subject, body)
    record = {
        "uid": uid,
        "from": sender,
        "subject": subject,
        "political_spam": bool(result.get("political_spam")),
        "confidence": result.get("confidence", 0.0),
        "reason": result.get("reason", "")
    }
    database.append(record)
    if record["political_spam"]:
        add_gmail_label(mail, uid)
save_database(database, DATABASE_FILE)
print(f"Processed {len(uids)} unread emails.")
