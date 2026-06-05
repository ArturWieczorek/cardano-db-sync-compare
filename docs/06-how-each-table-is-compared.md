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

## Chain-anchored vs accumulator tables (the diary analogy)

This is the idea behind `ANCHORS` and the whole "accumulator" notion, in plain
language.

### The one idea: a "chain coordinate"

The blockchain is just a long sequence of **blocks**, numbered 0, 1, 2, 3, … (that
number is `block_no`). Epochs (≈5-day periods) are numbered too. So "block
8,000,000" or "epoch 291" is an **address — a position — on the chain**. Call it a
*coordinate*.

**The diary analogy.** Picture the chain as a **diary** with **one page per day**,
where the page number is the block number.

- Most things you write are **dated** — they live on a specific page (block).
- But at the **back** of the diary you keep an **address book**: the first time you
  ever meet someone, you write their name once. Those entries are **not dated** —
  it's just a running list of everyone you've ever met.

*Dated entry* vs *undated address-book entry* is exactly *chain-anchored table* vs
*accumulator table*.

### Chain-anchored tables — "this row has a place on the chain"

Every row can be tied to a specific block (or epoch) — you can answer *"which block
does this row belong to?"* — either **directly** (the row has a block number) or
**indirectly** (it points at a transaction, which is in a block):

| Table | What it holds | Its "date" (chain coordinate) |
|---|---|---|
| `block` | the blocks themselves | its own `block_no` |
| `tx` | transactions | the block it's in (`block_id`) |
| `tx_out` | transaction outputs | the block of the tx that created it |
| `epoch_stake` | per-epoch stake snapshots | its `epoch_no` |

These are the **dated diary entries**. Because each row knows its block/epoch, you
can say *"only compare rows up to block X."*

### Accumulator (non-anchored) tables — "a fact about a thing, with no single block"

A **dictionary / registry**: one row the **first time some thing ever appears**, and
never again — the set only **grows** (it *accumulates*). The row is a fact about the
*thing*, not a record tied to one block:

| Table | One row per… | Added when… |
|---|---|---|
| `multi_asset` | distinct token (policy + name) | that token is **first ever minted** |
| `stake_address` | distinct staking address | that address is **first ever seen** |
| `pool_hash` | distinct stake pool | that pool is **first ever seen** |

These are the **undated address-book entries**: a `multi_asset` row says "this token
exists" — it isn't "in" one block (the token is used in thousands of later blocks),
and there is **no `block_no` column** on it.

### What "bound to the cutoff" means — and why accumulators can't be bounded

The two databases are usually at **different tips** (one synced further), so we only
compare up to the **lower** tip — the **common cutoff**. (Diary analogy: one friend
wrote to Day 110, the other to Day 100, so you agree to compare *only up to Day
100*.)

To **bound** a comparison means to add a **filter** — "only rows where block ≤
cutoff." To write that filter you need a **column to filter on** (a coordinate):

- **Chain-anchored** table → you *can* bound it: `WHERE block_no ≤ cutoff`. Easy,
  the rows are dated.
- **Accumulator** → you **can't** bound it: there is no block column to filter on,
  so you can't write "only the rows up to block X." (You can't ask for "address-book
  entries from before Day 100" — they aren't dated.) The tool is forced to compare
  the **whole** table on each side.

This is also why `--block-margin` can't help accumulators (next section): a margin
is just a different cutoff, and there's still no column to apply it to.

### Reading the accumulator sentence, term by term

> *"Accumulator tables have no **chain anchor**, so the tool **can't bound** them to
> the **common cutoff** and only reports a **row count difference**. That **count
> delta** is usually just the **tip gap** (the further-synced DB has seen a few more
> **first-appearances**), but the **count alone doesn't prove it**."*

- **no chain anchor** — the row has no coordinate; it's an undated address-book entry.
- **can't bound / common cutoff** — no block column to filter on, so we can't trim
  it to the agreed stopping block; we compare the whole table.
- **row count difference** — comparing whole tables, the further DB just has **more
  rows**, so the counts differ (`COUNT_DIFF`).
- **count delta** — the size of that difference (preview run: `multi_asset` 605,979
  vs 605,947 → delta **32**).
- **tip gap** — the ahead DB was ~971 blocks further along.
- **first-appearances** — in those extra blocks, a few brand-new tokens/addresses
  appeared for the first time → a few new rows. So the +32 is *consistent with* the
  tip gap.
- **count alone doesn't prove it** — +32 could also be "34 new − 2 old rows
  wrongly missing," which would be a real bug hiding behind the same number. The
  count can't tell those apart — which is why you run the **subset check** below
  (in preview: `only_db2 = 0`, so nothing old is missing → the +32 really are just
  new tip rows).

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

### What "tip-gap delta" means (and why `--block-margin` can't fix it)

A **tip-gap delta** is the most common reason an accumulator's row count differs:
the two databases are at slightly different **tips**, so the one synced further
has seen a few more *first-appearances* (new tokens, new stake addresses) and
therefore has a few more accumulator rows. It's benign — just the cost of
comparing two DBs that aren't at the exact same block.

A natural thought is "pull the cutoff back with `--block-margin` so both sides
only cover the same range." **That doesn't help accumulators** — `--block-margin`
(like the cutoff itself) only bounds **chain-anchored** tables, because bounding
needs a block/epoch coordinate to filter on. Accumulators have **no chain
anchor**, so they're always compared whole, and a margin can't trim them. The
right tool for an accumulator `COUNT_DIFF` is not a margin — it's the subset
check below.

### How to verify an accumulator COUNT_DIFF (tip-gap or real?)

A count delta *alone* doesn't prove it's only the tip gap — in principle a DB
could be missing some old rows while having extra new ones, netting a similar
count. To be **certain**, check whether the smaller key-set is a clean **subset**
of the larger. Two ways:

**Automatic — `--verify-accumulators`.** Add the flag; for every accumulator
`COUNT_DIFF` the tool streams both natural-key sets (server-side, index-ordered,
memory-bounded) and merge-compares them, reporting `only_db1` / `only_db2`:

```
multi_asset:   only_db1=32 only_db2=0 → db2 ⊆ db1 — db1 is a clean superset (tip-gap-consistent; db1 ahead)
stake_address: only_db1=10 only_db2=0 → db2 ⊆ db1 — db1 is a clean superset (tip-gap-consistent; db1 ahead)
```

`only_db2 = 0` is the decisive fact: the behind DB has **nothing** the ahead DB
lacks, so the whole delta is extra tip rows. If **both** sides are non-zero,
it's *not* a clean tip gap and deserves a closer look.

**Manual — `psql` + `comm`** (the same thing, by hand; useful to understand it).
Real example from the preview LSM-vs-standard run (DB1 = LSM, ~971 blocks ahead;
DB2 = standard):

```bash
# 1. dump each DB's natural-key set, sorted (multi_asset's key is (policy, name))
psql -d lsm-preview... -tAc \
  "SELECT encode(policy,'hex')||':'||encode(name,'hex') FROM multi_asset" | sort > lsm.txt
psql -d preview...     -tAc \
  "SELECT encode(policy,'hex')||':'||encode(name,'hex') FROM multi_asset" | sort > std.txt

# 2. compare the two sorted sets with comm
comm -23 lsm.txt std.txt | wc -l   # keys only in LSM (ahead)   → 32
comm -13 lsm.txt std.txt | wc -l   # keys only in standard (behind) → 0  ← must be 0
```

`comm -13` (lines only in the second file) returning **0** means the behind DB is
a subset of the ahead DB → the +32 are purely tip-gap rows. For `stake_address`
the key is `encode(hash_raw,'hex')` and the result was the same (10 / 0). This is
exactly what `--verify-accumulators` automates. See the full write-up in
[benchmarks/INVESTIGATION-preview-lsm-vs-standard.md](../benchmarks/INVESTIGATION-preview-lsm-vs-standard.md).

**Next:** [Performance and scaling →](07-performance-and-scaling.md)
