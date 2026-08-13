# GM-Agent

The **GM‑Agent** repository contains a small collection of command‑line utilities for interacting with Gmail and ProtonMail via IMAP and an optional digest tool that summarizes important emails.

## Environment variables

| Variable | Description |
|---|---|
| `GMAIL_USER` | Gmail account e‑mail address used to log in. |
| `GMAIL_APP_PASSWORD` | App password (or OAuth token) to access the IMAP service. |
| `PROTON_USER` | ProtonMail / Proton Bridge user name (usually an e‑mail). |
| `PROTON_BRIDGE_PASSWORD` | Proton mail bridge password/token. |
| `PROTON_IMAP_HOST` | Optional host for Proton Bridge, defaults to `127.0.0.1`. |
| `PROTON_IMAP_PORT` | Optional port for Proton IMAP, defaults to `1143`. |

## Scripts

### Gmail spam classifier (`gmail.py`)

Scans the **UNSEEN** mail in *Gmail* and classifies each message as possible political spam via an LLM. Matched messages are stored in `database.json`, and a label named “PotentialPoliticalSpam” is added to the message if it turns out to be spam.

```bash
python gmail.py
```

### Proton mail spam classifier (`proton.py`)

Same behaviour as `gmail.py`, but works against the Proton mail bridge via IMAP. Results are written into `database-proton.json`. If a message is detected as political spam it is copied into the special folder *PotentialPoliticalSpam*.

```bash
python proton.py
```

### Digest of important emails (`digest.py`)

Logs into Proton’s IMAP bridge, fetches unread e‑mails from the last three days and uses an LLM to determine whether each mail is “important”. The important items are summarised into a single paragraph and saved in `database-digest.json`.

```bash
python digest.py
```

## Dependencies

All scripts rely on the shared utilities under `email_utils.py`.  They require: 

* Python 3.11+ (type hints, f‑strings)
* `ollama` for LLM communication – make sure the Ollama server is running locally and exposes the model you want to use.
* IMAP access for Gmail / Proton Bridge.

## License

MIT.
