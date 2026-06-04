"""Turn two table schemas into a :class:`~db_sync_comparator.model.TablePlan`.

This is where the comparison rules from the primers are applied per table: drop
the surrogate ``id`` and GENERATED columns, translate foreign keys to natural
keys (or flag unmapped ones), classify the table (excluded / giant / accumulator
/ normal), pick its chain anchor, and choose a numeric value column for the cheap
proof.
"""

from __future__ import annotations

from db_sync_comparator.model import TablePlan, TableSchema
from db_sync_comparator.registries import (
    ANCHORS,
    EXCLUDED_TABLES,
    GIANT_TABLES,
    NUMERIC_TYPES,
    VALUE_COLUMNS,
    looks_like_fk,
    resolve_fk,
)
from db_sync_comparator.sql import JoinBuilder, build_anchor, natural_key_exprs, quote_ident

# FK-resolution depth budget for normal (non-giant) tables — effectively
# unbounded for the shallow db-sync schema, but a guard against pathological
# chains.
_NORMAL_MAX_DEPTH = 8


def plan_table(
    name: str,
    s1: TableSchema,
    s2: TableSchema,
    common_cols: dict[str, set[str]],
    full: bool,
    giant_fk_depth: int,
) -> TablePlan:
    """Produce the comparison plan (normalized SQL pieces) for one table.

    ``common_cols`` maps every table to the set of columns the two databases
    share; it is needed not just for ``name`` but for the FK-target tables whose
    natural keys are spliced in (schema-drift safety).
    """
    if name in EXCLUDED_TABLES:
        return TablePlan(name, "excluded", EXCLUDED_TABLES[name])

    common = common_cols.get(name, set(s1.columns) & set(s2.columns))
    only1 = sorted(set(s1.columns) - set(s2.columns))
    only2 = sorted(set(s2.columns) - set(s1.columns))

    is_giant = name in GIANT_TABLES and not full
    kind = "giant" if is_giant else "normal"
    max_depth = giant_fk_depth if is_giant else _NORMAL_MAX_DEPTH

    # Unknown tables default to accumulator (full-table compare) — a safe choice.
    anchor = ANCHORS.get(name, ("accumulator",))
    if anchor[0] == "accumulator" and not is_giant:
        kind = "accumulator"

    anchor_kind, spine, anchor_col, epoch_expr = build_anchor(anchor)

    jb = JoinBuilder()
    skipped: list[str] = []
    exprs: list[str] = []
    # Stable, version-independent column order: sort by name.
    for col in sorted(common):
        if col == "id":  # surrogate PK — always drifts
            continue
        if col in s1.generated or col in s2.generated:  # e.g. reward.earned_epoch
            continue
        ref_table = resolve_fk(name, col)
        if ref_table is not None:
            # Replace the drifting *_id with the referenced row's natural key.
            sub = natural_key_exprs(ref_table, jb.join("t0", col, ref_table), jb, common_cols, 1, max_depth, skipped)
            if sub is None:
                # over depth budget (tiered giants) or target has no registered
                # natural key: drop the column rather than hash a drifting id.
                marker = f"{col}->{ref_table}"
                if marker not in skipped:
                    skipped.append(marker)
                continue
            exprs.extend(sub)
        elif looks_like_fk(col):
            # Unmapped logical FK: never hash a raw id. Exclude + flag loudly so
            # the registry can be extended.
            skipped.append(f"{col} (UNMAPPED FK -> excluded)")
        else:
            exprs.append(f"t0.{quote_ident(col)}")

    plan = TablePlan(
        name=name,
        kind=kind,
        select_exprs=exprs,
        joins=jb.clauses(),
        anchor_kind=anchor_kind,
        spine=spine,
        anchor_col=anchor_col,
        epoch_expr=epoch_expr,
        skipped_cols=skipped,
        extra_cols={"only_db1": only1, "only_db2": only2} if (only1 or only2) else {},
    )
    for vc in VALUE_COLUMNS:
        # must be a real numeric column (datum.value / redeemer_data.value are jsonb)
        if (
            vc in common
            and resolve_fk(name, vc) is None
            and s1.coltypes.get(vc) in NUMERIC_TYPES
            and s2.coltypes.get(vc) in NUMERIC_TYPES
        ):
            plan.value_col = vc
            break
    return plan
