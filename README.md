# cardano-db-sync-compare

Check whether **two [cardano-db-sync](https://github.com/IntersectMBO/cardano-db-sync)
databases hold the same blockchain data** — fast enough to use as a release gate
on mainnet-sized (500 GB+) databases.

cardano-db-sync follows the Cardano chain and writes it into PostgreSQL so normal
SQL tools can read it. When a new db-sync version is released, you want proof that
it produced **the same data** as the previous version over the same chain — no
dropped rows, no corrupted values. This tool gives you that proof, per table, and
points at *where* in the chain any difference is.

It deliberately does **not** compare the two databases byte-for-byte (that would
be wrong — see [why](#why-its-built-this-way)). It compares them by *meaning*:
it fingerprints the actual blockchain content of every table, on the database
server, and reports which tables match and which differ.

---

## Quick start

```bash
# install (uv recommended)
make install                      # creates .venv, installs deps + the command
# …or with plain pip:
python3 -m venv .venv && . .venv/bin/activate && pip install -e .

# see exactly what it WILL do, without touching any data:
db-sync-compare \
  --db1 "dbname=mainnet_v1 host=/var/run/postgresql" \
  --db2 "dbname=mainnet_v2 host=/var/run/postgresql" \
  --plan

# a fast spot-check over a small window of the chain:
db-sync-compare --db1 ... --db2 ... --block-range 8000000:8010000

# the full pre-release comparison (writes a JSON report):
db-sync-compare --db1 ... --db2 ... --json report.json
```

Exit code is `0` if the databases are content-equivalent over the compared range,
`1` if discrepancies are found, `2` on an operational error — so it drops
straight into CI. Full option reference: [docs/05-running-it.md](docs/05-running-it.md).

---

## Why it's built this way

Two db-sync databases built from the same chain are **not** stored identically,
and a naive "read every row and compare" gets all of the following wrong. The
design is a direct response to three problems (the full story, from first
principles and with analogies, is in [docs/](docs/00-start-here.md)):

1. **Different tips.** One database is usually synced further than the other.
   → The tool compares only up to a **common chain boundary**, applied per table
   through an **indexed id-range window** (no whole-table scans).

2. **Surrogate id drift.** Every row gets an auto-numbered `id` from a sequence;
   rollbacks burn id numbers, so two syncs of the same chain end up with
   **different** `id`s for the same row — and every foreign key inherits the
   drift. (Measured on real mainnet DBs: 13.6.0.5 had 16,899 id-gaps in `block`,
   13.7.1.0 had 10.) → The tool **drops the `id`** and **translates every foreign
   key to the natural key** (block hash, tx hash, …) of the row it points at, so
   it compares meaning, not bookkeeping.

3. **Scale.** `ma_tx_out` has ~1.1 **billion** rows. → All hashing happens
   **inside PostgreSQL** as an order-independent, duplicate-safe **set hash**
   (sum of per-row MD5s); only a count and two numbers cross the wire. The few
   giant tables get **tiered** cheaper checks by default. On a mismatch, a
   **binary search** over chain ranges localizes it to a narrow block window.

These ideas are the same family used by battle-tested tools like Percona's
`pt-table-checksum` and Datafold's `data-diff`; the bespoke parts — natural-key
translation, a hand-built logical-FK map (db-sync declares no FK constraints),
and schema-drift tolerance — are what those generic tools can't do here. The
trade-offs behind each choice are written up in
[docs/04-what-i-used-and-why.md](docs/04-what-i-used-and-why.md).

---

## Project layout

```
cardano-db-sync-compare/
├── db_sync_comparator/        # the package (one responsibility per module)
│   ├── model.py               #   dataclasses (TableSchema/TablePlan/TableResult)
│   ├── registries.py          #   hand-built db-sync schema knowledge
│   ├── sql.py                 #   pure SQL generation (set hash, FK translation)
│   ├── schema.py              #   live-DB schema introspection
│   ├── planning.py            #   two schemas -> a per-table comparison plan
│   ├── ranges.py              #   per-DB id-range windows for the common tip
│   ├── compare.py             #   run the hashes; binary-search a mismatch
│   ├── db.py / report.py / cli.py
│   └── __main__.py            #   `python -m db_sync_comparator`
├── tests/                     # pytest suite (pure-logic; DB tests opt-in)
├── docs/                      # from-zero explanation + design rationale
├── pyproject.toml             # packaging, ruff, mypy, pytest config
├── Makefile  .pre-commit-config.yaml  .github/workflows/ci.yml
└── requirements.txt  requirements-dev.txt
```

Dependencies point inward toward `model`/`registries`; importing the top-level
package pulls in **no** database driver, so the pure-logic modules unit-test
without psycopg installed.

### Source of truth for the schema

`db_sync_comparator/registries.py` encodes db-sync schema knowledge (natural
keys, logical foreign keys, anchors) by hand — it **mirrors** db-sync but isn't
authoritative. When the schema changes, cross-check upstream, **at the git tag
matching the db-sync version you're comparing** (`master` runs ahead of releases):
the [schema reference](https://github.com/IntersectMBO/cardano-db-sync/blob/master/doc/schema.md),
the [schema source](https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/src/Cardano/Db/Schema)
(the authoritative Haskell definitions), and the
[migrations / on-the-wire DDL](https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/test/schema).
See [AGENTS.md](AGENTS.md) and [docs/09](docs/09-extending-and-limitations.md).

---

## Status

Validated against two real **mainnet** databases:

| | cardano-db-sync 13.6.0.5 | cardano-db-sync 13.7.1.0 |
|---|---|---|
| Size | 496 GB | 503 GB |
| Tip | block 13,313,031 / epoch 626 | block 13,488,662 / epoch 634 |
| Rollback id-gaps in `block` | 16,899 | 10 |

Every comparable table matched across the full shared history **except one** —
and that one was a real bug:

> **db-sync 13.7.1.0 stores stake-pool relay ports above 32767 as negative
> numbers** (a signed-16-bit overflow: `52636` is stored as `-12900`). 13.6.0.5
> stores them correctly; ~1,100+ mainnet relays are affected. The tool found and
> pinpointed it automatically — see the
> [case study](docs/08-case-study-pool-relay-port.md).

---

## Development

```bash
make check     # what CI runs: ruff check + ruff format --check + mypy + pytest
make test      # pytest only
make lint      # ruff check
make format    # ruff format (apply)
make typecheck # mypy
```

Install the git hooks so the same checks run before every commit (CI runs them
too, so passing locally is the cheapest way to keep CI green):

```bash
pre-commit install
```

`make test` runs the **DB-free** suite (pure-logic unit tests). There are also
two database-backed suites, both excluded from the default run:

```bash
# End-to-end tests on tiny SYNTHETIC fixtures against a real PostgreSQL.
# Spins a throwaway cluster via pytest-postgresql (or set
# DBSYNC_COMPARE_PG_EXTERNAL=1 + PG* to use an existing server / CI service).
# They seed two miniature db-sync-shaped DBs with deliberate id drift, a tip
# gap, and faults (corrupted value, dropped row, the pool_relay port overflow)
# and assert the tool classifies each correctly.
make test-db                       # = pytest -m fixture

# Integration tests against your OWN two db-sync databases (opt-in):
export DBSYNC_COMPARE_TEST_DSN1="dbname=v1 host=/var/run/postgresql"
export DBSYNC_COMPARE_TEST_DSN2="dbname=v2 host=/var/run/postgresql"
pytest -m integration
```

CI runs the DB-free suite on Python 3.10–3.12 and the `fixture` suite against a
PostgreSQL service container. The full testing strategy — what each tier is for,
why PostgreSQL and not SQLite, and how the synthetic fixtures work — is in
[docs/10-testing.md](docs/10-testing.md).

---

## Documentation

Start at **[docs/00-start-here.md](docs/00-start-here.md)**. The docs assume only
basic database words (table, row, primary/foreign key) and teach everything else
— indexing, hashing, Cardano, db-sync, id drift — from zero, with analogies.

## Links

- [CHANGELOG.md](CHANGELOG.md) · [AGENTS.md](AGENTS.md) · [License: Apache-2.0](LICENSE)
