# API Configuration & Smart Routing

BEL CODE supports multiple configuration styles so both beginners and advanced engineers can manage AI keys, multiple accounts, custom endpoints, and automatic failover.

---

## 1. Configuring via the UI ("Connect Your AI" & Settings)

1. Open **Connect Your AI** from the Welcome screen or left sidebar, OR click **Settings** at the bottom of the sidebar.
2. Select a provider (for example `Google Gemini API`, `Groq`, `OpenRouter`, `Cloudflare Workers AI`, `Ollama`).
3. Optionally enter a **Key Label** (such as `default`, `personal`, or `work`) to keep multiple keys under the same provider.
4. Choose a **Routing Role**:
   - **Primary AI Provider**: Used first for coding and chat requests.
   - **Secondary Failover**: Automatically used if Primary fails or hits a permanent quota limit.
   - **Final Fallback**: Last-resort free/local safety net.
5. Click **Save & Test Connection** to verify real authentication against the live API before starting work.

All keys saved through the UI or API are encrypted locally in `~/.bel_code/vault.enc` — never stored in plain JSON or Git-tracked files.

---

## 2. Configuring via Environment Variables (`.env`)

BEL CODE automatically loads `.env` from the repository root (if present) alongside OS environment variables. See [`.env.example`](../.env.example) for the complete list:

```dotenv
GEMINI_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
MISTRAL_API_KEY=...
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...
NVIDIA_API_KEY=...
SAMBANOVA_API_KEY=...
CEREBRAS_API_KEY=...
HF_TOKEN=...
GITHUB_TOKEN=...
OLLAMA_HOST=http://localhost:11434
FREELLM_API_KEY=
```

Resolution order in `SecretVault`:
1. Encrypted key saved in `vault.enc` (explicit user configuration takes precedence).
2. Environment variable (shell export or `.env`).

---

## 3. Adding Any Custom OpenAI / Anthropic Endpoint (No Code Changes)

You can connect LM Studio, vLLM, LiteLLM, `llama.cpp`, Corporate Gateways, or any compatible proxy directly through the UI or HTTP API:

- **In UI:** Click **Connect Your Own API** (or **Open Guided Wizard -> Connect Your Own API**), choose **OpenAI-compatible** or **Anthropic-compatible**, enter **Base URL**, **Default Model ID**, and optional API key, then click **Save Custom Provider**.
- **Via API (`POST /api/providers/custom`):**

```json
{
  "provider_id": "litellm-local",
  "name": "Local LiteLLM Proxy",
  "endpoint_mode": "openai",
  "base_url": "http://localhost:4000/v1",
  "default_model": "qwen2.5-coder",
  "api_key": "",
  "set_as_primary": true
}
```

Registered custom providers are saved to non-secret user settings, synced with `opencode.json` (using `{env:...}` placeholders), and appear immediately in the model selector.

---

## 4. Smart Routing, HTTP 429 Handling & Cost Guardrails

BEL CODE's `ProviderManager` protects beginners from dropped requests and unexpected bills:

- **Temporary Rate Limit (HTTP 429 without daily quota exhaustion):** Applies exponential backoff retries and inspects `Retry-After` / `X-RateLimit-Reset` headers before failing over to your Secondary provider.
- **Permanent Quota Exhaustion (HTTP 429 with `daily limit`, `quota exceeded`, `insufficient_quota`, etc.):** Immediately marks the limit as permanent for the attempt (skipping fruitless retries) and routes cleanly to your configured Secondary / Fallback provider.
- **Clear Failover Notification:** Whenever failover activates, BEL CODE records the exact reason in `attempt_history` and informs the user in the response limitations note.
- **Strict No-Silent-Paid-Escalation Guardrail:** When running on a free (`$0.00`) provider, BEL CODE **never** automatically fails over to a paid commercial provider (`OpenAI`, `Anthropic`, `DeepSeek`, etc.) unless `allow_expensive_upgrade=True` is explicitly enabled and within configured USD budget caps.
