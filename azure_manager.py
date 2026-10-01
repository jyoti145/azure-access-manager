#!/usr/bin/env python3
"""Azure Access Manager - entry point.

    python azure_manager.py setup --dry-run
    python azure_manager.py --help
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("Azure Access Manager requires Python 3.11 or newer.")

from src.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
