# Primer 05 - Surrogate ids, sequences, and drift

> **What's in here:** the single most important idea in this whole project - why
> two databases holding *identical* blockchain data have *different* `id` numbers,
> and why that forces us to compare by *meaning* instead of by `id`.
>
> **Prerequisites:** [primer 01](01-databases-in-2-minutes.md) and
> [primer 04](04-cardano-and-dbsync-for-this-tool.md).

## The deli-ticket dispenser

When db-sync inserts a new row, where does its `id` come from? From a
**sequence** - think of the ticket dispenser at a deli counter. Every new row
pulls the next ticket: 1, 2, 3, 4, … The number just records *the order rows
arrived*. It carries **no blockchain meaning** - it isn't in the chain, it's an
internal bookkeeping number db-sync made up.

This kind of made-up unique id is called a **surrogate key** (as opposed to a
**natural key**, which is an identifier that comes from the data itself - like a
block hash or a transaction hash).

## Two honest syncs disagree on ticket numbers

Now run db-sync **twice** from scratch against the **same blockchain**. Both end
up with the same blocks and transactions - the same *data*. Do they assign the
same `id`s? Mostly… until a rollback happens.

Recall from [primer 04](04-cardano-and-dbsync-for-this-tool.md) that a rollback
**deletes** recently-added rows. But the deli dispenser **never rewinds**. If
tickets 500-520 were handed out and then those rows are deleted by a rollback,
the next row gets ticket **521**, not 500. Those 20 ticket numbers are gone
forever - a **gap**.

Two databases rarely hit the exact same rollbacks at the exact same moments. So
their gaps land in different places, and from the first differing rollback
onward, **every subsequent row has a different `id` in the two databases** - even
though the blockchain data is identical.

### This is not theoretical - here are the real numbers

Comparing two mainnet databases (db-sync 13.6.0.5 vs 13.7.1.0), we looked at the
`block` table:

| | 13.6.0.5 | 13.7.1.0 |
|---|---|---|
| Gaps in the `id` sequence (burned by rollbacks) | **16,899** | **10** |

And looking up the same block by its `block_no`:

| block_no | `id` in 13.6.0.5 | `id` in 13.7.1.0 |
|----------|------------------|------------------|
| 1            | 3          | 3          |
| 8,000,000    | 8,000,177  | 8,000,177  |
| **13,000,000** | **13,013,151** | **13,000,177** |

Early in the chain the `id`s match (same rollback history so far). Near the tip
they've drifted apart by thousands - the same block is row `13,013,151` in one
database and `13,000,177` in the other. **The `id` is not a reliable way to refer
to a row across two databases.**

## Why this breaks the obvious comparison

If you fingerprint each row including its `id`, then the same block produces a
*different* fingerprint in the two databases (because the `id` differs), and the
tool would scream "mismatch!" on essentially every table - a flood of false
alarms. So:

> **Rule 1: never include the surrogate `id` in the fingerprint.** It's
> meaningless bookkeeping that differs between databases. Drop it.

But it gets worse, because of foreign keys.

## Foreign keys drift too - and must be *translated*

Recall ([primer 01](01-databases-in-2-minutes.md)) that a foreign key stores
*another row's `id`*. For example `tx.block_id` stores the `id` of the block the
transaction belongs to. But block `id`s drift! So `tx.block_id` is `13,013,151`
in one database and `13,000,177` in the other - for the very same transaction in
the very same block. The foreign key has the same *meaning* but a different
*number*.

So we can't include raw foreign-key columns in the fingerprint either. Instead we
**translate** each foreign key into the **natural key** of the row it points at -
the identifier that's the same in both databases:

- `tx.block_id` → follow it to the `block` row, take that block's **hash** (a
  natural key). Now both databases produce the same value.
- `tx_out.tx_id` → follow to the `tx` row, take its **tx hash**.
- `ma_tx_out.ident` → follow to the `multi_asset` row, take its
  **(policy, asset-name)** pair.

> **Rule 2: replace every foreign key with the natural key of the row it points
> at, before fingerprinting.** Sometimes this chains: a multi-asset output points
> at an output, which points at a transaction, whose natural key is the tx hash.
> The tool follows the whole chain (see
> [how each table is compared](../06-how-each-table-is-compared.md)).

## The natural keys that *are* shared

Every db-sync table that other tables point at has some column(s) that come from
the blockchain and are therefore identical across databases:

| Table | Natural key (what's actually shared) |
|-------|--------------------------------------|
| `block` | block `hash` |
| `tx` | transaction `hash` |
| `stake_address` | the raw stake credential bytes |
| `pool_hash` | the pool's hash |
| `multi_asset` | (policy, asset name) |
| `tx_out` | (its transaction's hash, output index) |

These - not the `id`s - are the anchors the comparison is built on.

## One more wrinkle: db-sync doesn't *declare* its foreign keys

As noted in [primer 01](01-databases-in-2-minutes.md), db-sync doesn't ask
PostgreSQL to enforce foreign keys (for load speed). So the database can't *tell*
us "`block_id` points at `block`". Worse, the names are sometimes irregular -
some FK columns don't even end in `_id` (e.g. `drep_voter`, `return_address`,
`param_proposal`). The tool therefore carries a hand-built map of "this column
points at that table", explained in
[how each table is compared](../06-how-each-table-is-compared.md).

## Summary - the two rules the whole tool obeys

1. **Drop the surrogate `id`** from every fingerprint.
2. **Translate every foreign key** to the referenced row's **natural key** before
   fingerprinting.

With those two rules, two databases that hold the same chain produce the same
fingerprints - drift and all. Now we can actually compare them. The next docs put
this together.

**Next:** [Why compare two databases? →](../01-why-compare-two-databases.md)
