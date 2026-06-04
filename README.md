# cardano-dbsync-compare

A tool to check whether **two [cardano-db-sync](https://github.com/IntersectMBO/cardano-db-sync)
databases hold the same blockchain data** — fast enough to use as a release
gate on mainnet-sized (500 GB+) databases.

cardano-db-sync follows the Cardano blockchain and writes it into a PostgreSQL
database so normal SQL tools can read it. When a new version of db-sync is
released, you want to be sure it produced **the same data** as the previous
version over the same stretch of chain — no silently dropped rows, no corrupted
values. This tool answers that question.

It does **not** compare the two databases byte-for-byte (that would be wrong —
see below). It compares them by *meaning*: it fingerprints the actual blockchain
content of every table, on the database server, and tells you which tables match
and which differ.

---

## Why this is harder than `diff`

Two db-sync databases built from the same chain are **not** stored identically:

- They are usually at **different tips** (one synced further than the other).
- Every row has an auto-numbered `id`, and those numbers **drift** between
  databases (rollbacks burn id numbers). The `id`s — and every foreign key that
  points at them — are different even when the data is identical.
- They are **huge**: one table (`ma_tx_out`) has ~1.1 **billion** rows.

A naive "read every row and compare" approach gets all three wrong. This tool is
built around all three. The [docs](docs/00-start-here.md) explain exactly how,
from first principles.

---

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # psycopg 3

# See exactly what it WILL do, without touching any data:
python3 db_comparison.py \
  --db1 "dbname=mainnet_v1 host=/var/run/postgresql" \
  --db2 "dbname=mainnet_v2 host=/var/run/postgresql" \
  --plan

# A fast spot-check over a small window of the chain:
python3 db_comparison.py --db1 ... --db2 ... --block-range 8000000:8010000

# The full pre-release comparison (writes a JSON report):
python3 db_comparison.py --db1 ... --db2 ... --json report.json
```

Exit code is `0` if the databases are content-equivalent over the compared
range, `1` if discrepancies are found, `2` on an operational error — so it drops
straight into CI.

Full instructions: [docs/05-running-it.md](docs/05-running-it.md).

---

## Status

Validated against two real **mainnet** databases:

| | cardano-db-sync 13.6.0.5 | cardano-db-sync 13.7.1.0 |
|---|---|---|
| Size | 496 GB | 503 GB |
| Tip | block 13,313,031 / epoch 626 | block 13,488,662 / epoch 634 |
| Rollback id-gaps in `block` | 16,899 | 10 |

Result: every comparable table matched across the full shared history **except
one** — and that one was a real bug.

> **Headline finding.** db-sync **13.7.1.0** stores stake-pool relay ports above
> 32767 as **negative numbers** (a signed-16-bit overflow: `52636` is stored as
> `-12900`). Version 13.6.0.5 stores them correctly. About 1,100+ mainnet relays
> are affected. The tool found and pinpointed this automatically. See the
> [case study](docs/08-case-study-pool-relay-port.md).

---

## Documentation

Start at **[docs/00-start-here.md](docs/00-start-here.md)**. The docs assume you
know basic database words (table, row, column, primary key, foreign key) and
teach everything else — indexing, hashing, Cardano, db-sync — from zero, with
analogies.

---

## License

[Apache-2.0](LICENSE), matching cardano-db-sync upstream.
