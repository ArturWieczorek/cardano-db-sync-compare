"""Tests for the logical-FK resolver and registry invariants."""

from __future__ import annotations

from db_sync_comparator.registries import (
    EXCLUDED_TABLES,
    FK_MAP,
    GIANT_TABLES,
    GLOBAL_FK,
    NATURAL_KEYS,
    looks_like_fk,
    resolve_fk,
)


def test_resolve_fk_map_overrides_global():
    # tx_out_id is ambiguous: tx in tx_in, but tx_out in ma_tx_out.
    assert resolve_fk("tx_in", "tx_out_id") == "tx"
    assert resolve_fk("ma_tx_out", "tx_out_id") == "tx_out"
    # hash_id means different targets in different tables.
    assert resolve_fk("pool_retire", "hash_id") == "pool_hash"
    assert resolve_fk("drep_distr", "hash_id") == "drep_hash"


def test_resolve_fk_global_and_unknown():
    assert resolve_fk("anytable", "tx_id") == "tx"
    assert resolve_fk("anytable", "block_id") == "block"
    # irregular names with no _id suffix are still mapped
    assert resolve_fk("voting_procedure", "drep_voter") == "drep_hash"
    assert resolve_fk("gov_action_proposal", "return_address") == "stake_address"
    assert resolve_fk("anytable", "not_a_real_column") is None


def test_looks_like_fk():
    assert looks_like_fk("addr_id")
    assert looks_like_fk("some_new_id")
    assert not looks_like_fk("id")  # the surrogate PK is handled separately
    assert not looks_like_fk("view")
    assert not looks_like_fk("drep_voter")  # mapped explicitly, no _id suffix


def test_natural_key_fk_targets_are_resolvable():
    """Every ("fk", col, target) inside a natural key must itself have a natural
    key, otherwise translation chains can't terminate."""
    for table, spec in NATURAL_KEYS.items():
        for part in spec:
            if part[0] == "fk":
                _, _col, target = part
                assert target in NATURAL_KEYS, f"{table}: fk target {target!r} has no NATURAL_KEYS entry"


def test_fk_targets_have_natural_keys():
    """Every table a logical FK points at must have a natural key (so its rows
    can be translated)."""
    for target in set(GLOBAL_FK.values()) | set(FK_MAP.values()):
        if target in EXCLUDED_TABLES:
            continue  # off_chain_vote_data is excluded; never actually translated
        assert target in NATURAL_KEYS, f"FK target {target!r} has no NATURAL_KEYS entry"


def test_giant_and_excluded_are_disjoint():
    assert not (GIANT_TABLES & set(EXCLUDED_TABLES))


def test_new_committee_anchored_via_gov_action_proposal():
    # Regression: new_committee has no epoch_no column (it has
    # gov_action_proposal_id), so it must be anchored like committee, not by epoch.
    from db_sync_comparator.registries import ANCHORS
    from db_sync_comparator.sql import build_anchor

    assert ANCHORS["new_committee"] == ("gap_fk", "gov_action_proposal_id")
    kind, spine, col, _ = build_anchor(ANCHORS["new_committee"])
    assert (kind, spine, col) == ("idrange", "gov_action_proposal", "gov_action_proposal_id")
