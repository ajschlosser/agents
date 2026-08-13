# gm‑agent

 The **gm‑agent** repo ships lightweight, pure‑Python CLI tools that use IMAP to analyze Gmail and ProtonMail traffic. An Ollama LLM backend flags political spam in unread mail and can optionally generate concise summaries of recent *important* emails ⭐

## Environment variables

| Variable | Description |
|---|---|
| `GMAIL_USER` | Gmail address used for IMAP login. |
| `GMAIL_APP_PASSWORD` | App password or OAuth token for Gmail IMAP. |
| `PROTON_USER` | ProtonMail / Proton Bridge user name (usually an email). |
| `PROTON_BRIDGE_PASSWORD` | Password/token for the Proton Bridge. |
| `PROTON_IMAP_HOST` | Optional host for Proton Bridge, defaults to `127.0.0.1`. |
| `PROTON_IMAP_PORT` | Optional port for Proton IMAP, defaults to `1143`. |

## Command‑line utilities

Before running any tool, ensure the variables above are available in your shell. Export them manually or source a `.env` file:

```bash
export $(cat .env | xargs)
```

### Gmail spam classifier (`gmail.py`)

Scans UNSEEN mail in Gmail and classifies each message as possible political spam via an LLM. Matched messages are stored in `database.json`; if a message is marked *political spam* it receives the Gmail label **PotentialPoliticalSpam**.

```bash
python gmail.py
```

### Proton mail spam classifier (`proton.py`)

Same behaviour as `gmail.py`, but against the Proton Bridge via IMAP. Results are written to `database-proton.json`; political spam messages are copied into the folder *PotentialPoliticalSpam*.

```bash
python proton.py
```

### Digest of important emails (`digest.py`)

Logs into the Proton IMAP bridge, fetches unread e‑mails from the last three days and uses an LLM to flag “important” items. The identified messages are summarised into a single paragraph and saved in `database-digest.json`.

```bash
python digest.py
```

## Dependencies

- Python 3.11+ (type hints, f‑strings)
- A local Ollama server running the desired model.
- IMAP access to Gmail or a Proton Bridge instance.

All scripts share utilities under `email_utils.py`.  No additional external libraries are required.

## Configuration files

`gm-agent` ships several lightweight lookup tables that influence classifier behaviour:

- `political_domains.txt`: domains frequently associated with political content (used for headline pre‑filtering).
- `political_emails.txt`: known spam email addresses flagged automatically.

These files live at the repo root and can be edited or replaced to tune detection.

## License

    MIT – still the same license, editing test. Note: this is a test change to demonstrate patching capability.
