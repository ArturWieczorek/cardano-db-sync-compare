# Documentation

A from-zero explanation of what this tool does and why it's built the way it is.
It assumes you know basic database words (table, row, column, primary key,
foreign key) and teaches everything else — indexing, hashing, Cardano, db-sync,
surrogate-id drift — with analogies.

**Start at [00-start-here.md](00-start-here.md)** for a reading map.

## Primers (prerequisite concepts, read first if you're new)

1. [Databases in 2 minutes](primers/01-databases-in-2-minutes.md)
2. [Indexes and table scans](primers/02-indexes-and-table-scans.md)
3. [Hashing and fingerprints](primers/03-hashing-and-fingerprints.md)
4. [Cardano and db-sync, just enough](primers/04-cardano-and-dbsync-for-this-tool.md)
5. [Surrogate ids, sequences, and drift](primers/05-surrogate-ids-sequences-and-drift.md)

## Core docs

1. [Why compare two databases?](01-why-compare-two-databases.md)
2. [The three hard problems](02-the-three-hard-problems.md)
3. [How it works](03-how-it-works.md)
4. [What I used and why](04-what-i-used-and-why.md)
5. [Running it](05-running-it.md)
6. [How each table is compared](06-how-each-table-is-compared.md)
7. [Performance and scaling](07-performance-and-scaling.md)
8. [Case study: the pool-relay port bug](08-case-study-pool-relay-port.md)
9. [Extending and limitations](09-extending-and-limitations.md)
10. [Testing strategy](10-testing.md)
