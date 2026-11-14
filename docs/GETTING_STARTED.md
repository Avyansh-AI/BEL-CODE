# Getting Started with BEL CODE

This guide takes you from downloading **BEL CODE** to completing your first AI-assisted coding task in a few minutes — with **zero paid subscriptions required**.

---

## 1. System Requirements

- **OS:** Windows 10/11, macOS, or Linux
- **Python:** 3.10 or newer (`python --version`)
- **Git:** 2.30 or newer (`git --version`)
- **Optional (for upstream OpenCode TypeScript bridge):** Node.js 18+ (`node --version`)
- **Optional (for 100% offline local AI):** [Ollama](https://ollama.com/) with 8 GB+ RAM

---

## 2. Download & Install

```bash
git clone --recurse-submodules https://github.com/Avyansh-AI/BEL-CODE.git
cd BEL-CODE
pip install -r requirements.txt
```

If you cloned without `--recurse-submodules`, initialize `vendor/opencode`:

```bash
git submodule update --init --recursive
```

*(Optional, for contributors or editable CLI install)*:

```bash
pip install -e ".[dev]"
```

For OS-specific virtual environment instructions, see [Installation Guide](INSTALLATION.md).

---

## 3. Launch BEL CODE

Start the local BEL CODE server:

```bash
python run_bel_code.py --port 8765
```

Then open your browser to:

```text
http://127.0.0.1:8765
```

---

## 4. Connect Your AI (First Launch)

On first launch, BEL CODE shows **Welcome to BEL CODE** with three beginner-friendly setup paths:

1. **Connect a Free Cloud AI Provider** — Pick a verified no-credit-card provider such as **Google Gemini API**, **Groq**, **OpenRouter (`openrouter/free`)**, **Cloudflare Workers AI**, **Mistral AI**, **SambaNova Cloud**, **Cerebras**, or **NVIDIA NIM**, follow the 4-step guide inside **Connect Your AI**, paste your API key, and click **Save & Test Connection**.
2. **Connect Your Own API** — Connect any OpenAI-compatible or Anthropic-compatible API, proxy (LiteLLM / vLLM / LM Studio), or commercial key.
3. **Use a Local AI Model** — Use **Ollama** on your own computer completely free and offline with **no API key needed** (see [Local AI Guide](LOCAL_AI.md)).

### Prefer `.env` Configuration Instead of the UI?

Copy `.env.example` to `.env` in the repository root and fill in whichever provider key(s) you want:

```bash
cp .env.example .env
```

Never commit `.env` to Git (it is already listed in `.gitignore`). Restart BEL CODE after editing `.env`.

---

## 5. Run Your First Coding Task

1. Click **Open Workspace Folder** (`+` next to **Projects** in the left sidebar) or choose the current project in the composer.
2. Type a request in the bottom composer, for example:
   - `"Summarize the structure of this repository."`
   - `"Add unit tests for the calculator module."`
3. Watch the AI timeline execute, inspect file diffs, or switch to **Open IDE** in the top-right corner for Explorer, Code Editor, Diff viewer, Terminal, and Problems.

---

## Next Guides

- [Installation Guide](INSTALLATION.md)
- [Free AI Providers Comparison](FREE_AI_PROVIDERS.md)
- [API Configuration & Routing](API_CONFIGURATION.md)
- [Local AI Setup with Ollama](LOCAL_AI.md)
- [Architecture Overview](ARCHITECTURE.md)
- [Agents & Autonomous Workflow](AGENTS.md)
- [Skills & Skill Forging](SKILL_FORGING.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Privacy & Security](PRIVACY_AND_SECURITY.md)
