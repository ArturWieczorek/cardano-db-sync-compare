"""Pure SQL generation - no database access.

Everything here is a string-building function (plus the small ``JoinBuilder``
helper), which makes it straightforward to unit-test without a database. The two
ideas implemented here:

* the order-independent, duplicate-safe **set hash** (``SETHASH_SELECT`` +
  ``hash_sql``), and
* **foreign-key translation** to natural keys (``natural_key_exprs``), with the
  ``JoinBuilder`` accumulating the de-duplicated LEFT JOINs it needs.

See ``docs/primers/03-hashing-and-fingerprints.md`` and
``docs/03-how-it-works.md``.
"""

from __future__ import annotations

from db_sync_comparator.model import TablePlan
from db_sync_comparator.registries import NATURAL_KEYS

# md5 -> two 60-bit numeric chunks, summed: order-independent & duplicate-safe.
SETHASH_SELECT = (
    "count(*) AS n, "
    "coalesce(sum(('x'||substr(h,1,15))::bit(60)::bigint::numeric),0) AS s1, "
    "coalesce(sum(('x'||substr(h,17,15))::bit(60)::bigint::numeric),0) AS s2"
)


def quote_ident(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


class JoinBuilder:
    """Accumulates idempotent LEFT JOINs and hands out stable aliases.

    Asking for the same (parent_alias, fk_col, target) join twice returns the
    same alias and adds the join only once, so a table reached by two paths is
    joined a single time.
    """

    def __init__(self, root_alias: str = "t0") -> None:
        self.root = root_alias
        self._joins: dict[tuple[str, str, str], str] = {}
        self._order: list[str] = []
        self._n = 0

    def join(self, parent_alias: str, fk_col: str, target: str, target_pk: str = "id") -> str:
        key = (parent_alias, fk_col, target)
        if key in self._joins:
            return self._joins[key]
        self._n += 1
        alias = f"j{self._n}"
        self._joins[key] = alias
        self._order.append(
            f"LEFT JOIN {quote_ident(target)} {alias} "
            f"ON {parent_alias}.{quote_ident(fk_col)} = {alias}.{quote_ident(target_pk)}"
        )
        return alias

    def clauses(self) -> str:
        return "\n  ".join(self._order)


def natural_key_exprs(
    target: str,
    alias: str,
    jb: JoinBuilder,
    common_cols: dict[str, set[str]],
    depth: int,
    max_depth: int,
    skipped: list[str],
) -> list[str] | None:
    """SQL expressions for the natural key of ``target`` rooted at ``alias``.

    Resolves ``("fk", ...)`` parts recursively (adding joins via ``jb``). Returns
    ``None`` if it cannot be resolved within ``max_depth`` or the target has no
    registered natural key - the caller then drops the referencing column rather
    than hash a drifting id, recording it in ``skipped``.
    """
    spec = NATURAL_KEYS.get(target)
    if spec is None:
        return None
    out: list[str] = []
    for part in spec:
        if part[0] == "col":
            col = part[1]
            if col in common_cols.get(target, set()):
                out.append(f"{alias}.{quote_ident(col)}")
            # else: column absent in one schema; skip silently (schema drift)
        else:  # ("fk", col, subtarget)
            _, col, subtarget = part
            if depth >= max_depth:
                skipped.append(f"{target}.{col}->{subtarget} (depth>{max_depth})")
                return None
            sub_alias = jb.join(alias, col, subtarget)
            sub = natural_key_exprs(subtarget, sub_alias, jb, common_cols, depth + 1, max_depth, skipped)
            if sub is None:
                return None
            out.extend(sub)
    return out


# The "spine" of a table is the ancestor whose surrogate-id range we precompute
# for a given chain window. Because rows are inserted in chain order, the ids of
# all rows belonging to a block window form a contiguous range *within one DB*
# (rollbacks only burn ids near the tip), so the bound becomes an indexed
# `t0.<fk> BETWEEN lo AND hi` - no join to block, no sequential scan. The lo/hi
# differ between the two DBs (id drift) but select the same logical rows.
def build_anchor(anchor: tuple) -> tuple[str, str | None, str | None, str | None]:
    """Map an ``ANCHORS`` entry to ``(kind, spine, anchor_col, epoch_expr)``."""
    kind = anchor[0]
    if kind == "self_block":
        return "idrange", "block", "id", None
    if kind == "block_fk":
        return "idrange", "block", anchor[1], None
    if kind == "tx_fk":
        return "idrange", "tx", anchor[1], None
    if kind == "tx_fk_via_txout":
        return "idrange", "tx_out", anchor[1], None
    if kind == "pool_update_fk":
        return "idrange", "pool_update", anchor[1], None
    if kind == "gap_fk":
        return "idrange", "gov_action_proposal", anchor[1], None
    if kind == "epoch":
        return "epoch", None, None, f"t0.{quote_ident(anchor[1])}"
    return "none", None, None, None  # accumulator


def bound_predicate(plan: TablePlan, ranges: dict, cutoff_epoch: int, in_block_range: bool) -> str:
    """WHERE predicate for ONE database, using that database's spine id-ranges."""
    if plan.anchor_kind == "idrange":
        r = ranges.get(plan.spine)
        if r is None or r[0] is None:
            return "FALSE"  # no rows in the window
        lo, hi = r
        assert plan.anchor_col is not None  # always set when anchor_kind == "idrange"
        return f"t0.{quote_ident(plan.anchor_col)} BETWEEN {lo} AND {hi}"
    if plan.anchor_kind == "epoch":
        if in_block_range:
            return "FALSE"  # epoch tables out of scope for a block window
        return f"{plan.epoch_expr} <= {cutoff_epoch}"
    # accumulator: full-table compare in cutoff mode; out of scope for a window
    return "FALSE" if in_block_range else "TRUE"


def hash_sql(plan: TablePlan, predicate: str) -> str:
    """The set-hash query (count + two summed md5 halves) for one table."""
    row = "md5(ROW(" + ", ".join(plan.select_exprs) + ")::text) AS h" if plan.select_exprs else "md5('')::text AS h"
    return (
        f"SELECT {SETHASH_SELECT} FROM (\n"
        f"  SELECT {row}\n"
        f"  FROM {quote_ident(plan.name)} t0\n"
        f"  {plan.joins}\n"
        f"  WHERE {predicate}\n"
        f") q"
    )


def hash_sql_bucketed(plan: TablePlan, predicate: str, thresholds: list[int]) -> str:
    """Like :func:`hash_sql`, but computes the set-hash **per bucket** in a single
    pass: each row is assigned to a bucket via ``width_bucket(anchor_col, thresholds)``
    (the per-DB id boundaries from :func:`ranges.bucket_boundary_ids`), then grouped.

    Reuses the exact same normalized row hash, joins and predicate as ``hash_sql``;
    the only addition is the bucket key and a ``GROUP BY``. One scan replaces the
    many re-scans of the bisection localizer. See ``docs/07``.
    """
    assert plan.anchor_col is not None  # bucketing is only for idrange tables
    arr = "ARRAY[" + ",".join(str(int(t)) for t in thresholds) + "]::bigint[]"
    bkt = f"width_bucket(t0.{quote_ident(plan.anchor_col)}, {arr})"
    row = "md5(ROW(" + ", ".join(plan.select_exprs) + ")::text) AS h" if plan.select_exprs else "md5('')::text AS h"
    return (
        f"SELECT bkt, {SETHASH_SELECT} FROM (\n"
        f"  SELECT {bkt} AS bkt, {row}\n"
        f"  FROM {quote_ident(plan.name)} t0\n"
        f"  {plan.joins}\n"
        f"  WHERE {predicate}\n"
        f") q GROUP BY bkt"
    )


def value_sql(plan: TablePlan, predicate: str) -> str | None:
    """The cheap numeric proof (sum/min/max) for a giant table, or ``None``."""
    if not plan.value_col:
        return None
    v = f"t0.{quote_ident(plan.value_col)}::numeric"
    return (
        f"SELECT coalesce(sum({v}),0), coalesce(min({v}),0), coalesce(max({v}),0)\n"
        f"FROM {quote_ident(plan.name)} t0\n  {plan.joins}\n  WHERE {predicate}"
    )
