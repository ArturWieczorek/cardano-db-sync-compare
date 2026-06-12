# 11 - The code, end to end (a programmer's walkthrough)

> **What's in here:** how the program actually runs, from `main()` to the exit code,
> mapped onto the real modules and functions - so a programmer can read or change the
> code with a map in hand. The earlier docs explain the *ideas*; this one explains the
> *code*.
>
> **Prerequisites:** [how it works](03-how-it-works.md). The concept docs it points back
> to do the heavy explaining; this doc ties them to functions.

## The module tour

The package is `db_sync_comparator/`. Each file has one job:

| File | Job | Talks to a database? |
|------|-----|----------------------|
| `model.py` | The data classes (`TableSchema`, `TablePlan`, `TableResult`) everything else passes around. | No |
| `registries.py` | The hand-built db-sync schema knowledge: excluded tables, giant tables, the FK map, natural keys, anchors. | No |
| `sql.py` | Pure functions that build SQL **strings** (the set-hash query, the joins, the bound predicate). | No |
| `schema.py` | Reads each database's real columns, types, and indexes from the PostgreSQL catalog. | Yes |
| `planning.py` | Turns two `TableSchema`s into one `TablePlan` (what to hash, how to bound, what to drop). | No |
| `ranges.py` | Computes the per-database id-range windows for the common chain boundary. | Yes |
| `db.py` | Opens psycopg connections and streams rows. The **only** file that imports psycopg. | Yes |
| `compare.py` | The engine: run a table's hashes, decide the verdict, localize a mismatch. | Yes |
| `verify.py` | The opt-in accumulator subset check (`--verify-accumulators`). | Yes |
| `report.py` | The terminal summary and the JSON report; computes the exit-code inputs. | No |
| `cli.py` | Argument parsing and the orchestration of everything above (`main`). | Yes |

**Dependency direction:** everything points *inward* toward `model` and `registries`,
and only `db` imports psycopg. That is deliberate: it means `sql`, `planning`, and
`registries` are pure Python you can import and unit-test with **no database at all**
(see [testing](10-testing.md), Tier 1). If you ever find yourself importing psycopg
outside `db.py`, stop - that breaks the property.

## `main()` step by step

The whole run lives in `cli.py:main()`. Here it is in order, grouped into phases. Each
step names the function so you can jump to it.

### Phase 0 - setup

1. **Parse arguments** - `build_arg_parser().parse_args()`. Every flag from
   [running it](05-running-it.md) lives here.
2. **Point libpq at credentials** - if `--pgpass` was given, set `PGPASSFILE` in the
   environment so the connections can authenticate.
3. **Open two connections** - `db.connect(args.db1, ...)` and `db.connect(args.db2, ...)`.
   If either fails, print to stderr and **return exit code 2** (an operational failure,
   distinct from "found a difference").
4. **Read both schemas** - `schema.introspect(c1)` and `schema.introspect(c2)`. This asks
   the PostgreSQL catalog for every table's columns, types, primary key, and (the few)
   declared foreign keys. See "How data is obtained" below.
5. **Work out what to compare** - intersect the table names and, per table, the column
   names, so only the columns **both** databases have are ever hashed (this is how schema
   drift between versions is handled - [extending](09-extending-and-limitations.md)).
   Tables present in only one database are recorded and skipped.
6. **Find the common boundary** - `ranges.get_tip` on each database gives its tip
   `(block_no, epoch_no)`; the cutoff is the **lower** of the two, pulled back by
   `--block-margin` / `--epoch-margin` to stay out of the near-tip rollback zone
   ([performance, the rollback-zone caveat](07-performance-and-scaling.md)).
7. **Compute the id-range windows** - `ranges.compute_spine_ranges` walks
   `block -> tx -> tx_out/...` with the index-seek trick (the
   [primer 02](primers/02-indexes-and-table-scans.md) 3-minutes-to-1.8-seconds story) to
   find, **per database**, the contiguous id range that corresponds to the same stretch
   of chain. The two ranges have different numbers but select the same chain facts.

### Phase 0.5 - plan every table

8. **Build a `TablePlan` per table** - `planning.plan_table(...)`. For each table this
   decides: is it excluded? is it a giant (tiered) or normal? what is its anchor (how it
   binds to the chain)? and, column by column, whether to hash it as-is, translate it
   (a foreign key -> the target's natural key), drop it (an FK over the depth budget), or
   flag it `UNMAPPED` (looks like an FK but isn't in the registry). The output carries the
   ready-to-use `select_exprs` and `joins`.
9. **Warn about unindexed anchors** - using `schema.indexed_columns`, print a note for any
   table whose anchor column has no usable index, because windowing it falls back to a
   full scan ([performance](07-performance-and-scaling.md)).
10. **`--plan` short-circuit** - if `--plan` was passed, print each plan and the exact SQL
    it would run, then **return 0**. No data is read. (This is the auditability the raw-SQL
    design buys - see [doc 12](12-the-generated-sql-annotated.md) for what it prints.)

### Phase 1 - hash every table (in parallel)

11. **Compare tables concurrently** - a `ThreadPoolExecutor(max_workers=args.workers)`
    runs `work(plan)` for each non-excluded table. Each worker **opens its own two
    connections** (one per database), calls `compare.compare_table(...)`, and closes them.
    Results stream back as they finish, each printed with `OK` or `!!`.

    `compare_table` does, per table: run the **set-hash** query on both databases (count +
    two summed md5 halves - [primer 03](primers/03-hashing-and-fingerprints.md)); if the
    table has a numeric value column, also run the cheap **sum/min/max proof**; then pick
    a verdict (the ladder below).

### Phase 2 - localize the mismatches

12. **Pin down *where* each difference is** - for every table that came back as a real
    mismatch (`HASH_DIFF`/`COUNT_DIFF`/`VALUE_DIFF`, **excluding** accumulators and
    one-sided-zero cases, and skipped entirely in `--block-range` mode), call
    `compare.localize` (binary search) or `compare.localize_buckets` (single pass) to
    narrow it to a block/epoch window ([how it works, idea 5](03-how-it-works.md),
    [performance](07-performance-and-scaling.md)). This is **non-authoritative**: it only
    annotates *where*, never changes the verdict. A failure here is caught and printed,
    never fatal.

### Phase 3 - verify, report, exit

13. **Verify accumulators (opt-in)** - with `--verify-accumulators`, for each accumulator
    `COUNT_DIFF`, `verify.verify_accumulator` streams both natural-key sets and checks
    whether one is a clean subset of the other (the tip-gap proof from
    [doc 06](06-how-each-table-is-compared.md)).
14. **Summarise** - `report.print_summary(...)` prints the counts and returns
    `(hard, errors)`.
15. **JSON report (opt-in)** - `--json` writes a structured report via
    `report.build_json_report` / `write_json_report`.
16. **Exit code** - `return 1 if (hard or errors) else 0`.

### The flow, at a glance

```
parse args ─▶ connect x2 ─▶ introspect x2 ─▶ shared tables/cols
     │
     ▼
get tips ─▶ cutoff (minus margins) ─▶ compute_spine_ranges x2
     │
     ▼
plan_table for every table  ──(--plan? print SQL and exit 0)
     │
     ▼
Phase 1: ThreadPool ─▶ compare_table per table (count + set-hash + value proof)
     │
     ▼
Phase 2: localize the real mismatches (bisect or buckets)   [non-authoritative]
     │
     ▼
verify accumulators (opt-in) ─▶ print_summary ─▶ JSON (opt-in) ─▶ exit 0 / 1 / 2
```

## How data is obtained

- **Connections** - `db.connect(dsn, work_mem)` opens an **autocommit** psycopg
  connection and runs `SET work_mem=...`. Big `work_mem` keeps the FK-translation hash
  joins in memory instead of spilling to disk ([performance](07-performance-and-scaling.md)).
  Only `SELECT` (and session `SET`) statements are ever sent - the tool never writes.
- **Per-run connection count** - Phase 1 uses up to `2 x --workers` connections (each
  worker holds one per database), Phase 2 uses 2 more for localization, and each
  accumulator verify opens 2 of its own. Threads, not processes, because the work is
  **I/O-bound**: the client fires a query and waits on PostgreSQL, and psycopg releases
  Python's GIL while waiting on the socket, so threads give real concurrency here. (Why
  this also means the client language barely matters:
  [doc 07](07-performance-and-scaling.md#would-a-faster-language-rust-go-c-help--no-the-client-isnt-the-bottleneck).)
- **Statement timeout** - `--statement-timeout` (0 = none) is applied per query in
  `compare.run_scalar`. Use 0 for full runs; giant-table queries legitimately run over an
  hour.
- **Streaming huge key sets** - the accumulator verify can't pull 10M+ keys into RAM, so
  `db.stream_keys` uses a **named (server-side) cursor** on a read-only transaction
  (`default_transaction_read_only=on`) and yields rows in batches. The keys come out
  ordered with `COLLATE "C"` (byte order) so the two streams can be merge-compared
  directly, like the Unix `comm` command (`verify.merge_compare`).

## How a verdict is decided

`compare.compare_table` chooses a status with this ladder (first match wins):

1. **one side has 0 rows, the other does not** -> `COUNT_DIFF` noted *"likely disabled in
   config (insert_options) for that version, not a data difference"*. (Real case:
   `pool_stat` 0 vs 1.1M.) Not localized.
2. **an accumulator table with different counts** -> `COUNT_DIFF`, **informational** (the
   tip-gap delta - [doc 06](06-how-each-table-is-compared.md)).
3. **any other different count** -> `COUNT_DIFF` (a real row add/drop).
4. **same count, different hash** -> `HASH_DIFF` (a value changed - this is how the
   [pool_relay.port bug](08-case-study-pool-relay-port.md) showed up).
5. **same count and hash, different value sum/min/max** -> `VALUE_DIFF`.
6. otherwise -> `MATCH`.
7. **any exception** while comparing one table -> `ERROR` for that table only; it is
   recorded and the run continues (a bad table never aborts the others).

`report.print_summary` then counts these: `HASH_DIFF`/`VALUE_DIFF`, and `COUNT_DIFF` on a
**non-accumulator** table, are **hard** discrepancies; accumulator count-deltas are
counted separately as **informational**; `ERROR`s are their own bucket. The exit code is
`1` if there were any hard discrepancies or errors, else `0` (and `2` was the earlier
connection-failure path).

## Standards and guarantees

- **Read-only.** Only `SELECT` and session `SET` are issued; the verify path additionally
  forces a read-only transaction. The tool cannot modify, grow, or corrupt either
  database.
- **Deterministic and order-independent.** Columns are hashed in sorted-name order, rows
  are *summed* (so physical row order is irrelevant), and the JSON report is sorted by
  table name. Two honest syncs of the same chain produce identical hashes regardless of
  insert order or surrogate ids.
- **Localization never changes a verdict.** Phase 2 only re-reads data to report *where*;
  `bisect` and `buckets` are interchangeable and cannot flip `MATCH`/`DIFF` or the exit
  code.
- **Raw SQL, not an ORM.** `sql.py` builds plain strings, which is why `--plan` can show
  you the exact query for every table before you trust a result
  ([doc 12](12-the-generated-sql-annotated.md)).
- **A known checksum family.** The md5 -> summed-numeric set hash is the same idea as
  Percona `pt-table-checksum` and Datafold `data-diff`; the db-sync-specific parts
  (FK translation, chain bounding, the registries) are what those generic tools can't do
  ([doc 04](04-what-i-used-and-why.md)).
- **The registries track a db-sync version.** `registries.py` mirrors db-sync but is not
  authoritative; cross-check it at the **git tag matching the db-sync version under
  comparison** ([doc 06](06-how-each-table-is-compared.md),
  [extending](09-extending-and-limitations.md)).

**Next:** [The generated SQL, annotated →](12-the-generated-sql-annotated.md)
