# Contributing to BEL Code

Thank you for your interest in contributing to **BEL Code**! This project is open-source under the [MIT License](LICENSE).

## Development Setup

1. **Clone with submodules**:
   ```bash
   git clone --recurse-submodules <your-fork-url>
   cd bel-code
   git -C vendor/opencode remote add upstream https://github.com/anomalyco/opencode.git 2>/dev/null || true
   ```

2. **Create a virtual environment and install dependencies**:
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. **Run the automated test suite**:
   ```bash
   pytest -v
   ```

4. **Start the local web application**:
   ```bash
   python run_bel_code.py --port 8765
   ```
   Open `http://127.0.0.1:8765` in your browser.

## Architectural Guidelines

- **Respect the 3-Layer Boundary**:
  - `vendor/opencode/` is the upstream [OpenCode](https://github.com/anomalyco/opencode) foundation. Do **not** make direct edits inside `vendor/opencode/`; place extensions and adapters in `bel_code/opencode_bridge/`.
  - `bel_code/` contains Layer 2 (intelligence, providers, workflow engine, security, git, memory) and Layer 3 (FastAPI server & web workspace).
  - Document any upstream-facing protocol changes in `UPSTREAM_MODIFICATIONS.md` and preserve attribution in `THIRD_PARTY_NOTICES.md`.
- **Keep BEL Code Standalone**: Never introduce external assistant/project dependencies or non-public imports (verified by `tests/test_independence.py`).
- **Never Commit Secrets**:
  - Do not include real API keys (`freellmapi-`, `sk-`, `thk_`, etc.) or `.bel_code/` / `vault.enc` files in code, tests, docs, or commits.
  - Tests should use synthetic dummy strings and mock HTTP transport handlers.

## Pull Request Checklist

- [ ] Code is clean, focused, and avoids duplicating logic across helpers.
- [ ] New behavior is covered by tests in `tests/`.
- [ ] `pytest -v` passes with zero failures (including `tests/test_independence.py`).
- [ ] Documentation (`README.md`, docstrings, `UPSTREAM_MODIFICATIONS.md`) is updated if needed.
