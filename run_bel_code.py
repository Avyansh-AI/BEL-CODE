#!/usr/bin/env python3
"""
Standalone Launcher for BEL Code v2.1.0 (OpenCode-Powered 3-Layer Architecture).

Usage:
    python run_bel_code.py                      # Start the BEL Code 4-Zone Web IDE Server
    python run_bel_code.py --port 8420          # Start server on custom port
    python run_bel_code.py --benchmark all      # Run all 10 Acceptance Benchmarks
    python run_bel_code.py --task "..."         # Run a task from CLI
"""

import sys
from bel_code.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
