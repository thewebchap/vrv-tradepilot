# VRV TradePilot — Version 1

## 1. What VRV TradePilot Version 1 does

TradePilot signs in as **one Microsoft 365 user in your organization** and watches **that user's own Outlook inbox**. When an email arrives with two PDF attachments, it:

1. downloads the two PDFs,
2. finds the fields listed in `config.yaml` (Invoice Number, PO Number, Quantity, Amount, Currency) using the text **and its position** on the page,
3. compares the values between the two PDFs,
4. replies **in the same email thread**:
   - **GOOD TO GO** when everything matches,
   - a list of the differences when something doesn't match,
   - **REVIEW REQUIRED** with the reason when it couldn't check reliably (field not found, unreadable or scanned PDF, fewer than two PDFs).

It runs as a plain Python program (`python main.py`) that checks the inbox every 30 seconds. No server, no webhook, no AI: all PDF processing happens on your computer.

| File | What it does |
|---|---|
| `main.py` | Polling loop: check inbox → process new emails → reply |
| `outlook.py` | Microsoft Graph: sign-in (device code), list mail, attachments, reply |
| `pdf_parser.py` | Reads PDF words + coordinates, finds field values, normalizes them |
| `extraction.py` | Per document: PDF parser first, optional local AI model only when needed (section 13) |
| `local_model.py` | Optional local document model (Ollama, on this machine) that only *reads* field values |
| `compare.py` | Compares the two documents → PASS / FAIL / REVIEW_REQUIRED, writes reply text |
| `storage.py` | SQLite `tradepilot.db`: which emails were handled (no double replies) |
| `config.yaml` | Fields to extract, poll interval |
| `make_sample_pdfs.py` | Creates sample PDFs for your first test |
| `check_pdfs.py` | Checks two PDFs on your machine without email (`python check_pdfs.py a.pdf b.pdf`) |

**Version 1 limitations**
- Only PDF attachments are used; other attachments are ignored.
- If more than two PDFs are attached, **only the first two** are compared.
- Emails with **no** PDFs get **no reply**. This matters because TradePilot's own replies land in the same inbox; replying to them would start an endless loop.
- Scanned PDFs (images without text) → REVIEW REQUIRED, unless the optional local model is enabled (section 13).

## 2. Requirements

- macOS or Linux with **Python 3.12 or newer** (`python3 --version`)
- A Microsoft 365 work account with an Exchange Online mailbox. TradePilot reads and replies from this mailbox.
- Access to the Microsoft Entra admin center to create an app registration (section 4). If your organization doesn't let normal users register apps, ask IT to do sections 4–5 and give you the two IDs.

## 3. Python environment setup

Homebrew's Python refuses `pip install` system-wide (`externally-managed-environment`). Use a virtual environment; **never** `sudo pip` or `--break-system-packages`.

In the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> **If you have `alias python=...` / `alias pip=...` lines in `~/.zshrc`** (check with `type python`): aliases override the virtual environment, so `python` keeps pointing at Homebrew's Python and `pip install` fails with `externally-managed-environment`. Either delete those alias lines from `~/.zshrc` and open a new terminal, or run `unalias python python3 pip pip3 2>/dev/null` in the terminal after `source .venv/bin/activate`. After activating, `type python` must show `.../vrv-tradepilot/.venv/bin/python`.

Your prompt now starts with `(.venv)`. In every new terminal, run `source .venv/bin/activate` again before using TradePilot.

Check that everything works (no Microsoft account needed):

```bash
pytest
```

## 4. Microsoft App Registration

TradePilot needs an "app registration" so Microsoft 365 knows which program is asking to read the mailbox. It's created once, in your organization's tenant.

1. Open the **Microsoft Entra admin center**: <https://entra.microsoft.com> and sign in with your work account.
2. In the left menu go to **Entra ID → App registrations** (older menu: **Identity → Applications → App registrations**).
3. Click **+ New registration** (or open an existing `VRV TradePilot` registration if IT already created one).
4. **Name**: `VRV TradePilot`
5. **Supported account types**: **Accounts in this organizational directory only (Single tenant)**.
6. **Redirect URI**: leave **empty**. The device code flow doesn't use a redirect URI.
7. Click **Register**.
8. On the app's **Overview** page copy two values:

| Microsoft name (Overview page) | Put in `.env` as |
|---|---|
| **Application (client) ID** | `MICROSOFT_CLIENT_ID=` |
| **Directory (tenant) ID** | `MICROSOFT_TENANT_ID=` |

   (Ignore "Object ID".)
9. **Required for device code sign-in:** in the left menu open **Authentication**. Under **Advanced settings**, set **Allow public client flows** to **Yes**, and click **Save**. Without this, sign-in fails with `AADSTS7000218`.

**No client secret or certificate is needed.** TradePilot is a "public client": it signs in *as you* through the device code flow and only gets the access you approve. Don't create a client secret for this version.

## 5. Microsoft Graph permissions

TradePilot uses **delegated** permissions: it acts as the signed-in user, on that user's own mailbox (`/me`). Don't add Application permissions.

1. In the app registration open **API permissions** → **+ Add a permission** → **Microsoft Graph** → **Delegated permissions**.
2. Tick these three, then click **Add permissions**:

| Permission (Delegated) | Why |
|---|---|
| `Mail.ReadWrite` | Read new emails and attachments, and mark processed emails as read |
| `Mail.Send` | Send the reply from your mailbox |
| `offline_access` | Refresh token, so TradePilot stays signed in between runs |

`User.Read` (Delegated) is usually already listed; leave it.

In the code, TradePilot requests `Mail.ReadWrite` and `Mail.Send`. MSAL adds `offline_access` to every sign-in automatically and rejects it if it's listed explicitly. Never use `.default`.

**User consent vs admin consent.** Microsoft marks these three permissions as not requiring admin consent, so normally you approve them yourself at first sign-in. But many organizations restrict user consent (for example, "only apps from verified publishers"). In that case sign-in stops with **"Need admin approval"** (`AADSTS65001` / `AADSTS90094`). Then a Global Administrator, Privileged Role Administrator or Cloud Application Administrator opens this app registration → **API permissions** → **Grant admin consent for <organization>** → **Yes**, and the Status column shows **Granted for <organization>**. After that, run TradePilot again.

## 6. .env setup

```bash
cp .env.example .env
```

Edit `.env`:

```
MICROSOFT_CLIENT_ID=<Application (client) ID>
MICROSOFT_TENANT_ID=<Directory (tenant) ID>
POLL_INTERVAL_SECONDS=30
SQLITE_PATH=tradepilot.db
LOG_LEVEL=INFO
```

- TradePilot signs in at `https://login.microsoftonline.com/<MICROSOFT_TENANT_ID>`, your organization only. `consumers`, `common` and `organizations` are rejected.
- No mailbox ID is needed: the mailbox is whoever signs in.
- `POLL_INTERVAL_SECONDS` overrides `poll_interval_seconds` in `config.yaml`.
- There are no secrets in `.env`, and your password is never stored.

## 7. First login using device code

```bash
source .venv/bin/activate
python main.py
```

The first time, Microsoft's device-login message is printed (Microsoft decides the exact URL and code):

```
To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code ABCD1234 to authenticate.
```

1. Open that page in any browser (any device), enter the code.
2. Sign in with **your organizational Microsoft 365 account**: the mailbox TradePilot should watch. Complete MFA if asked.
3. Microsoft shows "VRV TradePilot … Read and write access to your mail, Send mail as you, Maintain access to data you have given it access to". Click **Accept**.
   If you see **Need admin approval** instead, see section 5.
4. Back in the terminal: `Signed in as you@yourcompany.com` and `Authenticated as you@yourcompany.com`, then polling starts.

TradePilot saves the sign-in to `.token_cache.json` (readable only by you, excluded from git). **Next runs don't ask you to sign in**: TradePilot loads the cache, finds your account, and MSAL silently refreshes the access token. You only see a device code again when the saved sign-in can no longer be used. That happens when it expires after a long period of non-use, when the tenant requires re-authentication (`AADSTS50058`, `interaction_required`, `invalid_grant`, e.g. after a password change, MFA or Conditional Access policy, or revoked sessions), when you delete `.token_cache.json`, or when you switch to a different tenant. TradePilot then falls back to the device code automatically; it doesn't crash.

Keep `.token_cache.json` private: anyone with this file can access your mailbox until the sign-in expires. Delete it to sign out.

## 8. Running TradePilot

```bash
source .venv/bin/activate
python main.py
```

Leave it running; stop it with **Ctrl+C**. Normal output:

```
2026-10-04 12:00:00 INFO    TradePilot started (polling every 30s)
2026-10-04 12:00:01 INFO    Authenticated as you@yourcompany.com
2026-10-04 12:00:01 INFO    Processing emails received after 2026-10-04T10:00:00Z
2026-10-04 12:00:01 INFO    Checking inbox
2026-10-04 12:00:02 INFO    0 new message(s)
```

## 9. Sending a test email

1. Create sample PDFs (in a second terminal, with the venv activated):
   ```bash
   python make_sample_pdfs.py
   ```
   This writes `samples/invoice.pdf`, `samples/po_match.pdf` (same values, different layout) and `samples/po_mismatch.pdf` (Quantity 25, Amount 12,900.00).
2. Make sure `python main.py` is running.
3. From any mail app, send an email **to the signed-in user's work address**. Sending it from that same address is fine. Attach `invoice.pdf` and `po_match.pdf`.
4. Wait up to 30 seconds (one polling cycle). The log shows:
   ```
   INFO    1 new message(s)
   INFO    Processing 'Test' from you@yourcompany.com
   INFO    Attachments: ['invoice.pdf', 'po_match.pdf']
   INFO    Fields in invoice.pdf: {'invoice_number': 'INV-1024', 'po_number': 'PO-7781', 'quantity': '20', ...}
   INFO    Fields in po_match.pdf: {...}
   INFO    Result: PASS  mismatches=[]  issues=[]
   INFO    Reply sent
   ```
5. In Outlook, the email thread now contains TradePilot's reply:
   ```
   VRV TradePilot validation completed.

   All configured checks passed.

   Status: GOOD TO GO
   ```
   On the next poll the log shows that reply arriving as a new message with no PDFs, and ignoring it. That's expected.
6. Repeat with `invoice.pdf` + `po_mismatch.pdf`. The reply lists the Quantity and Amount differences.
7. Optional, to look inside the database:
   ```bash
   sqlite3 tradepilot.db "SELECT message_id, status, processed_at, error FROM processed_messages ORDER BY id DESC LIMIT 5;"
   ```

## 10. How polling works

Every `POLL_INTERVAL_SECONDS` (default 30) TradePilot asks Microsoft Graph for the newest 50 Inbox emails received **since TradePilot was first started**, and processes the ones it hasn't handled yet, oldest first.

- **Starting point:** the very first time it runs, TradePilot stores the current time in `tradepilot.db`. Emails already in your inbox before that moment are never processed, so it won't reply to your old mail. The start time is kept across restarts, so emails that arrive while TradePilot is stopped are processed when it starts again.
- To start fresh (for example "only from now on" after a long break), stop TradePilot and delete `tradepilot.db`.
- After replying, TradePilot marks the email as read (cosmetic; it doesn't rely on read/unread).
- One failing email never stops the loop. The error is logged and the next poll continues.

## 11. How duplicate protection works

Every email TradePilot looks at gets a row in `tradepilot.db` (table `processed_messages`), keyed by the Microsoft Graph message ID:

| status | meaning | processed again? |
|---|---|---|
| `PASS` / `FAIL` / `REVIEW_REQUIRED` | reply sent (if the reply failed, `error` says so) | never |
| `IGNORED` | no PDF attachments | never |
| `REPLYING` | TradePilot stopped while sending the reply | never, so no risk of replying twice |
| `FAILED` | Outlook/Graph error *before* replying (e.g. a network problem) | yes, on later polls, up to 3 attempts |
| `PROCESSING` | TradePilot stopped mid-way, before replying | yes |

Before processing, TradePilot checks this table; an email with a finished row is skipped. A reply is never retried after an uncertain failure (a timeout or Microsoft server error), because it might already have been sent.

## 12. Troubleshooting

| Problem | What to do |
|---|---|
| `externally-managed-environment` or `No module named 'yaml'` | You're not using the virtual environment's Python. Run `source .venv/bin/activate`; if `type python` still shows `/opt/homebrew/...`, a shell alias is overriding it (see the note in section 3). |
| `MICROSOFT_CLIENT_ID and MICROSOFT_TENANT_ID must be set` | Create `.env` (section 6) in the folder you run `python main.py` from. |
| `MICROSOFT_TENANT_ID ... was not found` | Use the **Directory (tenant) ID** GUID from the app's Overview page. |
| `AADSTS7000218` (... `client_assertion` or `client_secret` ...) | **Allow public client flows** is not set to **Yes** (section 4, step 9). |
| `AADSTS700016` / `AADSTS700038` / `unauthorized_client` | Wrong `MICROSOFT_CLIENT_ID` (copy **Application (client) ID**, not Object ID), or the app is registered in a different tenant than `MICROSOFT_TENANT_ID`. |
| `AADSTS50020` / "account from a different organization" | You signed in with an account outside `MICROSOFT_TENANT_ID` (e.g. a personal Hotmail account). Run again and sign in with your work account. |
| `Need admin approval` / `AADSTS65001` / `AADSTS90094` / `consent_required` | Your organization blocks user consent. An admin must click **Grant admin consent** (section 5). |
| `AADSTS53003` / blocked by Conditional Access | Your organization's sign-in policy blocks device code sign-in or this app. Ask IT to allow it. |
| `authorization_declined` / `expired_token` | You cancelled the sign-in, or didn't finish within ~15 minutes. Run `python main.py` again. |
| `Saved sign-in can no longer be used (...); signing in again` | Normal after expiry, a password change or a new policy (`AADSTS50058`, `interaction_required`, `invalid_grant`). Just complete the device code sign-in. |
| Device code shown again on every start | `.token_cache.json` can't be written (the log says `Could not save`), or you run from a different folder. Always run from the project folder. |
| `401` / `InvalidAuthenticationToken` | Sign-in expired or was revoked. Delete `.token_cache.json` and run `python main.py` to sign in again. |
| `403` / `ErrorAccessDenied` | A permission is missing. Check section 5, then delete `.token_cache.json` and sign in again so you approve the new permissions. |
| `429` in the log | Microsoft is throttling; TradePilot waits as instructed and retries. Increase `POLL_INTERVAL_SECONDS` if it happens often. |
| No reply / `0 new message(s)` | The email arrived *before* TradePilot was first started, it went to **Junk** (only the Inbox is checked), or it has no PDF attachment. |
| `REVIEW REQUIRED: ... has no extractable text` | The PDF is a scanned image. If you can't select its text in a PDF viewer, Version 1 can't read it. |
| `Required field "..." could not be identified` | The PDF uses a label TradePilot doesn't know. Add the exact wording under `labels:` in `config.yaml` (put labels with `#` in quotes), and restart. |
| `Reply failed` in the log | Check `Mail.Send` (section 5). The email is marked done to avoid duplicates; reply manually or resend the email. |

## 13. Optional local AI extraction

Some documents have layouts the PDF parser can't read reliably: values far from their labels, unusual tables, scanned pages. For those, TradePilot can ask a **local** document model to read the values.

**How it's used**
- **The PDF parser stays the default.** The model is called only for a document where a *required* field is missing or ambiguous. If the parser finds everything, the model isn't used.
- **The model only reads values.** It's asked for the printed text of each field in `config.yaml`, as strict JSON. It's never asked whether documents match. PASS / FAIL / REVIEW REQUIRED is always decided by `compare.py`, with the same normalization and rules as before.
- **Safety rules** (`extraction.py`):
  - A value found by the PDF parser is never overwritten. If the model reads something different, the field is `CONFLICT` and the result is REVIEW REQUIRED.
  - A missing value is filled only if the model's value is valid for the field's type and pattern, and, for PDFs with a text layer, actually appears in the PDF's text. This stops invented values.
  - For a field the parser found several different values for, the model may only pick one of those values.
  - Model unavailable, too slow, or invalid JSON twice: REVIEW REQUIRED. The polling loop keeps running.
- **Audit:** every field records its source (`deterministic` or `local_model`) in `tradepilot.db` (`result_json` → `documents`). Replies say which values came from the model, e.g. `Note: read by the local document model: Quantity (po.pdf).`
- **Privacy:** documents never leave your machine. Pages are rendered to images locally and sent only to Ollama on `localhost`. Nothing goes to OpenAI, Anthropic or any other cloud service.

**Model choice: `qwen2.5vl:7b` via Ollama**

Qwen2.5-VL is a vision-language model that's strong at reading documents: text, tables, key/value layouts and scans. The 7B version runs on a 16 GB Apple Silicon Mac and on an ordinary on-prem server. Ollama runs it locally behind a simple HTTP API and can force the answer into a JSON schema. Use a model with **vision** support: text-only models (e.g. `qwen2.5:7b`) silently ignore the page images. TradePilot logs a warning at startup if the model has no vision support.

**Hardware (rough guide)**
- `qwen2.5vl:7b`: about 6 GB download, about 8–10 GB free RAM while running. On an M1/M2 with 16 GB, expect roughly 30 seconds to 2 minutes per document; only documents that need the fallback pay this cost.
- On a server: 16 GB+ RAM; an NVIDIA GPU with 8 GB+ VRAM makes it many times faster.
- Lighter option: `qwen2.5vl:3b` (faster, less accurate). Larger: `qwen2.5vl:32b` (needs a large GPU).

**Setup**

1. Install Ollama (separate from TradePilot): `brew install ollama`, or download from <https://ollama.com/download>. Check with `ollama --version`.
2. Start it in its own terminal and leave it running:
   ```bash
   ollama serve
   ```
   (or run it in the background: `brew services start ollama`)
3. Download the model (once, about 6 GB):
   ```bash
   ollama pull qwen2.5vl:7b
   ```
4. Check that it's running and the model is there:
   ```bash
   curl http://localhost:11434/api/tags
   ollama list            # must list qwen2.5vl:7b
   ```
5. In `.env`:
   ```
   LOCAL_MODEL_ENABLED=true
   LOCAL_MODEL_PROVIDER=ollama
   LOCAL_MODEL_NAME=qwen2.5vl:7b
   OLLAMA_BASE_URL=http://localhost:11434
   # LOCAL_MODEL_TIMEOUT_SECONDS=180
   ```
   `config.yaml` → `extraction: use_local_model_fallback: true` must also be set (it is by default). Set `LOCAL_MODEL_ENABLED=false` to go back to PDF-parser-only behaviour.
6. Try two PDFs without email:
   ```bash
   source .venv/bin/activate
   python check_pdfs.py samples/invoice.pdf /path/to/difficult.pdf
   ```
   It prints each field with `source=deterministic` or `source=local_model`, the comparison result, and the reply that would be sent.
7. Run TradePilot as usual: `python main.py`. At startup the log says one of:
   - `Local model fallback: on (ollama, qwen2.5vl:7b)`: ready;
   - `Local model fallback: on, but Ollama is not reachable ...`: start `ollama serve`;
   - `Local model fallback: off (PDF parser only)`.

| Problem | Fix |
|---|---|
| `Ollama is not reachable` | Start `ollama serve` (step 2); check `OLLAMA_BASE_URL`. |
| `model 'qwen2.5vl:7b' is not pulled` | `ollama pull qwen2.5vl:7b` |
| `has no vision support` | `LOCAL_MODEL_NAME` is a text-only model; use `qwen2.5vl:7b`. |
| `did not answer within 180s` | The first call loads the model (slow). Increase `LOCAL_MODEL_TIMEOUT_SECONDS`, or use `qwen2.5vl:3b`. |
| `... read differently by the PDF parser and the local model` | Working as designed: two readings disagree, so a human checks. |
| `local model value '...' does not appear in the PDF text` | The model's answer couldn't be found in the document, so it was rejected. |

