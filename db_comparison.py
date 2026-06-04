#!/usr/bin/env python3
"""
db_comparison.py — content-equivalence comparator for two cardano-db-sync databases.

Purpose
-------
Decide whether two independently-synced db-sync PostgreSQL databases (e.g. a
release candidate vs. the previous release, or Core vs. a re-sync) hold the
*same blockchain data*, fast enough to use as a pre-release integrity gate on
mainnet-sized (>500 GB) databases.

Why the naive "hash every row in Python" approach is wrong here
---------------------------------------------------------------
Two db-sync databases that represent the identical chain are NOT row-for-row
identical at the storage level:

1.  Different tips. One DB is usually synced further than the other. We must
    compare only the data up to a *common chain boundary*.

2.  Surrogate id drift. Every table has a bigint `id` assigned from a Postgres
    sequence as rows are inserted. Rollbacks during a sync burn sequence values
    (the sequence is not rewound), so two syncs with different rollback
    histories end up with different `id` values for the same logical row — and
    every foreign-key `*_id` column inherits that drift. Measured on real
    mainnet DBs: 13.6.0.5 had 16,899 id-gaps in `block`, 13.7.1.0 had 10; ids
    were identical early in the chain and diverged near the tip. Hashing raw
    `id`/`*_id` columns therefore produces false mismatches.

3.  Scale. `ma_tx_out` has ~1.1 billion rows, `epoch_stake`/`reward` ~450M,
    `tx_out`/`tx_in` ~340M. Pulling rows to the client is infeasible.

How this tool solves it
-----------------------
*   All hashing happens server-side. Only tiny aggregates cross the wire.
*   Order-independent, duplicate-safe set hash: md5 each *normalized* row, split
    the digest into two 60-bit chunks and SUM them as numeric. Two tables hash
    equal iff they are the same multiset of rows. No sort, no client memory.
    (Same family of technique as Percona's pt-table-checksum.)
*   Rows are *normalized* before hashing: the surrogate `id` and PostgreSQL
    GENERATED columns are dropped, and every foreign-key `*_id` is replaced by
    the version-stable natural key of the row it points at (recursively), via
    joins. So id/FK drift is cancelled out and only chain content is compared.
*   A common chain boundary (block_no / epoch_no, optionally minus a security
    margin) is applied per-table through its anchor path, so each DB computes a
    comparable aggregate over the same logical row set.
*   Schema-drift aware: both schemas are introspected and only the intersection
    of columns is compared (added/removed columns are reported, not hashed).
*   Tiered effort: small/medium tables get a full per-column normalized hash;
    the few giant tables get cheap proofs (row count + numeric SUM/MIN/MAX) plus
    a shallow normalized hash, with `--full` to force deep hashing everywhere.
*   On a mismatch, a Merkle-style bisection over block_no/epoch_no ranges
    localizes the discrepancy to a narrow chain window for follow-up.

This file is intentionally dependency-light: psycopg 3 (`pip install
'psycopg[binary]'`) and the standard library.

Usage
-----
    python3 db_comparison.py \
        --db1 "dbname=mainnet-13.6.0.5 host=/var/run/postgresql" \
        --db2 "dbname=mainnet-13.7.1.0 host=/var/run/postgresql" \
        --pgpass config/pgpass-mainnet

    # audit what will be compared, without touching data:
    python3 db_comparison.py --db1 ... --db2 ... --plan

    # bounded validation on a block window (fast):
    python3 db_comparison.py --db1 ... --db2 ... --block-range 8000000:8010000

Exit code is 0 when the databases are content-equivalent over the compared
range, 1 when discrepancies are found, 2 on operational error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit("psycopg 3 is required:  pip install 'psycopg[binary]'")


# ---------------------------------------------------------------------------
# Domain registries  (the "very detailed schema analysis")
#
# These encode cardano-db-sync schema knowledge that cannot be introspected:
#  * which tables are not deterministically comparable between two syncs,
#  * the version-stable natural key of every foreign-key target table,
#  * how each table is anchored to a chain coordinate (block_no / epoch_no) so
#    we can bound the comparison at a common tip.
# Authoritative source: cardano-db/src/Cardano/Db/Schema/ and
# .claude/docs/04-schema-reference.md.
# ---------------------------------------------------------------------------

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
    "ma_tx_out", "epoch_stake", "reward", "reward_rest",
    "tx_out", "tx_in", "tx_metadata", "tx",
    "redeemer", "extra_key_witness", "datum",
    "reference_tx_in", "collateral_tx_in",
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
    "block_id": "block", "previous_id": "block",
    "tx_id": "tx", "registered_tx_id": "tx", "announced_tx_id": "tx",
    "tx_in_id": "tx", "consumed_by_tx_id": "tx",
    "addr_id": "stake_address", "stake_address_id": "stake_address",
    "reward_addr_id": "stake_address", "return_address": "stake_address",
    "voting_anchor_id": "voting_anchor",
    "redeemer_id": "redeemer", "redeemer_data_id": "redeemer_data",
    "cost_model_id": "cost_model",
    "ident": "multi_asset",
    "pool_hash_id": "pool_hash", "pool_id": "pool_hash",
    "drep_hash_id": "drep_hash", "drep_voter": "drep_hash",
    "pool_voter": "pool_hash", "committee_voter": "committee_hash",
    "committee_hash_id": "committee_hash",
    "cold_key_id": "committee_hash", "hot_key_id": "committee_hash",
    "committee_id": "committee", "constitution_id": "constitution",
    "gov_action_proposal_id": "gov_action_proposal",
    "no_confidence_id": "gov_action_proposal",
    "prev_gov_action_proposal": "gov_action_proposal",
    "param_proposal": "param_proposal",
    "pool_update_id": "pool_update", "update_id": "pool_update",
    "meta_id": "pool_metadata_ref", "pmr_id": "pool_metadata_ref",
    "inline_datum_id": "datum", "reference_script_id": "script",
    "slot_leader_id": "slot_leader", "invalid": "event_info",
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


def resolve_fk(table: str, col: str) -> Optional[str]:
    """Return the target table of a logical foreign key, or None."""
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
    "block":         [("col", "hash")],
    "tx":            [("col", "hash")],
    "slot_leader":   [("col", "hash")],
    "stake_address": [("col", "hash_raw")],
    "pool_hash":     [("col", "hash_raw")],
    "multi_asset":   [("col", "policy"), ("col", "name")],
    "datum":         [("col", "hash")],
    "script":        [("col", "hash")],
    "redeemer_data": [("col", "hash")],
    "cost_model":    [("col", "hash")],
    "drep_hash":     [("col", "view")],                       # unique; raw is NULL for Always* markers
    "committee_hash": [("col", "raw"), ("col", "has_script")],
    "voting_anchor": [("col", "data_hash"), ("col", "url"), ("col", "type")],
    "redeemer":      [("fk", "tx_id", "tx"), ("col", "purpose"), ("col", "index")],
    "tx_out":        [("fk", "tx_id", "tx"), ("col", "index")],
    "pool_update":   [("fk", "registered_tx_id", "tx"), ("col", "cert_index")],
    "pool_metadata_ref": [("fk", "registered_tx_id", "tx"), ("col", "url"), ("col", "hash")],
    "gov_action_proposal": [("fk", "tx_id", "tx"), ("col", "index")],
    "param_proposal": [("fk", "registered_tx_id", "tx"), ("col", "key"), ("col", "epoch_no")],
    "committee":     [("fk", "gov_action_proposal_id", "gov_action_proposal"),
                      ("col", "quorum_numerator"), ("col", "quorum_denominator")],
    "constitution":  [("fk", "gov_action_proposal_id", "gov_action_proposal"),
                      ("fk", "voting_anchor_id", "voting_anchor")],
    "event_info":    [("fk", "tx_id", "tx"), ("col", "epoch"), ("col", "type")],
}

# How each table is bounded to a common chain coordinate. Forms:
#   ("self_block",)            row is a block; use its own block_no
#   ("block_fk", col)          join block via FK `col`; bound block_no
#   ("tx_fk", col)             join tx via FK `col` then block; bound block_no
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
    "ma_tx_out": ("tx_fk_via_txout", "tx_out_id"),   # ma_tx_out -> tx_out -> tx -> block

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

# md5 -> two 60-bit numeric chunks, summed: order-independent & duplicate-safe.
SETHASH_SELECT = (
    "count(*) AS n, "
    "coalesce(sum(('x'||substr(h,1,15))::bit(60)::bigint::numeric),0) AS s1, "
    "coalesce(sum(('x'||substr(h,17,15))::bit(60)::bigint::numeric),0) AS s2"
)


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

@dataclass
class TableSchema:
    name: str
    columns: list[str]                       # ordinal order
    coltypes: dict[str, str]
    pk: list[str]
    generated: set[str]
    fks: dict[str, tuple[str, str]]          # local_col -> (ref_table, ref_col)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def introspect(conn) -> dict[str, TableSchema]:
    """Read columns, primary keys, generated columns and foreign keys."""
    cols: dict[str, TableSchema] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name, data_type,
                   (is_generated = 'ALWAYS') AS gen
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """)
        for tname, cname, dtype, gen in cur.fetchall():
            ts = cols.get(tname)
            if ts is None:
                ts = TableSchema(tname, [], {}, [], set(), {})
                cols[tname] = ts
            ts.columns.append(cname)
            ts.coltypes[cname] = dtype
            if gen:
                ts.generated.add(cname)

        # only base tables
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'
        """)
        base = {r[0] for r in cur.fetchall()}
        for t in list(cols):
            if t not in base:
                del cols[t]

        cur.execute("""
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema='public' AND tc.constraint_type='PRIMARY KEY'
            ORDER BY kcu.ordinal_position
        """)
        for tname, cname in cur.fetchall():
            if tname in cols:
                cols[tname].pk.append(cname)

        cur.execute("""
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS ref_table, ccu.column_name AS ref_col
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.table_schema='public' AND tc.constraint_type='FOREIGN KEY'
        """)
        for tname, cname, rtable, rcol in cur.fetchall():
            if tname in cols:
                cols[tname].fks[cname] = (rtable, rcol)
    return cols


# ---------------------------------------------------------------------------
# SQL generation: normalized projection + anchor bound
# ---------------------------------------------------------------------------

class JoinBuilder:
    """Accumulates idempotent LEFT JOINs and hands out stable aliases."""

    def __init__(self, root_alias: str = "t0"):
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


@dataclass
class TablePlan:
    name: str
    kind: str                                # 'normal' | 'giant' | 'accumulator' | 'excluded'
    reason: str = ""
    select_exprs: list[str] = field(default_factory=list)   # normalized columns to hash
    joins: str = ""
    anchor_kind: str = "none"                # 'idrange' | 'epoch' | 'none'
    spine: Optional[str] = None              # spine table whose id-range bounds this one
    anchor_col: Optional[str] = None         # column on t0 carrying the spine FK
    epoch_expr: Optional[str] = None         # epoch expression for epoch-anchored tables
    value_col: Optional[str] = None
    skipped_cols: list[str] = field(default_factory=list)   # columns dropped (deep FK over budget)
    extra_cols: dict[str, list[str]] = field(default_factory=dict)  # version-only columns


def natural_key_exprs(target: str, alias: str, jb: JoinBuilder,
                      common_cols: dict[str, set[str]], depth: int, max_depth: int,
                      skipped: list[str]) -> Optional[list[str]]:
    """SQL expressions for the natural key of `target` rooted at `alias`.

    Returns None if it cannot be resolved within max_depth (caller should drop
    the referencing column and record it as skipped)."""
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
            sub = natural_key_exprs(subtarget, sub_alias, jb, common_cols,
                                    depth + 1, max_depth, skipped)
            if sub is None:
                return None
            out.extend(sub)
    return out


# The "spine" of a table is the ancestor whose surrogate-id range we precompute
# for a given chain window. Because rows are inserted in chain order, the ids of
# all rows belonging to a block window form a contiguous range *within one DB*
# (rollbacks only burn ids near the tip), so the bound becomes an indexed
# `t0.<fk> BETWEEN lo AND hi` — no join to block, no sequential scan. The lo/hi
# differ between the two DBs (id drift) but select the same logical rows.
def build_anchor(anchor: tuple) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Return (kind, spine, anchor_col, epoch_expr)."""
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
    return "none", None, None, None          # accumulator


def plan_table(name: str, s1: TableSchema, s2: TableSchema, full: bool,
               giant_fk_depth: int) -> TablePlan:
    """Produce the comparison plan (normalized SQL pieces) for one table."""
    if name in EXCLUDED_TABLES:
        return TablePlan(name, "excluded", EXCLUDED_TABLES[name])

    common_all = {t: (set(a.columns) & set(b.columns))
                  for t, a, b in [(name, s1, s2)]}
    # We also need common columns of any FK-target table for natural keys.
    # Built lazily by the caller via shared dict; here we only need `name`'s.
    common = set(s1.columns) & set(s2.columns)
    only1 = sorted(set(s1.columns) - set(s2.columns))
    only2 = sorted(set(s2.columns) - set(s1.columns))

    is_giant = name in GIANT_TABLES and not full
    kind = "giant" if is_giant else "normal"
    max_depth = giant_fk_depth if is_giant else 8

    anchor = ANCHORS.get(name)
    if anchor is None:
        # Unknown table: be safe — treat as accumulator, full-table compare.
        anchor = ("accumulator",)
    if anchor[0] == "accumulator":
        kind = "accumulator" if kind != "giant" else "giant"

    anchor_kind, spine, anchor_col, epoch_expr = build_anchor(anchor)

    jb = JoinBuilder()
    skipped: list[str] = []
    exprs: list[str] = []
    # Stable, version-independent column order: sort by name.
    for col in sorted(common):
        if col == "id":                                     # surrogate PK — always drifts
            continue
        if col in s1.generated or col in s2.generated:      # e.g. reward.earned_epoch
            continue
        ref_table = resolve_fk(name, col)
        if ref_table is not None:
            # Replace the drifting *_id with the referenced row's natural key.
            sub = natural_key_exprs(ref_table, jb.join("t0", col, ref_table),
                                    jb, COMMON_COLS, 1, max_depth, skipped)
            if sub is None:
                # over depth budget (tiered giants) or target has no registered
                # natural key: drop the column rather than hash a drifting id.
                if f"{name}.{col}->{ref_table}" not in " ".join(skipped):
                    skipped.append(f"{col}->{ref_table}")
                continue
            exprs.extend(sub)
        elif looks_like_fk(col):
            # Unmapped logical FK: never hash a raw id. Exclude + flag loudly so
            # the registry can be extended.
            skipped.append(f"{col} (UNMAPPED FK -> excluded)")
        else:
            exprs.append(f"t0.{quote_ident(col)}")

    plan = TablePlan(
        name=name, kind=kind,
        select_exprs=exprs, joins=jb.clauses(),
        anchor_kind=anchor_kind, spine=spine, anchor_col=anchor_col,
        epoch_expr=epoch_expr,
        skipped_cols=skipped,
        extra_cols={"only_db1": only1, "only_db2": only2} if (only1 or only2) else {},
    )
    numeric_types = {"numeric", "bigint", "integer", "smallint", "double precision"}
    for vc in VALUE_COLUMNS:
        # must be a real numeric column (datum.value / redeemer_data.value are jsonb)
        if (vc in common and resolve_fk(name, vc) is None
                and s1.coltypes.get(vc) in numeric_types
                and s2.coltypes.get(vc) in numeric_types):
            plan.value_col = vc
            break
    return plan


# Filled once after introspection; used by natural_key_exprs for schema drift.
COMMON_COLS: dict[str, set[str]] = {}


# ---------------------------------------------------------------------------
# Query assembly + execution
# ---------------------------------------------------------------------------

def bound_predicate(plan: TablePlan, ranges: dict, cutoff_epoch: int,
                    in_block_range: bool) -> str:
    """Predicate for ONE database, using that database's spine id-ranges."""
    if plan.anchor_kind == "idrange":
        r = ranges.get(plan.spine)
        if r is None or r[0] is None:
            return "FALSE"                       # no rows in the window
        lo, hi = r
        return f"t0.{quote_ident(plan.anchor_col)} BETWEEN {lo} AND {hi}"
    if plan.anchor_kind == "epoch":
        if in_block_range:
            return "FALSE"                       # epoch tables out of scope for a block window
        return f"{plan.epoch_expr} <= {cutoff_epoch}"
    # accumulator: full-table compare in cutoff mode; out of scope for a window
    return "FALSE" if in_block_range else "TRUE"


def hash_sql(plan: TablePlan, predicate: str) -> str:
    row = "md5(ROW(" + ", ".join(plan.select_exprs) + ")::text) AS h" \
        if plan.select_exprs else "md5('')::text AS h"
    return (
        f"SELECT {SETHASH_SELECT} FROM (\n"
        f"  SELECT {row}\n"
        f"  FROM {quote_ident(plan.name)} t0\n"
        f"  {plan.joins}\n"
        f"  WHERE {predicate}\n"
        f") q"
    )


def value_sql(plan: TablePlan, predicate: str) -> Optional[str]:
    if not plan.value_col:
        return None
    v = f"t0.{quote_ident(plan.value_col)}::numeric"
    return (
        f"SELECT coalesce(sum({v}),0), coalesce(min({v}),0), coalesce(max({v}),0)\n"
        f"FROM {quote_ident(plan.name)} t0\n  {plan.joins}\n  WHERE {predicate}"
    )


@dataclass
class TableResult:
    name: str
    kind: str
    n1: int = 0
    n2: int = 0
    h1: tuple = (0, 0)
    h2: tuple = (0, 0)
    value1: Optional[tuple] = None
    value2: Optional[tuple] = None
    status: str = "PENDING"          # MATCH | COUNT_DIFF | HASH_DIFF | VALUE_DIFF | EXCLUDED | ERROR
    note: str = ""
    seconds: float = 0.0
    skipped_cols: list[str] = field(default_factory=list)
    schema_drift: dict = field(default_factory=dict)
    localized: list[str] = field(default_factory=list)


def run_scalar(conn, sql: str, timeout_ms: int) -> tuple:
    with conn.cursor() as cur:
        if timeout_ms:
            cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        cur.execute(sql)
        return cur.fetchone()


def compare_table(plan: TablePlan, conn1, conn2, ranges1: dict, ranges2: dict,
                  cutoff_epoch: int, in_block_range: bool, timeout_ms: int) -> TableResult:
    r = TableResult(plan.name, plan.kind, skipped_cols=plan.skipped_cols,
                    schema_drift=plan.extra_cols)
    start = time.time()
    try:
        pred1 = bound_predicate(plan, ranges1, cutoff_epoch, in_block_range)
        pred2 = bound_predicate(plan, ranges2, cutoff_epoch, in_block_range)
        a = run_scalar(conn1, hash_sql(plan, pred1), timeout_ms)
        b = run_scalar(conn2, hash_sql(plan, pred2), timeout_ms)
        r.n1, r.h1 = a[0], (int(a[1]), int(a[2]))
        r.n2, r.h2 = b[0], (int(b[1]), int(b[2]))

        v1, v2 = value_sql(plan, pred1), value_sql(plan, pred2)
        if v1:
            va = run_scalar(conn1, v1, timeout_ms)
            vb = run_scalar(conn2, v2, timeout_ms)
            r.value1 = tuple(str(x) for x in va)
            r.value2 = tuple(str(x) for x in vb)

        if plan.kind == "accumulator" and r.n1 != r.n2:
            # Expected when one DB is synced further; flag, don't fail, but the
            # content hash of the shared rows still must agree if counts match.
            r.status = "COUNT_DIFF"
            r.note = ("accumulator table; count delta usually reflects the tip "
                      "gap (objects first-seen after the cutoff)")
        elif r.n1 != r.n2:
            r.status = "COUNT_DIFF"
            r.note = f"row count differs ({r.n1} vs {r.n2})"
        elif r.h1 != r.h2:
            r.status = "HASH_DIFF"
            r.note = "row counts match but content hash differs"
        elif r.value1 is not None and r.value1 != r.value2:
            r.status = "VALUE_DIFF"
            r.note = f"value aggregate differs: {r.value1} vs {r.value2}"
        else:
            r.status = "MATCH"
    except Exception as exc:                     # operational, per-table
        r.status = "ERROR"
        r.note = f"{type(exc).__name__}: {exc}"
    r.seconds = time.time() - start
    return r


# ---------------------------------------------------------------------------
# Phase 2: Merkle-style bisection to localize a mismatch
# ---------------------------------------------------------------------------

def localize(plan: TablePlan, conn1, conn2, lo: int, hi: int, cutoff_block: int,
             timeout_ms: int, max_leaves: int = 8, min_width: int = 2000) -> list[str]:
    """Recursively bisect the [lo,hi] window on the common chain coordinate
    (block_no for id-range tables, epoch_no for epoch tables), returning the
    narrowest windows whose hashes still differ."""
    found: list[str] = []
    is_block = plan.anchor_kind == "idrange"
    coord = "block_no" if is_block else "epoch_no"

    def hashes(a: int, b: int) -> tuple[tuple, tuple]:
        if is_block:
            r1 = compute_spine_ranges(conn1, cutoff_block, (a, b))
            r2 = compute_spine_ranges(conn2, cutoff_block, (a, b))
            p1 = bound_predicate(plan, r1, 0, False)
            p2 = bound_predicate(plan, r2, 0, False)
        else:
            p1 = p2 = f"{plan.epoch_expr} BETWEEN {a} AND {b}"
        ra = run_scalar(conn1, hash_sql(plan, p1), timeout_ms)
        rb = run_scalar(conn2, hash_sql(plan, p2), timeout_ms)
        return (ra[0], int(ra[1]), int(ra[2])), (rb[0], int(rb[1]), int(rb[2]))

    if not is_block:
        min_width = 0
    stack = [(lo, hi)]
    while stack and len(found) < max_leaves:
        a, b = stack.pop()
        ha, hb = hashes(a, b)
        if ha == hb:
            continue
        if b - a <= min_width:
            kindword = "content differs" if ha[0] == hb[0] else "row count differs"
            found.append(f"{coord} {a}..{b}: {kindword} (db1 n={ha[0]}, db2 n={hb[0]})")
            continue
        mid = (a + b) // 2
        stack.append((mid + 1, b))
        stack.append((a, mid))
    return found


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_WORK_MEM = "256MB"


def connect(dsn: str):
    conn = psycopg.connect(dsn, autocommit=True)
    # Larger work_mem keeps the big FK-translation hash joins in memory instead
    # of spilling to disk during a full run.
    conn.execute(f"SET work_mem = '{_WORK_MEM}'")
    return conn


def indexed_columns(conn) -> set[tuple[str, str]]:
    """(table, first_index_column) pairs — used to tell whether a table's anchor
    column can be range-scanned (vs forcing a sequential scan)."""
    out: set[tuple[str, str]] = set()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.relname, a.attname
            FROM pg_index i
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
            WHERE n.nspname = 'public'
        """)
        for tbl, col in cur.fetchall():
            out.add((tbl, col))
    return out


def get_tip(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT max(block_no), max(epoch_no) FROM block")
        bn, en = cur.fetchone()
        return int(bn), int(en)


def _minmax(conn, table: str, col: str, lo, hi) -> tuple:
    """Smallest/largest id whose `col` is in [lo,hi].

    Written as two ORDER BY ... LIMIT 1 index seeks rather than min(id)/max(id):
    with a filter on a non-PK column, the planner would otherwise walk the PK
    index across the whole table (minutes on 121M-row tx). The seek uses the
    index on `col` and returns in milliseconds."""
    t, c = quote_ident(table), quote_ident(col)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT "
            f"(SELECT id FROM {t} WHERE {c} BETWEEN %s AND %s ORDER BY {c} ASC,  id ASC  LIMIT 1), "
            f"(SELECT id FROM {t} WHERE {c} BETWEEN %s AND %s ORDER BY {c} DESC, id DESC LIMIT 1)",
            (lo, hi, lo, hi))
        return cur.fetchone()


def compute_spine_ranges(conn, cutoff_block: int,
                         window: Optional[tuple[int, int]]) -> dict:
    """Per-DB surrogate-id ranges for each spine table, derived by walking the
    chain spine block -> tx -> {tx_out, pool_update, gov_action_proposal}. These
    bound every table to a common chain window via an indexed range scan.

    Returns spine -> (min_id, max_id) or (None, None) when the window is empty.
    """
    ranges: dict[str, tuple] = {}
    with conn.cursor() as cur:
        if window is not None:
            ranges["block"] = _minmax(conn, "block", "block_no", window[0], window[1])
        else:
            # cutoff mode: everything up to (and including) the cutoff block;
            # id 0.. keeps genesis / EBB rows (NULL block_no) that sit below it.
            cur.execute(
                "SELECT id FROM block WHERE block_no <= %s "
                "ORDER BY block_no DESC, id DESC LIMIT 1", (cutoff_block,))
            row = cur.fetchone()
            ranges["block"] = (0, row[0] if row else None)

    b = ranges["block"]
    if b[0] is None:
        for s in ("tx", "tx_out", "pool_update", "gov_action_proposal"):
            ranges[s] = (None, None)
        return ranges
    ranges["tx"] = _minmax(conn, "tx", "block_id", b[0], b[1])
    t = ranges["tx"]
    if t[0] is None:
        for s in ("tx_out", "pool_update", "gov_action_proposal"):
            ranges[s] = (None, None)
        return ranges
    ranges["tx_out"] = _minmax(conn, "tx_out", "tx_id", t[0], t[1])
    ranges["pool_update"] = _minmax(conn, "pool_update", "registered_tx_id", t[0], t[1])
    ranges["gov_action_proposal"] = _minmax(conn, "gov_action_proposal", "tx_id", t[0], t[1])
    return ranges


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare two cardano-db-sync databases for chain-content equivalence.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--db1", required=True, help="psycopg/libpq conninfo for DB 1 (e.g. 'dbname=... host=/var/run/postgresql')")
    ap.add_argument("--db2", required=True, help="conninfo for DB 2")
    ap.add_argument("--pgpass", help="path to a pgpass file (sets PGPASSFILE)")
    ap.add_argument("--workers", type=int, default=4, help="parallel tables (each uses 2 connections)")
    ap.add_argument("--full", action="store_true", help="full per-column deep hash on giant tables too")
    ap.add_argument("--giant-fk-depth", type=int, default=1, help="max FK-resolution depth for giant tables in tiered mode")
    ap.add_argument("--cutoff-block", type=int, help="override common boundary block_no")
    ap.add_argument("--epoch-margin", type=int, default=2, help="epochs to subtract from the cutoff epoch (avoid the in-progress epoch)")
    ap.add_argument("--block-range", help="LO:HI — compare only this block window (skips epoch tables); great for fast validation")
    ap.add_argument("--tables", help="comma-separated subset of tables to compare")
    ap.add_argument("--no-localize", action="store_true", help="skip Merkle bisection of mismatches")
    ap.add_argument("--statement-timeout", type=int, default=0, help="per-statement timeout in ms (0 = none)")
    ap.add_argument("--work-mem", default="256MB", help="work_mem per session (helps the big FK-translation hash joins)")
    ap.add_argument("--plan", action="store_true", help="print the comparison plan + generated SQL and exit (no data access for hashing)")
    ap.add_argument("--json", help="write a structured report to this path")
    args = ap.parse_args()

    if args.pgpass:
        os.environ["PGPASSFILE"] = args.pgpass
    global _WORK_MEM
    _WORK_MEM = args.work_mem

    block_range = None
    if args.block_range:
        lo, hi = args.block_range.split(":")
        block_range = (int(lo), int(hi))

    # Introspect both schemas.
    try:
        c1 = connect(args.db1)
        c2 = connect(args.db2)
    except Exception as exc:
        print(f"connection failed: {exc}", file=sys.stderr)
        return 2

    schema1 = introspect(c1)
    schema2 = introspect(c2)
    COMMON_COLS.clear()
    for t in set(schema1) & set(schema2):
        COMMON_COLS[t] = set(schema1[t].columns) & set(schema2[t].columns)

    common_tables = sorted(set(schema1) & set(schema2))
    only_db1 = sorted(set(schema1) - set(schema2))
    only_db2 = sorted(set(schema2) - set(schema1))

    if args.tables:
        wanted = {t.strip() for t in args.tables.split(",")}
        common_tables = [t for t in common_tables if t in wanted]

    # Boundary.
    bn1, en1 = get_tip(c1)
    bn2, en2 = get_tip(c2)
    cutoff_block = args.cutoff_block if args.cutoff_block else min(bn1, bn2)
    cutoff_epoch = max(0, min(en1, en2) - args.epoch_margin)

    print("=" * 78)
    print("cardano-db-sync database comparison")
    print("=" * 78)
    print(f"DB1 tip: block {bn1}, epoch {en1}")
    print(f"DB2 tip: block {bn2}, epoch {en2}")
    print(f"common boundary: block_no <= {cutoff_block}, epoch_no <= {cutoff_epoch}"
          + (f"   (block window {block_range[0]}..{block_range[1]})" if block_range else ""))
    if only_db1:
        print(f"tables only in DB1 (not compared): {', '.join(only_db1)}")
    if only_db2:
        print(f"tables only in DB2 (not compared): {', '.join(only_db2)}")

    # Per-DB indexed id-range windows for the common boundary (cheap: a handful
    # of min/max(id) lookups). These bound every table without scanning it.
    in_block_range = block_range is not None
    ranges1 = compute_spine_ranges(c1, cutoff_block, block_range)
    ranges2 = compute_spine_ranges(c2, cutoff_block, block_range)
    print(f"DB1 spine id-ranges: {ranges1}")
    print(f"DB2 spine id-ranges: {ranges2}")
    print()

    # Build plans.
    plans: list[TablePlan] = []
    for t in common_tables:
        p = plan_table(t, schema1[t], schema2[t], args.full, args.giant_fk_depth)
        plans.append(p)

    # Warn about id-range tables whose anchor column is not indexed: the window
    # bound degrades to a sequential scan (fine for a full run, slow for a
    # narrow window or Phase-2 bisection).
    idx = indexed_columns(c1)
    unindexed = [p.name for p in plans if p.anchor_kind == "idrange"
                 and p.anchor_col != "id" and (p.name, p.anchor_col) not in idx]
    if unindexed:
        print(f"note: anchor column not indexed (window bound = seq scan): {', '.join(unindexed)}")

    if args.plan:
        for p in plans:
            print("-" * 78)
            print(f"{p.name}  [{p.kind}]" + (f"  EXCLUDED: {p.reason}" if p.kind == "excluded" else ""))
            if p.kind == "excluded":
                continue
            if p.anchor_kind == "idrange":
                print(f"  anchor: id-range on t0.{p.anchor_col} via spine '{p.spine}'")
            elif p.anchor_kind == "epoch":
                print(f"  anchor: epoch on {p.epoch_expr}")
            else:
                print("  anchor: none (accumulator — full-table compare)")
            if p.skipped_cols:
                print(f"  columns dropped (FK over depth budget): {p.skipped_cols}")
            if p.extra_cols:
                print(f"  schema drift: {p.extra_cols}")
            pred = bound_predicate(p, ranges1, cutoff_epoch, in_block_range)
            print("  " + hash_sql(p, pred).replace("\n", "\n  "))
        return 0

    to_compare = [p for p in plans if p.kind != "excluded"]
    excluded = [p for p in plans if p.kind == "excluded"]

    # Phase 1: counts + hashes, parallel across tables (own connections).
    results: list[TableResult] = []
    print(f"Phase 1: hashing {len(to_compare)} tables with {args.workers} workers ...\n")

    def work(plan: TablePlan) -> TableResult:
        w1 = connect(args.db1)
        w2 = connect(args.db2)
        try:
            return compare_table(plan, w1, w2, ranges1, ranges2,
                                 cutoff_epoch, in_block_range, args.statement_timeout)
        finally:
            w1.close()
            w2.close()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, p): p for p in to_compare}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            flag = "OK " if r.status == "MATCH" else "!! "
            print(f"  {flag}{r.name:<28} {r.status:<11} "
                  f"n={r.n1}/{r.n2}  {r.seconds:6.1f}s"
                  + (f"  {r.note}" if r.note and r.status != "MATCH" else ""))

    # Phase 2: localize content/hash mismatches on chain-anchored tables.
    mismatches = [r for r in results if r.status in ("HASH_DIFF", "COUNT_DIFF", "VALUE_DIFF")
                  and r.kind != "accumulator"]
    if mismatches and not args.no_localize and not block_range:
        print("\nPhase 2: localizing mismatches ...")
        plan_by_name = {p.name: p for p in to_compare}
        lc1, lc2 = connect(args.db1), connect(args.db2)
        for r in mismatches:
            p = plan_by_name[r.name]
            if p.anchor_kind == "idrange":
                lo, hi = 0, cutoff_block
            elif p.anchor_kind == "epoch":
                lo, hi = 0, cutoff_epoch
            else:
                continue
            try:
                r.localized = localize(p, lc1, lc2, lo, hi, cutoff_block,
                                       args.statement_timeout)
                for line in r.localized:
                    print(f"  {r.name}: {line}")
            except Exception as exc:
                print(f"  {r.name}: localize failed: {exc}")
        lc1.close()
        lc2.close()

    # Summary.
    n_match = sum(1 for r in results if r.status == "MATCH")
    n_acc = sum(1 for r in results if r.status == "COUNT_DIFF" and r.kind == "accumulator")
    hard = [r for r in results if r.status in ("HASH_DIFF", "VALUE_DIFF")
            or (r.status == "COUNT_DIFF" and r.kind != "accumulator")]
    errors = [r for r in results if r.status == "ERROR"]

    print("\n" + "=" * 78)
    print(f"SUMMARY: {n_match} match, {len(hard)} discrepancies, "
          f"{n_acc} accumulator count-deltas (informational), "
          f"{len(errors)} errors, {len(excluded)} excluded")
    if hard:
        print("\nDISCREPANCIES:")
        for r in hard:
            print(f"  {r.name}: {r.status} — {r.note}")
            for line in r.localized:
                print(f"      -> {line}")
            if r.skipped_cols:
                print(f"      (note: columns not hashed: {r.skipped_cols})")
    if errors:
        print("\nERRORS:")
        for r in errors:
            print(f"  {r.name}: {r.note}")
    print("=" * 78)

    if args.json:
        report = {
            "db1_tip": {"block": bn1, "epoch": en1},
            "db2_tip": {"block": bn2, "epoch": en2},
            "cutoff": {"block": cutoff_block, "epoch": cutoff_epoch},
            "block_range": block_range,
            "tables_only_db1": only_db1,
            "tables_only_db2": only_db2,
            "excluded": {p.name: p.reason for p in excluded},
            "results": [
                {
                    "table": r.name, "kind": r.kind, "status": r.status,
                    "n1": r.n1, "n2": r.n2, "hash1": list(r.h1), "hash2": list(r.h2),
                    "value1": r.value1, "value2": r.value2, "note": r.note,
                    "seconds": round(r.seconds, 2), "skipped_cols": r.skipped_cols,
                    "schema_drift": r.schema_drift, "localized": r.localized,
                }
                for r in sorted(results, key=lambda x: x.name)
            ],
        }
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"report written to {args.json}")

    return 1 if (hard or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
