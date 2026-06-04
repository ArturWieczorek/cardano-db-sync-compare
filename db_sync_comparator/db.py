"""Database connection handling.

The only module that imports psycopg, so the pure-logic modules stay importable
without the driver installed.
"""

from __future__ import annotations

import sys

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit("psycopg 3 is required:  pip install 'psycopg[binary]'")


def connect(dsn: str, work_mem: str = "256MB"):
    """Open an autocommit, read-only-style connection with a larger ``work_mem``.

    The bigger ``work_mem`` keeps the FK-translation hash joins in memory instead
    of spilling to disk during a full run.
    """
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(f"SET work_mem = '{work_mem}'")
    return conn
