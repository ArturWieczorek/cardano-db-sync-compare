"""Unit tests for the bucket-localization pure logic (no DB)."""

from __future__ import annotations

from db_sync_comparator.compare import align_bucket_boundaries
from db_sync_comparator.model import TablePlan
from db_sync_comparator.ranges import block_edges
from db_sync_comparator.sql import hash_sql_bucketed


def test_block_edges_small_chain():
    assert block_edges(5, 1024) == [0, 1, 2, 3, 4, 5]


def test_block_edges_large_chain():
    e = block_edges(13_313_031, 1024)
    assert e[0] == 0
    assert e[-1] == 13_313_031  # last edge is exactly the cutoff
    assert all(e[i] < e[i + 1] for i in range(len(e) - 1))  # strictly increasing
    assert 1000 <= len(e) <= 1100


def test_hash_sql_bucketed_shape():
    p = TablePlan(name="tx_out", kind="giant", anchor_col="tx_id", select_exprs=['t0."value"'])
    sql = hash_sql_bucketed(p, "TRUE", [10, 20, 30])
    assert 'width_bucket(t0."tx_id", ARRAY[10,20,30]::bigint[])' in sql
    assert "GROUP BY bkt" in sql
    assert 'md5(ROW(t0."value")::text) AS h' in sql
    assert "count(*)" in sql  # reuses SETHASH_SELECT


def test_align_drops_nulls_and_enforces_increasing_in_both():
    edges = [0, 1, 2, 3]
    b1 = [10, None, 20, 30]
    b2 = [100, None, 200, 200]  # e=3 not increasing in db2
    ke, t1, t2 = align_bucket_boundaries(edges, b1, b2)
    assert ke == [0, 2]
    assert t1 == [10, 20]
    assert t2 == [100, 200]


def test_align_enforces_increasing_in_db1_too():
    ke, t1, t2 = align_bucket_boundaries([0, 1, 2], [10, 5, 20], [100, 200, 300])
    assert ke == [0, 2]
    assert t1 == [10, 20] and t2 == [100, 300]


def test_align_all_aligned():
    ke, t1, t2 = align_bucket_boundaries([0, 1, 2], [1, 2, 3], [10, 20, 30])
    assert ke == [0, 1, 2] and t1 == [1, 2, 3] and t2 == [10, 20, 30]
