# Start here

> **What's in here:** a reading map for the rest of the docs, so you read them in
> the right order for your background.

This tool rests on a few ideas that are simple once explained but easy to get
wrong. The docs build them up one at a time. Pick your starting point:

### If you are new to databases-beyond-the-basics

You know what a table, a row, a primary key (PK) and a foreign key (FK) are, but
"indexing" and "hashing" are fuzzy. **Read the primers first, in order:**

1. [Databases in 2 minutes](primers/01-databases-in-2-minutes.md) — a quick
   shared vocabulary so the rest of the docs are unambiguous.
2. [Indexes and table scans](primers/02-indexes-and-table-scans.md) — why some
   queries are instant and others read the whole table. **Important.**
3. [Hashing and fingerprints](primers/03-hashing-and-fingerprints.md) — how we
   compare two huge tables without shipping them anywhere. **Important.**
4. [Cardano and db-sync, just enough](primers/04-cardano-and-dbsync-for-this-tool.md)
   — blocks, epochs, transactions, rollbacks, and how db-sync turns them into
   rows.
5. [Surrogate ids, sequences, and drift](primers/05-surrogate-ids-sequences-and-drift.md)
   — the single most important idea: why row `id`s differ between two databases
   that hold identical data. **The most important primer.**

### Then read the core docs, in order

1. [Why compare two databases?](01-why-compare-two-databases.md) — the real-world
   QA scenario.
2. [The three hard problems](02-the-three-hard-problems.md) — what makes this
   harder than `diff`.
3. [How it works](03-how-it-works.md) — the five ideas the tool is built on.
4. [What I used and why](04-what-i-used-and-why.md) — every technique choice,
   with the alternatives I rejected and why.
5. [Running it](05-running-it.md) — install, command-line options, reading the
   output.
6. [How each table is compared](06-how-each-table-is-compared.md) — the db-sync
   schema knowledge baked into the tool.
7. [Performance and scaling](07-performance-and-scaling.md) — what makes it fast
   or slow on a 500 GB database.
8. [Case study: the pool-relay port bug](08-case-study-pool-relay-port.md) — a
   real regression the tool caught, read end to end.
9. [Extending and limitations](09-extending-and-limitations.md) — how to grow the
   tool and what it deliberately doesn't do.

### If you already know db-sync well

Skip to [The three hard problems](02-the-three-hard-problems.md) and
[How it works](03-how-it-works.md), then [Running it](05-running-it.md).
