# 04 - What I used and why

> **What's in here:** every significant technique choice, written as *what I
> considered*, *what I chose*, and *the trade-off*. This is the design rationale.
>
> **Prerequisites:** [how it works](03-how-it-works.md).

## Set-fingerprint: sum of MD5 halves

**Considered:** (a) pull every row to the client and compare; (b) sort all per-row
hashes then hash the sorted list; (c) combine per-row hashes with XOR; (d) sum the
per-row hashes.

**Chose:** (d) - MD5 each translated row, split the digest into two halves, keep a
running **sum** of each half (see
[primer 03](primers/03-hashing-and-fingerprints.md)).

**Why / trade-off:** (a) is impossible at 500 GB. (b) needs the database to sort
hundreds of millions of hashes - huge temporary space and time. (c) XOR makes
duplicate rows cancel (`x XOR x = 0`), so a duplicate-or-drop bug could hide.
Summation is **order-independent** (no sort), **streams** in constant memory, and
**counts duplicates**. The only cost is an astronomically small chance of two
different tables summing to the same pair of numbers - far below the noise floor
of hardware error, and a row-count check runs alongside anyway.

## Compute on the server, in SQL - not in the client

**Considered:** doing the hashing in Python after fetching rows, vs. expressing
the whole fingerprint as a single SQL query the database runs.

**Chose:** SQL on the server. Only a count and two numbers return per table.

**Why / trade-off:** moving 500 GB to the client to hash it defeats the purpose.
PostgreSQL has `md5()` and `sum()` built in and runs them right next to the data,
in parallel internally. The trade-off is that the comparison logic lives in
generated SQL, which is harder to read than Python - so the tool has a `--plan`
mode that prints the exact SQL it will run for every table, for auditing. See
[the generated SQL, annotated](12-the-generated-sql-annotated.md) for that output
taken apart piece by piece.

## psycopg 3 (not shelling out to `psql`)

**Considered:** driving PostgreSQL via the `psql` command-line tool vs. the
`psycopg` library.

**Chose:** [psycopg 3](https://www.psycopg.org/), the standard modern PostgreSQL
driver for Python.

**Why / trade-off:** it gives clean connection handling, parameterized queries,
and lets the tool run many tables concurrently on separate connections from
threads. `psql` would mean fragile text parsing. The trade-off is one dependency
(`pip install 'psycopg[binary]'`), which is trivial.

> **Why Python at all, and not Rust/Go/C++?** Because the client does ~0.1% of
> the work - all the hashing/joining/scanning runs inside PostgreSQL. On a full
> mainnet run the Python client used ~18 s of CPU across ~5 h of wall-clock. A
> compiled rewrite would change nothing measurable; the database is the
> bottleneck. Full analysis:
> [docs/07 - "Would a faster language help?"](07-performance-and-scaling.md#would-a-faster-language-rust-go-c-help--no-the-client-isnt-the-bottleneck).

## Bounding by id-range windows - not by joining to `block`

**Considered:** to restrict a table to the common chain boundary, either (a) join
every table up to the `block` table and filter on `block_no`, or (b) precompute a
contiguous **id range** per table and filter with `BETWEEN`
([how it works, idea 3](03-how-it-works.md)).

**Chose:** (b), the id-range window, derived with index seeks.

**Why / trade-off - with the real timing story:** approach (a) made PostgreSQL
read enormous tables top to bottom. A single boundary lookup written the obvious
way -

```sql
SELECT min(id), max(id) FROM tx WHERE block_id BETWEEN 8000177 AND 8010177;
```

- ran for **over 3 minutes**, because the database used the wrong sorted list (see
[primer 02](primers/02-indexes-and-table-scans.md)). Rewritten as an index seek
(`ORDER BY block_id … LIMIT 1`) it returned in **1.8 seconds**. The id-range
window means each table is filtered by a cheap, index-friendly `BETWEEN` on its
own column. The trade-off: it assumes ticket numbers follow chain order, which
holds for settled history but can wobble in the tip rollback zone - so we stay a
margin below the tip ([limitations](09-extending-and-limitations.md)).

## Tiered effort on the giant tables

**Considered:** fully translate-and-fingerprint every column of every table, vs.
cheaper proofs on the few billion-row tables.

**Chose:** tiered by default (count + numeric sum/min/max + a shallow fingerprint
on giants), with `--full` to force the exhaustive version everywhere.

**Why / trade-off:** the exhaustive fingerprint on `ma_tx_out` (1.1B rows) needs a
deep multi-table translation that dominates the whole run. The cheap proofs catch
real corruption (wrong totals, missing rows) in a fraction of the time. The
trade-off is a small blind spot on the giants in the default mode, which `--full`
removes when you want certainty over speed.

## Excluding network-fetched and per-instance tables

**Considered:** comparing all ~75 tables vs. excluding some by default.

**Chose:** exclude ~16 tables whose contents are *not* a function of the chain.

**Why / trade-off:** tables like `off_chain_pool_data` are filled by fetching
metadata over **HTTP** - which URLs resolved, when, and what bytes came back are
not deterministic and legitimately differ between two syncs. Tables like `meta`
and `schema_version` record *this database's* own version and start time. Hashing
these would produce differences that mean nothing. The trade-off is that those
tables aren't checked here; if you need to, compare them out of band. The full
list and the reason for each is in
[how each table is compared](06-how-each-table-is-compared.md), and `--plan`
prints them.

## Why a bespoke tool, not an existing data-diff?

**Considered:** established cross-database comparison tools -
[Datafold **data-diff**](https://github.com/datafold/data-diff),
Percona **pt-table-checksum**, **pg_comparator**.

**Chose:** a purpose-built tool that *reuses their core idea* (checksum a set of
rows and compare; binary-search to localize differences) but adds the db-sync-specific
parts they can't do.

**Why / trade-off:** those tools assume the two tables share a **stable key** to
line rows up by. Here the keys (`id`s) deliberately **don't** line up
([primer 05](primers/05-surrogate-ids-sequences-and-drift.md)), the foreign keys
need **translating to natural keys**, the foreign keys aren't even **declared** in
the database (so they must be mapped by hand), and the two databases can be at
**different schema versions**. A generic tool handles none of those. The
trade-off is that this tool carries hand-built db-sync schema knowledge that must
be maintained as the schema evolves ([extending](09-extending-and-limitations.md)) -
a deliberate price for getting the comparison *correct*.

## `work_mem` and worker parallelism

**Considered:** default PostgreSQL memory settings and serial execution.

**Chose:** raise `work_mem` per session (default `256MB`, tunable) and compare
multiple tables concurrently (`--workers`).

**Why / trade-off:** the translation joins on a full run build large in-memory
hash tables; more `work_mem` keeps them from spilling to disk. Parallel tables use
the server's cores. The trade-off is memory pressure - too many workers × large
`work_mem` can strain the server, so both are knobs, not fixed
([performance and scaling](07-performance-and-scaling.md)).

**Next:** [Running it →](05-running-it.md)
