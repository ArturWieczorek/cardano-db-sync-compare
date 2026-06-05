# 06 — How each table is compared

> **What's in here:** the db-sync schema knowledge baked into the tool — the
> hand-built tables of rules that make "compare by meaning" work. These live as
> dictionaries in `db_sync_comparator/registries.py`.
>
> **Prerequisites:** [primer 05](primers/05-surrogate-ids-sequences-and-drift.md)
> and [how it works](03-how-it-works.md).

Because db-sync doesn't *declare* its foreign keys and some columns aren't tied to
the chain in obvious ways, the tool can't figure everything out by itself. It
carries five small registries of schema knowledge. Here's what each one is for.

> **These registries mirror db-sync; db-sync is the source of truth.** When the
> schema changes, verify against the upstream
> [schema reference](https://github.com/IntersectMBO/cardano-db-sync/blob/master/doc/schema.md),
> the [schema source](https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/src/Cardano/Db/Schema)
> (authoritative), and the
> [migrations](https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/test/schema)
> — at the git **tag matching the db-sync version you're comparing**, since
> `master` runs ahead of releases.

## 1. `EXCLUDED_TABLES` — tables we deliberately don't compare

About 16 tables, each with a reason, because their contents aren't a pure function
of the chain:

- **Network-fetched** (`off_chain_pool_data`, `off_chain_vote_data`, their helper
  and error tables): filled by fetching metadata over HTTP. Which URLs resolved,
  when, and what bytes came back are non-deterministic — two honest syncs differ.
- **Per-instance bookkeeping** (`meta`, `schema_version`, `extra_migrations`,
  `schema_migrations`): record *this* database's version, start time, and
  migration log — different by definition.
- **Volatile / operational** (`epoch_sync_time` is wall-clock sync durations;
  `reverse_index` is a tip-only rollback helper; `delisted_pool`,
  `reserved_pool_ticker` are SMASH operator state, not chain data).

`--plan` prints the list and the reason for each.

## 2. `GIANT_TABLES` — the ones that get tiered effort

The billion- and hundred-million-row tables (`ma_tx_out`, `epoch_stake`,
`reward`, `tx_out`, `tx_in`, `tx`, …). By default these get the cheaper tiered
check (count + numeric sum/min/max + a shallow fingerprint); `--full` upgrades
them to the exhaustive every-column fingerprint. See
[how it works, idea 4](03-how-it-works.md) and
[performance](07-performance-and-scaling.md).

## 3. `NATURAL_KEYS` — the shared identifier of each table

For every table that other tables point at, this says *what its natural key is* —
the column(s) that come from the blockchain and so match across databases
([primer 05](primers/05-surrogate-ids-sequences-and-drift.md)). Examples:

| Table | Natural key |
|-------|-------------|
| `block` | `hash` |
| `tx` | `hash` |
| `stake_address` | `hash_raw` |
| `pool_hash` | `hash_raw` |
| `multi_asset` | (`policy`, `name`) |
| `tx_out` | (its tx's `hash`, `index`) |
| `gov_action_proposal` | (its tx's `hash`, `index`) |

When the tool translates a foreign key, it looks up the target table here. Some
natural keys are themselves built from a foreign key (a `tx_out`'s key needs its
transaction's hash), so translation **chains** until it reaches plain chain data.

## 4. `GLOBAL_FK` and `FK_MAP` — the foreign-key map

Since the database doesn't declare foreign keys, the tool maps them by column name.

- **`GLOBAL_FK`** handles names that mean the same thing everywhere: `block_id` →
  `block`, `tx_id` → `tx`, `addr_id` → `stake_address`, `ident` → `multi_asset`,
  and so on — **including the irregular ones that don't end in `_id`** (`drep_voter`
  → `drep_hash`, `return_address` → `stake_address`, `param_proposal` →
  `param_proposal`, `prev_gov_action_proposal` → `gov_action_proposal`, `invalid`
  → `event_info`).
- **`FK_MAP`** overrides the rare ambiguous cases where the *same* column name
  means different things in different tables — e.g. `tx_out_id` points at `tx` in
  `tx_in` but at `tx_out` in `ma_tx_out`; `hash_id` points at `pool_hash` in
  `pool_retire` but at `drep_hash` in `drep_distr`.

Safety net: if a column *looks* like a foreign key (ends in `_id`) but isn't in
either map, the tool **excludes it from the fingerprint and flags it loudly**
rather than risk hashing a drifting id. `--plan` shows zero such "UNMAPPED"
columns on the current schema — but this is how a future schema addition would
surface.

## 5. `ANCHORS` — how a table is tied to a chain coordinate

To bound a table to the common boundary ([how it works, idea 3](03-how-it-works.md)),
the tool needs to know how each table connects to the chain. `ANCHORS` records
that, in a few shapes:

- **`block` itself** — bounded by its own `id`.
- **via a transaction** — e.g. `tx_out` through `tx_id`, `tx_in` through
  `tx_in_id`; bounded by the transaction id-range.
- **via the output then the transaction** — `ma_tx_out` through
  `tx_out_id → tx_out → tx`.
- **via a pool update or a governance proposal** — for the pool and governance
  child tables.
- **by epoch** — `epoch_stake`, `reward`, `drep_distr`, `pool_stat`, … are bounded
  by `epoch_no` (they're written per epoch).
- **accumulator** — tables with no clean chain anchor (next section).

## The "accumulator" tables

A few "definition" tables — `multi_asset`, `stake_address`, `pool_hash`,
`drep_hash`, `committee_hash`, `cost_model`, `slot_leader` — just **accumulate**
one row the first time some object ever appears, with no tidy link back to a
single block. The tool can't cheaply bound them to the window, so it compares them
**whole** and treats a row-count difference as **informational**: when database 2
is synced further, it has simply seen more distinct objects. (Example from the
validation run: `multi_asset` 11,108,713 vs 11,128,803 — the ~20k extra are tokens
first minted in the blocks only database 2 has.) A count difference here is
expected; it is **not** a failure.

**Next:** [Performance and scaling →](07-performance-and-scaling.md)
