"""Live-database schema introspection.

Reads columns, primary keys, GENERATED columns and (the rare) declared foreign
keys from ``information_schema``/``pg_catalog``. db-sync declares almost no FK
constraints, so the logical FK map in :mod:`db_sync_comparator.registries` is
what actually drives translation - but we still read what's declared.
"""

from __future__ import annotations

from db_sync_comparator.model import TableSchema


def introspect(conn) -> dict[str, TableSchema]:
    """Return ``{table_name: TableSchema}`` for every base table in ``public``."""
    cols: dict[str, TableSchema] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type,
                   (is_generated = 'ALWAYS') AS gen
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        )
        for tname, cname, dtype, gen in cur.fetchall():
            ts = cols.get(tname)
            if ts is None:
                ts = TableSchema(tname, [], {}, [], set(), {})
                cols[tname] = ts
            ts.columns.append(cname)
            ts.coltypes[cname] = dtype
            if gen:
                ts.generated.add(cname)

        # keep only base tables (drop views etc.)
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'
            """
        )
        base = {r[0] for r in cur.fetchall()}
        for t in list(cols):
            if t not in base:
                del cols[t]

        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema='public' AND tc.constraint_type='PRIMARY KEY'
            ORDER BY kcu.ordinal_position
            """
        )
        for tname, cname in cur.fetchall():
            if tname in cols:
                cols[tname].pk.append(cname)

        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS ref_table, ccu.column_name AS ref_col
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.table_schema='public' AND tc.constraint_type='FOREIGN KEY'
            """
        )
        for tname, cname, rtable, rcol in cur.fetchall():
            if tname in cols:
                cols[tname].fks[cname] = (rtable, rcol)
    return cols


def indexed_columns(conn) -> set[tuple[str, str]]:
    """``(table, first_index_column)`` pairs.

    Used to tell whether a table's anchor column can be range-scanned (fast) or
    forces a sequential scan when bounding to a narrow window.
    """
    out: set[tuple[str, str]] = set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.relname, a.attname
            FROM pg_index i
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
            WHERE n.nspname = 'public'
            """
        )
        for tbl, col in cur.fetchall():
            out.add((tbl, col))
    return out
