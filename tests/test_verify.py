"""Tests for the accumulator subset-verification logic (pure parts)."""

from __future__ import annotations

from db_sync_comparator.verify import accumulator_key_sql, key_query, merge_compare


def test_accumulator_key_sql_plain_columns():
    sql = accumulator_key_sql("multi_asset")  # natural key (policy, name)
    assert sql is not None
    assert '"policy"::text' in sql and '"name"::text' in sql
    assert "chr(31)" in sql  # multi-column separator


def test_accumulator_key_sql_single_column():
    assert accumulator_key_sql("stake_address") == "coalesce(\"hash_raw\"::text, '')"


def test_accumulator_key_sql_address():
    # Address (use_address_table) variant: the address table's natural key is the
    # raw address bytes, so --verify-accumulators can subset-check it.
    assert accumulator_key_sql("address") == "coalesce(\"raw\"::text, '')"


def test_accumulator_key_sql_none_when_no_natural_key():
    # committee_member is an accumulator but has no registered natural key
    assert accumulator_key_sql("committee_member") is None


def test_key_query_orders_with_c_collation():
    q = key_query("stake_address")
    assert q is not None
    assert "ORDER BY" in q and 'COLLATE "C"' in q and 'FROM "stake_address"' in q


def test_merge_compare_clean_superset():
    # db2 has everything db1 has, plus two extras → db1 ⊆ db2
    r = merge_compare(iter(["a", "c"]), iter(["a", "b", "c", "d"]))
    assert r["both"] == 2
    assert r["only_db1"] == 0
    assert r["only_db2"] == 2
    assert r["examples_db2"] == ["b", "d"]


def test_merge_compare_neither_subset():
    r = merge_compare(iter(["a", "b", "x"]), iter(["a", "c", "x"]))
    assert r["both"] == 2  # a, x
    assert r["only_db1"] == 1 and r["examples_db1"] == ["b"]
    assert r["only_db2"] == 1 and r["examples_db2"] == ["c"]


def test_merge_compare_identical():
    r = merge_compare(iter(["a", "b", "c"]), iter(["a", "b", "c"]))
    assert (r["only_db1"], r["only_db2"], r["both"]) == (0, 0, 3)


def test_merge_compare_one_side_empty():
    r = merge_compare(iter([]), iter(["a", "b"]))
    assert (r["only_db1"], r["only_db2"], r["both"]) == (0, 2, 0)
