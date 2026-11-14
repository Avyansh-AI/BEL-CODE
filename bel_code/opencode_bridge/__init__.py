"""
BEL Code Layer 1 <-> Layer 2 OpenCode Foundation Bridge (`bel_code/opencode_bridge`).

Exposes:
- `OpenCodeRuntimeManager`: Inspects `vendor/opencode`, manages `opencode.json`, and runs the OpenCode
  session/tool/agent/diff/revert/permission protocol runtime.
- `OpenCodeClient`: Python HTTP & SDK client covering all OpenCode server routes.
- `build_default_opencode_config`: Canonical `opencode.json` schema generator with FreeLLM primary integration.
"""

from bel_code.opencode_bridge.client import OpenCodeClient
from bel_code.opencode_bridge.runtime import (
    DEFAULT_OPENCODE_CONFIG_PATH,
    DEFAULT_VENDOR_OPENCODE_DIR,
    OPENCODE_BUILTIN_AGENTS,
    OPENCODE_BUILTIN_COMMANDS,
    OPENCODE_BUILTIN_TOOLS,
    OPENCODE_SERVER_ROUTES,
    PINNED_UPSTREAM_COMMIT,
    UPSTREAM_REPO_URL,
    OpenCodeRuntimeManager,
    build_default_opencode_config,
)

__all__ = [
    "DEFAULT_OPENCODE_CONFIG_PATH",
    "DEFAULT_VENDOR_OPENCODE_DIR",
    "OPENCODE_BUILTIN_AGENTS",
    "OPENCODE_BUILTIN_COMMANDS",
    "OPENCODE_BUILTIN_TOOLS",
    "OPENCODE_SERVER_ROUTES",
    "PINNED_UPSTREAM_COMMIT",
    "UPSTREAM_REPO_URL",
    "OpenCodeClient",
    "OpenCodeRuntimeManager",
    "build_default_opencode_config",
]
