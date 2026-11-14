"""
Maintainable AI Provider Registry Loader & Verification Guardrails for BEL Code.

Loads provider metadata (registration links, free-tier classifications, verified dates,
known restrictions, setup guides, and model IDs) from `bel_code/providers/registry.json`
so the backend, onboarding wizard UI, and documentation share one updateable source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


REGISTRY_FILE = Path(__file__).resolve().with_name("registry.json")

FREE_CLASSIFICATIONS = {
    "permanently_free",
    "free_tier_with_limits",
    "local_open_source",
}
TRIAL_CLASSIFICATIONS = {"free_trial_credits"}


def load_provider_registry(registry_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and normalize the maintained provider registry JSON."""
    path = registry_path or REGISTRY_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    providers = []
    for entry in data.get("providers", []):
        if not isinstance(entry, dict) or not entry.get("provider_id"):
            continue
        item = dict(entry)
        verified = bool(item.get("verified")) and bool(item.get("last_verified"))
        classification = str(item.get("free_tier_classification", "paid")).strip().lower()
        # Require explicit verification before advertising a provider as having free access
        item["advertised_free"] = bool(verified and classification in FREE_CLASSIFICATIONS)
        item["is_trial_credits"] = bool(verified and classification in TRIAL_CLASSIFICATIONS)
        item["is_paid"] = bool(classification == "paid")
        providers.append(item)
    return {
        "schema_version": data.get("schema_version", "1.0"),
        "last_updated": data.get("last_updated", ""),
        "providers": providers,
    }


def get_registry_entry(provider_id: str, registry_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return a single normalized registry entry by `provider_id`, or `None` if absent."""
    pid = provider_id.strip().lower()
    for item in load_provider_registry(registry_path).get("providers", []):
        if item.get("provider_id", "").lower() == pid:
            return item
    return None


def get_registry_map(registry_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return a mapping of `provider_id -> registry entry`."""
    return {
        item["provider_id"]: item
        for item in load_provider_registry(registry_path).get("providers", [])
    }
