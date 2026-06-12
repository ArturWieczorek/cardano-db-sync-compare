"""Per-database id-range windows for the common chain boundary.

Because rows are inserted in chain order, all rows belonging to a stretch of
chain have surrogate ids in one contiguous range *within a single database*.
This module computes those ranges by walking the chain spine
``block -> tx -> {tx_out, pool_update, gov_action_proposal}`` using index seeks
(never ``min(id)/max(id)`` with a non-PK filter, which the planner turns into a
whole-table scan). See ``docs/03-how-it-works.md`` (idea 3) and
``docs/primers/02-indexes-and-table-scans.md``.
"""

from __future__ import annotations

from db_sync_comparator.sql import quote_ident


def get_tip(conn) -> tuple[int, int]:
    """Return ``(max block_no, max epoch_no)`` for a database."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(block_no), max(epoch_no) FROM block")
        bn, en = cur.fetchone()
        return int(bn), int(en)


def _minmax(conn, table: str, col: str, lo, hi) -> tuple:
    """Smallest/largest id whose ``col`` is in ``[lo, hi]``.

    Written as two ``ORDER BY ... LIMIT 1`` index seeks rather than
    ``min(id)/max(id)``: with a filter on a non-PK column, the planner would
    otherwise walk the PK index across the whole table (minutes on the 121M-row
    ``tx``). The seek uses the index on ``col`` and returns in milliseconds.
    """
    t, c = quote_ident(table), quote_ident(col)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT "
            f"(SELECT id FROM {t} WHERE {c} BETWEEN %s AND %s ORDER BY {c} ASC,  id ASC  LIMIT 1), "
            f"(SELECT id FROM {t} WHERE {c} BETWEEN %s AND %s ORDER BY {c} DESC, id DESC LIMIT 1)",
            (lo, hi, lo, hi),
        )
        return cur.fetchone()


def compute_spine_ranges(conn, cutoff_block: int, window: tuple[int, int] | None) -> dict:
    """Per-DB surrogate-id ranges for each spine table.

    Returns ``spine -> (min_id, max_id)``, or ``(None, None)`` when the window is
    empty. ``window`` is a ``(lo_block, hi_block)`` pair for block-window mode, or
    ``None`` for cutoff mode (everything up to ``cutoff_block``).
    """
    ranges: dict[str, tuple] = {}
    with conn.cursor() as cur:
        if window is not None:
            ranges["block"] = _minmax(conn, "block", "block_no", window[0], window[1])
        else:
            # cutoff mode: everything up to (and including) the cutoff block;
            # id 0.. keeps genesis / EBB rows (NULL block_no) that sit below it.
            cur.execute(
                "SELECT id FROM block WHERE block_no <= %s ORDER BY block_no DESC, id DESC LIMIT 1",
                (cutoff_block,),
            )
            row = cur.fetchone()
            ranges["block"] = (0, row[0] if row else None)

    b = ranges["block"]
    if b[0] is None:
        for s in ("tx", "tx_out", "pool_update", "gov_action_proposal"):
            ranges[s] = (None, None)
        return ranges
    ranges["tx"] = _minmax(conn, "tx", "block_id", b[0], b[1])
    t = ranges["tx"]
    if t[0] is None:
        for s in ("tx_out", "pool_update", "gov_action_proposal"):
            ranges[s] = (None, None)
        return ranges
    ranges["tx_out"] = _minmax(conn, "tx_out", "tx_id", t[0], t[1])
    ranges["pool_update"] = _minmax(conn, "pool_update", "registered_tx_id", t[0], t[1])
    ranges["gov_action_proposal"] = _minmax(conn, "gov_action_proposal", "tx_id", t[0], t[1])
    return ranges


# How to walk from a block to each spine table's id, for the bucket-boundary
# query below (same chain as compute_spine_ranges).
_SPINE_STEPS: dict[str, list[tuple[str, str]]] = {
    "block": [],
    "tx": [("tx", "block_id")],
    "tx_out": [("tx", "block_id"), ("tx_out", "tx_id")],
    "pool_update": [("tx", "block_id"), ("pool_update", "registered_tx_id")],
    "gov_action_proposal": [("tx", "block_id"), ("gov_action_proposal", "tx_id")],
}


def block_edges(cutoff_block: int, n_buckets: int) -> list[int]:
    """Evenly-spaced block_no edges 0, W, 2W, …, cutoff (pure).

    Used by the bucket localizer to split the chain into ~n_buckets windows. The
    last edge is always exactly ``cutoff_block``.
    """
    n = max(1, n_buckets)
    w = max(1, cutoff_block // n)
    edges = list(range(0, cutoff_block + 1, w))
    if edges[-1] != cutoff_block:
        edges.append(cutoff_block)
    return edges


def bucket_boundary_ids(conn, spine: str, edges: list[int]) -> list:
    """For each block edge, the spine-table id of the first row at/after that block.

    One query (a correlated index-seek per edge, via ``unnest`` of the edge list),
    so it's cheap. Returns a list aligned with ``edges``; an entry is ``None`` when
    no row exists at/after that edge. These ids are in the same space as a table's
    bucket anchor column (e.g. ``tx_out.tx_id``), so they can be used directly as
    ``width_bucket`` thresholds - and because they're computed per database, the
    per-DB thresholds differ (id drift) but **bucket k = the same block range** on
    both, the same property the id-range windows rely on.
    """
    expr = "(SELECT id FROM block WHERE block_no >= s.e ORDER BY block_no, id LIMIT 1)"
    for tbl, col in _SPINE_STEPS[spine]:
        t, c = quote_ident(tbl), quote_ident(col)
        expr = f"(SELECT id FROM {t} WHERE {c} >= {expr} ORDER BY {c}, id LIMIT 1)"
    sql = f"SELECT s.e, {expr} FROM unnest(%s::bigint[]) AS s(e) ORDER BY s.e"
    with conn.cursor() as cur:
        cur.execute(sql, (edges,))
        by_e = {int(e): (int(b) if b is not None else None) for e, b in cur.fetchall()}
    return [by_e.get(e) for e in edges]
