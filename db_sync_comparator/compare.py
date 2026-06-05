"""Execute the comparison for one table and localize any mismatch.

``compare_table`` runs the set-hash (and optional value proof) on both databases
and classifies the result. ``localize`` binary-searches a mismatching table down
to a narrow chain window. See ``docs/03-how-it-works.md`` (ideas 2 and 5).
"""

from __future__ import annotations

import time

from db_sync_comparator.model import TablePlan, TableResult
from db_sync_comparator.ranges import compute_spine_ranges
from db_sync_comparator.sql import bound_predicate, hash_sql, value_sql


def run_scalar(conn, sql: str, timeout_ms: int) -> tuple:
    """Run a single-row query, optionally under a statement timeout."""
    with conn.cursor() as cur:
        if timeout_ms:
            cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        cur.execute(sql)
        return cur.fetchone()


def compare_table(
    plan: TablePlan,
    conn1,
    conn2,
    ranges1: dict,
    ranges2: dict,
    cutoff_epoch: int,
    in_block_range: bool,
    timeout_ms: int,
) -> TableResult:
    """Hash ``plan``'s table in both databases and classify the outcome."""
    r = TableResult(plan.name, plan.kind, skipped_cols=plan.skipped_cols, schema_drift=plan.extra_cols)
    start = time.time()
    try:
        pred1 = bound_predicate(plan, ranges1, cutoff_epoch, in_block_range)
        pred2 = bound_predicate(plan, ranges2, cutoff_epoch, in_block_range)
        a = run_scalar(conn1, hash_sql(plan, pred1), timeout_ms)
        b = run_scalar(conn2, hash_sql(plan, pred2), timeout_ms)
        r.n1, r.h1 = a[0], (int(a[1]), int(a[2]))
        r.n2, r.h2 = b[0], (int(b[1]), int(b[2]))

        v1, v2 = value_sql(plan, pred1), value_sql(plan, pred2)
        if v1 is not None and v2 is not None:
            va = run_scalar(conn1, v1, timeout_ms)
            vb = run_scalar(conn2, v2, timeout_ms)
            r.value1 = tuple(str(x) for x in va)
            r.value2 = tuple(str(x) for x in vb)

        if (r.n1 == 0) != (r.n2 == 0):
            # One side empty, the other populated → almost never data corruption;
            # the table was disabled in that version's insert_options (e.g.
            # pool_stat / governance / offchain), or is a feature only one
            # version writes. Flag clearly and don't bother localizing.
            r.status = "COUNT_DIFF"
            r.note = (
                f"one side has 0 rows ({r.n1} vs {r.n2}) — table likely disabled in "
                "config (insert_options) for that version, not a data difference"
            )
        elif plan.kind == "accumulator" and r.n1 != r.n2:
            # Expected when one DB is synced further; flag, don't fail.
            r.status = "COUNT_DIFF"
            r.note = "accumulator table; count delta usually reflects the tip gap (objects first-seen after the cutoff)"
        elif r.n1 != r.n2:
            r.status = "COUNT_DIFF"
            r.note = f"row count differs ({r.n1} vs {r.n2})"
        elif r.h1 != r.h2:
            r.status = "HASH_DIFF"
            r.note = "row counts match but content hash differs"
        elif r.value1 is not None and r.value1 != r.value2:
            r.status = "VALUE_DIFF"
            r.note = f"value aggregate differs: {r.value1} vs {r.value2}"
        else:
            r.status = "MATCH"
    except Exception as exc:  # operational, per-table — reported, never fatal
        r.status = "ERROR"
        r.note = f"{type(exc).__name__}: {exc}"
    r.seconds = time.time() - start
    return r


def localize(
    plan: TablePlan,
    conn1,
    conn2,
    lo: int,
    hi: int,
    cutoff_block: int,
    timeout_ms: int,
    max_leaves: int = 8,
    min_width: int = 2000,
) -> list[str]:
    """Binary-search the ``[lo, hi]`` window on the common chain coordinate.

    ``block_no`` for id-range tables, ``epoch_no`` for epoch tables. Returns the
    narrowest windows whose hashes still differ.
    """
    found: list[str] = []
    is_block = plan.anchor_kind == "idrange"
    coord = "block_no" if is_block else "epoch_no"

    def hashes(a: int, b: int) -> tuple[tuple, tuple]:
        if is_block:
            r1 = compute_spine_ranges(conn1, cutoff_block, (a, b))
            r2 = compute_spine_ranges(conn2, cutoff_block, (a, b))
            p1 = bound_predicate(plan, r1, 0, False)
            p2 = bound_predicate(plan, r2, 0, False)
        else:
            p1 = p2 = f"{plan.epoch_expr} BETWEEN {a} AND {b}"
        ra = run_scalar(conn1, hash_sql(plan, p1), timeout_ms)
        rb = run_scalar(conn2, hash_sql(plan, p2), timeout_ms)
        return (ra[0], int(ra[1]), int(ra[2])), (rb[0], int(rb[1]), int(rb[2]))

    if not is_block:
        min_width = 0
    stack = [(lo, hi)]
    while stack and len(found) < max_leaves:
        a, b = stack.pop()
        ha, hb = hashes(a, b)
        if ha == hb:
            continue
        if b - a <= min_width:
            kindword = "content differs" if ha[0] == hb[0] else "row count differs"
            found.append(f"{coord} {a}..{b}: {kindword} (db1 n={ha[0]}, db2 n={hb[0]})")
            continue
        mid = (a + b) // 2
        stack.append((mid + 1, b))
        stack.append((a, mid))
    return found
