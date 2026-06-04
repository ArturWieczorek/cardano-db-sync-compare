# Primer 04 — Cardano and db-sync, just enough

> **What's in here:** the minimum Cardano vocabulary needed to follow the rest of
> the docs, and what cardano-db-sync actually does. No prior blockchain knowledge
> assumed.
>
> **Prerequisites:** [primer 01](01-databases-in-2-minutes.md).

## The chain, in five words each

- **Block** — a batch of confirmed transactions, chained to the previous block.
  Blocks are numbered: `block_no` 0, 1, 2, … (the block "height").
- **Slot** — a tick of the clock (~1 second). Most slots are empty; some produce a
  block. `slot_no` is the absolute tick count since the chain began.
- **Epoch** — a long period of 5 days (432,000 slots). Mainnet is past epoch 600.
  Lots of bookkeeping (stake snapshots, rewards) happens once per epoch.
- **Transaction (tx)** — a transfer: it consumes some existing funds and creates
  new outputs. Identified by a unique **transaction hash**.
- **UTxO (output)** — an "unspent transaction output": a chunk of funds sitting at
  an address, waiting to be spent by a later transaction. The wallet model of
  Cardano is built from these.

Each block has a unique **block hash**, each transaction a unique **tx hash**.
These hashes come *from the blockchain itself* and are identical in every correct
copy of the chain, anywhere in the world. Remember that — they are the reliable
identifiers.

## What cardano-db-sync does

A **cardano-node** validates the blockchain and keeps it. **cardano-db-sync** is a
read-only *follower*: it connects to a node, receives the chain block by block,
and **writes the contents into a PostgreSQL database** as ordinary tables —
`block`, `tx`, `tx_out` (outputs), `tx_in` (inputs), `stake_address`,
`pool_hash` (stake pools), and ~70 more. Then explorers, wallets, and analysts
query that database with plain SQL.

db-sync does **not** decide what's valid (the node already did) and does **not**
serve queries (downstream apps do). Its one job: faithfully turn the chain into
relational rows and stay caught up with the tip.

```
cardano-node  ──(blocks)──▶  cardano-db-sync  ──(rows)──▶  PostgreSQL
```

The **tip** is the latest block. Two db-sync databases are rarely at exactly the
same tip — one may be a few thousand or a few hundred thousand blocks further
along. That matters for comparing them (see
[the three hard problems](../02-the-three-hard-problems.md)).

## Rollbacks — the chain changes its mind

Occasionally the network briefly disagrees about the most recent few blocks, then
settles. When that happens the node tells db-sync: *"discard the last N blocks; the
real chain went a different way."* This is a **rollback**. db-sync **deletes** the
rows for those blocks from PostgreSQL and then writes the rows for the blocks that
actually won.

Rollbacks only ever affect the **recent tip**; deep history is settled and never
changes. But — and this is the seed of the central problem in this tool —
rollbacks mean that two databases that ended up with the *same* final chain may
have **inserted and deleted different rows along the way**. That leaves different
fingerprints in the bookkeeping (specifically, in the row `id` numbers), which the
next primer is entirely about.

> Want more depth on db-sync's architecture, threads, and schema? The parent
> `cardano-db-sync` repository has a detailed set of docs under `.claude/docs/`.
> This primer is only what you need for the comparison tool.

**Next:** [Surrogate ids, sequences, and drift →](05-surrogate-ids-sequences-and-drift.md)
