"""Tests verifying that BEL Code is 100% standalone and contains no hardcoded secrets."""

import ast
import re
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCAN_EXTENSIONS = {".py", ".js", ".html", ".md", ".toml"}
IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", "dist", "build", ".bel_code", "vendor"}

# Forbidden external assistant package names (BEL Code is `bel_code`, NOT `bel` or `bel_assistant`)
FORBIDDEN_IMPORTS = {"bel", "bel_assistant", "bel_core", "bel_agent"}

# Real-looking hardcoded secret patterns (excluding short placeholders/examples in docs or tests)
REAL_SECRET_PATTERNS = [
    # Real OpenAI/Anthropic keys (not test placeholders with 'test', 'dummy', 'example', 'xxx', 'placeholder', 'secret', '1234', 'abcd')
    re.compile(r"\bsk-(?:proj-|ant-)?(?!(?:test|dummy|example|mock|fake|placeholder|sample|live_test|xxx|1234|abcd))[A-Za-z0-9]{32,}\b"),
    re.compile(r"\bthk_live_(?!(?:test|dummy|example|mock|fake|placeholder|sample|xxx|1234|abcd))[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bfreellmapi-(?!(?:test|dummy|example|mock|fake|placeholder|sample|xxx|1234|abcd))[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
]


def iter_project_files():
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SCAN_EXTENSIONS:
            yield path


def test_project_root_exists():
    assert PROJECT_ROOT.exists()
    assert (PROJECT_ROOT / "bel_code").exists()


def test_zero_external_bel_imports_in_python_files():
    """Verify no Python file imports an external `bel` assistant package."""
    violations = []
    for py_file in PROJECT_ROOT.rglob("*.py"):
        if any(part in IGNORE_DIRS for part in py_file.parts):
            continue
        source = py_file.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            violations.append(f"{py_file}: SyntaxError {exc}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_pkg = alias.name.split(".")[0]
                    if top_pkg in FORBIDDEN_IMPORTS:
                        violations.append(f"{py_file}:{node.lineno} imports forbidden package '{alias.name}'")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_pkg = node.module.split(".")[0]
                    if top_pkg in FORBIDDEN_IMPORTS:
                        violations.append(f"{py_file}:{node.lineno} imports from forbidden package '{node.module}'")

    assert not violations, "Standalone architecture violation(s) found:\n" + "\n".join(violations)


def test_no_hardcoded_api_credentials():
    """Scan all .py, .js, .html, .md, .toml files for hardcoded live credentials."""
    findings = []
    for file_path in iter_project_files():
        content = file_path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(content.splitlines(), start=1):
            for pattern in REAL_SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    findings.append(f"{file_path.relative_to(PROJECT_ROOT)}:{line_no} matched secret pattern: {match.group(0)[:12]}...")

    assert not findings, "Hardcoded credentials detected:\n" + "\n".join(findings)


def test_standalone_package_structure():
    """Ensure BEL Code owns its own namespace `bel_code` and has required subpackages."""
    expected_dirs = [
        "config",
        "providers",
        "security",
        "tools",
        "git_ops",
        "intelligence",
        "memory",
        "advanced",
        "engine",
        "server",
        "frontend",
    ]
    bel_code_dir = PROJECT_ROOT / "bel_code"
    for sub in expected_dirs:
        assert (bel_code_dir / sub).exists(), f"Missing expected subpackage directory: bel_code/{sub}"
