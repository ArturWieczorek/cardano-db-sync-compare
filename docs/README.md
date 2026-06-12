# Documentation

A from-zero explanation of what this tool does and why it's built the way it is.
It assumes you know basic database words (table, row, column, primary key,
foreign key) and teaches everything else - indexing, hashing, Cardano, db-sync,
migrations, surrogate-id drift - with analogies.

**Start at [00-start-here.md](00-start-here.md)** for a grouped reading map. The docs
fall into three groups:

## A - Foundations (general database ideas)

1. [Databases in 2 minutes](primers/01-databases-in-2-minutes.md)
2. [Indexes and table scans](primers/02-indexes-and-table-scans.md)
3. [Hashing and fingerprints](primers/03-hashing-and-fingerprints.md)

## B - cardano-db-sync explained (what db-sync is and how its database works)

4. [Cardano and db-sync, just enough](primers/04-cardano-and-dbsync-for-this-tool.md)
5. [Migrations and schema stages](primers/06-migrations-and-schema-stages.md)
6. [How db-sync loads a whole blockchain fast](primers/07-how-db-sync-loads-fast.md)
7. [Column types and saving space](primers/08-column-types-and-saving-space.md)

## C - The comparison tool (this project)

8. [Surrogate ids, sequences, and drift](primers/05-surrogate-ids-sequences-and-drift.md) (the bridge concept)
9. [Why compare two databases?](01-why-compare-two-databases.md)
10. [The three hard problems](02-the-three-hard-problems.md)
11. [How it works](03-how-it-works.md)
12. [What I used and why](04-what-i-used-and-why.md)
13. [Running it](05-running-it.md)
14. [How each table is compared](06-how-each-table-is-compared.md)
15. [Performance and scaling](07-performance-and-scaling.md)
16. [Case study: the pool-relay port bug](08-case-study-pool-relay-port.md)
17. [Extending and limitations](09-extending-and-limitations.md)
18. [Testing strategy](10-testing.md)

Going into the code (for programmers):

19. [The code, end to end](11-the-code-end-to-end.md) - module tour + `main()` walkthrough
20. [The generated SQL, annotated](12-the-generated-sql-annotated.md) - the real queries, dissected

> The primer files are numbered 01-08 on disk for historical reasons; the groups above
> are the intended reading order (note primer 05 reads last, as the bridge into group C).
