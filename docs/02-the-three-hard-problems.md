# 02 — The three hard problems

> **What's in here:** the three things that make this much harder than running
> `diff`, each with the real mainnet numbers, and why the naive approach fails on
> each.
>
> **Prerequisites:** [primer 05](primers/05-surrogate-ids-sequences-and-drift.md).

A first attempt at this tool actually existed: it pulled every row of every table
into Python, hashed each row, sorted all the hashes, and compared. It fails on all
three problems below. Understanding why is the fastest way to understand the
design.

## Problem 1 — The two databases are at different tips

One database is almost always synced further than the other. In the validation
run:

| | 13.6.0.5 | 13.7.1.0 |
|---|---|---|
| Tip block | 13,313,031 | 13,488,662 |
| Tip epoch | 626 | 634 |

Database 2 is ~175,000 blocks ahead. If you compare whole tables, **every** table
"differs" simply because one has more rows. Meaningless.

**So:** the tool must pick a **common boundary** — the lower of the two tips — and
compare only data at or below it. (How it applies that boundary cheaply, per
table, is in [how it works](03-how-it-works.md).)

## Problem 2 — The row ids drift (and so do foreign keys)

This is the big one, covered in full in
[primer 05](primers/05-surrogate-ids-sequences-and-drift.md). The short version:
rollbacks burn `id` numbers, so the same block is row `13,013,151` in one database
and `13,000,177` in the other:

| block_no | `id` in 13.6.0.5 | `id` in 13.7.1.0 |
|----------|------------------|------------------|
| 13,000,000 | 13,013,151 | 13,000,177 |

The naive tool hashed each row *including* its `id` and foreign keys, so it would
report a mismatch on nearly every table touched by a rollback — pure noise that
hides any real difference.

**So:** the tool must compare by **meaning** — drop the `id`, translate foreign
keys to natural keys ([primer 05](primers/05-surrogate-ids-sequences-and-drift.md),
Rules 1 and 2) — before fingerprinting.

## Problem 3 — Scale

These are the biggest tables in the 13.7.1.0 mainnet database:

| Table | Rows |
|-------|------|
| `ma_tx_out` | ~1,130,000,000 |
| `epoch_stake` | ~455,000,000 |
| `reward` | ~425,000,000 |
| `tx_out` | ~348,000,000 |
| `tx_in` | ~337,000,000 |
| `tx` | ~121,000,000 |

The naive tool streamed all of these out of *both* databases and sorted a billion
hashes in memory. That cannot run on a real database — it would move terabytes
over the network and exhaust memory.

**So:** all the heavy work must happen **inside PostgreSQL**, sending back only
tiny fingerprints ([primer 03](primers/03-hashing-and-fingerprints.md)); the
biggest tables get **cheaper checks** by default; and work runs in **parallel**.
(All in [how it works](03-how-it-works.md) and
[performance and scaling](07-performance-and-scaling.md).)

## The scorecard

| Problem | Naive approach | This tool |
|---------|----------------|-----------|
| Different tips | compares whole tables → everything differs | common boundary per table |
| Id / FK drift | hashes raw ids → false mismatches everywhere | drop id, translate FKs to natural keys |
| Scale | pulls rows to client, sorts in RAM | fingerprints inside the database; tiered; parallel |

**Next:** [How it works →](03-how-it-works.md)
