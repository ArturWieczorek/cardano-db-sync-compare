# 10 - Testing strategy

> **What's in here:** the three kinds of tests in this repo - what each is for,
> how to run it, and (for the database-backed ones) how they actually work -
> in plain language.
>
> **Prerequisites:** none, though the primers on
> [hashing](primers/03-hashing-and-fingerprints.md) and
> [id drift](primers/05-surrogate-ids-sequences-and-drift.md) make the
> "why" land harder.

## Three tiers, on purpose

Testing a tool that talks to a 500 GB database is a balancing act: you want tests
that are **fast and run anywhere**, but also tests that prove the thing **actually
works against a real database**. So there are three tiers, each covering what the
one above it can't:

| Tier | What it tests | Needs a database? | When it runs |
|------|---------------|-------------------|--------------|
| **Unit** | the pure logic (which SQL we *generate*, registry rules) | No | always - `make test`, every CI run |
| **Fixture (e2e)** | that the generated SQL *runs on PostgreSQL and gives the right verdict*, on tiny synthetic data | Yes (throwaway) | on demand - `make test-db`; a CI job |
| **Integration** | a sanity check against *your own* two real db-sync databases | Yes (your DBs) | opt-in - you set two connection strings |

## Tier 1 - Unit tests (no database)

These check the parts of the tool that are just Python turning inputs into
strings and decisions - no database needed, so they run in milliseconds on any
machine and in every CI run. They cover:

- the **foreign-key map** (`resolve_fk`) and registry invariants (e.g. every FK
  target has a natural key),
- the **SQL we generate** (quoting, joins, the set-hash query shape, the bound
  predicate),
- **natural-key expansion** (following `tx_out → tx`, stopping at the depth
  limit),
- **plan decisions** (drop the `id`, translate FKs, flag an unmapped FK, tier the
  giants, never sum a `jsonb` column).

Run them:

```bash
make test          # = pytest  (the default run is DB-free)
```

They're fast and deterministic, but they have a blind spot: they prove we
*generate* the right SQL, not that PostgreSQL *runs* it correctly and returns the
right answer. That's what Tier 2 is for.

## Why a real PostgreSQL, and not SQLite?

A natural idea is to use SQLite files as throwaway test databases - they're tiny
and need no server. **It doesn't work here, and the reason is the whole point of
the tool.** The tool's value is the *PostgreSQL-specific* SQL it generates:
`md5(ROW(...)::text)`, the `::bit(60)::numeric` digest maths, `information_schema`
and `pg_catalog` lookups, index-seek `ORDER BY … LIMIT`. SQLite supports almost
none of it - it doesn't even have an `md5()` function.

To test on SQLite you'd have to write a **second version** of all that SQL in
SQLite's dialect - and then your tests would be checking *that* reimplementation,
not the code that runs in production. It's like testing a recipe by cooking it in
a microwave that can't do half the steps: a green result tells you nothing about
the real kitchen. For a data-integrity tool, that's the worst kind of false
confidence. So the database-backed tests use **real PostgreSQL**.

## Tier 2 - Fixture (end-to-end) tests

These run the *real* flow - introspect the schema, compute the id-range windows,
hash and compare, localize a mismatch - against **two tiny synthetic databases**
we build on a throwaway PostgreSQL. "Synthetic" (hand-made), not a slice of real
mainnet data, because for the *negative* tests we need to author the wrongness
precisely - and small hand-made data is fast, deterministic, and reviewable.

### What the two fixture databases look like

Each is a miniature "db-sync-shaped" database - a dozen blocks, a few
transactions, outputs, a multi-asset, a pool with a relay, an epoch-stake row.
The trick is that the two are **deliberately not stored identically**, in exactly
the two ways real databases differ ([the hard problems](02-the-three-hard-problems.md)):

```
            DB1 (the "old version")        DB2 (the "new version")
blocks      block_no 0..5                  block_no 0..5  + an extra 6   ← tip gap
row ids     1, 2, 3, …                     101, 102, 103, …             ← id drift
content     identical chain data           identical chain data
```

- **Id drift:** DB2's rows use shifted `id`s, so the same logical row has a
  different `id` and different foreign-key values than in DB1. A correct
  comparison must still say **MATCH** - which only happens if foreign-key
  translation ([primer 05](primers/05-surrogate-ids-sequences-and-drift.md)) is
  working. This is the single most important property to test.
- **Tip gap:** DB2 has one extra block beyond the common boundary. Bounding must
  **exclude** it, or DB2 would look "bigger" everywhere.

### What each test asserts

On top of that matching baseline, each negative test introduces **one** deliberate
fault into DB2 and checks the verdict:

| Test | Fault injected into DB2 | Expected verdict |
|------|-------------------------|------------------|
| baseline (several tables) | none | `MATCH` despite id drift |
| tip gap | (the extra block) | `block` still `MATCH`, only the 6 shared blocks compared |
| corrupted value | bump one `tx_out.value` | `HASH_DIFF` (same row count) |
| dropped row | delete one `tx_out` row | `COUNT_DIFF` |
| **port overflow** | set a `pool_relay.port` to `-12900` | `HASH_DIFF`, **localized** to the right block window |
| extra asset | insert a `multi_asset` row | `COUNT_DIFF`, flagged *informational* (accumulator) |

That port-overflow test reproduces the **real regression** this tool found on
mainnet (see the [case study](08-case-study-pool-relay-port.md)) - so we'd catch
it again if it ever came back.

### How to run them

```bash
make test-db          # = pytest -m fixture
```

Locally this uses **pytest-postgresql**, which spins up a private throwaway
PostgreSQL just for the test run and tears it down afterwards (it needs the
PostgreSQL server binaries installed, which most dev machines with Postgres
already have). Alternatively, point the tests at an **existing** server - handy
in CI, or to reuse a local one:

```bash
export DBSYNC_COMPARE_PG_EXTERNAL=1
export PGHOST=localhost PGPORT=5432 PGUSER=postgres PGPASSWORD=postgres
pytest -m fixture
```

These tests are **excluded from the default `make test`** (which stays DB-free),
so the everyday run never needs a database.

### How they work under the hood

Creating a PostgreSQL database is the slow part (it copies a template and forces
everything to disk), so the tests don't do it per test. Instead:

1. **Once per test session**, two empty databases are created and the schema is
   built in each.
2. **Before each test**, both databases are wiped (`TRUNCATE`) and reseeded with
   the baseline content - fast, no database creation. The test then mutates DB2
   to inject its fault and runs the comparison.
3. **At the end of the session**, the two databases are dropped.

The result: each test starts from an identical, clean baseline (so they don't
interfere with each other), and the whole suite runs in a couple of seconds.

## Tier 3 - Integration tests (your own databases)

When you have two actual db-sync databases handy, these run a real (small)
comparison against them - confirming the tool introspects them, computes ranges,
and reports `MATCH` for a historical window. They're **opt-in**: they only run
when you provide two connection strings, otherwise pytest skips them.

```bash
export DBSYNC_COMPARE_TEST_DSN1="dbname=mainnet_v1 host=/var/run/postgresql"
export DBSYNC_COMPARE_TEST_DSN2="dbname=mainnet_v2 host=/var/run/postgresql"
pytest -m integration
```

Use them as a final sanity check against real data; they're not part of routine
CI (CI has no access to your databases).

## What CI runs

The [CI workflow](../.github/workflows/ci.yml) has two jobs:

- the **DB-free** job runs unit tests (plus lint, format-check, and type-check) on
  Python 3.10, 3.11, and 3.12;
- the **e2e** job starts a PostgreSQL service container and runs the `fixture`
  tests against it.

So the same checks you run with `make check` (DB-free) and `make test-db` (e2e)
are exactly what CI enforces.

## Adding a test when you change the engine

If you touch the comparison logic or the [registries](06-how-each-table-is-compared.md),
add or extend a test in the matching tier - a unit test for a pure-logic change,
a `fixture` test if the behaviour only shows up end-to-end. See
[extending and limitations](09-extending-and-limitations.md) and
[AGENTS.md](../AGENTS.md).

---

Want to go into the code next? **Next:**
[The code, end to end →](11-the-code-end-to-end.md). Or back to the
[docs index](README.md) · [start here](00-start-here.md).
