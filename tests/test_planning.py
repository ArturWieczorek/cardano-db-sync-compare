"""Tests for plan_table: classification, id/FK handling, tiering, value column."""

from __future__ import annotations

from db_sync_comparator.planning import plan_table


def _common(schemas: dict) -> dict[str, set[str]]:
    """Build the common-columns map assuming both DBs share each schema's columns."""
    return {name: set(s.columns) for name, s in schemas.items()}


def test_excluded_table(make_schema):
    s = make_schema("meta", ["id", "version"])
    p = plan_table("meta", s, s, {"meta": {"id", "version"}}, full=False, giant_fk_depth=1)
    assert p.kind == "excluded"


def test_normal_table_drops_id_translates_fks_picks_value(make_schema):
    schemas = {
        "withdrawal": make_schema(
            "withdrawal",
            ["id", "addr_id", "amount", "redeemer_id", "tx_id"],
            coltypes={"amount": "numeric"},
        ),
        "stake_address": make_schema("stake_address", ["id", "hash_raw"]),
        "tx": make_schema("tx", ["id", "hash"]),
        "redeemer": make_schema("redeemer", ["id", "tx_id", "purpose", "index"]),
    }
    common = _common(schemas)
    p = plan_table("withdrawal", schemas["withdrawal"], schemas["withdrawal"], common, full=False, giant_fk_depth=1)

    assert p.kind == "normal"
    assert p.anchor_kind == "idrange" and p.spine == "tx" and p.anchor_col == "tx_id"
    # addr_id->hash_raw (1) + amount (1) + redeemer_id->(tx.hash,purpose,index) (3) + tx_id->hash (1) = 6
    assert len(p.select_exprs) == 6
    # surrogate id never hashed
    assert not any(e == 't0."id"' for e in p.select_exprs)
    assert p.value_col == "amount"


def test_generated_column_is_dropped(make_schema):
    schemas = {
        "withdrawal": make_schema(
            "withdrawal",
            ["id", "amount", "tx_id", "gencol"],
            coltypes={"amount": "numeric"},
            generated={"gencol"},
        ),
        "tx": make_schema("tx", ["id", "hash"]),
    }
    common = _common(schemas)
    p = plan_table("withdrawal", schemas["withdrawal"], schemas["withdrawal"], common, full=False, giant_fk_depth=1)
    assert not any("gencol" in e for e in p.select_exprs)


def test_unmapped_fk_is_flagged_not_hashed(make_schema):
    schemas = {
        "withdrawal": make_schema("withdrawal", ["id", "tx_id", "mystery_id"]),
        "tx": make_schema("tx", ["id", "hash"]),
    }
    common = _common(schemas)
    p = plan_table("withdrawal", schemas["withdrawal"], schemas["withdrawal"], common, full=False, giant_fk_depth=1)
    assert any("mystery_id" in s and "UNMAPPED" in s for s in p.skipped_cols)
    assert not any("mystery_id" in e for e in p.select_exprs)


def test_giant_tiering_drops_deep_fk_but_full_includes_it(make_schema):
    schemas = {
        "ma_tx_out": make_schema(
            "ma_tx_out", ["id", "ident", "quantity", "tx_out_id"], coltypes={"quantity": "numeric"}
        ),
        "multi_asset": make_schema("multi_asset", ["id", "policy", "name"]),
        "tx_out": make_schema("tx_out", ["id", "tx_id", "index"]),
        "tx": make_schema("tx", ["id", "hash"]),
    }
    common = _common(schemas)

    tiered = plan_table("ma_tx_out", schemas["ma_tx_out"], schemas["ma_tx_out"], common, full=False, giant_fk_depth=1)
    assert tiered.kind == "giant"
    # ident->(policy,name) + quantity = 3; tx_out_id dropped (depth budget)
    assert len(tiered.select_exprs) == 3
    assert any("tx_out_id" in s for s in tiered.skipped_cols)
    assert tiered.value_col == "quantity"

    full = plan_table("ma_tx_out", schemas["ma_tx_out"], schemas["ma_tx_out"], common, full=True, giant_fk_depth=1)
    assert full.kind == "normal"
    # now tx_out_id resolves to (tx.hash, index): ident(2) + quantity(1) + (hash,index)(2) = 5
    assert len(full.select_exprs) == 5


def test_value_col_skips_jsonb(make_schema):
    """Regression: datum.value / redeemer_data.value are jsonb and must never be
    chosen for the numeric sum/min/max proof."""
    schemas = {
        "datum": make_schema(
            "datum", ["id", "hash", "tx_id", "value", "bytes"], coltypes={"value": "jsonb", "bytes": "bytea"}
        ),
        "tx": make_schema("tx", ["id", "hash"]),
    }
    common = _common(schemas)
    p = plan_table("datum", schemas["datum"], schemas["datum"], common, full=False, giant_fk_depth=1)
    assert p.value_col is None


def test_accumulator_classification(make_schema):
    schemas = {"multi_asset": make_schema("multi_asset", ["id", "policy", "name", "fingerprint"])}
    common = _common(schemas)
    p = plan_table("multi_asset", schemas["multi_asset"], schemas["multi_asset"], common, full=False, giant_fk_depth=1)
    assert p.kind == "accumulator"
    assert p.anchor_kind == "none"
