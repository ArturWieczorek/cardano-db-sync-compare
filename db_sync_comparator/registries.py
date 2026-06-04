"""Hand-built cardano-db-sync schema knowledge.

This is the "very detailed schema analysis": facts about the db-sync schema that
cannot be introspected from the database and must be encoded by hand —

* which tables are *not* a deterministic function of the chain (so we skip them),
* which tables are huge enough to deserve cheaper (tiered) checks,
* the version-stable **natural key** of every table other tables point at,
* the **logical foreign keys** (db-sync declares none in PostgreSQL), and
* how each table is **anchored** to a chain coordinate so it can be bounded to a
  common tip.

Authoritative source: ``cardano-db/src/Cardano/Db/Schema/`` in the db-sync repo.
See ``docs/06-how-each-table-is-compared.md`` for the prose explanation.
"""

from __future__ import annotations

# Tables excluded from data comparison by default and *why*. These are either
# per-instance bookkeeping or network-dependent and so legitimately differ
# between two syncs of the same chain.
EXCLUDED_TABLES: dict[str, str] = {
    "schema_version": "per-instance schema bookkeeping",
    "schema_migrations": "per-instance migration log",
    "extra_migrations": "per-instance migration log",
    "meta": "records this sync's version/start_time — differs by definition",
    "epoch_sync_time": "wall-clock sync duration per epoch — non-deterministic",
    "reverse_index": "near-tip rollback helper; volatile, id-encoded payload",
    # Off-chain fetchers hit the network: which URLs resolved, when, and the
    # fetched bytes are not a function of the chain. Disabled by default in
    # db-sync >=13.7.0.3 anyway. Compare these out-of-band if needed.
    "off_chain_pool_data": "network-fetched metadata; non-deterministic",
    "off_chain_pool_fetch_error": "network-fetch failures; non-deterministic",
    "off_chain_vote_data": "network-fetched metadata; non-deterministic",
    "off_chain_vote_author": "derived from off_chain_vote_data",
    "off_chain_vote_drep_data": "derived from off_chain_vote_data",
    "off_chain_vote_external_update": "derived from off_chain_vote_data",
    "off_chain_vote_gov_action_data": "derived from off_chain_vote_data",
    "off_chain_vote_reference": "derived from off_chain_vote_data",
    "off_chain_vote_fetch_error": "network-fetch failures; non-deterministic",
    # SMASH operator state, not chain data:
    "delisted_pool": "SMASH operator state, not chain-derived",
    "reserved_pool_ticker": "SMASH operator state, not chain-derived",
}

# Tables big enough that a full per-column deep-join hash is expensive. In the
# default (tiered) mode these get cheap aggregates + a shallow hash; `--full`
# forces a complete normalized hash here too.
GIANT_TABLES: set[str] = {
    "ma_tx_out",
    "epoch_stake",
    "reward",
    "reward_rest",
    "tx_out",
    "tx_in",
    "tx_metadata",
    "tx",
    "redeemer",
    "extra_key_witness",
    "datum",
    "reference_tx_in",
    "collateral_tx_in",
}

# db-sync does NOT create referential FK constraints in PostgreSQL (it would
# cripple insert/rollback performance), so foreign keys cannot be introspected
# from information_schema. They are logical, by column name — and the names are
# irregular (the Hasql rewrite dropped the `_id` suffix on several: drep_voter,
# return_address, param_proposal, prev_gov_action_proposal, invalid). We
# therefore map them explicitly. GLOBAL_FK is column-name -> target for names
# that mean the same thing everywhere; FK_MAP overrides per (table, column) for
# the ambiguous ones (e.g. tx_out_id means tx in tx_in but tx_out in ma_tx_out;
# hash_id means pool_hash or drep_hash depending on the table).
GLOBAL_FK: dict[str, str] = {
    "block_id": "block",
    "previous_id": "block",
    "tx_id": "tx",
    "registered_tx_id": "tx",
    "announced_tx_id": "tx",
    "tx_in_id": "tx",
    "consumed_by_tx_id": "tx",
    "addr_id": "stake_address",
    "stake_address_id": "stake_address",
    "reward_addr_id": "stake_address",
    "return_address": "stake_address",
    "voting_anchor_id": "voting_anchor",
    "redeemer_id": "redeemer",
    "redeemer_data_id": "redeemer_data",
    "cost_model_id": "cost_model",
    "ident": "multi_asset",
    "pool_hash_id": "pool_hash",
    "pool_id": "pool_hash",
    "drep_hash_id": "drep_hash",
    "drep_voter": "drep_hash",
    "pool_voter": "pool_hash",
    "committee_voter": "committee_hash",
    "committee_hash_id": "committee_hash",
    "cold_key_id": "committee_hash",
    "hot_key_id": "committee_hash",
    "committee_id": "committee",
    "constitution_id": "constitution",
    "gov_action_proposal_id": "gov_action_proposal",
    "no_confidence_id": "gov_action_proposal",
    "prev_gov_action_proposal": "gov_action_proposal",
    "param_proposal": "param_proposal",
    "pool_update_id": "pool_update",
    "update_id": "pool_update",
    "meta_id": "pool_metadata_ref",
    "pmr_id": "pool_metadata_ref",
    "inline_datum_id": "datum",
    "reference_script_id": "script",
    "slot_leader_id": "slot_leader",
    "invalid": "event_info",
    "off_chain_vote_data_id": "off_chain_vote_data",
}

FK_MAP: dict[tuple[str, str], str] = {
    ("tx_in", "tx_out_id"): "tx",
    ("collateral_tx_in", "tx_out_id"): "tx",
    ("reference_tx_in", "tx_out_id"): "tx",
    ("ma_tx_out", "tx_out_id"): "tx_out",
    ("drep_distr", "hash_id"): "drep_hash",
    ("pool_retire", "hash_id"): "pool_hash",
    ("pool_update", "hash_id"): "pool_hash",
}


def resolve_fk(table: str, col: str) -> str | None:
    """Return the target table of a logical foreign key, or ``None``.

    ``FK_MAP`` (per table+column) takes precedence over ``GLOBAL_FK`` (per
    column name).
    """
    if (table, col) in FK_MAP:
        return FK_MAP[(table, col)]
    return GLOBAL_FK.get(col)


def looks_like_fk(col: str) -> bool:
    """Heuristic for catching an *unmapped* logical FK so we never hash a
    drifting id raw. Mapped FKs are handled before this is consulted."""
    return col != "id" and col.endswith("_id")


# Natural (version-stable) key of each foreign-key TARGET table, expressed as a
# list of parts. A part is either:
#   ("col", name)            -> a plain column of the target table
#   ("fk", col, target)      -> a FK of the target table, resolved recursively
# A row's identity in two different syncs is the same iff these parts match.
NATURAL_KEYS: dict[str, list[tuple]] = {
    "block": [("col", "hash")],
    "tx": [("col", "hash")],
    "slot_leader": [("col", "hash")],
    "stake_address": [("col", "hash_raw")],
    "pool_hash": [("col", "hash_raw")],
    "multi_asset": [("col", "policy"), ("col", "name")],
    "datum": [("col", "hash")],
    "script": [("col", "hash")],
    "redeemer_data": [("col", "hash")],
    "cost_model": [("col", "hash")],
    "drep_hash": [("col", "view")],  # unique; raw is NULL for Always* markers
    "committee_hash": [("col", "raw"), ("col", "has_script")],
    "voting_anchor": [("col", "data_hash"), ("col", "url"), ("col", "type")],
    "redeemer": [("fk", "tx_id", "tx"), ("col", "purpose"), ("col", "index")],
    "tx_out": [("fk", "tx_id", "tx"), ("col", "index")],
    "pool_update": [("fk", "registered_tx_id", "tx"), ("col", "cert_index")],
    "pool_metadata_ref": [("fk", "registered_tx_id", "tx"), ("col", "url"), ("col", "hash")],
    "gov_action_proposal": [("fk", "tx_id", "tx"), ("col", "index")],
    "param_proposal": [("fk", "registered_tx_id", "tx"), ("col", "key"), ("col", "epoch_no")],
    "committee": [
        ("fk", "gov_action_proposal_id", "gov_action_proposal"),
        ("col", "quorum_numerator"),
        ("col", "quorum_denominator"),
    ],
    "constitution": [
        ("fk", "gov_action_proposal_id", "gov_action_proposal"),
        ("fk", "voting_anchor_id", "voting_anchor"),
    ],
    "event_info": [("fk", "tx_id", "tx"), ("col", "epoch"), ("col", "type")],
}

# How each table is bounded to a common chain coordinate. Forms:
#   ("self_block",)            row is a block; use its own block_no
#   ("block_fk", col)          join block via FK `col`; bound block_no
#   ("tx_fk", col)             join tx via FK `col` then block; bound block_no
#   ("tx_fk_via_txout", col)   join tx_out via `col` -> tx -> block
#   ("pool_update_fk", col)    join pool_update via `col` -> its tx -> block
#   ("gap_fk", col)            join gov_action_proposal via `col` -> tx -> block
#   ("epoch", expr)            bound the (epoch) expression directly
#   ("accumulator",)           monotonic definition table, no clean anchor;
#                              compared full-table, count delta is informational
ANCHORS: dict[str, tuple] = {
    "block": ("self_block",),
    "tx": ("block_fk", "block_id"),
    "ada_pots": ("block_fk", "block_id"),
    "epoch_param": ("block_fk", "block_id"),
    "voting_anchor": ("block_fk", "block_id"),
    "tx_out": ("tx_fk", "tx_id"),
    "collateral_tx_out": ("tx_fk", "tx_id"),
    "tx_metadata": ("tx_fk", "tx_id"),
    "tx_cbor": ("tx_fk", "tx_id"),
    "datum": ("tx_fk", "tx_id"),
    "script": ("tx_fk", "tx_id"),
    "redeemer": ("tx_fk", "tx_id"),
    "redeemer_data": ("tx_fk", "tx_id"),
    "extra_key_witness": ("tx_fk", "tx_id"),
    "ma_tx_mint": ("tx_fk", "tx_id"),
    "delegation": ("tx_fk", "tx_id"),
    "delegation_vote": ("tx_fk", "tx_id"),
    "drep_registration": ("tx_fk", "tx_id"),
    "stake_registration": ("tx_fk", "tx_id"),
    "stake_deregistration": ("tx_fk", "tx_id"),
    "withdrawal": ("tx_fk", "tx_id"),
    "treasury": ("tx_fk", "tx_id"),
    "reserve": ("tx_fk", "tx_id"),
    "pot_transfer": ("tx_fk", "tx_id"),
    "gov_action_proposal": ("tx_fk", "tx_id"),
    "voting_procedure": ("tx_fk", "tx_id"),
    "committee_registration": ("tx_fk", "tx_id"),
    "committee_de_registration": ("tx_fk", "tx_id"),
    "event_info": ("tx_fk", "tx_id"),
    "tx_in": ("tx_fk", "tx_in_id"),
    "collateral_tx_in": ("tx_fk", "tx_in_id"),
    "reference_tx_in": ("tx_fk", "tx_in_id"),
    "ma_tx_out": ("tx_fk_via_txout", "tx_out_id"),  # ma_tx_out -> tx_out -> tx -> block
    "pool_owner": ("pool_update_fk", "pool_update_id"),
    "pool_relay": ("pool_update_fk", "update_id"),
    "pool_update": ("tx_fk", "registered_tx_id"),
    "pool_retire": ("tx_fk", "announced_tx_id"),
    "pool_metadata_ref": ("tx_fk", "registered_tx_id"),
    "param_proposal": ("tx_fk", "registered_tx_id"),
    "treasury_withdrawal": ("gap_fk", "gov_action_proposal_id"),
    "constitution": ("gap_fk", "gov_action_proposal_id"),
    "committee": ("gap_fk", "gov_action_proposal_id"),
    "epoch": ("epoch", "no"),
    "epoch_stake": ("epoch", "epoch_no"),
    "epoch_stake_progress": ("epoch", "epoch_no"),
    "epoch_state": ("epoch", "epoch_no"),
    "drep_distr": ("epoch", "epoch_no"),
    "pool_stat": ("epoch", "epoch_no"),
    "reward": ("epoch", "earned_epoch"),
    "reward_rest": ("epoch", "earned_epoch"),
    "new_committee": ("epoch", "epoch_no"),
    # Monotonic definition tables with no single clean chain anchor. Compared
    # full-table; a positive count delta usually just reflects the tip gap.
    "multi_asset": ("accumulator",),
    "stake_address": ("accumulator",),
    "pool_hash": ("accumulator",),
    "drep_hash": ("accumulator",),
    "committee_hash": ("accumulator",),
    "cost_model": ("accumulator",),
    "slot_leader": ("accumulator",),
    "committee_member": ("accumulator",),
}

# Numeric columns worth aggregating (SUM/MIN/MAX) as a cheap value proof.
VALUE_COLUMNS = ("value", "quantity", "amount", "fee", "out_sum", "deposit")

# Column SQL types we accept for the numeric value proof (datum.value and
# redeemer_data.value are jsonb and must NOT be summed).
NUMERIC_TYPES = {"numeric", "bigint", "integer", "smallint", "double precision"}
