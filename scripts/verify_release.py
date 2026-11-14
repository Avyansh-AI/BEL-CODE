#!/usr/bin/env python3
"""Release verification script for BEL CODE.

Checks Python syntax and core imports, required open-source files, relative
Markdown links, opencode.json secret placeholders, and live credential hygiene.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

IGNORE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".bel_code",
    "vendor",
}

REQUIRED_FILES = [
    "LICENSE",
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "UPSTREAM_MODIFICATIONS.md",
    ".env.example",
    ".gitignore",
    "pyproject.toml",
    "requirements.txt",
    "opencode.json",
    "run_bel_code.py",
    "assets/bel-code-logo.png",
    "assets/screenshots/workspace-welcome.png",
    "assets/screenshots/connect-ai-wizard.png",
    "assets/screenshots/ide-mode.png",
    "assets/screenshots/agents-orchestration.png",
    "assets/screenshots/skills-forge.png",
    "docs/GETTING_STARTED.md",
    "docs/INSTALLATION.md",
    "docs/FREE_AI_PROVIDERS.md",
    "docs/API_CONFIGURATION.md",
    "docs/LOCAL_AI.md",
    "docs/ARCHITECTURE.md",
    "docs/AGENTS.md",
    "docs/SKILL_FORGING.md",
    "docs/TROUBLESHOOTING.md",
    "docs/PRIVACY_AND_SECURITY.md",
]

_SYNTHETIC_PREFIX = r"(?!(?:test|dummy|example|mock|fake|placeholder|sample|live_test|secretkey|rotatedkey|987654|998877|xxx|1234|abcd))"

LIVE_SECRET_PATTERNS = [
    re.compile(rf"\bsk-(?:proj-|ant-)?{_SYNTHETIC_PREFIX}[A-Za-z0-9]{{32,}}\b"),
    re.compile(rf"\bthk_live_{_SYNTHETIC_PREFIX}[A-Za-z0-9]{{24,}}\b"),
    re.compile(rf"\bfreellmapi-{_SYNTHETIC_PREFIX}[A-Za-z0-9]{{24,}}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    re.compile(r"BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY"),
]

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def check_required_files() -> list[str]:
    return [f"Missing required file: {rel}" for rel in REQUIRED_FILES if not (PROJECT_ROOT / rel).is_file()]


def check_python_syntax_and_imports() -> list[str]:
    errors: list[str] = []
    for py_file in PROJECT_ROOT.rglob("*.py"):
        if any(part in IGNORE_DIRS for part in py_file.parts):
            continue
        try:
            ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError as exc:
            errors.append(f"SyntaxError in {py_file.relative_to(PROJECT_ROOT)}: {exc}")

    for mod_name in ("bel_code", "bel_code.__main__", "bel_code.server.app"):
        try:
            importlib.import_module(mod_name)
        except Exception as exc:
            errors.append(f"Failed importing {mod_name}: {exc}")
    return errors


def check_markdown_links() -> list[str]:
    errors: list[str] = []
    md_files = [PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").glob("*.md"))]
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path_str = target.split("#", 1)[0]
            if not target_path_str:
                continue
            resolved = (md.parent / target_path_str).resolve()
            if not resolved.exists():
                errors.append(f"Broken link in {md.relative_to(PROJECT_ROOT)}: {target}")
    return errors


def check_secrets_and_opencode_config() -> list[str]:
    errors: list[str] = []
    cfg = json.loads((PROJECT_ROOT / "opencode.json").read_text(encoding="utf-8"))
    for pid, pdata in cfg.get("provider", {}).items():
        api_key = str((pdata.get("options") or {}).get("apiKey") or "")
        if api_key and not (api_key.startswith("{env:") and api_key.endswith("}")):
            errors.append(f"opencode.json provider '{pid}' apiKey must use {{env:...}} placeholder")

    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".js", ".mjs", ".html", ".css", ".md", ".json", ".toml", ".yml", ".yaml"}:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(content.splitlines(), start=1):
            for pattern in LIVE_SECRET_PATTERNS:
                if pattern.search(line):
                    errors.append(f"Potential live secret in {path.relative_to(PROJECT_ROOT)}:{line_no}")
    return errors


def main() -> int:
    checks = [
        ("Required files", check_required_files),
        ("Python syntax & imports", check_python_syntax_and_imports),
        ("Markdown links", check_markdown_links),
        ("Secret & opencode.json hygiene", check_secrets_and_opencode_config),
    ]
    all_errors: list[str] = []
    for label, fn in checks:
        errs = fn()
        if errs:
            print(f"[FAIL] {label}:")
            for err in errs:
                print(f"  - {err}")
            all_errors.extend(errs)
        else:
            print(f"[PASS] {label}")
    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
