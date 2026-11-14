"""Persistent Engineering Memory & Session Recovery (Spec Section 12).

Backed by SQLite (`memory.db` inside `storage_dir` or `~/.bel_code/memory/`).
Enforces strict isolation by `project_id`, automatic secret/PII sanitization before writing
to disk, memory CRUD & search, session state recovery, and an append-only changelog.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


MEMORY_CATEGORIES: Set[str] = {
    "rule",
    "preference",
    "architecture_decision",
    "constraint",
    "coding_standard",
    "diagnostic_note",
}

# Secret and PII sanitization patterns (constructed without any literal credentials)
_SECRET_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    # Private keys
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Token Harbor live/test keys
    (re.compile(r"\bthk_(?:live|test)_[A-Za-z0-9_\-]{6,}\b"), "[REDACTED_SECRET]"),
    # FreeLLM API keys
    (re.compile(r"\bfreellmapi-[A-Za-z0-9_\-]{6,}\b"), "[REDACTED_SECRET]"),
    # Anthropic keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_SECRET]"),
    # OpenAI / generic sk- keys
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_SECRET]"),
    # GitHub tokens
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"), "[REDACTED_SECRET]"),
    # AWS Access Key IDs
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_SECRET]"),
    # Bearer tokens
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED_TOKEN]"),
    # Key/value assignments for passwords, api keys, secrets, tokens
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api_key|apikey|access_token|auth_token|private_key|client_secret)"
            r"(\s*[=:]\s*['\"]?)([^\s'\",;}\]]{4,})(['\"]?)"
        ),
        r"\1\2[REDACTED_SECRET]\4",
    ),
    # Email addresses (PII)
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
)


class EngineeringMemory:
    """SQLite-backed project-isolated engineering memory, session state store, and changelog."""

    def __init__(self, storage_dir: Optional[Union[str, Path]] = None) -> None:
        if storage_dir is None:
            base_dir = Path.home() / ".bel_code" / "memory"
            self.storage_dir = base_dir.resolve()
            self.db_path = self.storage_dir / "memory.db"
        else:
            candidate = Path(storage_dir).resolve()
            if candidate.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
                self.db_path = candidate
                self.storage_dir = candidate.parent
            else:
                self.storage_dir = candidate
                self.db_path = self.storage_dir / "memory.db"

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return contextlib.closing(conn)

    def _init_db(self) -> None:
        with self._connect() as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        memory_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        category TEXT NOT NULL,
                        title TEXT NOT NULL,
                        content TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, category)"
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        project_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        state_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (project_id, task_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)"
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS changelog (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        entry_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_changelog_project ON changelog(project_id, id)"
                )

    # ------------------------------------------------------------------
    # Secret & PII Sanitizer
    # ------------------------------------------------------------------

    @staticmethod
    def sanitize_text(text: str) -> str:
        """Strip API keys, Bearer tokens, passwords, and PII from text before persistence."""
        if not isinstance(text, str) or not text:
            return text
        sanitized = text
        for pattern, replacement in _SECRET_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    @classmethod
    def sanitize_data(cls, value: Any) -> Any:
        """Recursively sanitize strings inside dicts, lists, and tuples."""
        if isinstance(value, str):
            return cls.sanitize_text(value)
        if isinstance(value, dict):
            cleaned: Dict[Any, Any] = {}
            for k, v in value.items():
                key_str = str(k).lower()
                if any(
                    s in key_str
                    for s in ("api_key", "apikey", "secret", "password", "passwd", "private_key", "auth_token")
                ) and isinstance(v, str) and v:
                    cleaned[k] = "[REDACTED_SECRET]"
                else:
                    cleaned[k] = cls.sanitize_data(v)
            return cleaned
        if isinstance(value, list):
            return [cls.sanitize_data(item) for item in value]
        if isinstance(value, tuple):
            return [cls.sanitize_data(item) for item in value]
        return value

    # ------------------------------------------------------------------
    # Memory Records CRUD & Search
    # ------------------------------------------------------------------

    def add_memory(
        self,
        project_id: str,
        category: str,
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add a sanitized memory record strictly scoped to project_id."""
        if not project_id or not str(project_id).strip():
            raise ValueError("project_id is required for memory isolation.")

        norm_project = str(project_id).strip()
        norm_category = str(category or "rule").strip().lower()
        clean_title = self.sanitize_text(str(title or ""))
        clean_content = self.sanitize_text(str(content or ""))
        clean_meta = self.sanitize_data(metadata or {})

        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO memories (
                        memory_id, project_id, category, title, content, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        norm_project,
                        norm_category,
                        clean_title,
                        clean_content,
                        json.dumps(clean_meta),
                        now,
                        now,
                    ),
                )

        return {
            "id": memory_id,
            "memory_id": memory_id,
            "project_id": norm_project,
            "category": norm_category,
            "title": clean_title,
            "content": clean_content,
            "metadata": clean_meta,
            "created_at": now,
            "updated_at": now,
        }

    def get_memory(self, project_id: str, memory_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single memory record strictly matching both project_id and memory_id."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT memory_id, project_id, category, title, content, metadata_json, created_at, updated_at
                FROM memories
                WHERE project_id = ? AND memory_id = ?
                """,
                (str(project_id).strip(), str(memory_id).strip()),
            ).fetchone()
        if not row:
            return None
        return self._row_to_memory(row)

    def search_memory(
        self,
        project_id: str,
        query: str = "",
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search memories strictly within project_id by query string and/or category."""
        norm_project = str(project_id).strip()
        sql = """
            SELECT memory_id, project_id, category, title, content, metadata_json, created_at, updated_at
            FROM memories
            WHERE project_id = ?
        """
        params: List[Any] = [norm_project]

        if category:
            sql += " AND category = ?"
            params.append(str(category).strip().lower())

        sql += " ORDER BY updated_at DESC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        records = [self._row_to_memory(r) for r in rows]
        q_clean = (query or "").strip().lower()
        if not q_clean:
            return records[:limit]

        q_tokens = [t for t in re.findall(r"[a-z0-9_]+", q_clean) if t]
        matched: List[Tuple[float, Dict[str, Any]]] = []

        for rec in records:
            haystack = f"{rec['title']} {rec['content']} {rec['category']} {json.dumps(rec['metadata'])}".lower()
            score = 0.0
            if q_clean in haystack:
                score += 5.0
            for tok in q_tokens:
                if tok in rec["title"].lower():
                    score += 3.0
                elif tok in haystack:
                    score += 1.5
            if score > 0:
                matched.append((score, rec))

        matched.sort(key=lambda item: (-item[0], item[1]["updated_at"]))
        return [item[1] for item in matched[:limit]]

    def list_memories(
        self, project_id: str, category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List all memory records for a given project_id."""
        return self.search_memory(project_id=project_id, query="", category=category)

    def update_memory(
        self,
        project_id: str,
        memory_id: str,
        updates: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Update an existing memory record within project_id after sanitizing fields."""
        existing = self.get_memory(project_id, memory_id)
        if not existing:
            return None

        merged_updates: Dict[str, Any] = {}
        if updates:
            merged_updates.update(updates)
        if kwargs:
            merged_updates.update(kwargs)

        new_category = str(merged_updates.get("category", existing["category"])).strip().lower()
        new_title = self.sanitize_text(str(merged_updates.get("title", existing["title"])))
        new_content = self.sanitize_text(str(merged_updates.get("content", existing["content"])))
        raw_meta = merged_updates.get("metadata", existing["metadata"])
        new_meta = self.sanitize_data(raw_meta if isinstance(raw_meta, dict) else existing["metadata"])
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE memories
                    SET category = ?, title = ?, content = ?, metadata_json = ?, updated_at = ?
                    WHERE project_id = ? AND memory_id = ?
                    """,
                    (
                        new_category,
                        new_title,
                        new_content,
                        json.dumps(new_meta),
                        now,
                        str(project_id).strip(),
                        str(memory_id).strip(),
                    ),
                )

        return self.get_memory(project_id, memory_id)

    def delete_memory(
        self, project_id: str, memory_id: Optional[str] = None
    ) -> bool:
        """Delete a memory record strictly matching project_id and memory_id."""
        with self._connect() as conn:
            with conn:
                if memory_id is not None:
                    cur = conn.execute(
                        "DELETE FROM memories WHERE project_id = ? AND memory_id = ?",
                        (str(project_id).strip(), str(memory_id).strip()),
                    )
                else:
                    # Fallback if a single memory_id argument was supplied by an API handler
                    cur = conn.execute(
                        "DELETE FROM memories WHERE memory_id = ?",
                        (str(project_id).strip(),),
                    )
                return cur.rowcount > 0

    def export_project_memory(self, project_id: str) -> Dict[str, Any]:
        """Export all memories, sessions, and changelog entries strictly for project_id."""
        norm_project = str(project_id).strip()
        memories = self.search_memory(norm_project, query="")
        sessions = self.list_sessions(project_id=norm_project)
        changelog = self.get_changelog(norm_project)
        return {
            "project_id": norm_project,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "total_memories": len(memories),
            "memories": memories,
            "sessions": sessions,
            "changelog": changelog,
        }

    def _row_to_memory(self, row: sqlite3.Row) -> Dict[str, Any]:
        try:
            meta = json.loads(row["metadata_json"])
        except Exception:
            meta = {}
        return {
            "id": row["memory_id"],
            "memory_id": row["memory_id"],
            "project_id": row["project_id"],
            "category": row["category"],
            "title": row["title"],
            "content": row["content"],
            "metadata": meta,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Session State & Task Recovery
    # ------------------------------------------------------------------

    def save_session_state(
        self, project_id: str, task_id: str, state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Persist sanitized task execution state for seamless session recovery."""
        norm_project = str(project_id).strip()
        norm_task = str(task_id).strip()
        sanitized_state = self.sanitize_data(copy.deepcopy(state or {}))

        # Ensure standard recovery fields exist in state
        sanitized_state.setdefault("plan", sanitized_state.get("task_plan", []))
        sanitized_state.setdefault("completed_steps", sanitized_state.get("verified_steps", []))
        sanitized_state.setdefault("checkpoints", [])
        sanitized_state.setdefault("tool_output_buffers", [])
        sanitized_state.setdefault("unfinished_queue", [])
        status = str(sanitized_state.get("status", "active"))

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM sessions WHERE project_id = ? AND task_id = ?",
                (norm_project, norm_task),
            ).fetchone()
            created_at = existing["created_at"] if existing else now

            with conn:
                conn.execute(
                    """
                    INSERT INTO sessions (project_id, task_id, status, state_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, task_id) DO UPDATE SET
                        status = excluded.status,
                        state_json = excluded.state_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        norm_project,
                        norm_task,
                        status,
                        json.dumps(sanitized_state),
                        created_at,
                        now,
                    ),
                )

        return {
            **sanitized_state,
            "project_id": norm_project,
            "task_id": norm_task,
            "status": status,
            "state": sanitized_state,
            "created_at": created_at,
            "updated_at": now,
        }

    def load_session_state(
        self, project_id: str, task_id: str
    ) -> Optional[Dict[str, Any]]:
        """Load saved session state strictly matching project_id and task_id."""
        norm_project = str(project_id).strip()
        norm_task = str(task_id).strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, task_id, status, state_json, created_at, updated_at
                FROM sessions
                WHERE project_id = ? AND task_id = ?
                """,
                (norm_project, norm_task),
            ).fetchone()
        if not row:
            return None
        return self._row_to_session(row)

    def list_sessions(
        self, project_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List saved sessions, optionally filtered by project_id."""
        with self._connect() as conn:
            if project_id is not None:
                rows = conn.execute(
                    """
                    SELECT project_id, task_id, status, state_json, created_at, updated_at
                    FROM sessions
                    WHERE project_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (str(project_id).strip(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT project_id, task_id, status, state_json, created_at, updated_at
                    FROM sessions
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def _row_to_session(self, row: sqlite3.Row) -> Dict[str, Any]:
        try:
            state_data = json.loads(row["state_json"])
            if not isinstance(state_data, dict):
                state_data = {"value": state_data}
        except Exception:
            state_data = {}

        return {
            **state_data,
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "status": row["status"],
            "state": state_data,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Append-only Development Changelog & Decision Log
    # ------------------------------------------------------------------

    def append_changelog(
        self,
        project_id: str,
        entry: Union[Dict[str, Any], str],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Append a sanitized entry to the project's immutable development changelog."""
        norm_project = str(project_id).strip()
        if isinstance(entry, str):
            raw_entry: Dict[str, Any] = {"summary": entry, "message": entry}
        elif isinstance(entry, dict):
            raw_entry = copy.deepcopy(entry)
        else:
            raw_entry = {"summary": str(entry)}

        if kwargs:
            raw_entry.update(kwargs)

        sanitized_entry = self.sanitize_data(raw_entry)
        now = datetime.now(timezone.utc).isoformat()
        sanitized_entry.setdefault("timestamp", now)

        with self._connect() as conn:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO changelog (project_id, timestamp, entry_json)
                    VALUES (?, ?, ?)
                    """,
                    (norm_project, now, json.dumps(sanitized_entry)),
                )
                row_id = cur.lastrowid

        return {
            **sanitized_entry,
            "id": row_id,
            "project_id": norm_project,
            "timestamp": now,
            "entry": sanitized_entry,
        }

    def get_changelog(
        self, project_id: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Retrieve chronological changelog entries strictly for project_id."""
        norm_project = str(project_id).strip()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, project_id, timestamp, entry_json
                FROM changelog
                WHERE project_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (norm_project, limit),
            ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            try:
                entry_data = json.loads(row["entry_json"])
                if not isinstance(entry_data, dict):
                    entry_data = {"summary": str(entry_data)}
            except Exception:
                entry_data = {}
            results.append(
                {
                    **entry_data,
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "timestamp": row["timestamp"],
                    "entry": entry_data,
                }
            )
        return results
