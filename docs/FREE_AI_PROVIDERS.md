# Free AI Providers Guide

All facts below match BEL CODE's maintained registry ([bel_code/providers/registry.json](../bel_code/providers/registry.json)), last verified **2026-11-14**.  
**Transparency promise:** BEL CODE clearly separates **ongoing free tiers**, **rate/daily-limited free tiers**, **small monthly trial credits**, **local models**, and **paid services** — never advertising time-limited credits as permanently free.

---

## Quick Comparison

| Provider | Classification | Card Required? | API Base URL | OpenAI-Compatible | Privacy Note |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Google Gemini API** | Ongoing free prototyping tier | No (eligible regions) | `https://generativelanguage.googleapis.com/v1beta` | Yes (`/v1beta/openai/`) | Free-tier prompts may be used by Google to improve models |
| **Groq** | Ongoing free tier (strict TPM) | No | `https://api.groq.com/openai/v1` | Yes | Prompts not used for training by default |
| **Cloudflare Workers AI** | Ongoing free daily allowance | No | `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1` | Yes | 10,000 free Neurons/day; requires Account ID + Token |
| **OpenRouter (`openrouter/free`)** | Free tier with daily caps | No | `https://openrouter.ai/api/v1` | Yes | Zero prompt logging by OpenRouter by default; check upstream hosts |
| **Mistral AI (Free Mode)** | Free experimentation tier | No (phone check possible) | `https://api.mistral.ai/v1` | Yes | Check Console privacy settings; ~1 RPS limit |
| **NVIDIA NIM** | Free prototyping API | No | `https://integrate.api.nvidia.com/v1` | Yes | Evaluation/prototyping only; not for production |
| **SambaNova Cloud** | Free evaluation tier | No | `https://api.sambanova.ai/v1` | Yes | Strict 20 RPD / 200K TPD on major models; content owned by user |
| **Cerebras Cloud** | Free developer tier | No | `https://api.cerebras.ai/v1` | Yes | Per-minute TPM & daily token allocations |
| **GitHub Models** | Free prototyping playground | No | `https://models.github.ai/inference` | Yes | Needs GitHub PAT (`models:read`); prototyping only |
| **Hugging Face Inference** | Small monthly free credits | No | `https://router.huggingface.co/v1` | Yes | **$0.10/month** free credit (depletes quickly) |
| **Ollama (Local AI)** | 100% Free Open-Source Local | No | `http://localhost:11434/v1` | Yes | 100% local & offline; zero data leaves your machine |
| **OpenAI / Anthropic** | Paid pay-as-you-go | Yes | Official API endpoints | OpenAI: Yes / Anthropic: No | Disabled by default; never auto-escalated from free providers |

---

## Detailed Provider Notes

### 1. Google Gemini API (Recommended Starting Point)
- **Official Console & Key Creation:** <https://aistudio.google.com/apikey>
- **Credit Card:** Not required on the Free Tier (available regions; EEA/UK/Switzerland rules may differ).
- **Verified Free Models:** `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.0-flash`, `gemini-2.5-pro`.
- **Rate Limits & Quotas:** Enforced per Google Cloud project across RPM, TPM, and RPD (resets midnight Pacific Time). Multiple keys inside the same project share the same project quota.
- **Context Window:** Up to 1M tokens on supported Gemini 2.5 models (bounded per minute by free-tier TPM limits).
- **Privacy & Commercial Warning:** On Google AI Studio's **Free Tier**, prompts and responses may be reviewed by human annotators and used to improve Google products. Do **not** submit confidential proprietary code on the free tier; use Paid Tier or local Ollama for sensitive repositories.

### 2. Groq
- **Official Console & Key Creation:** <https://console.groq.com/keys>
- **Credit Card:** Not required.
- **Verified Free Models:** `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`.
- **Rate Limits:** Per-model organization buckets (~30 RPM, ~6,000–8,000 TPM depending on model, ~1,000–14,400 RPD).
- **Context Window:** Up to 128K tokens (note: large prompts easily exceed free-tier 6K–8K TPM caps and return HTTP 429).
- **Privacy & Commercial:** Does not train on API inputs/outputs by default; temporary logs retained briefly for reliability/abuse prevention.

### 3. OpenRouter (Free Models Only)
- **Official Console & Key Creation:** <https://openrouter.ai/settings/keys>
- **Credit Card:** Not required.
- **Default Safe Model:** `openrouter/free` (automatically selects an active `$0.00` free community model).
- **Rate Limits:** Capped at **20 RPM**; **50 free requests/day** on unfunded accounts (increases to **1,000 requests/day** after purchasing $10 lifetime account credits).
- **Safety Rule in BEL CODE:** BEL CODE defaults OpenRouter to `openrouter/free` ($0.00) so beginners are never unexpectedly charged.

### 4. Mistral AI (La Plateforme Free Mode)
- **Official Console & Key Creation:** <https://console.mistral.ai/api-keys/>
- **Credit Card:** Not required for Free/Experiment Mode (phone verification may be requested).
- **Verified Free Models:** `codestral-latest`, `mistral-small-latest`, `mistral-large-latest`.
- **Rate Limits:** Very strict burst rate (~1 RPS and low RPM/TPM limits); rapid multi-turn coding loops may trigger HTTP 429, handled automatically by BEL CODE's backoff and failover.
- **Commercial Restriction:** Experimentation/prototyping only; production workloads require upgrading to a paid workspace tier.

### 5. Cloudflare Workers AI
- **Official Dashboard:** <https://dash.cloudflare.com/>
- **Credit Card:** Not required for Workers Free plan.
- **Quotas:** **10,000 free Neurons per day** across edge-hosted open-weight models (`@cf/qwen/qwen2.5-coder-32b-instruct`, `@cf/meta/llama-3.3-70b-instruct-fp8-fast`). Beyond 10,000 Neurons on a paid plan costs `$0.011 / 1,000 Neurons`.
- **Setup Requirement:** Requires **both** your **Cloudflare Account ID** and an API Token (`Workers AI Read & Edit`) because `{account_id}` is part of the URL.

### 6. NVIDIA NIM
- **Official Portal:** <https://build.nvidia.com/settings/api-keys>
- **Credit Card:** Not required for NVIDIA Developer evaluation access.
- **Verified Models:** `qwen/qwen2.5-coder-32b-instruct`, `meta/llama-3.3-70b-instruct`, `deepseek-ai/deepseek-r1`.
- **Rate Limits & Restriction:** Dynamic prototyping limits (~40 RPM baseline). Strictly licensed for evaluation and prototyping — not commercial production.

### 7. SambaNova Cloud
- **Official Portal:** <https://cloud.sambanova.ai/apis>
- **Credit Card:** Not required for Free Tier.
- **Verified Models:** `Meta-Llama-3.3-70B-Instruct`, `DeepSeek-V3.1`, `gpt-oss-120b`.
- **Quotas:** Tight Free Tier limits (e.g., **20 RPM**, **20 requests/day**, **200,000 tokens/day** on major production models) — ideal as a secondary failover!

### 8. Cerebras Cloud
- **Official Portal:** <https://cloud.cerebras.ai/>
- **Credit Card:** Not required.
- **Verified Models:** `llama-3.3-70b`, `llama3.1-8b`, `qwen-3-32b`.
- **Quotas:** Developer free tier with daily token allocations (~1M tokens/day tier depending on current console policy) paired with per-minute RPM/TPM caps.

### 9. GitHub Models & Hugging Face Inference Providers
- **GitHub Models (`https://models.github.ai/inference`):** Free prototyping using a GitHub Personal Access Token (`Models: Read`). Low per-request token limits (~8K in / 4K out on flagship models) make it suited for lightweight testing, not huge repo context.
- **Hugging Face Inference Providers (`https://router.huggingface.co/v1`):** **Trial/credit tier** — Free accounts receive **$0.10/month** in credits. Once exhausted, requests fail unless you upgrade to PRO or enable pay-as-you-go billing.

### 10. Ollama (Local) & FreeLLMAPI (Local Gateway)
- Completely free and local on your machine (`http://localhost:11434/v1` for Ollama; `http://localhost:31415/v1` for FreeLLMAPI). See [Local AI Guide](LOCAL_AI.md).
