# Privacy & Security

BEL CODE is designed so you retain control over your source code, credentials, and telemetry.

---

## 1. Local Encrypted API Key Storage (`SecretVault`)

- **Where secrets live:** Keys saved via the UI wizard, Settings modal, or `POST /api/providers/configure` are stored in **`~/.bel_code/vault.enc`** (excluded from Git via `.gitignore`).
- **Encryption:** `SecretVault` derives encryption keys using PBKDF2-HMAC-SHA256 / OS keyring integration where available and encrypts key material at rest.
- **No Plaintext Config Files:** `opencode.json` never receives raw secrets — only `{env:PROVIDER_API_KEY}` placeholders.

---

## 2. Redaction in Logs, UI, and Error Messages

- `bel_code/config/settings.py` and `bel_code/providers/base.py` apply secret-pattern and active-key redaction (`***REDACTED***`) to:
  - API responses (`/api/providers/configure`, `/api/providers/custom`, `/api/providers`).
  - HTTP transport errors and `RateLimitError` messages (so a third-party error body echoing an auth header cannot leak your token into the UI or logs).
- Audit logs and server responses never return stored API keys to the frontend.

---

## 3. Repository & `.env` Hygiene

- `.gitignore` explicitly blocks:
  - `.env` and `.env.*` (while keeping `.env.example` clean)
  - `.bel_code/`, `vault.enc`, `bel_code.db`
  - `.pytest_cache/`, `__pycache__/`, `.venv/`
- Automated CI and local test `tests/test_opencode_foundation.py::test_no_hardcoded_api_credentials` verify that committed code never embeds live credentials.

---

## 4. Telemetry & Outbound Network Connections

- **BEL CODE sends zero telemetry or analytics to any BEL server.**
- Outbound network connections happen **only** to:
  1. The AI provider API base URLs you configure and invoke (when running cloud models), or `localhost` when using Ollama / local gateways.
  2. External documentation/signup links that **you manually click** in your browser.

---

## 5. Third-Party Cloud Provider Data Retention Warnings

Cloud AI providers enforce their own terms — check privacy before pasting confidential code:

- **Google Gemini API (Free Tier):** Google states that Free Tier prompts, uploads, and outputs may be reviewed by human reviewers and used to improve Google products. Switch to a Paid Tier project for confidential data.
- **Mistral AI (Free Mode) & NVIDIA NIM (Free Hosted):** Check console opt-out / evaluation terms before submitting sensitive code.
- **Groq & OpenRouter:** Do not use prompts for training by default (OpenRouter logs metadata only unless prompt logging is opted in; note upstream third-party hosts).
- **Maximum Privacy Option:** Use **Ollama** locally (`http://localhost:11434/v1`) — 100% of code and prompts remain strictly on your device.
