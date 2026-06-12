# Start here

> **What's in here:** a reading map for the rest of the docs, grouped so you can read
> the part you need. The docs come in three groups: **Foundations** (general database
> ideas), **cardano-db-sync explained** (what db-sync is and how its database works),
> and **The comparison tool** (this project).

Pick your starting point by what you already know. If you are new to all of it, just
read top to bottom - each group builds on the one before.

## Group A - Foundations (general database ideas)

Generic database concepts, no Cardano knowledge needed. Read these first if
"indexing" or "hashing" are fuzzy; skim or skip if they are second nature.

1. [Databases in 2 minutes](primers/01-databases-in-2-minutes.md) - a shared
   vocabulary (tables, rows, primary keys, foreign keys) so the rest is unambiguous.
2. [Indexes and table scans](primers/02-indexes-and-table-scans.md) - why some
   queries are instant and others read the whole table. **Important.**
3. [Hashing and fingerprints](primers/03-hashing-and-fingerprints.md) - how we
   compare two huge tables without shipping them anywhere. **Important.**

## Group B - cardano-db-sync explained

What db-sync is, and how its database is built and filled. Read this group if you are
new to db-sync, or just curious how the database you are querying came to be. None of
it is required to *run* the comparison tool, but it explains most of what you see
inside a db-sync database.

4. [Cardano and db-sync, just enough](primers/04-cardano-and-dbsync-for-this-tool.md)
   - blocks, epochs, transactions, rollbacks, and how db-sync turns them into rows.
5. [Migrations and schema stages](primers/06-migrations-and-schema-stages.md) - how
   db-sync builds and updates the database shape, and what the `migration-2-0048-...sql`
   filenames and four "stages" mean.
6. [How db-sync loads a whole blockchain fast](primers/07-how-db-sync-loads-fast.md)
   - deferred indexes, bulk inserts, batching, and in-memory caches; the `--force-indexes`
   and `--disable-cache` flags.
7. [Column types and saving space](primers/08-column-types-and-saving-space.md) -
   db-sync's custom column types (DOMAINs) and the `tx_out` consumed/prune option that
   trades history for disk.

## Group C - The comparison tool (this project)

How and why this tool compares two db-sync databases. The first item is the bridge: it
explains the one db-sync quirk that shapes the entire tool.

8. [Surrogate ids, sequences, and drift](primers/05-surrogate-ids-sequences-and-drift.md)
   - the single most important idea: why row `id`s differ between two databases that
   hold identical data. **The most important primer for this tool.**
9. [Why compare two databases?](01-why-compare-two-databases.md) - the real-world QA
   scenario.
10. [The three hard problems](02-the-three-hard-problems.md) - what makes this harder
    than `diff`.
11. [How it works](03-how-it-works.md) - the five ideas the tool is built on.
12. [What I used and why](04-what-i-used-and-why.md) - every technique choice, with the
    alternatives I rejected and why.
13. [Running it](05-running-it.md) - install, command-line options, reading the output.
14. [How each table is compared](06-how-each-table-is-compared.md) - the db-sync schema
    knowledge baked into the tool.
15. [Performance and scaling](07-performance-and-scaling.md) - what makes it fast or
    slow on a 500 GB database.
16. [Case study: the pool-relay port bug](08-case-study-pool-relay-port.md) - a real
    regression the tool caught, read end to end.
17. [Extending and limitations](09-extending-and-limitations.md) - how to grow the tool
    and what it deliberately doesn't do.
18. [Testing strategy](10-testing.md) - the three tiers of tests, why PostgreSQL not
    SQLite, and how the synthetic-database tests work.

For readers who want to go **into the code** (a programmer with basic knowledge):

19. [The code, end to end](11-the-code-end-to-end.md) - a module tour and a step-by-step
    walkthrough of how the program runs, tied to real functions.
20. [The generated SQL, annotated](12-the-generated-sql-annotated.md) - the actual
    queries the tool sends, for real tables, taken apart piece by piece.

## Shortcuts

- **Already know databases well?** Skip Group A; skim Group B if db-sync is new to you.
- **Already know db-sync well?** Skip to Group C: start at
  [The three hard problems](02-the-three-hard-problems.md) and
  [How it works](03-how-it-works.md), then [Running it](05-running-it.md).
- **Just want db-sync internals?** Read Group B on its own; it stands alone.
