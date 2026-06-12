# 01 - Why compare two databases?

> **What's in here:** the real-world reason this tool exists, and what "the two
> databases are the same" has to mean.
>
> **Prerequisites:** the [primers](primers/05-surrogate-ids-sequences-and-drift.md),
> especially primer 05.

## The scenario

cardano-db-sync ships new versions regularly. A release might rewrite how data is
stored, add a faster code path, change a library, or fix a bug elsewhere. Every
such change carries a risk: **did it accidentally change the data that lands in
PostgreSQL?** A dropped row, a mis-decoded number, a value written in the wrong
column - any of these would silently corrupt what every downstream explorer,
wallet, and analytics pipeline reads.

So a core QA task before blessing a release is:

> Sync the **old** version and the **new** version against the **same chain**,
> and prove they wrote **the same data**.

That's what this tool automates. Database 1 is (say) the trusted previous release;
database 2 is the release candidate. If every table's blockchain content matches,
the new version is data-faithful. If a table differs, the tool tells you which
table and **where in the chain** - so a human can look at the actual rows and
decide whether it's a real regression or an intended change.

## What "the same" has to mean

After the primers, you know it can't mean "byte-for-byte identical storage". Two
faithful databases differ in ways that are **expected and meaningless**:

- Their row `id`s differ (the deli-ticket drift from
  [primer 05](primers/05-surrogate-ids-sequences-and-drift.md)).
- Their foreign-key columns differ for the same reason.
- One is synced further than the other (different **tips**).
- Some tables are filled from the **network**, not the chain (pool metadata
  fetched over HTTP), so they legitimately differ run to run.

"The same" must mean: **for the stretch of chain both databases cover, every table
contains the same set of blockchain facts** - once you ignore the meaningless
bookkeeping (`id`s), translate the foreign keys to natural keys, and set aside the
genuinely non-chain tables.

That's a precise, checkable statement. Turning it into something we can compute
quickly on a 500 GB database is the rest of these docs.

## What this tool is and isn't

- **It is** a content-equivalence checker: same chain facts, yes or no, per table,
  with the differing chain region pinpointed.
- **It is not** a row-by-row visual diff tool, and not a replacement for db-sync's
  own internal consistency checks. When it flags a table, you drill into that
  table's rows yourself (the [case study](08-case-study-pool-relay-port.md) shows
  how).

**Next:** [The three hard problems →](02-the-three-hard-problems.md)
