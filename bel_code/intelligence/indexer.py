"""Project Intelligence & Codebase Understanding (Spec Section 3).

Provides automated workspace scanning, multi-language symbol indexing (Python AST +
structural parsers for JS/TS/Go/Rust), architecture dependency mapping with Mermaid
diagram generation, BM25/TF-IDF + symbol context retrieval, and code health analysis.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


DEFAULT_IGNORED_DIRS: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    "dist",
    "build",
    "target",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
    "coverage",
    "htmlcov",
    "vendor",
    ".bel_code",
}

SENSITIVE_FILE_PATTERNS: Tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.enc",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.so",
    "*.dll",
    "*.exe",
    "*.bin",
    "id_rsa*",
    "id_ed25519*",
    "credentials.json",
    "secrets.json",
)

EXTENSION_TO_LANGUAGE: Dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".c": "C",
    ".h": "C/C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".sql": "SQL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
    ".sh": "Shell",
    ".ps1": "PowerShell",
}

INSTRUCTION_FILENAMES: Set[str] = {
    "claude.md",
    "contributing.md",
    "readme.md",
    "agents.md",
    "architecture.md",
    "feature_tracker.md",
    "security.md",
    "code_of_conduct.md",
}

CONFIG_FILENAMES: Set[str] = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "package.json",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
    "next.config.js",
    "tailwind.config.js",
    "cargo.toml",
    "go.mod",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "makefile",
    ".gitignore",
    ".flake8",
    ".eslintrc.json",
}

ENTRYPOINT_FILENAMES: Set[str] = {
    "main.py",
    "app.py",
    "server.py",
    "cli.py",
    "run.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "__main__.py",
    "index.js",
    "index.ts",
    "server.js",
    "server.ts",
    "main.go",
    "main.rs",
    "lib.rs",
}


class ProjectIntelligence:
    """Automated workspace scanner, AST symbol indexer, architecture mapper, and retriever."""

    def __init__(self, workspace_root: Union[str, Path]) -> None:
        self.workspace_root: Path = Path(workspace_root).resolve()
        self._scan_cache: Optional[Dict[str, Any]] = None
        self._symbol_cache: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Internal helpers for .gitignore and file filtering
    # ------------------------------------------------------------------

    def _load_gitignore_patterns(self) -> List[str]:
        patterns: List[str] = []
        gitignore_path = self.workspace_root / ".gitignore"
        if gitignore_path.is_file():
            try:
                for raw_line in gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or line.startswith("!"):
                        continue
                    patterns.append(line.rstrip("/"))
            except OSError:
                pass
        return patterns

    def _is_ignored(self, rel_path: str, is_dir: bool, gitignore_patterns: List[str]) -> bool:
        parts = rel_path.replace("\\", "/").split("/")
        name = parts[-1]

        # Check ignored directories
        for part in parts[:-1] if not is_dir else parts:
            if part in DEFAULT_IGNORED_DIRS or part.endswith(".egg-info"):
                return True

        # Sensitive files check (allow .gitignore itself for config listing, block .env etc.)
        if not is_dir:
            lower_name = name.lower()
            if lower_name == ".env" or lower_name.startswith(".env."):
                return True
            for pat in SENSITIVE_FILE_PATTERNS:
                if fnmatch.fnmatch(lower_name, pat.lower()):
                    return True

        # Check .gitignore patterns
        norm_rel = rel_path.replace("\\", "/")
        for pat in gitignore_patterns:
            clean_pat = pat.lstrip("/")
            if fnmatch.fnmatch(name, clean_pat) or fnmatch.fnmatch(norm_rel, clean_pat):
                return True
            if any(fnmatch.fnmatch(p, clean_pat) for p in parts):
                return True

        return False

    def _iter_workspace_files(self) -> List[Path]:
        if not self.workspace_root.exists():
            return []
        gitignore_patterns = self._load_gitignore_patterns()
        collected: List[Path] = []

        for root, dirs, files in os.walk(self.workspace_root):
            root_path = Path(root)
            try:
                rel_root = root_path.relative_to(self.workspace_root).as_posix()
            except ValueError:
                continue
            if rel_root == ".":
                rel_root = ""

            # Filter dirs in-place so os.walk skips ignored subtrees
            kept_dirs: List[str] = []
            for d in sorted(dirs):
                d_rel = f"{rel_root}/{d}".lstrip("/")
                if not self._is_ignored(d_rel, is_dir=True, gitignore_patterns=gitignore_patterns):
                    kept_dirs.append(d)
            dirs[:] = kept_dirs

            for f in sorted(files):
                f_rel = f"{rel_root}/{f}".lstrip("/")
                if self._is_ignored(f_rel, is_dir=False, gitignore_patterns=gitignore_patterns):
                    continue
                full_path = root_path / f
                if full_path.is_file():
                    collected.append(full_path)

        return collected

    # ------------------------------------------------------------------
    # 1. Project Scan
    # ------------------------------------------------------------------

    def scan_project(self) -> Dict[str, Any]:
        """Scan workspace_root respecting .gitignore and sensitive paths."""
        files = self._iter_workspace_files()
        rel_files: List[str] = []
        languages: Counter[str] = Counter()
        frameworks: Set[str] = set()
        build_systems: Set[str] = set()
        entry_points: List[str] = []
        test_files: List[str] = []
        config_files: List[str] = []
        instruction_files: List[str] = []
        instruction_contents: Dict[str, str] = {}
        dependencies: Dict[str, Any] = {
            "manifests": [],
            "python": [],
            "javascript": [],
            "rust": [],
            "go": [],
            "all": [],
        }
        total_lines = 0
        top_level_dirs: Set[str] = set()

        for file_path in files:
            rel = file_path.relative_to(self.workspace_root).as_posix()
            rel_files.append(rel)
            parts = rel.split("/")
            if len(parts) > 1:
                top_level_dirs.add(parts[0])

            fname = file_path.name
            lower_name = fname.lower()
            ext = file_path.suffix.lower()

            lang = EXTENSION_TO_LANGUAGE.get(ext)
            if lang:
                languages[lang] += 1
            elif lower_name == "dockerfile":
                languages["Dockerfile"] += 1
            elif lower_name == "makefile":
                languages["Makefile"] += 1

            # Instruction files
            if lower_name in INSTRUCTION_FILENAMES:
                instruction_files.append(rel)
                try:
                    content_preview = file_path.read_text(encoding="utf-8", errors="ignore")[:2000]
                    instruction_contents[rel] = content_preview
                except OSError:
                    pass

            # Config & Build files
            if lower_name in CONFIG_FILENAMES or ext in (".toml", ".ini", ".cfg"):
                if rel not in config_files:
                    config_files.append(rel)

            if lower_name == "pyproject.toml":
                build_systems.add("pyproject.toml")
                dependencies["manifests"].append(rel)
                py_deps = self._parse_pyproject_dependencies(file_path)
                dependencies["python"].extend(py_deps)
            elif lower_name in ("requirements.txt", "requirements-dev.txt"):
                build_systems.add("pip")
                dependencies["manifests"].append(rel)
                req_deps = self._parse_requirements_txt(file_path)
                dependencies["python"].extend(req_deps)
            elif lower_name == "setup.py":
                build_systems.add("setuptools")
                dependencies["manifests"].append(rel)
            elif lower_name == "package.json":
                build_systems.add("npm")
                dependencies["manifests"].append(rel)
                js_deps = self._parse_package_json(file_path)
                dependencies["javascript"].extend(js_deps)
            elif lower_name == "cargo.toml":
                build_systems.add("cargo")
                dependencies["manifests"].append(rel)
                rust_deps = self._parse_cargo_toml(file_path)
                dependencies["rust"].extend(rust_deps)
            elif lower_name == "go.mod":
                build_systems.add("go")
                dependencies["manifests"].append(rel)
                go_deps = self._parse_go_mod(file_path)
                dependencies["go"].extend(go_deps)
            elif lower_name == "makefile":
                build_systems.add("make")
            elif lower_name in ("dockerfile", "docker-compose.yml", "docker-compose.yaml"):
                build_systems.add("docker")

            # Test files detection
            if (
                lower_name.startswith("test_")
                or lower_name.endswith("_test.py")
                or lower_name.endswith(".test.js")
                or lower_name.endswith(".spec.js")
                or lower_name.endswith(".test.ts")
                or lower_name.endswith(".spec.ts")
                or lower_name.endswith("_test.go")
                or "tests" in [p.lower() for p in parts[:-1]]
                or "test" in [p.lower() for p in parts[:-1]]
            ) and ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs"):
                test_files.append(rel)
                if ext == ".py":
                    frameworks.add("Pytest")

            # Entry points & source framework sniffing
            try:
                if file_path.stat().st_size <= 512_000:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                    lines = text.splitlines()
                    total_lines += len(lines)
                    self._detect_frameworks_from_source(text, ext, frameworks)
                    if lower_name in ENTRYPOINT_FILENAMES or self._looks_like_entrypoint(text, ext):
                        if rel not in entry_points:
                            entry_points.append(rel)
            except OSError:
                pass

        # Deduplicate dependencies and detect frameworks from dependencies
        for key in ("python", "javascript", "rust", "go"):
            dependencies[key] = sorted(set(dependencies[key]))
        all_deps = sorted(
            set(
                dependencies["python"]
                + dependencies["javascript"]
                + dependencies["rust"]
                + dependencies["go"]
            )
        )
        dependencies["all"] = all_deps
        self._detect_frameworks_from_deps(all_deps, frameworks)

        primary_language = languages.most_common(1)[0][0] if languages else "Unknown"

        result: Dict[str, Any] = {
            "workspace_root": str(self.workspace_root),
            "total_files": len(rel_files),
            "total_lines": total_lines,
            "files": rel_files,
            "languages": dict(languages),
            "primary_language": primary_language,
            "frameworks": sorted(frameworks),
            "build_systems": sorted(build_systems),
            "dependencies": dependencies,
            "entry_points": sorted(entry_points),
            "test_files": sorted(test_files),
            "config_files": sorted(config_files),
            "instruction_files": sorted(instruction_files),
            "instruction_contents": instruction_contents,
            "top_level_directories": sorted(top_level_dirs),
        }
        self._scan_cache = result
        return result

    def _parse_requirements_txt(self, path: Path) -> List[str]:
        deps: List[str] = []
        try:
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                pkg = re.split(r"[<>=!~;\[\s]", line)[0].strip()
                if pkg:
                    deps.append(pkg)
        except OSError:
            pass
        return deps

    def _parse_pyproject_dependencies(self, path: Path) -> List[str]:
        deps: List[str] = []
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            in_deps = False
            for raw in content.splitlines():
                line = raw.strip()
                if line.startswith("dependencies") and "[" in line:
                    in_deps = True
                    # Handle single-line dependencies = ["a", "b"]
                    for m in re.findall(r'["\']([A-Za-z0-9_.\-]+)', line):
                        deps.append(m)
                    if "]" in line:
                        in_deps = False
                    continue
                if in_deps:
                    if line.startswith("]"):
                        in_deps = False
                        continue
                    m = re.match(r'^["\']([A-Za-z0-9_.\-]+)', line)
                    if m:
                        deps.append(m.group(1))
        except OSError:
            pass
        return deps

    def _parse_package_json(self, path: Path) -> List[str]:
        deps: List[str] = []
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict):
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    sec_data = data.get(section)
                    if isinstance(sec_data, dict):
                        deps.extend(sec_data.keys())
        except Exception:
            pass
        return deps

    def _parse_cargo_toml(self, path: Path) -> List[str]:
        deps: List[str] = []
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            in_deps = False
            for raw in content.splitlines():
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_deps = "dependencies" in line.lower()
                    continue
                if in_deps and "=" in line and not line.startswith("#"):
                    pkg = line.split("=", 1)[0].strip()
                    if pkg:
                        deps.append(pkg)
        except OSError:
            pass
        return deps

    def _parse_go_mod(self, path: Path) -> List[str]:
        deps: List[str] = []
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            for raw in content.splitlines():
                line = raw.strip()
                if not line or line.startswith("//") or line.startswith("module ") or line.startswith("go "):
                    continue
                if line.startswith("require") or line == ")" or line == "(":
                    line = line.replace("require", "").replace("(", "").strip()
                if line:
                    parts = line.split()
                    if parts and "/" in parts[0]:
                        deps.append(parts[0])
        except OSError:
            pass
        return deps

    def _detect_frameworks_from_deps(self, all_deps: List[str], frameworks: Set[str]) -> None:
        dep_map = {
            "fastapi": "FastAPI",
            "flask": "Flask",
            "django": "Django",
            "pytest": "Pytest",
            "pydantic": "Pydantic",
            "sqlalchemy": "SQLAlchemy",
            "uvicorn": "Uvicorn",
            "react": "React",
            "next": "Next.js",
            "vue": "Vue",
            "express": "Express",
            "tailwindcss": "Tailwind CSS",
            "playwright": "Playwright",
            "httpx": "HTTPX",
        }
        for dep in all_deps:
            low = dep.lower()
            if low in dep_map:
                frameworks.add(dep_map[low])

    def _detect_frameworks_from_source(self, text: str, ext: str, frameworks: Set[str]) -> None:
        if ext == ".py":
            if re.search(r"\b(?:from\s+fastapi|import\s+fastapi|FastAPI\s*\()", text):
                frameworks.add("FastAPI")
            if re.search(r"\b(?:from\s+flask|import\s+flask|Flask\s*\()", text):
                frameworks.add("Flask")
            if re.search(r"\b(?:from\s+django|import\s+django)\b", text):
                frameworks.add("Django")
            if re.search(r"\b(?:import\s+pytest|from\s+pytest)\b", text):
                frameworks.add("Pytest")
            if re.search(r"\b(?:from\s+pydantic|import\s+pydantic|BaseModel)\b", text):
                frameworks.add("Pydantic")
            if re.search(r"\b(?:from\s+sqlalchemy|import\s+sqlalchemy)\b", text):
                frameworks.add("SQLAlchemy")
        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            if re.search(r"""from\s+['"]react['"]|require\(['"]react['"]\)|useState\s*\(""", text):
                frameworks.add("React")
            if re.search(r"""from\s+['"]next""", text):
                frameworks.add("Next.js")
            if re.search(r"""from\s+['"]vue['"]""", text):
                frameworks.add("Vue")
            if re.search(r"""require\(['"]express['"]\)|from\s+['"]express['"]""", text):
                frameworks.add("Express")
        elif ext in (".html", ".css"):
            if "tailwind" in text.lower():
                frameworks.add("Tailwind CSS")

    def _looks_like_entrypoint(self, text: str, ext: str) -> bool:
        if ext == ".py":
            if 'if __name__ == "__main__"' in text or "if __name__ == '__main__'" in text:
                return True
            if "FastAPI(" in text or "Flask(__name__)" in text:
                return True
        elif ext in (".js", ".ts"):
            if "app.listen(" in text or "createServer(" in text:
                return True
        elif ext == ".go":
            if "func main()" in text:
                return True
        elif ext == ".rs":
            if "fn main()" in text:
                return True
        return False

    # ------------------------------------------------------------------
    # 2. Symbol Indexer (Python AST + Structural Parsers)
    # ------------------------------------------------------------------

    def build_symbol_index(self) -> Dict[str, Any]:
        """Parse workspace files to build a rich index of symbols, modules, APIs, and relationships."""
        files = self._iter_workspace_files()
        all_symbols: List[Dict[str, Any]] = []
        files_index: Dict[str, Dict[str, Any]] = {}
        modules: List[str] = []
        apis: List[Dict[str, Any]] = []
        relationships: Dict[str, List[str]] = defaultdict(list)

        for file_path in files:
            rel = file_path.relative_to(self.workspace_root).as_posix()
            ext = file_path.suffix.lower()
            lang = EXTENSION_TO_LANGUAGE.get(ext, "Other")
            if ext not in (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs"):
                continue

            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            line_count = len(source.splitlines())
            if ext == ".py":
                mod_name = rel[:-3].replace("/", ".")
                if mod_name.endswith(".__init__"):
                    mod_name = mod_name[:-9]
                if mod_name:
                    modules.append(mod_name)
                file_info, file_symbols, file_apis = self._parse_python_ast(rel, source, line_count)
            else:
                modules.append(rel)
                file_info, file_symbols, file_apis = self._parse_structural_file(rel, source, ext, lang, line_count)

            files_index[rel] = file_info
            all_symbols.extend(file_symbols)
            apis.extend(file_apis)

            # Record cross-file relationships (imports + calls)
            targets = sorted(set(file_info.get("imports", []) + file_info.get("calls", [])))
            relationships[rel] = targets

        index_result: Dict[str, Any] = {
            "workspace_root": str(self.workspace_root),
            "total_symbols": len(all_symbols),
            "total_indexed_files": len(files_index),
            "symbols": all_symbols,
            "files": files_index,
            "modules": sorted(set(modules)),
            "apis": apis,
            "relationships": dict(relationships),
            "cross_references": dict(relationships),
        }
        self._symbol_cache = index_result
        return index_result

    def _parse_python_ast(
        self, rel_path: str, source: str, line_count: int
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        symbols: List[Dict[str, Any]] = []
        apis: List[Dict[str, Any]] = []
        imports: List[str] = []
        file_calls: Set[str] = set()
        module_doc = ""

        try:
            tree = ast.parse(source, filename=rel_path)
            module_doc = ast.get_docstring(tree) or ""
        except SyntaxError:
            # Fallback to regex extraction if file has syntax errors (e.g. legacy Python 2 file)
            return self._parse_python_fallback(rel_path, source, line_count)

        # Extract top-level and nested imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base_mod = node.module or ""
                if base_mod:
                    imports.append(base_mod)
                for alias in node.names:
                    full_imp = f"{base_mod}.{alias.name}".lstrip(".")
                    imports.append(full_imp)

        def _extract_calls(subtree: ast.AST) -> List[str]:
            calls: List[str] = []
            for sub in ast.walk(subtree):
                if isinstance(sub, ast.Call):
                    if isinstance(sub.func, ast.Name):
                        calls.append(sub.func.id)
                    elif isinstance(sub.func, ast.Attribute):
                        calls.append(sub.func.attr)
            return sorted(set(calls))

        def _format_args(args_node: ast.arguments) -> str:
            arg_names = [a.arg for a in args_node.args]
            if args_node.vararg:
                arg_names.append(f"*{args_node.vararg.arg}")
            if args_node.kwarg:
                arg_names.append(f"**{args_node.kwarg.arg}")
            return f"({', '.join(arg_names)})"

        def _check_route_decorators(
            decorator_list: List[ast.expr], func_name: str, lineno: int, doc: str
        ) -> None:
            http_methods = {"get", "post", "put", "delete", "patch", "options", "head", "route", "api_route"}
            for dec in decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    attr = dec.func.attr.lower()
                    if attr in http_methods:
                        route_path = "/"
                        if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                            route_path = dec.args[0].value
                        method = "GET" if attr in ("route", "api_route") else attr.upper()
                        apis.append(
                            {
                                "method": method,
                                "path": route_path,
                                "handler": func_name,
                                "file": rel_path,
                                "line": lineno,
                                "docstring": doc,
                            }
                        )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                doc = ast.get_docstring(node) or ""
                calls = _extract_calls(node)
                file_calls.update(calls)
                sig = f"{node.name}{_format_args(node.args)}"
                end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
                _check_route_decorators(node.decorator_list, node.name, node.lineno, doc)
                symbols.append(
                    {
                        "name": node.name,
                        "kind": kind,
                        "file": rel_path,
                        "line": node.lineno,
                        "end_line": end_line,
                        "docstring": doc,
                        "signature": sig,
                        "calls": calls,
                        "parent": None,
                    }
                )
            elif isinstance(node, ast.ClassDef):
                doc = ast.get_docstring(node) or ""
                bases: List[str] = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        bases.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        bases.append(b.attr)
                end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
                method_names: List[str] = []
                cls_calls = _extract_calls(node)
                file_calls.update(cls_calls)

                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_names.append(item.name)
                        m_doc = ast.get_docstring(item) or ""
                        m_calls = _extract_calls(item)
                        m_sig = f"{item.name}{_format_args(item.args)}"
                        m_end = getattr(item, "end_lineno", item.lineno) or item.lineno
                        _check_route_decorators(item.decorator_list, f"{node.name}.{item.name}", item.lineno, m_doc)
                        symbols.append(
                            {
                                "name": item.name,
                                "qualified_name": f"{node.name}.{item.name}",
                                "kind": "method",
                                "file": rel_path,
                                "line": item.lineno,
                                "end_line": m_end,
                                "docstring": m_doc,
                                "signature": m_sig,
                                "calls": m_calls,
                                "parent": node.name,
                            }
                        )

                symbols.append(
                    {
                        "name": node.name,
                        "kind": "class",
                        "file": rel_path,
                        "line": node.lineno,
                        "end_line": end_line,
                        "docstring": doc,
                        "signature": f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}",
                        "bases": bases,
                        "methods": method_names,
                        "calls": cls_calls,
                        "parent": None,
                    }
                )

        # Also catch top-level calls outside functions
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    file_calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    file_calls.add(node.func.attr)

        file_info: Dict[str, Any] = {
            "language": "Python",
            "lines": line_count,
            "docstring": module_doc,
            "imports": sorted(set(imports)),
            "calls": sorted(file_calls),
            "symbols": symbols,
            "apis": apis,
        }
        return file_info, symbols, apis

    def _parse_python_fallback(
        self, rel_path: str, source: str, line_count: int
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        symbols: List[Dict[str, Any]] = []
        imports: List[str] = []
        for idx, line in enumerate(source.splitlines(), start=1):
            m_fn = re.match(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", line)
            if m_fn:
                symbols.append(
                    {
                        "name": m_fn.group(1),
                        "kind": "function",
                        "file": rel_path,
                        "line": idx,
                        "end_line": idx + 5,
                        "docstring": "",
                        "signature": f"{m_fn.group(1)}({m_fn.group(2)})",
                        "calls": [],
                        "parent": None,
                    }
                )
            m_cls = re.match(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if m_cls:
                symbols.append(
                    {
                        "name": m_cls.group(1),
                        "kind": "class",
                        "file": rel_path,
                        "line": idx,
                        "end_line": idx + 10,
                        "docstring": "",
                        "signature": f"class {m_cls.group(1)}",
                        "calls": [],
                        "parent": None,
                    }
                )
            m_imp = re.match(r"^\s*(?:from\s+([A-Za-z0-9_.]+)\s+import|import\s+([A-Za-z0-9_.]+))", line)
            if m_imp:
                imp = m_imp.group(1) or m_imp.group(2)
                if imp:
                    imports.append(imp)
        file_info = {
            "language": "Python",
            "lines": line_count,
            "docstring": "",
            "imports": sorted(set(imports)),
            "calls": [],
            "symbols": symbols,
            "apis": [],
        }
        return file_info, symbols, []

    def _parse_structural_file(
        self, rel_path: str, source: str, ext: str, lang: str, line_count: int
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        symbols: List[Dict[str, Any]] = []
        apis: List[Dict[str, Any]] = []
        imports: List[str] = []
        calls: Set[str] = set()
        lines = source.splitlines()

        for idx, line in enumerate(lines, start=1):
            if ext in (".js", ".jsx", ".ts", ".tsx"):
                # Imports
                m_imp = re.search(r"""(?:from|require\()\s*['"]([^'"]+)['"]""", line)
                if m_imp:
                    imports.append(m_imp.group(1))
                # Function declarations
                m_fn = re.search(
                    r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^)]*)\)",
                    line,
                )
                if m_fn:
                    symbols.append(
                        {
                            "name": m_fn.group(1),
                            "kind": "function",
                            "file": rel_path,
                            "line": idx,
                            "end_line": min(idx + 15, line_count),
                            "docstring": "",
                            "signature": f"{m_fn.group(1)}({m_fn.group(2)})",
                            "calls": [],
                            "parent": None,
                        }
                    )
                # Arrow functions assigned to const/let
                m_arrow = re.search(
                    r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>",
                    line,
                )
                if m_arrow:
                    symbols.append(
                        {
                            "name": m_arrow.group(1),
                            "kind": "function",
                            "file": rel_path,
                            "line": idx,
                            "end_line": min(idx + 15, line_count),
                            "docstring": "",
                            "signature": f"{m_arrow.group(1)}({m_arrow.group(2)})",
                            "calls": [],
                            "parent": None,
                        }
                    )
                # Classes / Interfaces
                m_cls = re.search(r"(?:export\s+)?(class|interface)\s+([A-Za-z_$][A-Za-z0-9_$]*)", line)
                if m_cls:
                    symbols.append(
                        {
                            "name": m_cls.group(2),
                            "kind": m_cls.group(1),
                            "file": rel_path,
                            "line": idx,
                            "end_line": min(idx + 25, line_count),
                            "docstring": "",
                            "signature": f"{m_cls.group(1)} {m_cls.group(2)}",
                            "calls": [],
                            "parent": None,
                        }
                    )
                # Express / REST routes
                m_route = re.search(
                    r"""\b(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]""",
                    line,
                )
                if m_route:
                    apis.append(
                        {
                            "method": m_route.group(1).upper(),
                            "path": m_route.group(2),
                            "handler": f"{rel_path}:{idx}",
                            "file": rel_path,
                            "line": idx,
                            "docstring": "",
                        }
                    )
            elif ext == ".go":
                m_fn = re.match(r"^\s*func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", line)
                if m_fn:
                    symbols.append(
                        {
                            "name": m_fn.group(1),
                            "kind": "function",
                            "file": rel_path,
                            "line": idx,
                            "end_line": min(idx + 15, line_count),
                            "docstring": "",
                            "signature": f"func {m_fn.group(1)}({m_fn.group(2)})",
                            "calls": [],
                            "parent": None,
                        }
                    )
                m_type = re.match(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(struct|interface)", line)
                if m_type:
                    symbols.append(
                        {
                            "name": m_type.group(1),
                            "kind": m_type.group(2),
                            "file": rel_path,
                            "line": idx,
                            "end_line": min(idx + 15, line_count),
                            "docstring": "",
                            "signature": f"type {m_type.group(1)} {m_type.group(2)}",
                            "calls": [],
                            "parent": None,
                        }
                    )
            elif ext == ".rs":
                m_fn = re.match(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", line)
                if m_fn:
                    symbols.append(
                        {
                            "name": m_fn.group(1),
                            "kind": "function",
                            "file": rel_path,
                            "line": idx,
                            "end_line": min(idx + 15, line_count),
                            "docstring": "",
                            "signature": f"fn {m_fn.group(1)}({m_fn.group(2)})",
                            "calls": [],
                            "parent": None,
                        }
                    )
                m_st = re.match(r"^\s*(?:pub\s+)?(struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
                if m_st:
                    symbols.append(
                        {
                            "name": m_st.group(2),
                            "kind": m_st.group(1),
                            "file": rel_path,
                            "line": idx,
                            "end_line": min(idx + 15, line_count),
                            "docstring": "",
                            "signature": f"{m_st.group(1)} {m_st.group(2)}",
                            "calls": [],
                            "parent": None,
                        }
                    )

        file_info = {
            "language": lang,
            "lines": line_count,
            "docstring": "",
            "imports": sorted(set(imports)),
            "calls": sorted(calls),
            "symbols": symbols,
            "apis": apis,
        }
        return file_info, symbols, apis

    def search_symbols(self, query: str, top_k: int = 20) -> List[Dict[str, Any]]:
        """Search indexed symbols by name, signature, or docstring."""
        index = self._symbol_cache or self.build_symbol_index()
        q_low = query.strip().lower()
        if not q_low:
            return index["symbols"][:top_k]

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for sym in index["symbols"]:
            name_low = sym["name"].lower()
            doc_low = (sym.get("docstring") or "").lower()
            file_low = sym["file"].lower()
            score = 0.0
            if name_low == q_low:
                score += 10.0
            elif q_low in name_low:
                score += 6.0
            if q_low in doc_low:
                score += 3.0
            if q_low in file_low:
                score += 2.0
            if score > 0:
                scored.append((score, sym))

        scored.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line"]))
        return [item[1] for item in scored[:top_k]]

    # ------------------------------------------------------------------
    # 3. Architecture Map & Mermaid Diagram Generation
    # ------------------------------------------------------------------

    def generate_architecture_map(self) -> Dict[str, Any]:
        """Generate an architecture summary, module layer breakdown, dependency graph edges, and Mermaid diagram."""
        scan = self._scan_cache or self.scan_project()
        index = self._symbol_cache or self.build_symbol_index()

        layers: Dict[str, List[str]] = {
            "presentation_ui": [],
            "api_server": [],
            "orchestration_engine": [],
            "intelligence_memory": [],
            "security_tools_git": [],
            "providers_config": [],
            "core_domain": [],
            "tests": [],
        }

        module_summaries: List[Dict[str, Any]] = []
        edges: List[Dict[str, str]] = []
        seen_edges: Set[Tuple[str, str]] = set()

        # Map module names to relative file paths for internal edge resolution
        mod_to_file: Dict[str, str] = {}
        for rel_file in index["files"]:
            if rel_file.endswith(".py"):
                mod_key = rel_file[:-3].replace("/", ".")
                mod_to_file[mod_key] = rel_file
                parts = mod_key.split(".")
                if parts[-1] == "__init__" and len(parts) > 1:
                    mod_to_file[".".join(parts[:-1])] = rel_file
                mod_to_file[parts[-1]] = rel_file
            else:
                mod_to_file[Path(rel_file).stem] = rel_file

        for rel_file, fdata in index["files"].items():
            low_rel = rel_file.lower()
            if any(k in low_rel for k in ("frontend", "ui", "templates", "static", ".html", ".css")):
                layers["presentation_ui"].append(rel_file)
            elif any(k in low_rel for k in ("server", "api", "routes", "views", "controllers")):
                layers["api_server"].append(rel_file)
            elif any(k in low_rel for k in ("engine", "workflow", "orchestrat", "multi_agent", "debugger")):
                layers["orchestration_engine"].append(rel_file)
            elif any(k in low_rel for k in ("intelligence", "memory", "advanced", "indexer", "store", "capabilities")):
                layers["intelligence_memory"].append(rel_file)
            elif any(k in low_rel for k in ("security", "tools", "git", "policy", "filesystem", "terminal")):
                layers["security_tools_git"].append(rel_file)
            elif any(k in low_rel for k in ("provider", "config", "settings", "vault", "adapter")):
                layers["providers_config"].append(rel_file)
            elif any(k in low_rel for k in ("test", "tests", "spec")):
                layers["tests"].append(rel_file)
            else:
                layers["core_domain"].append(rel_file)

            module_summaries.append(
                {
                    "file": rel_file,
                    "language": fdata["language"],
                    "lines": fdata["lines"],
                    "symbol_count": len(fdata["symbols"]),
                    "docstring": (fdata.get("docstring") or "").splitlines()[0] if fdata.get("docstring") else "",
                }
            )

            # Resolve internal dependency edges
            src_group = self._module_group_label(rel_file)
            for imp in fdata.get("imports", []):
                target_file = mod_to_file.get(imp)
                if not target_file:
                    # Check prefix match
                    for cand_mod, cand_file in mod_to_file.items():
                        if imp == cand_mod or imp.startswith(f"{cand_mod}."):
                            target_file = cand_file
                            break
                if target_file and target_file != rel_file:
                    tgt_group = self._module_group_label(target_file)
                    edge_pair = (src_group, tgt_group)
                    if src_group != tgt_group and edge_pair not in seen_edges:
                        seen_edges.add(edge_pair)
                        edges.append(
                            {
                                "source": src_group,
                                "target": tgt_group,
                                "source_file": rel_file,
                                "target_file": target_file,
                                "type": "import",
                            }
                        )

        # Build valid Mermaid flowchart TD diagram
        mermaid_lines = ["flowchart TD"]
        node_ids: Dict[str, str] = {}

        def _get_node_id(label: str) -> str:
            if label not in node_ids:
                nid = f"M{len(node_ids) + 1}"
                safe_label = label.replace('"', "'")
                node_ids[label] = nid
                mermaid_lines.append(f'    {nid}["{safe_label}"]')
            return node_ids[label]

        if edges:
            for edge in edges[:40]:
                s_id = _get_node_id(edge["source"])
                t_id = _get_node_id(edge["target"])
                mermaid_lines.append(f"    {s_id} --> {t_id}")
        else:
            # Ensure valid diagram even on single-file or empty workspaces
            active_layers = [k for k, v in layers.items() if v]
            if len(active_layers) >= 2:
                for i in range(len(active_layers) - 1):
                    s_id = _get_node_id(active_layers[i])
                    t_id = _get_node_id(active_layers[i + 1])
                    mermaid_lines.append(f"    {s_id} --> {t_id}")
            else:
                root_id = _get_node_id(self.workspace_root.name or "Workspace")
                core_id = _get_node_id("ApplicationCore")
                mermaid_lines.append(f"    {root_id} --> {core_id}")

        mermaid_str = "\n".join(mermaid_lines)
        non_empty_layers = {k: v for k, v in layers.items() if v}
        summary = (
            f"Workspace '{self.workspace_root.name}' contains {scan['total_files']} files "
            f"({scan['total_lines']} lines) across {len(non_empty_layers)} architectural layers "
            f"with {index['total_symbols']} indexed symbols and {len(edges)} cross-module dependency edges."
        )

        return {
            "workspace_root": str(self.workspace_root),
            "summary": summary,
            "layers": non_empty_layers,
            "modules": module_summaries,
            "edges": edges,
            "dependency_graph": edges,
            "mermaid": mermaid_str,
            "mermaid_diagram": mermaid_str,
            "diagram": mermaid_str,
        }

    def _module_group_label(self, rel_file: str) -> str:
        parts = rel_file.replace("\\", "/").split("/")
        if len(parts) >= 3 and parts[0] == "bel_code":
            return f"bel_code.{parts[1]}"
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}" if len(parts) > 2 else parts[0]
        return Path(rel_file).stem

    # ------------------------------------------------------------------
    # 4. Context Retrieval (BM25 / TF-IDF + AST Symbol Matching)
    # ------------------------------------------------------------------

    def retrieve_context(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Retrieve the most relevant files and AST symbol snippets for a natural-language query."""
        index = self._symbol_cache or self.build_symbol_index()
        files = self._iter_workspace_files()
        if not files:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            query_tokens = [query.strip().lower()] if query.strip() else ["main"]

        # Build document corpus for BM25
        docs: List[Dict[str, Any]] = []
        df: Counter[str] = Counter()
        total_doc_len = 0

        for file_path in files:
            rel = file_path.relative_to(self.workspace_root).as_posix()
            ext = file_path.suffix.lower()
            try:
                if file_path.stat().st_size > 512_000:
                    continue
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            file_meta = index["files"].get(rel, {})
            symbols = file_meta.get("symbols", [])
            symbol_names = [s["name"] for s in symbols]
            docstrings = " ".join(s.get("docstring", "") for s in symbols) + " " + file_meta.get("docstring", "")

            tokens = self._tokenize(f"{rel} {' '.join(symbol_names)} {docstrings} {content}")
            tf = Counter(tokens)
            for term in set(tokens):
                df[term] += 1
            total_doc_len += len(tokens)

            docs.append(
                {
                    "file": rel,
                    "path": file_path,
                    "language": EXTENSION_TO_LANGUAGE.get(ext, "Text"),
                    "content": content,
                    "lines": content.splitlines(),
                    "tokens": tokens,
                    "tf": tf,
                    "doc_len": max(len(tokens), 1),
                    "symbols": symbols,
                }
            )

        if not docs:
            return []

        n_docs = len(docs)
        avg_dl = max(total_doc_len / n_docs, 1.0)
        k1 = 1.5
        b = 0.75

        scored_results: List[Dict[str, Any]] = []
        for doc in docs:
            bm25_score = 0.0
            matched_terms: List[str] = []

            for q_term in query_tokens:
                f_q = doc["tf"].get(q_term, 0)
                if f_q > 0:
                    matched_terms.append(q_term)
                    n_q = df.get(q_term, 0)
                    idf = math.log(1.0 + (n_docs - n_q + 0.5) / (n_q + 0.5))
                    denom = f_q + k1 * (1.0 - b + b * (doc["doc_len"] / avg_dl))
                    bm25_score += idf * ((f_q * (k1 + 1.0)) / denom)

            # Boost for symbol name, docstring, and path matches
            matched_symbols: List[Dict[str, Any]] = []
            symbol_boost = 0.0
            for sym in doc["symbols"]:
                sym_name_low = sym["name"].lower()
                sym_tokens = set(self._tokenize(sym["name"] + " " + sym.get("docstring", "")))
                overlap = [t for t in query_tokens if t in sym_tokens or t in sym_name_low]
                if overlap:
                    symbol_boost += 2.5 * len(overlap)
                    matched_symbols.append(
                        {
                            "name": sym["name"],
                            "kind": sym["kind"],
                            "line": sym["line"],
                            "end_line": sym.get("end_line", sym["line"]),
                            "signature": sym.get("signature", sym["name"]),
                            "docstring": sym.get("docstring", ""),
                        }
                    )

            path_tokens = set(self._tokenize(doc["file"]))
            path_overlap = [t for t in query_tokens if t in path_tokens]
            path_boost = 2.0 * len(path_overlap)

            total_score = round(bm25_score + symbol_boost + path_boost, 4)
            if total_score <= 0 and len(docs) > top_k:
                continue

            # Extract targeted snippet around best matching symbol or matching line
            start_line, end_line, snippet = self._extract_best_snippet(
                doc["lines"], matched_symbols, query_tokens
            )

            reasons: List[str] = []
            if matched_symbols:
                reasons.append(f"Matched symbols: {', '.join(s['name'] for s in matched_symbols[:4])}")
            if path_overlap:
                reasons.append(f"Filename match: {', '.join(path_overlap)}")
            if matched_terms:
                reasons.append(f"BM25 terms: {', '.join(sorted(set(matched_terms))[:6])}")

            scored_results.append(
                {
                    "file": doc["file"],
                    "language": doc["language"],
                    "score": max(total_score, 0.01),
                    "matched_symbols": matched_symbols,
                    "snippet": snippet,
                    "start_line": start_line,
                    "end_line": end_line,
                    "reason": "; ".join(reasons) if reasons else "Workspace fallback context",
                }
            )

        scored_results.sort(key=lambda item: (-item["score"], item["file"]))
        return scored_results[: max(top_k, 1)]

    def _tokenize(self, text: str) -> List[str]:
        # Split camelCase and snake_case into lowercase tokens
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
        raw_tokens = re.findall(r"[A-Za-z0-9_]{2,}", expanded.lower())
        result: List[str] = []
        for tok in raw_tokens:
            result.append(tok)
            if "_" in tok:
                for sub in tok.split("_"):
                    if len(sub) >= 2:
                        result.append(sub)
        return result

    def _extract_best_snippet(
        self,
        lines: List[str],
        matched_symbols: List[Dict[str, Any]],
        query_tokens: List[str],
    ) -> Tuple[int, int, str]:
        if not lines:
            return 1, 1, ""

        if matched_symbols:
            top_sym = matched_symbols[0]
            s_line = max(1, top_sym["line"] - 2)
            e_line = min(len(lines), max(top_sym.get("end_line", s_line + 20), s_line + 15))
            e_line = min(e_line, s_line + 45)
            snippet = "\n".join(lines[s_line - 1 : e_line])
            return s_line, e_line, snippet

        # Find line with highest query token overlap
        best_idx = 0
        best_hits = 0
        for idx, line in enumerate(lines):
            low = line.lower()
            hits = sum(1 for tok in query_tokens if tok in low)
            if hits > best_hits:
                best_hits = hits
                best_idx = idx

        s_line = max(1, best_idx - 3)
        e_line = min(len(lines), s_line + 30)
        snippet = "\n".join(lines[s_line - 1 : e_line])
        return s_line, e_line, snippet

    # ------------------------------------------------------------------
    # 5. Code Health Analysis
    # ------------------------------------------------------------------

    def analyze_code_health(self) -> Dict[str, Any]:
        """Detect duplicated code blocks, dead/unused functions, circular imports, dependency risks, and architectural issues."""
        scan = self._scan_cache or self.scan_project()
        index = self._symbol_cache or self.build_symbol_index()

        duplicated_blocks = self._detect_duplicates()
        dead_code = self._detect_dead_functions(index)
        circular_imports = self._detect_circular_imports(index)
        dependency_risks = self._detect_dependency_risks()
        architectural_inconsistencies = self._detect_architectural_inconsistencies(scan, index)

        penalty = (
            len(duplicated_blocks) * 5
            + len(dead_code) * 3
            + len(circular_imports) * 10
            + len(dependency_risks) * 4
            + len(architectural_inconsistencies) * 4
        )
        health_score = float(max(0, min(100, 100 - penalty)))

        return {
            "workspace_root": str(self.workspace_root),
            "health_score": health_score,
            "duplicated_blocks": duplicated_blocks,
            "duplicates": duplicated_blocks,
            "dead_code": dead_code,
            "unused_functions": dead_code,
            "circular_imports": circular_imports,
            "dependency_risks": dependency_risks,
            "architectural_inconsistencies": architectural_inconsistencies,
            "summary": (
                f"Code health score: {health_score}/100 "
                f"({len(duplicated_blocks)} duplicate blocks, {len(dead_code)} unused symbols, "
                f"{len(circular_imports)} circular imports, {len(dependency_risks)} dependency risks)."
            ),
        }

    def _detect_duplicates(self, window_size: int = 3) -> List[Dict[str, Any]]:
        files = self._iter_workspace_files()
        block_locations: Dict[str, List[Tuple[str, int, str]]] = defaultdict(list)

        for file_path in files:
            if file_path.suffix.lower() not in (".py", ".js", ".ts", ".go", ".rs"):
                continue
            rel = file_path.relative_to(self.workspace_root).as_posix()
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
                raw_lines = source.splitlines()
            except OSError:
                continue

            # 1. AST function body duplicate detection for Python files
            if file_path.suffix.lower() == ".py":
                try:
                    tree = ast.parse(source, filename=rel)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                            body_stmts = [
                                s
                                for s in node.body
                                if not (
                                    isinstance(s, ast.Expr)
                                    and isinstance(getattr(s, "value", None), ast.Constant)
                                    and isinstance(getattr(s.value, "value", None), str)
                                )
                            ]
                            if not body_stmts:
                                continue
                            norm_dump = "|".join(ast.dump(s, annotate_fields=True, include_attributes=False) for s in body_stmts)
                            if len(norm_dump) >= 35:
                                preview = f"def {node.name}(...)"
                                block_locations[f"ast:{norm_dump}"].append((rel, node.lineno, preview))
                except SyntaxError:
                    pass

            # 2. Sliding-window normalized line duplicate detection
            meaningful: List[Tuple[int, str, str]] = []
            for lineno, raw in enumerate(raw_lines, start=1):
                stripped = raw.strip()
                if (
                    not stripped
                    or stripped.startswith("#")
                    or stripped.startswith("//")
                    or stripped.startswith("import ")
                    or stripped.startswith("from ")
                    or stripped.startswith("def ")
                    or stripped.startswith("async def ")
                    or stripped.startswith("class ")
                    or stripped in ("pass", "{", "}", "};", ")", "]):")
                ):
                    continue
                norm = re.sub(r"\s+", " ", stripped)
                if len(norm) >= 4:
                    meaningful.append((lineno, norm, stripped))

            if len(meaningful) < window_size:
                continue

            seen_in_same_window: Set[str] = set()
            for i in range(len(meaningful) - window_size + 1):
                chunk = meaningful[i : i + window_size]
                signature = "\n".join(item[1] for item in chunk)
                loc_key = f"{rel}:{chunk[0][0] // window_size}:{signature}"
                if loc_key in seen_in_same_window:
                    continue
                seen_in_same_window.add(loc_key)
                preview = "\n".join(item[2] for item in chunk[:3])
                block_locations[signature].append((rel, chunk[0][0], preview))

        duplicates: List[Dict[str, Any]] = []
        for sig, locs in block_locations.items():
            if len(locs) >= 2:
                duplicates.append(
                    {
                        "occurrences": len(locs),
                        "files": [loc[0] for loc in locs],
                        "locations": [{"file": loc[0], "line": loc[1]} for loc in locs],
                        "line_count": window_size,
                        "snippet_preview": locs[0][2],
                    }
                )
        return duplicates[:25]

    def _detect_dead_functions(self, index: Dict[str, Any]) -> List[Dict[str, Any]]:
        all_calls: Set[str] = set()
        all_source_texts: List[str] = []

        for rel_file, fdata in index["files"].items():
            all_calls.update(fdata.get("calls", []))
            full_path = self.workspace_root / rel_file
            try:
                all_source_texts.append(full_path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass

        combined_corpus = "\n".join(all_source_texts)
        api_handlers = {a["handler"].split(".")[-1] for a in index.get("apis", [])}

        dead: List[Dict[str, Any]] = []
        for sym in index["symbols"]:
            if sym["kind"] not in ("function", "async_function"):
                continue
            name = sym["name"]
            rel_file = sym["file"]
            # Skip special methods, tests, entrypoints, route handlers, and private helpers starting with __
            if (
                name.startswith("test_")
                or name.endswith("_test")
                or (name.startswith("__") and name.endswith("__"))
                or name in ("main", "cli", "app", "setup", "teardown")
                or name in api_handlers
                or "tests/" in rel_file
                or rel_file.startswith("test_")
            ):
                continue

            # Check if called anywhere or referenced more than once across the corpus
            if name not in all_calls:
                occurrences = len(re.findall(rf"\b{re.escape(name)}\b", combined_corpus))
                if occurrences <= 1:
                    dead.append(
                        {
                            "name": name,
                            "kind": sym["kind"],
                            "file": rel_file,
                            "line": sym["line"],
                            "reason": f"Function '{name}' is defined at {rel_file}:{sym['line']} but never called or referenced.",
                        }
                    )
        return dead

    def _detect_circular_imports(self, index: Dict[str, Any]) -> List[List[str]]:
        mod_to_file: Dict[str, str] = {}
        for rel_file in index["files"]:
            if rel_file.endswith(".py"):
                mod_name = rel_file[:-3].replace("/", ".")
                mod_to_file[mod_name] = rel_file
                mod_to_file[Path(rel_file).stem] = rel_file

        adj: Dict[str, Set[str]] = defaultdict(set)
        for rel_file, fdata in index["files"].items():
            for imp in fdata.get("imports", []):
                tgt = mod_to_file.get(imp)
                if not tgt and "." in imp:
                    tgt = mod_to_file.get(imp.split(".")[-1])
                if tgt and tgt != rel_file:
                    adj[rel_file].add(tgt)

        cycles: List[List[str]] = []
        visited: Set[str] = set()
        stack: List[str] = []
        on_stack: Set[str] = set()

        def _dfs(node: str) -> None:
            visited.add(node)
            stack.append(node)
            on_stack.add(node)
            for nbr in adj.get(node, set()):
                if nbr not in visited:
                    _dfs(nbr)
                elif nbr in on_stack:
                    idx = stack.index(nbr)
                    cycle = stack[idx:] + [nbr]
                    if cycle not in cycles:
                        cycles.append(cycle)
            stack.pop()
            on_stack.remove(node)

        for f in list(adj.keys()):
            if f not in visited:
                _dfs(f)

        return cycles

    def _detect_dependency_risks(self) -> List[Dict[str, Any]]:
        risks: List[Dict[str, Any]] = []
        req_path = self.workspace_root / "requirements.txt"
        if req_path.is_file():
            try:
                for lineno, raw in enumerate(req_path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                    line = raw.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    if not any(op in line for op in ("==", ">=", "~=", "<=")):
                        risks.append(
                            {
                                "file": "requirements.txt",
                                "line": lineno,
                                "dependency": line,
                                "risk": "unpinned_version",
                                "description": f"Dependency '{line}' has no version constraint.",
                            }
                        )
                    elif "*" in line:
                        risks.append(
                            {
                                "file": "requirements.txt",
                                "line": lineno,
                                "dependency": line,
                                "risk": "wildcard_version",
                                "description": f"Dependency '{line}' uses wildcard version matching.",
                            }
                        )
            except OSError:
                pass
        return risks

    def _detect_architectural_inconsistencies(
        self, scan: Dict[str, Any], index: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        for rel_file, fdata in index["files"].items():
            if fdata["lines"] > 1200:
                issues.append(
                    {
                        "file": rel_file,
                        "type": "large_module",
                        "description": f"Module '{rel_file}' has {fdata['lines']} lines; consider decomposing.",
                    }
                )
            # Check low-level config importing UI/server layer
            if "config" in rel_file.lower():
                for imp in fdata.get("imports", []):
                    if any(u in imp.lower() for u in ("frontend", "server")):
                        issues.append(
                            {
                                "file": rel_file,
                                "type": "layer_violation",
                                "description": f"Low-level config module '{rel_file}' imports high-level '{imp}'.",
                            }
                        )
        return issues
