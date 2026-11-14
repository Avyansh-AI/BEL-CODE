# Installation Guide

This guide covers full installation of **BEL CODE** on Windows, macOS, and Linux, including optional dependencies and clean-environment verification.

---

## 1. System Requirements

| Requirement | Minimum | Notes |
| :--- | :--- | :--- |
| **Operating System** | Windows 10/11, macOS 12+, or Linux | Tested across Python 3.10, 3.11, and 3.12 |
| **Python** | `3.10+` | Verify with `python --version` (or `python3 --version`) |
| **Git** | `2.30+` | Required for submodule checkout, checkpoints, and rollback |
| **Node.js** *(Optional)* | `18+` | Only needed if invoking the `@opencode-ai/sdk` TypeScript/ESM bridge (`sdk_adapter.mjs`) |
| **Ollama** *(Optional)* | Latest | Only needed for 100% offline local models (8 GB+ RAM recommended) |

---

## 2. Clone the Repository

Clone BEL CODE together with the pinned upstream `vendor/opencode` submodule:

```bash
git clone --recurse-submodules https://github.com/Avyansh-AI/BEL-CODE.git
cd BEL-CODE
```

If you already cloned without `--recurse-submodules`, initialize the submodule:

```bash
git submodule update --init --recursive
```

*(Optional, for contributors syncing upstream OpenCode later)*:

```bash
git -C vendor/opencode remote add upstream https://github.com/anomalyco/opencode.git
```

---

## 3. Create a Virtual Environment & Install Dependencies

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Editable Package & Developer Extras

To install the `bel-code` CLI command and developer extras (`pytest`, `playwright`, `keyring`):

```bash
pip install -e ".[dev]"
```

Core runtime dependencies (from `pyproject.toml`):
- `fastapi>=0.100.0`
- `uvicorn>=0.23.0`
- `pydantic>=2.0.0`
- `httpx>=0.24.0`

---

## 4. Verify the Installation

Run the CLI version check, workspace scan, and full automated test suite:

```bash
python run_bel_code.py --version
python run_bel_code.py --scan
pytest -v
```

If you installed in editable mode (`pip install -e .`), you can also use:

```bash
bel-code --version
```

---

## 5. Launch BEL CODE

Start the local web server (default host `127.0.0.1`, default CLI port `8420`, or specify `--port 8765`):

```bash
python run_bel_code.py --port 8765
```

Open **`http://127.0.0.1:8765`** in your browser.

For first-time AI provider setup, continue to [Getting Started](GETTING_STARTED.md) or [API Configuration](API_CONFIGURATION.md).
