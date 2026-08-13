"""
email_utils.py

Centralised helpers for email parsing, database persistence and
LLM classification used by digest.py, gmail.py and proton.py.

The module exposes pure functions only; it performs no I/O beyond JSON read/write.
"""

import json
import os
from typing import List, Dict
import requests
import email
import re

# ---------- Email‑header decoding ----------

def decode_text(value: str) -> str:
    """Decode RFC-2047 encoded header values."""
    if not value:
        return ""
    result = []
    for part, charset in email.header.decode_header(value):
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)

# ---------- Plain‑text body extraction ----------

def get_plain_body(msg: email.message.EmailMessage) -> str:
    """Return the first text/plain part, stripping HTML otherwise."""
    def _decode(part):
        payload = part.get_payload(decode=True)
        if not payload:
            return None
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except Exception:
            return None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                text = _decode(part)
                if text is not None:
                    return text
        # Fallback to first html part stripped of tags
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_text = _decode(part)
                if html_text is not None:
                    return re.sub(r"<[^>]+>", "", html_text)
    else:
        text = _decode(msg)
        if text is not None:
            return re.sub(r"<[^>]+>", "", text)

    return ""

# ---------- JSON DB helpers ----------

def load_database(path: str) -> List[Dict]:
    """Load a list of records from `path`.  Return [] on missing/invalid file."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []

def save_database(db: List[Dict], path: str) -> None:
    """Atomically persist `db` to `path`."""
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as fp:
        json.dump(db, fp, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)

def already_processed(db: List[Dict], uid: str) -> bool:
    """Return True if UID is already present in the database."""
    uid = str(uid)
    return any(str(rec.get("uid")) == uid for rec in db)

# ---------- LLM client wrapper ----------

class LLMClient:
    """Thin wrapper around Ollama chat API, providing two classification helpers."""
    def __init__(self, base_url: str = "http://localhost:11434/api/chat", model: str = "qwen3:1.7b"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _post(self, messages) -> Dict:
        resp = requests.post(
            f"{self.base_url}/chat",
            json={"model": self.model, "messages": messages},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "message" in data:
            return {"content": data["message"].get("content", "")}
        elif isinstance(data, list):
            return {"content": data[0].get("content", "")}
        else:
            raise ValueError("Unexpected Ollama response format")

    def classify_political_spam(self, sender: str, subject: str, body: str) -> Dict:
        """Return a dict with keys: political_spam (bool), reason (str)."""
        prompt = f"""You are an email spam classifier.

Determine whether this email is POLITICAL SPAM.
Political spam means unsolicited or mass-distributed email primarily intended to promote, oppose, fundraise for, or influence opinions about:

- political candidates
- political parties
- elections
- ballot initiatives
- political advocacy organizations
- ideological campaigns
- political fundraising

It also includes emails about politicians, elections, political parties,
or political issues that are sent in a mass‑distribution manner.

Do NOT classify the following as political spam:

- legitimate personal correspondence
- receipts
- account alerts
- ordinary commercial spam
- transactional emails
- newsletters clearly requested by the user

Return ONLY valid JSON in this exact form:

{{
  "political_spam": true,
  "sender": "email@email.com",
  "timestamp": "ISO 8601 timestamp of email at time of sending",
  "reason": "short explanation",
  "description": "short description of the email content"
}}

NOTE: The 'description' field must NEVER mention politics UNLESS the email is actually about politics.

EMAIL:

From: {sender}
Subject: {subject}

{body[:8000]}
"""
        res = self._post([{"role": "user", "content": prompt}])
        content = res.get("content") or ""
        try:
            return json.loads(content)
        except Exception:
            return {}

    def classify_importance(self, sender: str, subject: str, body: str) -> Dict:
        """Return a dict describing importance of an email."""
        prompt = f"""Determine the importance of the following email.
An email is important if it has an urgent impact on personal or family health,
finance, or security. If it can be safely ignored without consequence, it is
not important.
Provide JSON with keys: sender, subject, timestamp, important (bool), reason.

Subject: {subject}
Body preview: {body[:750]}
"""
        res = self._post([{"role": "user", "content": prompt}])
        content = res.get("content") or ""
        try:
            return json.loads(content)
        except Exception:
            return {}
