"""Entry point for ``python -m db_sync_comparator``."""

from __future__ import annotations

import sys

from db_sync_comparator.cli import main

if __name__ == "__main__":
    sys.exit(main())
