"""
CLI & Server Entry Point for BEL Code v2.1.0 Standalone Platform.
Usage:
    python -m bel_code                      # Launch BEL Code Web IDE Server
    python -m bel_code --port 8080          # Launch on custom port
    python -m bel_code --benchmark all      # Run all 10 Acceptance Benchmarks
    python -m bel_code --task "Build API"   # Execute an engineering task via CLI
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from bel_code import __version__


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bel-code",
        description=f"BEL Code v{__version__} — Standalone Autonomous AI Software Engineering Platform",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"BEL Code v{__version__}",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind the BEL Code web server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8420,
        help="Port to bind the BEL Code web server (default: 8420)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Initial project workspace path (defaults to current working directory)",
    )
    parser.add_argument(
        "--mode",
        choices=["ask", "supervised", "autonomous"],
        default="supervised",
        help="Operating security mode (ask, supervised, autonomous)",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Execute a standalone autonomous engineering task from the CLI and exit",
    )
    parser.add_argument(
        "--benchmark",
        default=None,
        help="Run acceptance benchmark(s): 'all' or a specific benchmark ID (e.g., build_app_from_scratch)",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan the active project workspace and output project intelligence JSON",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development server",
    )

    args = parser.parse_args(argv)
    workspace_path = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()

    if args.scan:
        from bel_code.intelligence.indexer import ProjectIntelligence

        intel = ProjectIntelligence(workspace_root=workspace_path)
        report = {
            "scan": intel.scan_project(),
            "architecture": intel.generate_architecture_map(),
            "health": intel.analyze_code_health(),
        }
        print(json.dumps(report, indent=2, default=str))
        return 0

    if args.benchmark:
        from bel_code.engine.workflow import AutonomousWorkflowEngine

        engine = AutonomousWorkflowEngine(workspace_root=workspace_path)
        if args.benchmark.strip().lower() == "all":
            result = engine.run_all_benchmarks()
        else:
            result = engine.run_acceptance_benchmark(args.benchmark.strip())
        print(json.dumps(result, indent=2, default=str))
        if isinstance(result, dict):
            if "all_passed" in result:
                return 0 if result["all_passed"] else 1
            if "passed" in result:
                return 0 if result["passed"] else 1
        return 0

    if args.task:
        from bel_code.engine.workflow import AutonomousWorkflowEngine

        engine = AutonomousWorkflowEngine(workspace_root=workspace_path)
        result = engine.execute_task(
            prompt=args.task,
            mode=args.mode,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") in ("completed", "delivered", "success", "passed") else 1

    # Default: launch FastAPI server
    import os
    import uvicorn

    os.environ["BEL_CODE_WORKSPACE"] = str(workspace_path)
    os.environ["BEL_CODE_SECURITY_MODE"] = args.mode
    print(f"============================================================")
    print(f"  BEL Code v{__version__} — Autonomous AI Software Engineering IDE")
    print(f"  Standalone Product | Primary Provider: FreeLLM API")
    print(f"  Active Workspace : {workspace_path}")
    print(f"  Security Mode    : {args.mode}")
    print(f"  Web Interface    : http://{args.host}:{args.port}")
    print(f"============================================================")
    uvicorn.run(
        "bel_code.server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
