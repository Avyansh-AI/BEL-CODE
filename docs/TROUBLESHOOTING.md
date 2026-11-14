# Troubleshooting Guide

Common setup issues and exact fixes when connecting AI providers to BEL CODE.

---

## 1. Invalid or Rejected API Key (`HTTP 401` / `403`)

- **Symptom:** `Authentication failed for '<provider>'.` or `API key missing`.
- **Fixes:**
  - Verify there are no trailing spaces or quotes around your key when pasting or editing `.env`.
  - Check that the key matches the selected provider (for example `AIza...` for Gemini, `gsk_...` for Groq, `sk-or-...` for OpenRouter, `nvapi-...` for NVIDIA NIM, `hf_...` for Hugging Face).
  - For **Hugging Face**, ensure your fine-grained token has **"Make calls to Inference Providers"** permission checked.
  - For **GitHub Models**, ensure your PAT has the **`models:read`** account permission enabled.

---

## 2. Rate Limit / Quota Exhausted (`HTTP 429`)

- **Temporary Rate Limit (RPM / TPM / ~1 RPS burst):**
  - **Symptom:** `429 Too Many Requests` during fast back-to-back tool calls (common on **Groq** 6K–8K TPM free tier or **Mistral** ~1 RPS free mode).
  - **Fix:** Wait a few seconds (BEL CODE automatically applies exponential backoff) or configure a **Secondary Failover** provider (e.g., Gemini + Groq + Cerebras).
- **Daily Quota Exhausted (RPD / Neurons / Credits):**
  - **Symptom:** `quota exceeded`, `daily limit`, `credits exhausted` (e.g., OpenRouter 50 RPD unfunded limit, SambaNova 20 RPD limit, Cloudflare 10,000 Neurons/day, Hugging Face `$0.10/mo` limit).
  - **Fix:** Switch to another configured free provider or local **Ollama**. BEL CODE immediately fails over when a Secondary provider is configured instead of wasting retries.

---

## 3. Cloudflare Workers AI Fails with Missing `account_id`

- **Symptom:** Base URL still contains `{account_id}`.
- **Fix:** Copy your 32-character **Account ID** from the Cloudflare Dashboard (`Workers & Pages -> Overview`) and enter it into the **Cloudflare Account ID** field in the wizard (or set `CLOUDFLARE_ACCOUNT_ID` in `.env`).

---

## 4. Ollama Local Connection Refused (`localhost:11434`)

- **Symptom:** `Ollama server is not reachable at http://localhost:11434.`
- **Fix:**
  1. Ensure Ollama is installed and running (`ollama serve` or launch the desktop tray app).
  2. Verify at least one model is downloaded (`ollama list` / `ollama pull qwen2.5-coder:7b`).
  3. If running Ollama inside Docker/WSL2 or on a custom port, update Base URL or `OLLAMA_HOST`.

---

## 5. Local FreeLLMAPI Gateway Not Responding

- **Symptom:** `FreeLLMAPI server not reachable on localhost:31415 or localhost:3001`.
- **Fix:** FreeLLMAPI (`freellm`) is a **separate local gateway server**. Unless you installed and started FreeLLMAPI locally, switch your Primary AI provider inside **Connect Your AI** to a free cloud provider (**Gemini**, **Groq**, **OpenRouter**) or local **Ollama**.

---

## 6. Model Not Found / Deprecation Errors (`HTTP 404`)

- Free model IDs hosted by OpenRouter, Groq, or NVIDIA NIM rotate occasionally.
- Click **Discover Models** inside **Settings** or re-select `openrouter/free` (which auto-selects active zero-cost models).

---

## 7. Prompt Too Large / Context Window Exceeded (`HTTP 400` / `413`)

- Free tiers often cap tokens-per-minute (Groq 6K–8K TPM) or tokens-per-request (GitHub Models ~8K input) below the model's theoretical maximum.
- Keep attached files focused instead of attaching huge generated files, or route large-context tasks to Gemini (`gemini-2.5-flash`) or a local 14B/32B Ollama model.
