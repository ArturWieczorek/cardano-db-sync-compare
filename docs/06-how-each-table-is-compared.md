# 06 - How each table is compared

> **What's in here:** the db-sync schema knowledge baked into the tool - the
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
> - at the git **tag matching the db-sync version you're comparing**, since
> `master` runs ahead of releases.

## 1. `EXCLUDED_TABLES` - tables we deliberately don't compare

**The big idea:** the tool answers *"do these two databases hold the same
**blockchain** data?"* About 16 tables are excluded because their contents are
**not blockchain data** - they're facts about *this database instance*, data
*downloaded from the internet*, or *operator decisions*. Comparing them would flag
differences that are **expected and meaningless**, drowning out real signals.
`--plan` prints the full list with a reason for each.

They fall into four groups.

### Group 1 - Per-instance bookkeeping (about *this* database, not the chain)

| Table | What it holds | Why excluded |
|---|---|---|
| `meta` | this sync's db-sync **version**, **start time**, network name | Two syncs started at different times by different versions → differs by definition (e.g. `start_time` = when you launched db-sync). |
| `schema_version` | the migration stage numbers (e.g. `15.48.6`) | Different db-sync versions have different schema versions - that's the point of a version. (This is how we spotted **44 vs 48**.) |
| `schema_migrations`, `extra_migrations` | the log of which migrations ran | Per-instance history. (`schema_migrations` is legacy and may not exist on newer schemas - why the run showed 16, not 17.) |

### Group 2 - Network-fetched off-chain metadata (downloaded over HTTP)

The chain only stores a **URL + a hash**. db-sync *optionally* downloads the actual
JSON and stores it. **Whether** it downloaded, **when**, and **what bytes** came
back depend on the network at that moment - not the blockchain.

| Table | What it holds | Why excluded |
|---|---|---|
| `off_chain_pool_data` | downloaded **stake-pool** metadata (name, ticker, JSON) | A pool points at e.g. `https://mypool.io/meta.json`; one sync fetched it, the other hit a 404 or a changed file → different rows, neither "wrong". |
| `off_chain_pool_fetch_error` | log of **failed** pool-metadata fetches | Depends on transient network failures. |
| `off_chain_vote_data` | downloaded **governance/DRep** metadata | Fetched from URLs the chain points at. |
| `off_chain_vote_author`, `off_chain_vote_drep_data`, `off_chain_vote_external_update`, `off_chain_vote_gov_action_data`, `off_chain_vote_reference` | fields **parsed from** the downloaded vote metadata | Derived from `off_chain_vote_data`; inherit its non-determinism. |
| `off_chain_vote_fetch_error` | log of **failed** vote-metadata fetches | Non-deterministic. |

(These fetchers are **off by default** since db-sync 13.7.0.3, so these tables are
often empty anyway.)

> **Contrast - why `pool_relay` is *not* here.** `pool_relay` holds the relay
> endpoints (ipv4/dns/**port**) a pool declares **in its on-chain registration
> certificate** - that's blockchain data, so it **is** compared. The off-chain
> *metadata JSON* (`off_chain_pool_data`) is downloaded from a URL and is excluded.
> Same pool, two different things. This is exactly why the on-chain
> `pool_relay.port` overflow was caught while off-chain noise was ignored.

### Group 3 - Volatile / operational (transient internal state)

| Table | What it holds | Why excluded |
|---|---|---|
| `epoch_sync_time` | **wall-clock seconds** this db-sync took to sync each epoch | A performance metric of the machine, not the chain. |
| `epoch_sync_enabled` | single-row toggle: whether epoch aggregation is on | Operator config, not chain-derived (it gates the `epoch` view, below). |
| `reverse_index` | a near-tip helper that makes **rollbacks** fast | Only near the tip, encodes internal **row-ids** (which drift), and gets pruned - volatile and meaningless to compare. |

> **Note - `epoch` is a view since schema stage-two v49 (`migration-2-0049`).** It
> is `epoch_finalized UNION ALL epoch_current`. Introspection compares **base
> tables only**, so the view itself is skipped; the finalized-epoch aggregate
> (`out_sum`/`fees`/tx & block counts) is compared via the **`epoch_finalized`**
> base table, bounded by epoch number `no`. `epoch_current` is the volatile,
> in-progress epoch and stays skipped - which `--epoch-margin` excluded anyway.

### Group 4 - SMASH operator state (human/admin decisions, not chain)

| Table | What it holds | Why excluded |
|---|---|---|
| `delisted_pool` | pools an operator **manually delisted** in SMASH | A moderation list set by an admin, not derived from the chain. |
| `reserved_pool_ticker` | reserved ticker names | Registry/operator policy, not chain data. |

### Is excluding them good design? - Yes; including them would be *wrong*

- **Including them would create false alarms.** `schema_version` *will* differ
  between versions; `meta.start_time` *will* differ between launches; `off_chain_*`
  *can* differ because a website was up for one sync and down for the other. If we
  hashed these, nearly every comparison would "fail" on noise and **hide** the one
  real issue (like `pool_relay.port`).
- **It's transparent, not hidden.** Each excluded table carries a one-line reason;
  `--plan` prints the full list; the summary reports the count.
- **It's reversible if you care.** If your question were "did the off-chain
  fetcher behave the same?", compare those out-of-band with a network-aware check -
  but that's a *different* question from "same chain data", and it's
  non-deterministic by nature.
- **Honest limitation:** the verdict therefore says nothing about off-chain
  metadata, sync timing, or SMASH state. For a **release data-integrity gate on
  chain content**, that's the right scope.

In short: the comparator compares **what the blockchain deterministically
produces** and excludes **what the instance / network / operator produces** - the
correct, honest way to answer "is the chain data the same?"

## 2. `GIANT_TABLES` - the ones that get tiered effort

The billion- and hundred-million-row tables (`ma_tx_out`, `epoch_stake`,
`reward`, `tx_out`, `tx_in`, `tx`, ...). By default these get the cheaper tiered
check (count + numeric sum/min/max + a shallow fingerprint); `--full` upgrades
them to the exhaustive every-column fingerprint. See
[how it works, idea 4](03-how-it-works.md) and
[performance](07-performance-and-scaling.md).

### Why giants are special

The normal fingerprint **translates every foreign key** (by joining to the row it
points at) and hashes **every column of every row**. On a billion-row table
(`ma_tx_out` is ~1.1B on mainnet; `epoch_stake`/`reward` ~450M each) that is the
single most expensive thing the tool can do. So by default giants get a *tiered*
check, and `--full` turns the tiering off and treats them like any other table.

Mechanically, "giant" means exactly one thing (`planning.py`): the FK translation
is allowed to go only **1 join deep** instead of effectively unlimited. Everything
else follows from that one cap.

```python
is_giant = name in GIANT_TABLES and not full      # --full cancels giant treatment
max_depth = giant_fk_depth if is_giant else 8      # giant_fk_depth default = 1
```

### Worked example: `ma_tx_out`, step by step

`ma_tx_out` records "this output holds this much of this token." Its columns:
`id` (surrogate), `ident` (FK -> `multi_asset`), `quantity` (a number), `tx_out_id`
(FK -> `tx_out`). Here is what the tiered check does to it.

**Step 1 - drop columns that must never be hashed.** The surrogate `id` (it drifts
between databases) and any `GENERATED` columns are dropped up front. Left:
`ident`, `quantity`, `tx_out_id`.

**Step 2 - translate foreign keys, but only 1 hop deep.** For each FK the tool
follows it to the target's *natural key* rather than hash the drifting id, capped at
`max_depth = 1`:

- `ident -> multi_asset`: `multi_asset`'s natural key is `(policy, name)` - plain
  columns, **1 hop**, within budget. Becomes `j1."policy", j1."name"`.
- `tx_out_id -> tx_out`: `tx_out`'s natural key needs *its* transaction's hash
  (`tx_out -> tx -> tx.hash`), a **2nd** hop. Over the depth-1 budget, so the tool
  **drops the column rather than hash a drifting id**, and says so loudly. That is
  the `--plan` line:

  ```
  ma_tx_out  [giant]
    columns dropped (FK over depth budget): ['tx_out.tx_id->tx (depth>1)', 'tx_out_id->tx_out']
  ```

  Tiering never silently omits anything - it reports exactly what it skipped.

**Step 3 - bound to the common slice** (identical to any table): `ma_tx_out` is
anchored by `tx_out_id` via the `tx_out` spine, so the bound is
`WHERE t0."tx_out_id" BETWEEN <lo> AND <hi>` (see [idea 3](03-how-it-works.md)).

**Step 4 - the set-hash query** over the *surviving* columns (note `tx_out_id` is
absent):

```sql
SELECT count(*) AS n,
       coalesce(sum(('x'||substr(h,1,15))::bit(60)::bigint::numeric),0) AS s1,
       coalesce(sum(('x'||substr(h,17,15))::bit(60)::bigint::numeric),0) AS s2
FROM (
  SELECT md5(ROW(j1."policy", j1."name", t0."quantity")::text) AS h
  FROM "ma_tx_out" t0
  LEFT JOIN "multi_asset" j1 ON t0."ident" = j1."id"
  LEFT JOIN "tx_out" j2 ON t0."tx_out_id" = j2."id"
  WHERE t0."tx_out_id" BETWEEN 1 AND 20356046
) q
```

The `tx_out` join (`j2`) is still emitted even though `tx_out_id` was dropped in
Step 2: the join builder registered it before the depth cut. It is harmless - it is a
`LEFT JOIN` on `tx_out`'s primary key and none of its columns are selected, so it
changes neither the row set nor the hash. (This is exactly what `--plan` prints.)

Only three numbers (`n, s1, s2`) come back over the wire - that is what makes a
billion rows feasible. (Full anatomy in
[the generated SQL, annotated](12-the-generated-sql-annotated.md).)

**Step 5 - the cheap numeric value proof.** A giant with a numeric value column
(`VALUE_COLUMNS = value, quantity, amount, fee, out_sum, deposit`) also gets a
`sum`/`min`/`max` over it - a bulk sanity check on the totals that catches corrupted
amounts fast. For `ma_tx_out` that column is `quantity`:

```sql
SELECT coalesce(sum(t0."quantity"::numeric),0),
       coalesce(min(t0."quantity"::numeric),0),
       coalesce(max(t0."quantity"::numeric),0)
FROM "ma_tx_out" t0 ... WHERE t0."tx_out_id" BETWEEN 1 AND 20356046
```

(Non-numeric "value" columns like `datum.value`, which is `jsonb`, are never summed -
the "value-column guard.")

**Step 6 - compare and classify.** Both queries run on both DBs; only the small
results travel back. The verdict is decided in this priority order (`compare.py`):
`COUNT_DIFF` (row counts differ, or one side is empty) -> `HASH_DIFF` (counts match,
summed MD5s differ) -> `VALUE_DIFF` (counts and hashes match, sum/min/max differ - a
last-resort tie-breaker) -> `MATCH`.

### The dials: `--full` and `--giant-fk-depth`

- **`--full`** raises the depth budget from 1 back to 8, so no columns are dropped:
  `ma_tx_out`'s `tx_out_id` now resolves all the way to `tx.hash` + `index`, and the
  fingerprint becomes `md5(ROW(policy, name, quantity, <tx.hash>, <index>))`. Complete,
  but the expensive billion-row deep joins are back.
- **`--giant-fk-depth N`** (default 1) is the middle ground: raise the giant depth
  budget without going all the way to `--full`.

| | Default (tiered) | `--full` |
|---|---|---|
| FK depth budget | 1 | 8 |
| `ident -> multi_asset` | `(policy, name)` | `(policy, name)` |
| `tx_out_id -> tx_out -> tx` | dropped (depth>1), reported | `tx.hash`, `index` |
| Row fingerprint | `md5(ROW(policy, name, quantity))` | `md5(ROW(policy, name, quantity, tx.hash, index))` |
| Value proof on `quantity` | sum/min/max | sum/min/max |
| Cost on a billion rows | cheap | expensive |

### Localizing a giant mismatch

If a giant's fingerprints differ, Phase 2 narrows down *where*. Because bisection
would re-scan the billion-row table ~12 times, this is exactly where
**`--localize buckets`** (one pass) pays off - see
[performance](07-performance-and-scaling.md#localizing-a-mismatch---localize-bisect-vs---localize-buckets).

**In one sentence:** a giant is compared by dropping its id, translating only its
1-hop foreign keys (and loudly reporting the deeper ones it drops), bounding it to
the common slice, then taking a count + summed-MD5 fingerprint over the surviving
columns plus a sum/min/max of its numeric value column - a fast "good-enough" proof
by default, upgradable to the exhaustive every-column hash with `--full`.

## 3. `NATURAL_KEYS` - the shared identifier of each table

For every table that other tables point at, this says *what its natural key is* -
the column(s) that come from the blockchain and so match across databases
([primer 05](primers/05-surrogate-ids-sequences-and-drift.md)). Examples:

| Table | Natural key |
|-------|-------------|
| `block` | `hash` |
| `tx` | `hash` |
| `stake_address` | `hash_raw` |
| `pool_hash` | `hash_raw` |
| `multi_asset` | (`policy`, `name`) |
| `address` | `raw` (Address variant only - see below) |
| `tx_out` | (its tx's `hash`, `index`) |
| `gov_action_proposal` | (its tx's `hash`, `index`) |

When the tool translates a foreign key, it looks up the target table here. Some
natural keys are themselves built from a foreign key (a `tx_out`'s key needs its
transaction's hash), so translation **chains** until it reaches plain chain data.

> **The two TxOut variants - Core and Address.** db-sync can store outputs two
> ways ([db-sync's `use_address_table`](https://github.com/IntersectMBO/cardano-db-sync/blob/master/doc/configuration.md)):
> in the **Core** variant the address is an inline column on `tx_out`, so it is
> fingerprinted directly. In the **Address** variant the address is deduplicated
> into a separate `address` table and `tx_out`/`collateral_tx_out` point at it via
> `address_id`. The tool supports both: `address_id` is mapped to `address`
> (so it translates to the address's natural key, `raw`, just like any other FK),
> and the `address` table itself is an accumulator (one row per distinct address).
> The two variants can't be compared *against each other* (different columns), but
> two databases that both use the Address variant - or both use Core - compare
> correctly. The `address` entries are simply never consulted when comparing Core
> databases, since neither the column nor the table exists there.

## 4. `GLOBAL_FK` and `FK_MAP` - the foreign-key map

Since the database doesn't declare foreign keys, the tool maps them by column name.

- **`GLOBAL_FK`** handles names that mean the same thing everywhere: `block_id` →
  `block`, `tx_id` → `tx`, `addr_id` → `stake_address`, `ident` → `multi_asset`,
  `address_id` → `address` (Address variant), and so on - **including the
  irregular ones that don't end in `_id`** (`drep_voter`
  → `drep_hash`, `return_address` → `stake_address`, `param_proposal` →
  `param_proposal`, `prev_gov_action_proposal` → `gov_action_proposal`, `invalid`
  → `event_info`).
- **`FK_MAP`** overrides the rare ambiguous cases where the *same* column name
  means different things in different tables - e.g. `tx_out_id` points at `tx` in
  `tx_in` but at `tx_out` in `ma_tx_out`; `hash_id` points at `pool_hash` in
  `pool_retire` but at `drep_hash` in `drep_distr`.

Safety net: if a column *looks* like a foreign key (ends in `_id`) but isn't in
either map, the tool **excludes it from the fingerprint and flags it loudly**
rather than risk hashing a drifting id. `--plan` shows zero such "UNMAPPED"
columns on the current schema - but this is how a future schema addition would
surface.

## 5. `ANCHORS` - how a table is tied to a chain coordinate

To bound a table to the common boundary ([how it works, idea 3](03-how-it-works.md)),
the tool needs to know how each table connects to the chain. `ANCHORS` records
that. Crucially, the boundary's id-ranges are computed only for the **five spine
tables** (`block`, `tx`, `tx_out`, `pool_update`, `gov_action_proposal`); every
other table just *reuses* whichever spine range its anchor points at - so the
window is computed per spine, not per table (see
[idea 3, "one boundary, a few ranges"](03-how-it-works.md)). The anchor shapes:

- **`block` itself** - bounded by its own `id`.
- **via a transaction** - e.g. `tx_out` through `tx_id`, `tx_in` through
  `tx_in_id`; bounded by the transaction id-range.
- **via the output then the transaction** - `ma_tx_out` through
  `tx_out_id → tx_out → tx`.
- **via a pool update or a governance proposal** - for the pool and governance
  child tables.
- **by epoch** - `epoch_stake`, `reward`, `drep_distr`, `pool_stat`, ... are bounded
  by `epoch_no` (they're written per epoch). `epoch_finalized` (below) is bounded
  the same way, by its `no` column.
- **accumulator** - tables with no clean chain anchor (next section).

> **db-sync 13.7.2.1 note.** Migration `migration-2-0049` turned `epoch` from a base
> table into a *view* (`epoch_finalized UNION ALL epoch_current`) and added the
> `epoch_finalized` base table plus the `epoch_current` view. The comparator reads
> only base tables (it drops views), so on the new schema the finalised epoch
> aggregates are compared through **`epoch_finalized`** - epoch-bounded, exactly as
> the old `epoch` table was - while the volatile in-progress epoch (`epoch_current`,
> and thus the `epoch` view) is correctly skipped. Because the two objects have
> different names, a *cross-version* run (pre-13.7.2.1 vs 13.7.2.1+) cannot line them
> up; the tool prints a note when it detects that split.

### Rows with no chain position: `NULL_ANCHOR_BOUNDS`

Governance tables such as `committee` and `constitution` are tied to the chain
through the proposal that created them, `gov_action_proposal_id`, and bounded with

```
WHERE t0."gov_action_proposal_id" BETWEEN <low> AND <high>
```

But the **genesis** committee and the **genesis** constitution - the ones Conway
started with - were not created by any proposal. db-sync inserts them at the first
Conway epoch boundary with `gov_action_proposal_id` = NULL. In SQL,
`NULL BETWEEN 1 AND 9` is not true, so those two rows were quietly left out of
every comparison. A wrong genesis quorum would have shown as `MATCH`.

Real data, both db-sync 13.8.0.0 databases:

| | preview | preprod |
|---|---|---|
| genesis committee | id 1, quorum 2/3, first in force in epoch **646** | id 1, quorum 2/3, epoch **163** |
| genesis constitution | id 1, anchor `ipfs://bafkreifnwj6...`, epoch **646** | same anchor, epoch **163** |

These rows have no block, but they do have a time: the `epoch_state` table records,
per epoch, which committee and constitution were in force, and db-sync writes the
genesis rows and that `epoch_state` row together. So a NULL-anchored row is now
included when an `epoch_state` row points at it in an epoch up to the cutoff epoch:

```
WHERE (t0."gov_action_proposal_id" BETWEEN 1 AND 1525
       OR (t0."gov_action_proposal_id" IS NULL AND EXISTS (
             SELECT 1 FROM "epoch_state" na
             WHERE na."committee_id" = t0."id" AND na."epoch_no" <= 1364)))
```

That keeps the "only what both databases have" rule. If one database stopped
before the Conway boundary, its `epoch_state` has no such row, and in the other
database the genesis row first appears after the cutoff - so it is left out on
**both** sides and nothing false is reported.

Where it applies: Phase 1 in normal (cutoff) mode, and `--plan` shows the extra
condition. Not in `--block-range` mode, which is a window of block positions, and
these rows have none. Phase 2 cannot put them in a block window either, so it
checks them as a group of their own and, when they differ, adds a line such as

```
committee: rows with no block (gov_action_proposal_id IS NULL, the genesis row): content differs (db1 n=1, db2 n=1)
```

next to any block windows (in the JSON it is `localized_no_block`, kept apart from
the `localized` window list) - then query the row with
`gov_action_proposal_id IS NULL` in both databases. The registry entry is `NULL_ANCHOR_BOUNDS` in
`registries.py`. `event_info.tx_id` is also nullable, but db-sync never writes
`event_info`, so it is not listed.

### Columns db-sync fills in later: `LIFECYCLE_EPOCH_COLUMNS`

Bounding works because a row below the cutoff normally never changes again. One
table breaks that rule. A **governance proposal** (`gov_action_proposal`) is
written when it is submitted, but four of its columns are filled in **later**, at
the epoch boundary where the event happens:

| Column | Set when |
|--------|----------|
| `ratified_epoch` | the proposal is ratified |
| `enacted_epoch` | it is enacted |
| `expired_epoch` | it expires |
| `dropped_epoch` | it is dropped (removed from the ledger) |

Think of a library card. The borrowing date is written when you take the book;
the return date is filled in weeks later. Two photocopies of the card made on
different days disagree on the return date, although nothing is wrong with
either copy - one was simply made before the book came back.

Real example (preprod, db-sync 13.7.2.1 vs 13.8.0.0): proposal `c26ecb07...#0` was
created in epoch 295.

| | 13.7.2.1 database (tip in epoch 302) | 13.8.0.0 database (tip in epoch 322) |
|---|---|---|
| `expired_epoch` | 302 | 302 |
| `dropped_epoch` | NULL | **303** |

The older database never reached epoch 303, so it could not know. Hashed raw, this
made `gov_action_proposal` a `HASH_DIFF` (and the run exit with 1) in **every**
comparison whose tips were in different epochs - even between two copies of the
same db-sync version.

So these columns are compared **only up to the cutoff epoch**. Each value is
hashed as NULL when it is above the cutoff epoch:

```
CASE WHEN t0."dropped_epoch" <= 300 THEN t0."dropped_epoch" END
```

With a cutoff epoch of 300, both databases hash the proposal above as
`(expired NULL, dropped NULL)` - a MATCH. A value of 300 or lower is compared
exactly, so a real disagreement (say one database ratified a proposal in epoch 299
and the other did not) is still a `HASH_DIFF`. That is safe because both databases
have processed every epoch boundary up to the lower tip's epoch, and the cutoff
epoch is at or below it.

How to see it: the run prints a `note: lifecycle epochs ... compared only up to
epoch N` line, `--plan` shows the `CASE WHEN` SQL and a `compared only up to epoch
N` line under the table, and the `--json` report lists the columns in each table's
`epoch_capped_cols`. When no value is above the cutoff epoch, the hash is exactly
the same as hashing the raw columns.

What it gives up: a disagreement in the few epochs between the cutoff epoch and
the lower tip (the `--epoch-margin`, 2 by default) is not compared - the same
window the epoch-bounded tables (`epoch_stake`, `reward`, ...) already leave out.
The list lives in `LIFECYCLE_EPOCH_COLUMNS` in `registries.py`; add a table there
only if db-sync updates a plain epoch column of an already-written, block-anchored
row (a test checks both conditions).

## Chain-anchored vs accumulator tables (the diary analogy)

This is the idea behind `ANCHORS` and the whole "accumulator" notion, in plain
language.

### The one idea: a "chain coordinate"

The blockchain is just a long sequence of **blocks**, numbered 0, 1, 2, 3, … (that
number is `block_no`). Epochs (≈5-day periods) are numbered too. So "block
8,000,000" or "epoch 291" is an **address - a position - on the chain**. Call it a
*coordinate*.

**The diary analogy.** Picture the chain as a **diary** with **one page per day**,
where the page number is the block number.

- Most things you write are **dated** - they live on a specific page (block).
- But at the **back** of the diary you keep an **address book**: the first time you
  ever meet someone, you write their name once. Those entries are **not dated** -
  it's just a running list of everyone you've ever met.

*Dated entry* vs *undated address-book entry* is exactly *chain-anchored table* vs
*accumulator table*.

### Chain-anchored tables - "this row has a place on the chain"

Every row can be tied to a specific block (or epoch) - you can answer *"which block
does this row belong to?"* - either **directly** (the row has a block number) or
**indirectly** (it points at a transaction, which is in a block):

| Table | What it holds | Its "date" (chain coordinate) |
|---|---|---|
| `block` | the blocks themselves | its own `block_no` |
| `tx` | transactions | the block it's in (`block_id`) |
| `tx_out` | transaction outputs | the block of the tx that created it |
| `epoch_stake` | per-epoch stake snapshots | its `epoch_no` |
| `epoch_finalized` | per-epoch aggregates (the `epoch` view's backing table) | its `no` |

These are the **dated diary entries**. Because each row knows its block/epoch, you
can say *"only compare rows up to block X."*

### Accumulator (non-anchored) tables - "a fact about a thing, with no single block"

A **dictionary / registry**: one row the **first time some thing ever appears**, and
never again - the set only **grows** (it *accumulates*). The row is a fact about the
*thing*, not a record tied to one block:

| Table | One row per… | Added when… |
|---|---|---|
| `multi_asset` | distinct token (policy + name) | that token is **first ever minted** |
| `stake_address` | distinct staking address | that address is **first ever seen** |
| `pool_hash` | distinct stake pool | that pool is **first ever seen** |

These are the **undated address-book entries**: a `multi_asset` row says "this token
exists" - it isn't "in" one block (the token is used in thousands of later blocks),
and there is **no `block_no` column** on it.

### What "bound to the cutoff" means - and why accumulators can't be bounded

The two databases are usually at **different tips** (one synced further), so we only
compare up to the **lower** tip - the **common cutoff**. (Diary analogy: one friend
wrote to Day 110, the other to Day 100, so you agree to compare *only up to Day
100*.)

To **bound** a comparison means to add a **filter** - "only rows where block ≤
cutoff." To write that filter you need a **column to filter on** (a coordinate):

- **Chain-anchored** table → you *can* bound it: `WHERE block_no ≤ cutoff`. Easy,
  the rows are dated.
- **Accumulator** → you **can't** bound it: there is no block column to filter on,
  so you can't write "only the rows up to block X." (You can't ask for "address-book
  entries from before Day 100" - they aren't dated.) The tool is forced to compare
  the **whole** table on each side.

This is also why `--block-margin` can't help accumulators (next section): a margin
is just a different cutoff, and there's still no column to apply it to.

### Reading the accumulator sentence, term by term

> *"Accumulator tables have no **chain anchor**, so the tool **can't bound** them to
> the **common cutoff** and only reports a **row count difference**. That **count
> delta** is usually just the **tip gap** (the further-synced DB has seen a few more
> **first-appearances**), but the **count alone doesn't prove it**."*

- **no chain anchor** - the row has no coordinate; it's an undated address-book entry.
- **can't bound / common cutoff** - no block column to filter on, so we can't trim
  it to the agreed stopping block; we compare the whole table.
- **row count difference** - comparing whole tables, the further DB just has **more
  rows**, so the counts differ (`COUNT_DIFF`).
- **count delta** - the size of that difference (preview run: `multi_asset` 605,979
  vs 605,947 → delta **32**).
- **tip gap** - the ahead DB was ~971 blocks further along.
- **first-appearances** - in those extra blocks, a few brand-new tokens/addresses
  appeared for the first time → a few new rows. So the +32 is *consistent with* the
  tip gap.
- **count alone doesn't prove it** - +32 could also be "34 new - 2 old rows
  wrongly missing," which would be a real bug hiding behind the same number. The
  count can't tell those apart - which is why you run the **subset check** below
  (in preview: `only_db2 = 0`, so nothing old is missing → the +32 really are just
  new tip rows).

## The "accumulator" tables

A few "definition" tables - `multi_asset`, `stake_address`, `pool_hash`,
`drep_hash`, `committee_hash`, `cost_model`, `slot_leader` (and `address` in the
Address variant) - just **accumulate** one row the first time some object ever
appears, with no tidy link back to a single block. The tool can't cheaply bound them to the window, so it compares them
**whole** and treats a row-count difference as **informational**: when database 2
is synced further, it has simply seen more distinct objects. (Example from the
validation run: `multi_asset` 11,108,713 vs 11,128,803 - the ~20k extra are tokens
first minted in the blocks only database 2 has.) A count difference here is
expected; it is **not** a failure.

### What "tip-gap delta" means (and why `--block-margin` can't fix it)

A **tip-gap delta** is the most common reason an accumulator's row count differs:
the two databases are at slightly different **tips**, so the one synced further
has seen a few more *first-appearances* (new tokens, new stake addresses) and
therefore has a few more accumulator rows. It's benign - just the cost of
comparing two DBs that aren't at the exact same block.

A natural thought is "pull the cutoff back with `--block-margin` so both sides
only cover the same range." **That doesn't help accumulators** - `--block-margin`
(like the cutoff itself) only bounds **chain-anchored** tables, because bounding
needs a block/epoch coordinate to filter on. Accumulators have **no chain
anchor**, so they're always compared whole, and a margin can't trim them. The
right tool for an accumulator `COUNT_DIFF` is not a margin - it's the subset
check below.

### How to verify an accumulator COUNT_DIFF (tip-gap or real?)

A count delta *alone* doesn't prove it's only the tip gap - in principle a DB
could be missing some old rows while having extra new ones, netting a similar
count. To be **certain**, check whether the smaller key-set is a clean **subset**
of the larger. Two ways:

**Automatic - `--verify-accumulators`.** Add the flag; for every accumulator
`COUNT_DIFF` the tool streams both natural-key sets (server-side, index-ordered,
memory-bounded) and merge-compares them, reporting `only_db1` / `only_db2`:

```
multi_asset:   only_db1=32 only_db2=0 → db2 ⊆ db1 - db1 is a clean superset (tip-gap-consistent; db1 ahead)
stake_address: only_db1=10 only_db2=0 → db2 ⊆ db1 - db1 is a clean superset (tip-gap-consistent; db1 ahead)
```

`only_db2 = 0` is the decisive fact: the behind DB has **nothing** the ahead DB
lacks, so the whole delta is extra tip rows. If **both** sides are non-zero,
it's *not* a clean tip gap and deserves a closer look.

**Why "subset" is exactly the signature of a pure tip gap.** Accumulators are
**append-only** - they only ever *grow* as you sync further (a row is added the first
time an object appears, never removed). So if the two databases follow the same chain
and one is simply behind, then *everything the behind DB has seen, the ahead DB has
also seen*, plus a few extras it hasn't reached yet - which is the literal definition
of a subset. That's why "behind DB's key set is contained in the ahead DB's" proves
the difference is only sync position. If instead each DB holds a key the other lacks
(neither is a subset), no "one is just ahead" story explains it - the sets have truly
diverged, so it's a real difference to investigate.

**Manual - `psql` + `comm`** (the same thing, by hand; useful to understand it).
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
