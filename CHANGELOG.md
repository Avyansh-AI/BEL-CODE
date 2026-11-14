# Changelog

All notable changes to **BEL CODE** are documented in this file.

## [2.1.0] - 2026-11-14

### Added
- **3-Layer OpenCode-Powered Architecture** (`vendor/opencode` v2.3.0 submodule, `opencode.json`, `bel_code/opencode_bridge/`) preserving strict upstream boundaries (`UPSTREAM_MODIFICATIONS.md`, `THIRD_PARTY_NOTICES.md`).
- **AI-First Workspace & On-Demand IDE** (`bel_code/frontend/`) with dark-graphite UI, floating task composer, conversation timeline, inline approval banners, and toggleable Explorer / Code & Diff Editor / Terminal / Problems / Output / Debug Console / Task Logs IDE mode.
- **Guided "Connect Your AI" Onboarding Wizard & Verified Provider Registry** (`bel_code/providers/registry.json`) supporting Google Gemini API, Groq, OpenRouter (`openrouter/free`), Mistral AI, Cloudflare Workers AI, NVIDIA NIM, SambaNova Cloud, Cerebras, GitHub Models, Hugging Face Inference, Ollama (local), FreeLLMAPI, Token Harbor, OpenAI, Anthropic, and custom OpenAI/Anthropic-compatible endpoints.
- **Local Encrypted Credential Storage (`SecretVault`)** in `bel_code/config/settings.py` using PBKDF2-HMAC-SHA256 + Encrypt-then-MAC (`~/.bel_code/vault.enc`), optional OS keyring, multi-account labels, key rotation, and automatic log/response redaction.
- **Smart Provider Routing & Budget Guardrails** (`bel_code/providers/router.py`) with task-type routing, HTTP 429 exponential backoff, permanent-quota failover, strict no-silent-paid-escalation guardrail, and per-task/per-project/global spend caps (`BudgetConfig`).
- **Autonomous Engineering Engine** (`bel_code/engine/`) with 7-stage lifecycle (`understand` -> `plan` -> `implement` -> `test` -> `repair` -> `review` -> `deliver`), 9 specialized multi-agent roles with disjoint file ownership locks, strict exit-code test verification, and 10 acceptance benchmarks.
- **Security Policy, Git Checkpoints & Memory** (`bel_code/security/`, `bel_code/git_ops/`, `bel_code/memory/`) featuring `ask`/`supervised`/`autonomous` modes, prompt-injection scanner, SHA-256 hash-chained audit log, non-destructive Git rollback preserving uncommitted files, and project-isolated SQLite memory.
- **Skills, Forge & Workspace Analyzers** (`bel_code/advanced/capabilities.py`) for custom skill registration/execution, project scaffolding templates, SAST security scan, database analysis, profiling, OpenAPI generation, CI/CD generation, and self-evaluation.
- **Open-Source Documentation & CI**: Full setup/provider/architecture/agent/skill guides (`docs/`), community templates (`.github/`), `.env.example`, and GitHub Actions CI across Python 3.10–3.12.
