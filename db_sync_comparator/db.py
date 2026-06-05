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


def stream_keys(dsn: str, sql: str, batch: int = 10000):
    """Yield one value per row from ``sql`` using a **server-side cursor**.

    Memory-bounded: rows are fetched ``batch`` at a time and never fully
    materialised, so this is safe even on a 10M-row accumulator. The connection
    is read-only and rolled back at the end (the queries are pure ``SELECT``s).
    """
    conn = psycopg.connect(dsn)  # not autocommit → a transaction for the named cursor
    try:
        conn.execute("SET default_transaction_read_only = on")
        with conn.cursor(name="dbsync_compare_stream") as cur:
            cur.itersize = batch
            cur.execute(sql)
            for row in cur:
                yield row[0]
    finally:
        conn.rollback()
        conn.close()
