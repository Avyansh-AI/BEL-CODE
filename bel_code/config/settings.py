"""
BEL Code Configuration, Security Mode, Budget Controls, and Secret Vault.

Standalone configuration management for BEL Code v2.1.
Provides:
- SecurityMode: ask, supervised, autonomous
- BudgetConfig & BudgetExceededError: per-task, per-project, and global spend caps with hard-stop triggers
- SecretVault: OS keyring (when available) + PBKDF2-HMAC-SHA256 authenticated encrypted local vault (~/.bel_code/vault.enc)
  with multi-account labels, key rotation, and automatic secret redaction
- SettingsManager: persistent non-secret settings (~/.bel_code/config.json) integrated with SecretVault and BudgetConfig
"""

from __future__ import annotations

import base64
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import secrets
from typing import Any, Dict, List, Optional, Union

try:
    import keyring as _keyring  # type: ignore
    HAS_KEYRING = True
except Exception:
    _keyring = None
    HAS_KEYRING = False


class SecurityMode(str, Enum):
    """Operating security modes for BEL Code (Spec Section 15)."""
    ASK = "ask"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class BudgetExceededError(RuntimeError):
    """Raised when a task, project, or global spending limit is exceeded under a hard stop."""

    def __init__(
        self,
        message: str,
        scope: str = "task",
        current_spend: float = 0.0,
        limit_usd: float = 0.0,
        requested_usd: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.current_spend = current_spend
        self.limit_usd = limit_usd
        self.requested_usd = requested_usd


class BudgetCheckResult(dict):
    """
    Dictionary and attribute-accessible result returned by BudgetConfig.check_budget().
    Supports both `res["allowed"]` / `res["hard_stop_triggered"]` and `res.allowed` / `res.hard_stop_triggered`.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(f"'BudgetCheckResult' object has no attribute '{name}'") from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


@dataclass
class BudgetConfig:
    """
    Token and cost budget guardrails (Spec Section 17).
    Enforces per-task, per-project, and global spending ceilings with hard-stop triggers.
    """
    per_task_usd: float = 5.0
    per_project_usd: float = 50.0
    global_usd: float = 200.0
    hard_stop: bool = True
    current_global_usd: float = 0.0
    task_spend: Dict[str, float] = field(default_factory=dict)
    project_spend: Dict[str, float] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)

    def get_task_spend(self, task_id: str = "default") -> float:
        return round(float(self.task_spend.get(task_id, 0.0)), 6)

    def get_project_spend(self, project_id: str = "default") -> float:
        return round(float(self.project_spend.get(project_id, 0.0)), 6)

    def check_budget(
        self,
        amount_usd: float = 0.0,
        task_id: str = "default",
        project_id: str = "default",
        raise_on_exceed: bool = False,
        record: bool = False,
    ) -> BudgetCheckResult:
        """
        Evaluate whether spending `amount_usd` stays within task, project, and global limits.

        Returns a `BudgetCheckResult` (dict subclass) with:
        - `allowed`: bool
        - `exceeded`: bool
        - `hard_stop_triggered`: bool
        - `scope`: None or "task" | "project" | "global"
        - `reason`: human-readable message
        - `projected_task_usd`, `projected_project_usd`, `projected_global_usd`

        If `raise_on_exceed=True` and a limit is breached, raises `BudgetExceededError`.
        If `record=True` and allowed, records the spend immediately.
        """
        amount = max(0.0, float(amount_usd))
        cur_task = self.get_task_spend(task_id)
        cur_proj = self.get_project_spend(project_id)
        cur_glob = round(float(self.current_global_usd), 6)

        proj_task = round(cur_task + amount, 6)
        proj_proj = round(cur_proj + amount, 6)
        proj_glob = round(cur_glob + amount, 6)

        breach_scope: Optional[str] = None
        reason = "Within budget limits."
        limit_val = 0.0
        current_val = 0.0

        if proj_task > self.per_task_usd:
            breach_scope = "task"
            limit_val = self.per_task_usd
            current_val = cur_task
            reason = (
                f"Task '{task_id}' budget exceeded: projected ${proj_task:.4f} > "
                f"per_task_usd limit ${self.per_task_usd:.4f}"
            )
        elif proj_proj > self.per_project_usd:
            breach_scope = "project"
            limit_val = self.per_project_usd
            current_val = cur_proj
            reason = (
                f"Project '{project_id}' budget exceeded: projected ${proj_proj:.4f} > "
                f"per_project_usd limit ${self.per_project_usd:.4f}"
            )
        elif proj_glob > self.global_usd:
            breach_scope = "global"
            limit_val = self.global_usd
            current_val = cur_glob
            reason = (
                f"Global budget exceeded: projected ${proj_glob:.4f} > "
                f"global_usd limit ${self.global_usd:.4f}"
            )

        exceeded = breach_scope is not None
        hard_stop_triggered = bool(exceeded and self.hard_stop)
        allowed = not hard_stop_triggered and not exceeded

        result = BudgetCheckResult(
            allowed=allowed,
            exceeded=exceeded,
            hard_stop=self.hard_stop,
            hard_stop_triggered=hard_stop_triggered,
            scope=breach_scope,
            reason=reason,
            amount_usd=amount,
            task_id=task_id,
            project_id=project_id,
            current_task_usd=cur_task,
            current_project_usd=cur_proj,
            current_global_usd=cur_glob,
            projected_task_usd=proj_task,
            projected_project_usd=proj_proj,
            projected_global_usd=proj_glob,
            per_task_usd=self.per_task_usd,
            per_project_usd=self.per_project_usd,
            global_usd=self.global_usd,
        )

        if exceeded and raise_on_exceed:
            raise BudgetExceededError(
                reason,
                scope=breach_scope or "unknown",
                current_spend=current_val,
                limit_usd=limit_val,
                requested_usd=amount,
            )

        if record and allowed and amount > 0:
            self.record_spend(amount, task_id=task_id, project_id=project_id)

        return result

    def enforce_budget(
        self,
        amount_usd: float,
        task_id: str = "default",
        project_id: str = "default",
        record: bool = True,
    ) -> BudgetCheckResult:
        """
        Check budget and raise `BudgetExceededError` if `hard_stop` is enabled and exceeded.
        Records spend if within budget and `record=True`.
        """
        res = self.check_budget(
            amount_usd=amount_usd,
            task_id=task_id,
            project_id=project_id,
            raise_on_exceed=self.hard_stop,
            record=record,
        )
        return res

    def record_spend(
        self,
        amount_usd: float,
        task_id: str = "default",
        project_id: str = "default",
        provider_id: str = "",
        model_id: str = "",
    ) -> Dict[str, Any]:
        """Record actual cost incurred by a request."""
        amount = max(0.0, float(amount_usd))
        self.task_spend[task_id] = round(self.task_spend.get(task_id, 0.0) + amount, 6)
        self.project_spend[project_id] = round(self.project_spend.get(project_id, 0.0) + amount, 6)
        self.current_global_usd = round(self.current_global_usd + amount, 6)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "amount_usd": amount,
            "task_id": task_id,
            "project_id": project_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "task_total_usd": self.task_spend[task_id],
            "project_total_usd": self.project_spend[project_id],
            "global_total_usd": self.current_global_usd,
        }
        self.history.append(entry)
        return entry

    def reset_spend(self, task_id: Optional[str] = None, project_id: Optional[str] = None) -> None:
        """Reset recorded spending for a specific task/project or globally."""
        if task_id is not None:
            self.task_spend.pop(task_id, None)
        elif project_id is not None:
            self.project_spend.pop(project_id, None)
        else:
            self.task_spend.clear()
            self.project_spend.clear()
            self.current_global_usd = 0.0
            self.history.clear()

    def get_summary(self) -> Dict[str, Any]:
        return {
            "per_task_usd": self.per_task_usd,
            "per_project_usd": self.per_project_usd,
            "global_usd": self.global_usd,
            "hard_stop": self.hard_stop,
            "current_global_usd": round(self.current_global_usd, 6),
            "remaining_global_usd": round(max(0.0, self.global_usd - self.current_global_usd), 6),
            "task_spend": dict(self.task_spend),
            "project_spend": dict(self.project_spend),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "per_task_usd": self.per_task_usd,
            "per_project_usd": self.per_project_usd,
            "global_usd": self.global_usd,
            "hard_stop": self.hard_stop,
            "current_global_usd": self.current_global_usd,
            "task_spend": dict(self.task_spend),
            "project_spend": dict(self.project_spend),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BudgetConfig":
        return cls(
            per_task_usd=float(data.get("per_task_usd", 5.0)),
            per_project_usd=float(data.get("per_project_usd", 50.0)),
            global_usd=float(data.get("global_usd", 200.0)),
            hard_stop=bool(data.get("hard_stop", True)),
            current_global_usd=float(data.get("current_global_usd", 0.0)),
            task_spend=dict(data.get("task_spend", {})),
            project_spend=dict(data.get("project_spend", {})),
        )


def _derive_keys(master_secret: str, salt: bytes, iterations: int = 100_000) -> tuple[bytes, bytes]:
    """Derive a 32-byte encryption key and 32-byte HMAC authentication key via PBKDF2-HMAC-SHA256."""
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        master_secret.encode("utf-8"),
        salt,
        iterations,
        dklen=64,
    )
    return derived[:32], derived[32:]


def _xor_keystream(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
    """Counter-mode HMAC-SHA256 stream cipher (pure stdlib, deterministic, cross-platform)."""
    out = bytearray(len(data))
    block_size = 32
    num_blocks = (len(data) + block_size - 1) // block_size
    for counter in range(num_blocks):
        counter_bytes = counter.to_bytes(8, byteorder="big")
        keystream_block = hmac.new(enc_key, nonce + counter_bytes, hashlib.sha256).digest()
        start = counter * block_size
        end = min(start + block_size, len(data))
        for idx in range(start, end):
            out[idx] = data[idx] ^ keystream_block[idx - start]
    return bytes(out)


def _encrypt_payload(plaintext_bytes: bytes, master_secret: str) -> Dict[str, Any]:
    """Authenticated encryption (Encrypt-then-MAC) using PBKDF2-HMAC-SHA256."""
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    enc_key, mac_key = _derive_keys(master_secret, salt)
    ciphertext = _xor_keystream(plaintext_bytes, enc_key, nonce)
    mac = hmac.new(mac_key, salt + nonce + ciphertext, hashlib.sha256).digest()
    return {
        "format": "BEL_VAULT_V2",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": 100_000,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "mac": base64.b64encode(mac).decode("ascii"),
    }


def _decrypt_payload(envelope: Dict[str, Any], master_secret: str) -> bytes:
    """Verify HMAC tag and decrypt ciphertext from a BEL_VAULT_V2 envelope."""
    if envelope.get("format") != "BEL_VAULT_V2":
        raise ValueError("Unsupported vault format.")
    iterations = int(envelope.get("iterations", 100_000))
    salt = base64.b64decode(envelope["salt"])
    nonce = base64.b64decode(envelope["nonce"])
    ciphertext = base64.b64decode(envelope["ciphertext"])
    expected_mac = base64.b64decode(envelope["mac"])

    enc_key, mac_key = _derive_keys(master_secret, salt, iterations=iterations)
    actual_mac = hmac.new(mac_key, salt + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_mac, actual_mac):
        raise ValueError("Vault integrity check failed: invalid master key or corrupted vault file.")
    return _xor_keystream(ciphertext, enc_key, nonce)


class AccountEntry(str):
    """
    String subclass representing an account label (so `"prod" in vault.list_accounts("tokenharbor")` is True)
    while also supporting dict-like (`acc["label"]`, `acc.get("masked_key")`) and attribute access.
    """

    def __new__(cls, label: str, metadata: Optional[Dict[str, Any]] = None) -> "AccountEntry":
        obj = super().__new__(cls, label)
        obj._meta = dict(metadata or {"label": label})
        return obj

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return super().__getitem__(key)
        return self._meta[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._meta.get(key, default)

    def __getattr__(self, name: str) -> Any:
        if name != "_meta" and name in self._meta:
            return self._meta[name]
        raise AttributeError(f"'AccountEntry' object has no attribute '{name}'")

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._meta)


class SecretVault:
    """
    Secure API key storage for BEL Code (Spec Section 2.2 & 15).
    Uses OS credential storage (`keyring` when available) + encrypted local vault file
    (`~/.bel_code/vault.enc` using PBKDF2-HMAC-SHA256 + authenticated encryption).

    Supports:
    - Multiple accounts/keys per provider via `label`
    - Key rotation (`rotate_key`)
    - `set_secret`, `get_secret`, `list_accounts`, `delete_secret`
    - `redact(text)` masking all stored secret values and known token patterns
    """

    KEYRING_SERVICE = "bel_code_standalone_vault"

    PROVIDER_ENV_VARS: Dict[str, Tuple[str, ...]] = {
        "freellm": ("FREELLM_API_KEY",),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "groq": ("GROQ_API_KEY",),
        "openrouter": ("OPENROUTER_API_KEY",),
        "mistral": ("MISTRAL_API_KEY",),
        "cloudflare": ("CLOUDFLARE_API_TOKEN",),
        "nvidia": ("NVIDIA_API_KEY",),
        "sambanova": ("SAMBANOVA_API_KEY",),
        "cerebras": ("CEREBRAS_API_KEY",),
        "huggingface": ("HF_TOKEN", "HUGGINGFACE_API_KEY"),
        "github_models": ("GITHUB_TOKEN",),
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "deepseek": ("DEEPSEEK_API_KEY",),
        "together": ("TOGETHER_API_KEY",),
        "fireworks": ("FIREWORKS_API_KEY",),
        "tokenharbor": ("TOKENHARBOR_API_KEY",),
        "custom": ("BEL_CODE_CUSTOM_API_KEY",),
    }

    # Compiled regex patterns for secret token redaction
    SECRET_PATTERNS = [
        re.compile(r"thk_live_[A-Za-z0-9_-]+"),
        re.compile(r"freellmapi-[A-Za-z0-9_-]+"),
        re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"gsk_[A-Za-z0-9_-]{16,}"),
        re.compile(r"nvapi-[A-Za-z0-9_-]{16,}"),
        re.compile(r"hf_[A-Za-z0-9_-]{16,}"),
        re.compile(r"(?:ghp_|github_pat_)[A-Za-z0-9_-]{16,}"),
        re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
        re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._-]{8,}"),
    ]

    def __init__(
        self,
        vault_path: Optional[Union[str, Path]] = None,
        master_key: Optional[str] = None,
        use_keyring: bool = False,
    ) -> None:
        if vault_path is None:
            base_dir = Path.home() / ".bel_code"
            self.vault_path = base_dir / "vault.enc"
        else:
            candidate = Path(vault_path)
            if candidate.exists() and candidate.is_dir():
                self.vault_path = candidate / "vault.enc"
            elif candidate.suffix == "" and not candidate.name.endswith(".enc"):
                # Could be a directory path passed by caller
                if "vault" not in candidate.name.lower():
                    self.vault_path = candidate / "vault.enc"
                else:
                    self.vault_path = candidate
            else:
                self.vault_path = candidate

        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self._explicit_master_key = master_key is not None
        self._master_key = master_key or self._default_machine_secret()
        self._use_keyring = bool(use_keyring and HAS_KEYRING)
        # Structure: { provider_id: { label: { "key": str, "created_at": str, "updated_at": str, "rotation_count": int, "active": bool, "history_hashes": list } } }
        self._store: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._load()

    @staticmethod
    def _default_machine_secret() -> str:
        env_secret = os.environ.get("BEL_CODE_VAULT_KEY")
        if env_secret:
            return env_secret
        user = "bel_user"
        try:
            user = getpass.getuser()
        except Exception:
            pass
        node = platform.node() or "localhost"
        return f"bel_code_v2_machine_key::{user}@{node}"

    def _load(self) -> None:
        if not self.vault_path.exists():
            self._store = {}
            return
        try:
            raw_text = self.vault_path.read_text(encoding="utf-8")
            envelope = json.loads(raw_text)
            decrypted = _decrypt_payload(envelope, self._master_key)
            data = json.loads(decrypted.decode("utf-8"))
            self._store = data.get("providers", {})
        except ValueError:
            if self._explicit_master_key:
                raise
            self._store = {}
        except Exception:
            self._store = {}

    def _save(self) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "2.1.0",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "providers": self._store,
        }
        plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
        envelope = _encrypt_payload(plaintext, self._master_key)
        self.vault_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

    @staticmethod
    def mask_value(secret_value: str) -> str:
        """Return a safe masked preview of a secret key."""
        if not secret_value:
            return ""
        if len(secret_value) <= 8:
            return "***REDACTED***"
        return f"{secret_value[:4]}...{secret_value[-3:]}"

    def set_secret(
        self,
        provider_id: str,
        key_value: str,
        label: str = "default",
        set_active: bool = True,
    ) -> Dict[str, Any]:
        """Store or update an API key for `provider_id` under account `label`."""
        provider_key = provider_id.strip().lower()
        account_label = (label or "default").strip()
        now = datetime.now(timezone.utc).isoformat()

        provider_accounts = self._store.setdefault(provider_key, {})
        existing = provider_accounts.get(account_label)

        if set_active:
            for acc in provider_accounts.values():
                acc["active"] = False

        key_hash = hashlib.sha256(key_value.encode("utf-8")).hexdigest()[:16]
        if existing:
            history = list(existing.get("history_hashes", []))
            if existing.get("key_hash") and existing.get("key_hash") != key_hash:
                history.append(existing["key_hash"])
            entry = {
                "key": key_value,
                "key_hash": key_hash,
                "label": account_label,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
                "rotation_count": int(existing.get("rotation_count", 0)),
                "active": bool(set_active or len(provider_accounts) == 0),
                "history_hashes": history,
            }
        else:
            entry = {
                "key": key_value,
                "key_hash": key_hash,
                "label": account_label,
                "created_at": now,
                "updated_at": now,
                "rotation_count": 0,
                "active": bool(set_active or len(provider_accounts) == 0),
                "history_hashes": [],
            }

        provider_accounts[account_label] = entry
        self._save()

        if self._use_keyring and _keyring is not None:
            try:
                _keyring.set_password(self.KEYRING_SERVICE, f"{provider_key}:{account_label}", key_value)
            except Exception:
                pass

        return {
            "provider_id": provider_key,
            "label": account_label,
            "masked_key": self.mask_value(key_value),
            "rotation_count": entry["rotation_count"],
            "active": entry["active"],
            "updated_at": now,
        }

    def get_secret(self, provider_id: str, label: str = "default") -> Optional[str]:
        """Retrieve the decrypted API key for `provider_id` and `label`."""
        provider_key = provider_id.strip().lower()
        account_label = (label or "default").strip()

        provider_accounts = self._store.get(provider_key, {})
        if account_label in provider_accounts:
            return provider_accounts[account_label].get("key")

        # If default requested and not present, return active or first available account
        if account_label == "default" and provider_accounts:
            for acc in provider_accounts.values():
                if acc.get("active"):
                    return acc.get("key")
            first_key = next(iter(provider_accounts.values()))
            return first_key.get("key")

        if self._use_keyring and _keyring is not None:
            try:
                val = _keyring.get_password(self.KEYRING_SERVICE, f"{provider_key}:{account_label}")
                if val:
                    return val
            except Exception:
                pass

        if account_label == "default":
            for env_name in self.PROVIDER_ENV_VARS.get(provider_key, (f"{provider_key.upper()}_API_KEY",)):
                env_val = os.environ.get(env_name, "").strip()
                if env_val:
                    return env_val

        return None

    def rotate_key(
        self,
        provider_id_or_master_key: str,
        new_key_value: Optional[str] = None,
        label: str = "default",
    ) -> Dict[str, Any]:
        """
        Rotate an API key for a provider account OR rotate the vault master key.
        - `rotate_key(provider_id, new_key_value, label="default")`: rotates the provider API key,
          increments `rotation_count`, and records previous key hash.
        - `rotate_key(new_master_key)` (when `new_key_value is None`): rotates the vault encryption master key.
        """
        if new_key_value is None:
            # Master encryption key rotation
            self._master_key = provider_id_or_master_key
            self._save()
            return {
                "rotated": "master_key",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        provider_key = provider_id_or_master_key.strip().lower()
        account_label = (label or "default").strip()
        now = datetime.now(timezone.utc).isoformat()

        provider_accounts = self._store.setdefault(provider_key, {})
        existing = provider_accounts.get(account_label)
        prev_rotation = int(existing.get("rotation_count", 0)) if existing else 0
        history = list(existing.get("history_hashes", [])) if existing else []
        if existing and existing.get("key_hash"):
            history.append(existing["key_hash"])

        new_hash = hashlib.sha256(new_key_value.encode("utf-8")).hexdigest()[:16]
        entry = {
            "key": new_key_value,
            "key_hash": new_hash,
            "label": account_label,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
            "rotated_at": now,
            "rotation_count": prev_rotation + 1,
            "active": True,
            "history_hashes": history,
        }
        for lbl, acc in provider_accounts.items():
            if lbl != account_label:
                acc["active"] = False
        provider_accounts[account_label] = entry
        self._save()

        if self._use_keyring and _keyring is not None:
            try:
                _keyring.set_password(self.KEYRING_SERVICE, f"{provider_key}:{account_label}", new_key_value)
            except Exception:
                pass

        return {
            "provider_id": provider_key,
            "label": account_label,
            "masked_key": self.mask_value(new_key_value),
            "rotation_count": entry["rotation_count"],
            "rotated_at": now,
            "previous_keys_count": len(history),
            "active": True,
        }

    def list_accounts(self, provider_id: Optional[str] = None) -> List[AccountEntry]:
        """List stored accounts (supports both string membership `"prod" in accounts` and dict access `acc["label"]`)."""
        results: List[AccountEntry] = []
        if provider_id is not None:
            provider_key = provider_id.strip().lower()
            providers_map = {provider_key: self._store.get(provider_key, {})}
        else:
            providers_map = self._store

        for pid, accounts in providers_map.items():
            for lbl, acc in accounts.items():
                meta = {
                    "provider_id": pid,
                    "label": lbl,
                    "masked_key": self.mask_value(acc.get("key", "")),
                    "created_at": acc.get("created_at"),
                    "updated_at": acc.get("updated_at"),
                    "rotation_count": int(acc.get("rotation_count", 0)),
                    "active": bool(acc.get("active", False)),
                }
                results.append(AccountEntry(lbl, meta))
        return results

    def delete_secret(self, provider_id: str, label: str = "default") -> bool:
        """Delete a stored secret for `(provider_id, label)`."""
        provider_key = provider_id.strip().lower()
        account_label = (label or "default").strip()
        provider_accounts = self._store.get(provider_key)
        if not provider_accounts or account_label not in provider_accounts:
            return False

        was_active = provider_accounts[account_label].get("active", False)
        del provider_accounts[account_label]

        if not provider_accounts:
            del self._store[provider_key]
        elif was_active:
            next_acc = next(iter(provider_accounts.values()))
            next_acc["active"] = True

        self._save()

        if self._use_keyring and _keyring is not None:
            try:
                _keyring.delete_password(self.KEYRING_SERVICE, f"{provider_key}:{account_label}")
            except Exception:
                pass

        return True

    def has_secret(self, provider_id: str, label: Optional[str] = None) -> bool:
        """Check whether an API key is configured in vault, keyring, or environment variables."""
        provider_key = provider_id.strip().lower()
        accounts = self._store.get(provider_key, {})
        if accounts:
            if label is None:
                if any(bool(acc.get("key")) for acc in accounts.values()):
                    return True
            elif bool(accounts.get(label, {}).get("key")):
                return True
        if label in (None, "default"):
            for env_name in self.PROVIDER_ENV_VARS.get(provider_key, (f"{provider_key.upper()}_API_KEY",)):
                if os.environ.get(env_name, "").strip():
                    return True
        return False

    def get_all_secret_values(self) -> List[str]:
        """Internal helper returning registered non-empty secrets for redaction."""
        values: List[str] = []
        for accounts in self._store.values():
            for acc in accounts.values():
                val = acc.get("key", "")
                if val and len(val) >= 4:
                    values.append(val)
        # Sort longest first so partial overlaps don't leak suffixes
        values.sort(key=len, reverse=True)
        return values

    def redact(self, text: str) -> str:
        """
        Redact all registered secrets as well as common API key / token patterns:
        - `thk_live_[A-Za-z0-9_-]+`
        - `freellmapi-[A-Za-z0-9_-]+`
        - `sk-[A-Za-z0-9_-]{16,}`
        - `Bearer [A-Za-z0-9._-]+`
        """
        if not text or not isinstance(text, str):
            return text

        redacted = text
        # 1. Redact all registered secret values
        for secret_val in self.get_all_secret_values():
            if secret_val in redacted:
                redacted = redacted.replace(secret_val, "[REDACTED]")

        # 2. Redact known credential token patterns
        for pattern in self.SECRET_PATTERNS:
            if pattern.pattern.startswith("(?i)(Bearer"):
                redacted = pattern.sub(r"\1[REDACTED]", redacted)
            else:
                redacted = pattern.sub("[REDACTED]", redacted)

        return redacted


class SettingsManager:
    """
    Central configuration manager for BEL Code.
    Persists non-secret settings in `~/.bel_code/config.json` (or `config_dir / "config.json"`),
    and delegates all secret management to `SecretVault` (`config_dir / "vault.enc"`).
    """

    DEFAULT_SETTINGS: Dict[str, Any] = {
        "version": "2.1.0",
        "product_name": "BEL Code",
        "standalone": True,
        "security_mode": SecurityMode.SUPERVISED.value,
        "primary_provider": "freellm",
        "secondary_provider": "tokenharbor",
        "tertiary_provider": "openai",
        "auto_routing": True,
        "allow_expensive_upgrade": False,
        "onboarding_completed": False,
        "providers": {},
        "routing_policy": {},
    }

    @staticmethod
    def load_dotenv(env_path: Optional[Path] = None) -> None:
        """Load key=value pairs from `.env` if present without overriding existing process env."""
        candidate = env_path or (Path.cwd() / ".env")
        if not candidate.is_file():
            return
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                key_name = k.strip()
                val = v.strip().strip('"').strip("'")
                if key_name and val:
                    os.environ.setdefault(key_name, val)
        except Exception:
            pass

    def __init__(
        self,
        config_dir: Optional[Union[str, Path]] = None,
        master_key: Optional[str] = None,
    ) -> None:
        self.load_dotenv()
        if config_dir is None:
            self.config_dir = Path.home() / ".bel_code"
        else:
            self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"
        self.vault = SecretVault(vault_path=self.config_dir / "vault.enc", master_key=master_key)
        self.budget = BudgetConfig()
        self._settings: Dict[str, Any] = json.loads(json.dumps(self.DEFAULT_SETTINGS))
        self.load()

    @property
    def security_mode(self) -> SecurityMode:
        raw = self._settings.get("security_mode", SecurityMode.SUPERVISED.value)
        try:
            return SecurityMode(raw)
        except ValueError:
            return SecurityMode.SUPERVISED

    @security_mode.setter
    def security_mode(self, mode: Union[SecurityMode, str]) -> None:
        if isinstance(mode, SecurityMode):
            self._settings["security_mode"] = mode.value
        else:
            self._settings["security_mode"] = SecurityMode(str(mode).lower()).value
        self.save()

    def load(self) -> Dict[str, Any]:
        """Load non-secret settings from `config.json`."""
        if self.config_file.exists():
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    budget_data = data.pop("budget", None)
                    if isinstance(budget_data, dict):
                        self.budget = BudgetConfig.from_dict(budget_data)
                    self._settings.update(data)
            except Exception:
                pass
        else:
            self.save()
        return self.get_settings()

    def save(self) -> None:
        """Persist non-secret settings to `config.json` (never writes raw secrets)."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(self._settings)
        payload["budget"] = self.budget.to_dict()
        serialized = json.dumps(payload, indent=2)
        # Ensure no secret ever leaks into config.json
        safe_serialized = self.vault.redact(serialized)
        self.config_file.write_text(safe_serialized, encoding="utf-8")

    def get_settings(self) -> Dict[str, Any]:
        """Return a copy of current configuration including budget summary."""
        result = json.loads(json.dumps(self._settings))
        result["budget"] = self.budget.get_summary()
        result["configured_accounts"] = self.vault.list_accounts()
        return result

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update configuration settings and persist to disk.
        If `budget` updates or `security_mode` updates are provided, validates and applies them.
        If any provider `api_key` is passed inside `updates`, routes it to `self.vault` instead of `config.json`.
        """
        if not isinstance(updates, dict):
            return self.get_settings()

        clean_updates = dict(updates)

        if "security_mode" in clean_updates:
            mode_val = clean_updates["security_mode"]
            if isinstance(mode_val, SecurityMode):
                clean_updates["security_mode"] = mode_val.value
            else:
                clean_updates["security_mode"] = SecurityMode(str(mode_val).lower()).value

        if "budget" in clean_updates and isinstance(clean_updates["budget"], dict):
            b_up = clean_updates.pop("budget")
            if "per_task_usd" in b_up:
                self.budget.per_task_usd = float(b_up["per_task_usd"])
            if "per_project_usd" in b_up:
                self.budget.per_project_usd = float(b_up["per_project_usd"])
            if "global_usd" in b_up:
                self.budget.global_usd = float(b_up["global_usd"])
            if "hard_stop" in b_up:
                self.budget.hard_stop = bool(b_up["hard_stop"])

        # Intercept any api_key fields to keep secrets strictly in SecretVault
        if "api_key" in clean_updates:
            raw_key = clean_updates.pop("api_key")
            target_provider = clean_updates.get("primary_provider", self._settings.get("primary_provider", "freellm"))
            if raw_key:
                self.vault.set_secret(str(target_provider), str(raw_key))

        if "providers" in clean_updates and isinstance(clean_updates["providers"], dict):
            providers_cfg = dict(self._settings.get("providers", {}))
            for pid, pdata in clean_updates["providers"].items():
                if isinstance(pdata, dict):
                    p_copy = dict(pdata)
                    if "api_key" in p_copy:
                        raw_k = p_copy.pop("api_key")
                        if raw_k:
                            self.vault.set_secret(pid, str(raw_k), label=p_copy.get("account_label", "default"))
                    providers_cfg[pid] = {**providers_cfg.get(pid, {}), **p_copy}
            clean_updates["providers"] = providers_cfg

        self._settings.update(clean_updates)
        self.save()
        return self.get_settings()
