"""Tests for pure SQL generation: quoting, joins, set-hash, bounds."""

from __future__ import annotations

from db_sync_comparator.model import TablePlan
from db_sync_comparator.sql import (
    SETHASH_SELECT,
    JoinBuilder,
    bound_predicate,
    build_anchor,
    epoch_capped_expr,
    hash_sql,
    quote_ident,
    value_sql,
)


def test_quote_ident_escapes_quotes():
    assert quote_ident("tx") == '"tx"'
    assert quote_ident('a"b') == '"a""b"'


def test_joinbuilder_is_idempotent_and_stable():
    jb = JoinBuilder()
    a = jb.join("t0", "tx_id", "tx")
    again = jb.join("t0", "tx_id", "tx")
    other = jb.join("t0", "block_id", "block")
    assert a == "j1" and again == "j1"  # same request -> same alias
    assert other == "j2"
    # only two distinct joins emitted
    assert jb.clauses().count("LEFT JOIN") == 2
    assert 'LEFT JOIN "tx" j1 ON t0."tx_id" = j1."id"' in jb.clauses()


def test_sethash_select_shape():
    assert "count(*)" in SETHASH_SELECT
    assert SETHASH_SELECT.count("sum(") == 2  # two summed digest halves


def test_build_anchor_kinds():
    assert build_anchor(("self_block",)) == ("idrange", "block", "id", None)
    assert build_anchor(("tx_fk", "tx_id")) == ("idrange", "tx", "tx_id", None)
    assert build_anchor(("tx_fk_via_txout", "tx_out_id")) == ("idrange", "tx_out", "tx_out_id", None)
    assert build_anchor(("accumulator",)) == ("none", None, None, None)
    kind, spine, col, expr = build_anchor(("epoch", "epoch_no"))
    assert kind == "epoch" and spine is None and col is None and expr == 't0."epoch_no"'


def _idrange_plan() -> TablePlan:
    return TablePlan(name="tx_out", kind="normal", anchor_kind="idrange", spine="tx", anchor_col="tx_id")


def test_bound_predicate_idrange():
    p = _idrange_plan()
    assert bound_predicate(p, {"tx": (10, 20)}, 0, False) == 't0."tx_id" BETWEEN 10 AND 20'
    # empty window or missing spine -> FALSE (selects nothing)
    assert bound_predicate(p, {"tx": (None, None)}, 0, False) == "FALSE"
    assert bound_predicate(p, {}, 0, False) == "FALSE"


def test_bound_predicate_epoch_and_accumulator():
    ep = TablePlan(name="epoch_stake", kind="normal", anchor_kind="epoch", epoch_expr='t0."epoch_no"')
    assert bound_predicate(ep, {}, 5, False) == 't0."epoch_no" <= 5'
    assert bound_predicate(ep, {}, 5, True) == "FALSE"  # out of scope in a block window
    acc = TablePlan(name="multi_asset", kind="accumulator", anchor_kind="none")
    assert bound_predicate(acc, {}, 5, False) == "TRUE"
    assert bound_predicate(acc, {}, 5, True) == "FALSE"


def test_hash_sql_shape():
    p = TablePlan(name="mytable", kind="normal", select_exprs=['t0."x"', 'j1."hash"'])
    sql = hash_sql(p, "TRUE")
    assert 'md5(ROW(t0."x", j1."hash")::text) AS h' in sql
    assert 'FROM "mytable" t0' in sql
    assert "WHERE TRUE" in sql
    # empty projection still produces a valid query
    empty = hash_sql(TablePlan(name="t", kind="normal"), "FALSE")
    assert "md5('')::text AS h" in empty


def test_value_sql():
    assert value_sql(TablePlan(name="t", kind="normal"), "TRUE") is None
    p = TablePlan(name="tx_out", kind="giant", value_col="value")
    sql = value_sql(p, "TRUE")
    assert sql is not None
    assert 'sum(t0."value"::numeric)' in sql


def test_epoch_capped_expr_hides_values_above_the_cutoff_epoch():
    # A value at or below the cutoff is kept as is; anything above becomes NULL.
    assert epoch_capped_expr("dropped_epoch", 300) == 'CASE WHEN t0."dropped_epoch" <= 300 THEN t0."dropped_epoch" END'


def _committee_plan(null_anchor=("epoch_state", "committee_id", "epoch_no")) -> TablePlan:
    return TablePlan(
        "committee",
        "normal",
        select_exprs=['t0."quorum_numerator"'],
        anchor_kind="idrange",
        spine="gov_action_proposal",
        anchor_col="gov_action_proposal_id",
        null_anchor=null_anchor,
    )


_GENESIS_COMMITTEE = (
    '(t0."gov_action_proposal_id" IS NULL AND EXISTS (SELECT 1 FROM "epoch_state" na '
    'WHERE na."committee_id" = t0."id" AND na."epoch_no" <= 300))'
)


def test_bound_predicate_is_unchanged_unless_null_anchored_rows_are_asked_for():
    ranges = {"gov_action_proposal": (1, 9)}
    old = 't0."gov_action_proposal_id" BETWEEN 1 AND 9'
    assert bound_predicate(_committee_plan(), ranges, 300, False) == old
    # asked for, but the table has no NULL-anchor bound registered
    assert bound_predicate(_committee_plan(None), ranges, 300, False, include_null_anchor=True) == old
    # asked for, but in --block-range mode (rows with no chain position can't be windowed)
    assert bound_predicate(_committee_plan(), ranges, 300, True, include_null_anchor=True) == old


def test_bound_predicate_adds_null_anchored_rows_bounded_by_epoch_state():
    ranges = {"gov_action_proposal": (1, 9)}
    got = bound_predicate(_committee_plan(), ranges, 300, False, include_null_anchor=True)
    assert got == f'(t0."gov_action_proposal_id" BETWEEN 1 AND 9 OR {_GENESIS_COMMITTEE})'
    # an empty id window still lets the genesis row in
    empty = bound_predicate(
        _committee_plan(), {"gov_action_proposal": (None, None)}, 300, False, include_null_anchor=True
    )
    assert empty == f"(FALSE OR {_GENESIS_COMMITTEE})"
