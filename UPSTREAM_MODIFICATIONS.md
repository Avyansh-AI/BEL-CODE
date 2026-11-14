# BEL Code — Upstream OpenCode Foundation & Modification Boundaries

## 1. Upstream Repository Provenance

BEL Code's Layer 1 Foundation is built on top of the open-source **OpenCode** project (`anomalyco/opencode`):

- **Upstream Repository**: `https://github.com/anomalyco/opencode`
- **Upstream Git Remote**: `upstream` (`https://github.com/anomalyco/opencode.git`)
- **Cloned Location**: `vendor/opencode/`
- **Pinned Upstream Commit Hash**: `cbe71595e1f370ff91b3081872b96bbe56519c41`
- **Upstream Version**: `2.3.0`
- **Upstream License**: MIT License (`Copyright (c) 2025 opencode`) — preserved in `vendor/opencode/LICENSE` and `THIRD_PARTY_NOTICES.md`.

---

## 2. Clean 3-Layer Architecture

To keep the upstream OpenCode codebase clean, modular, and rebase-friendly, BEL Code strictly separates responsibilities across three architectural layers:

### Layer 1: OpenCode Foundation (`vendor/opencode/`, `opencode.json`, `bel_code/opencode_bridge/`)
- **`vendor/opencode/`**: The upstream OpenCode monorepo:
  - `packages/core` & `packages/opencode`: Core runtime, session state machine (`src/session/`), tool execution engine (`src/tool/`), built-in agents (`src/agent/`), provider registry (`src/provider/`), permission engine (`src/permission/`), MCP integration (`src/mcp/`), skills (`src/skill/`), and filesystem operations (`src/filesystem/`).
  - `packages/server`: OpenCode HTTP & Server-Sent Events (SSE) server (`/global/health`, `/project`, `/session`, `/session/:id/message`, `/session/:id/diff`, `/session/:id/revert`, `/config`, `/agent`, `/file`, `/find`, `/mcp`, `/event`).
  - `packages/sdk` & `packages/sdk/js`: Official `@opencode-ai/sdk` / `@opencode/sdk` TypeScript/JS client.
  - `packages/cli`: OpenCode CLI (`opencode serve`).
- **`opencode.json`**: Declarative OpenCode configuration adhering to `https://opencode.ai/config.json`. Configures `provider.freellm` (`@ai-sdk/openai-compatible`) as the primary default intelligence provider using `{env:FREELLM_API_KEY}`, `provider.tokenharbor` as the secondary verified provider using `{env:TOKENHARBOR_API_KEY}`, agent roles (`build`, `plan`, `architect`, `reviewer`, `debugger`, `security_auditor`), and permission policies (`edit`, `bash`, `webfetch`).
- **`bel_code/opencode_bridge/`**:
  - `runtime.py` (`OpenCodeRuntimeManager`): Inspects `vendor/opencode`, synchronizes `opencode.json` without ever writing plaintext secrets to disk, injects decrypted API keys from `SecretVault` into runtime memory, and provides the OpenCode HTTP/SSE session/tool/diff/revert protocol bridge.
  - `client.py` (`OpenCodeClient`): Full Python client implementing all 24+ OpenCode server routes and SDK namespaces (`session`, `config`, `agent`, `file`, `find`, `project`, `mcp`, `event`).
  - `sdk_adapter.mjs`: Node/ESM adapter wrapping `@opencode-ai/sdk` (`vendor/opencode/packages/sdk/js`).

### Layer 2: BEL Code Intelligence (`bel_code/providers/`, `bel_code/intelligence/`, `bel_code/memory/`, `bel_code/advanced/`, `bel_code/engine/`, `bel_code/security/`)
- **`bel_code/providers/`**:
  - `freellm.py` (`FreeLLMAdapter`): Primary FreeLLM API adapter supporting both OpenAI-compatible gateway endpoints (`/v1/chat/completions`, `/v1/models`, smart routing modes `auto`, `auto:fast`, `auto:smart`, `fusion`) and direct FreeLLM REST endpoints (`https://apifreellm.com/api/v1/chat`), with streaming SSE parsing, HTTP 429 exponential backoff, and automatic structured JSON/XML `<tool_call>` prompt-engineering fallback.
  - `adapters.py` & `router.py` (`ProviderManager`): 12+ enterprise provider adapters (FreeLLM, Token Harbor, OpenAI, Anthropic, Gemini, Groq, OpenRouter, DeepSeek, Mistral, Together, Cerebras, Fireworks, Local/Ollama) with complexity-aware routing, circuit breakers, and cost budget enforcement.
- **`bel_code/config/settings.py`**: `SecretVault` (OS keyring + PBKDF2-HMAC-SHA256 authenticated encrypted vault `~/.bel_code/vault.enc`), `BudgetConfig`, and `SettingsManager`.
- **`bel_code/intelligence/indexer.py`**: AST symbol indexing, cross-file dependency graphs, Mermaid architecture map generation, and context retrieval.
- **`bel_code/memory/store.py`**: Project-isolated persistent engineering memory, decision records, changelogs, and session state recovery.
- **`bel_code/engine/`**:
  - `workflow.py` (`AutonomousWorkflowEngine`): 7-Stage Standard Engineering Lifecycle (`Understand` -> `Plan` -> `Implement` -> `Test` -> `Repair` -> `Review` -> `Deliver`) and 10 real-execution Acceptance Benchmarks.
  - `multi_agent.py` (`MultiAgentCoordinator`): 9 specialized subagent roles with disjoint `owned_files` lock enforcement and orchestrator integration verification.
  - `debugger.py` (`TestAndDebugEngine`): Automated test runner, stack trace fault localization, and minimal patch repair loop.
- **`bel_code/security/policy.py`**: `ask`, `supervised`, and `autonomous` security modes, human approval queue, prompt injection scanner, and SHA-256 hash-chained audit log.

### Layer 3: BEL Code Application (`bel_code/server/`, `bel_code/frontend/`)
- **`bel_code/server/app.py`**: FastAPI gateway exposing both OpenCode Foundation endpoints (`/api/opencode/*`, `/global/health`, `/session/*`, `/config/*`, `/agent`, `/file/*`, `/find/*`, `/event`), workspace explorer/editor/problems routes (`/api/files/*`, `/api/problems`), and all BEL Code Layer 2 intelligence endpoints.
- **`bel_code/frontend/`**: Redesigned 4-zone AI Software Engineering Workspace (`index.html`, `styles.css`, `app.js`) featuring Left Navigation & File Explorer, Central Session/Code Workspace (Idle & Active Execution Timeline), Right Contextual Panel (Changed Files, Code Diffs, Project Context, Agent Info, Task Progress, Test Results), and Collapsible Bottom Development Panel (Terminal, Output, Problems, Tests, Changes).

---

## 3. Upstream Modification Boundary Rules

1. **Do Not Hardcode Custom Business Logic Inside `vendor/opencode/`**:
   - All BEL Code custom intelligence, FreeLLM smart routing, multi-agent lock coordination, encrypted `SecretVault`, and 7-stage lifecycle orchestration live in `bel_code/` (Layers 2 & 3) and communicate with Layer 1 via `bel_code/opencode_bridge/` and `opencode.json`.
   - `bel_code/opencode_bridge/runtime.py` and `bel_code/opencode_bridge/sdk_adapter.mjs` interface directly with upstream v2.3 `@opencode/core`, `@opencode/server`, `@opencode/cli`, and `@opencode/sdk` (`vendor/opencode/packages/core`, `server`, `cli`, `sdk`) without adding or altering files inside `vendor/opencode/`.
2. **Zero Hardcoded Credentials**:
   - `opencode.json` must only reference environment variables (`{env:FREELLM_API_KEY}`, `{env:TOKENHARBOR_API_KEY}`).
   - Decrypted secrets are stored exclusively in `SecretVault` (`~/.bel_code/vault.enc`) and injected into runtime memory by `OpenCodeRuntimeManager.build_runtime_environment()`.
3. **Preserve Upstream Attribution**:
   - `vendor/opencode/LICENSE` and `THIRD_PARTY_NOTICES.md` must remain intact in all distributions.

---

## 4. Safe Upstream Update & Rebase Procedure

To fetch and integrate upstream `anomalyco/opencode` updates safely:

```powershell
# 1. Enter the upstream foundation directory
cd vendor/opencode

# 2. Verify the upstream remote points to anomalyco/opencode
git remote -v
# upstream  https://github.com/anomalyco/opencode.git (fetch)

# 3. Fetch latest upstream commits
git fetch upstream dev

# 4. Inspect upstream changelog and commit diff before merging
git log HEAD..upstream/dev --oneline

# 5. Rebase or fast-forward onto the target upstream commit
git merge --ff-only upstream/dev

# 6. Return to BEL Code root and run the full verification & foundation test suite
cd ../..
pytest -v
python run_bel_code.py --benchmark all
```
