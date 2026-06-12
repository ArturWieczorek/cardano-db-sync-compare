"""Optional deep verification of accumulator ``COUNT_DIFF``s (opt-in).

Accumulator tables (`multi_asset`, `stake_address`, `pool_hash`, …) have no chain
anchor, so the tool can't bound them to the common cutoff and only reports a row
**count** difference. That count delta is *usually* just the tip gap (the
further-synced DB has seen a few more first-appearances), but the count alone
doesn't prove it.

This module verifies it the way a human would: stream the natural-key **set**
from each database (server-side, index-ordered, memory-bounded) and merge-compare
them - the equivalent of `comm` on two sorted files. If one side is a clean
**subset** of the other, the delta is purely extra rows in the bigger DB
(consistent with the tip gap); if **neither** is a subset, that's a real
difference worth investigating.

Everything here is read-only. The merge itself (`merge_compare`) is pure and
unit-tested; only `verify_accumulator` touches the database (via
`db.stream_keys`).
"""

from __future__ import annotations

from db_sync_comparator.db import stream_keys
from db_sync_comparator.registries import NATURAL_KEYS
from db_sync_comparator.sql import quote_ident


def accumulator_key_sql(table: str) -> str | None:
    """SQL expression producing one comparable text key per row, or ``None`` if
    the table has no plain-column natural key registered (e.g. `committee_member`).

    Columns are cast to ``text`` (deterministic for bytea → ``\\x…``) and joined
    with an unprinted separator so multi-column keys can't collide.
    """
    spec = NATURAL_KEYS.get(table)
    if not spec:
        return None
    cols = []
    for part in spec:
        if part[0] != "col":  # accumulators never need FK-chained keys
            return None
        cols.append(f"coalesce({quote_ident(part[1])}::text, '')")
    return " || chr(31) || ".join(cols)


def key_query(table: str) -> str | None:
    """Full ``SELECT … ORDER BY … COLLATE "C"`` for the key stream (or ``None``).

    ``COLLATE "C"`` forces byte ordering, which is identical on both databases and
    matches Python's ``str`` comparison for the ASCII keys these columns produce -
    so the two ordered streams can be merge-compared directly.
    """
    expr = accumulator_key_sql(table)
    if expr is None:
        return None
    return f'SELECT ({expr}) AS k FROM {quote_ident(table)} ORDER BY ({expr}) COLLATE "C"'


def merge_compare(it1, it2, max_examples: int = 5) -> dict:
    """Merge two **sorted** key iterators; count keys only-in-1, only-in-2, both.

    Pure function (no I/O) so it is trivially unit-testable.
    """
    a = next(it1, None)
    b = next(it2, None)
    only1 = only2 = both = 0
    ex1: list = []
    ex2: list = []
    while a is not None and b is not None:
        if a == b:
            both += 1
            a, b = next(it1, None), next(it2, None)
        elif a < b:
            only1 += 1
            if len(ex1) < max_examples:
                ex1.append(a)
            a = next(it1, None)
        else:
            only2 += 1
            if len(ex2) < max_examples:
                ex2.append(b)
            b = next(it2, None)
    while a is not None:
        only1 += 1
        if len(ex1) < max_examples:
            ex1.append(a)
        a = next(it1, None)
    while b is not None:
        only2 += 1
        if len(ex2) < max_examples:
            ex2.append(b)
        b = next(it2, None)
    return {"only_db1": only1, "only_db2": only2, "both": both, "examples_db1": ex1, "examples_db2": ex2}


def _verdict(only1: int, only2: int) -> str:
    if only1 == 0 and only2 == 0:
        return "identical key sets (a count delta would then mean duplicate keys - investigate)"
    if only2 == 0:
        return (
            "db2 ⊆ db1 - db1 is a clean superset; delta is extra rows only in db1 (tip-gap-consistent if db1 is ahead)"
        )
    if only1 == 0:
        return (
            "db1 ⊆ db2 - db2 is a clean superset; delta is extra rows only in db2 (tip-gap-consistent if db2 is ahead)"
        )
    return f"NEITHER is a subset - {only1} keys only in db1 AND {only2} only in db2; NOT a clean tip gap, investigate"


def verify_accumulator(dsn1: str, dsn2: str, table: str) -> dict:
    """Subset-check an accumulator table's key sets across the two databases."""
    q = key_query(table)
    if q is None:
        return {"verified": False, "reason": f"no plain-column natural key registered for {table}"}
    result = merge_compare(stream_keys(dsn1, q), stream_keys(dsn2, q))
    result["verified"] = True
    result["verdict"] = _verdict(result["only_db1"], result["only_db2"])
    return result
