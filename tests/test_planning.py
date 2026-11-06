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


def test_epoch_finalized_epoch_anchored_with_value_proof(make_schema):
    """db-sync 13.7.2.1: `epoch_finalized` is a base table with an epoch-number
    column `no` and an `out_sum` value column. It must be epoch-anchored (like the
    old `epoch` table) rather than fall through to the accumulator default, and get
    a value proof on `out_sum` (domain word128type reports base type numeric)."""
    schemas = {
        "epoch_finalized": make_schema(
            "epoch_finalized",
            ["id", "out_sum", "fees", "tx_count", "blk_count", "no", "start_time", "end_time"],
            coltypes={"out_sum": "numeric", "fees": "numeric", "no": "integer"},
        )
    }
    common = _common(schemas)
    p = plan_table(
        "epoch_finalized",
        schemas["epoch_finalized"],
        schemas["epoch_finalized"],
        common,
        full=False,
        giant_fk_depth=1,
    )
    assert p.anchor_kind == "epoch"  # NOT accumulator
    assert p.epoch_expr == 't0."no"'
    assert p.value_col == "out_sum"
    # surrogate id never hashed; the epoch number `no` is hashed as plain data
    assert not any(e == 't0."id"' for e in p.select_exprs)
    assert any(e == 't0."no"' for e in p.select_exprs)


def test_address_variant_output_translates_address_id(make_schema):
    """Address (use_address_table) variant: collateral_tx_out has address_id (a
    FK to the address table) instead of an inline address column. It must be
    translated to the address's natural key (raw), not dropped as UNMAPPED."""
    schemas = {
        "collateral_tx_out": make_schema(
            "collateral_tx_out",
            ["id", "tx_id", "index", "address_id", "value"],
            coltypes={"value": "numeric"},
        ),
        "tx": make_schema("tx", ["id", "hash"]),
        "address": make_schema("address", ["id", "address", "raw", "stake_address_id"]),
        "stake_address": make_schema("stake_address", ["id", "hash_raw"]),
    }
    common = _common(schemas)
    p = plan_table(
        "collateral_tx_out",
        schemas["collateral_tx_out"],
        schemas["collateral_tx_out"],
        common,
        full=False,
        giant_fk_depth=1,
    )
    assert not any("address_id" in s for s in p.skipped_cols)  # not UNMAPPED
    assert any('"raw"' in e for e in p.select_exprs)  # translated to address.raw


def test_address_table_classified_accumulator(make_schema):
    schemas = {
        "address": make_schema("address", ["id", "address", "raw", "has_script", "payment_cred", "stake_address_id"]),
        "stake_address": make_schema("stake_address", ["id", "hash_raw"]),
    }
    common = _common(schemas)
    p = plan_table("address", schemas["address"], schemas["address"], common, full=False, giant_fk_depth=1)
    assert p.kind == "accumulator"
    assert p.anchor_kind == "none"
    # its own content is still fingerprinted (raw/address/payment_cred), id dropped
    assert not any(e == 't0."id"' for e in p.select_exprs)
    assert any('"raw"' in e for e in p.select_exprs)


def _gov_schemas(make_schema) -> dict:
    return {
        "gov_action_proposal": make_schema(
            "gov_action_proposal",
            ["id", "tx_id", "index", "deposit", "ratified_epoch", "enacted_epoch", "dropped_epoch", "expired_epoch"],
        ),
        "tx": make_schema("tx", ["id", "hash"]),
    }


def test_lifecycle_epochs_are_capped_at_the_cutoff_epoch(make_schema):
    schemas = _gov_schemas(make_schema)
    s = schemas["gov_action_proposal"]
    p = plan_table("gov_action_proposal", s, s, _common(schemas), False, 1, lifecycle_cutoff_epoch=300)
    for col in ("dropped_epoch", "enacted_epoch", "expired_epoch", "ratified_epoch"):
        assert f'CASE WHEN t0."{col}" <= 300 THEN t0."{col}" END' in p.select_exprs
        assert f't0."{col}"' not in p.select_exprs
    assert 't0."deposit"' in p.select_exprs  # other columns untouched
    assert p.epoch_capped_cols == ["dropped_epoch", "enacted_epoch", "expired_epoch", "ratified_epoch"]


def test_lifecycle_epochs_are_raw_without_a_cutoff(make_schema):
    # The default keeps today's exact projection (used by existing callers).
    schemas = _gov_schemas(make_schema)
    s = schemas["gov_action_proposal"]
    p = plan_table("gov_action_proposal", s, s, _common(schemas), False, 1)
    assert 't0."ratified_epoch"' in p.select_exprs
    assert p.epoch_capped_cols == []


def test_lifecycle_cutoff_does_not_touch_other_tables(make_schema):
    schemas = {
        "withdrawal": make_schema("withdrawal", ["id", "addr_id", "amount", "tx_id"], coltypes={"amount": "numeric"}),
        "stake_address": make_schema("stake_address", ["id", "hash_raw"]),
        "tx": make_schema("tx", ["id", "hash"]),
    }
    s = schemas["withdrawal"]
    with_cap = plan_table("withdrawal", s, s, _common(schemas), False, 1, lifecycle_cutoff_epoch=5)
    without = plan_table("withdrawal", s, s, _common(schemas), False, 1)
    assert with_cap.select_exprs == without.select_exprs
    assert with_cap.epoch_capped_cols == []


def _committee_schemas(make_schema, with_epoch_state: bool = True) -> dict:
    schemas = {
        "committee": make_schema(
            "committee", ["id", "gov_action_proposal_id", "quorum_numerator", "quorum_denominator"]
        ),
        "gov_action_proposal": make_schema("gov_action_proposal", ["id", "tx_id", "index"]),
        "tx": make_schema("tx", ["id", "hash"]),
    }
    if with_epoch_state:
        schemas["epoch_state"] = make_schema("epoch_state", ["id", "committee_id", "constitution_id", "epoch_no"])
    return schemas


def test_genesis_committee_bound_is_set_when_epoch_state_exists(make_schema):
    schemas = _committee_schemas(make_schema)
    s = schemas["committee"]
    p = plan_table("committee", s, s, _common(schemas), False, 1)
    assert p.null_anchor == ("epoch_state", "committee_id", "epoch_no")


def test_genesis_committee_bound_is_off_without_epoch_state(make_schema):
    # Older schemas without epoch_state keep today's predicate exactly.
    schemas = _committee_schemas(make_schema, with_epoch_state=False)
    s = schemas["committee"]
    p = plan_table("committee", s, s, _common(schemas), False, 1)
    assert p.null_anchor is None


def test_null_anchor_bound_only_for_registered_tables(make_schema):
    schemas = _gov_schemas(make_schema)
    s = schemas["gov_action_proposal"]
    assert plan_table("gov_action_proposal", s, s, _common(schemas), False, 1).null_anchor is None
