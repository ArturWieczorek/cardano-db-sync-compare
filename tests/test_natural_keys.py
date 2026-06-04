"""Tests for FK-to-natural-key expansion, including depth limiting and chains."""

from __future__ import annotations

from db_sync_comparator.sql import JoinBuilder, natural_key_exprs


def test_plain_column_key():
    jb = JoinBuilder()
    skipped: list[str] = []
    out = natural_key_exprs("block", "j1", jb, {"block": {"hash"}}, 1, 8, skipped)
    assert out == ['j1."hash"']
    assert skipped == []


def test_fk_chain_tx_out_to_tx():
    jb = JoinBuilder()
    skipped: list[str] = []
    out = natural_key_exprs("tx_out", "t0", jb, {"tx_out": {"index"}, "tx": {"hash"}}, 1, 8, skipped)
    # tx_out natural key = (tx.hash, tx_out.index) -> two expressions, one join
    assert out is not None and len(out) == 2
    assert any("hash" in e for e in out)
    assert any('"index"' in e for e in out)
    assert jb.clauses().count("LEFT JOIN") == 1
    assert skipped == []


def test_depth_limit_returns_none_and_records_skip():
    jb = JoinBuilder()
    skipped: list[str] = []
    # tx_out needs to follow tx_id (an fk) but max_depth=1 forbids it
    out = natural_key_exprs("tx_out", "t0", jb, {"tx_out": {"index"}, "tx": {"hash"}}, 1, 1, skipped)
    assert out is None
    assert skipped and "tx_out.tx_id->tx" in skipped[0]


def test_unregistered_target_returns_none():
    jb = JoinBuilder()
    assert natural_key_exprs("no_such_table", "t0", jb, {}, 1, 8, []) is None


def test_schema_drift_drops_missing_column():
    # if the target's natural-key column isn't shared by both DBs, it's omitted
    jb = JoinBuilder()
    out = natural_key_exprs("block", "j1", jb, {"block": set()}, 1, 8, [])
    assert out == []


def test_multi_part_natural_key():
    jb = JoinBuilder()
    out = natural_key_exprs("multi_asset", "j1", jb, {"multi_asset": {"policy", "name"}}, 1, 8, [])
    assert out is not None and len(out) == 2
    assert 'j1."policy"' in out and 'j1."name"' in out
