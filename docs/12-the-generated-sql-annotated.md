# 12 - The generated SQL, annotated

> **What's in here:** the *actual* SQL the tool sends to PostgreSQL, for real tables,
> taken apart piece by piece. Everything in the concept docs (the set hash, the id-range
> window, foreign-key translation) comes together in one query per table - this shows you
> that query and labels every part.
>
> **Prerequisites:** [primer 03 (hashing)](primers/03-hashing-and-fingerprints.md),
> [how it works](03-how-it-works.md), and
> [how each table is compared](06-how-each-table-is-compared.md). Helpful:
> [the code walkthrough](11-the-code-end-to-end.md).

## How to see this yourself

The tool can print the exact SQL it would run for every table, **without touching any
data**, with `--plan`:

```bash
db-sync-compare --db1 "dbname=... host=/var/run/postgresql" \
                --db2 "dbname=... host=/var/run/postgresql" \
                --tables pool_relay,tx_out,ma_tx_out --plan
```

Everything below is real `--plan` output from two mainnet databases (db-sync 13.6.0.5 vs
13.7.1.0), reformatted only for line width.

## How the id-range bounds are computed

The per-table queries below all carry a bound like `WHERE t0."tx_id" BETWEEN 1 AND ...`.
This section shows where those numbers come from - the SQL that runs *before* any table
is fingerprinted, to turn the one common boundary into the handful of spine ranges every
table reuses ([how it works, idea 3](03-how-it-works.md#one-boundary-a-few-ranges-reused-by-every-table)).
It lives in `ranges.py`. The numbers here are real, from preprod (db-sync 13.7.1.0, tip
block 4,804,649). You can run every query below by hand on your own database.

**Step 1 - each database's tip** (`ranges.get_tip`). One query per database:

```sql
SELECT max(block_no), max(epoch_no) FROM block;   -- preprod: 4804649, 293
```

**Step 2 - the common boundary (once, total).** The tool takes the *lower* of the two
tips and pulls it back by a margin: `cutoff_block = min(tip1, tip2) - --block-margin`
(default 0) and `cutoff_epoch = min(epoch1, epoch2) - --epoch-margin` (default 2, to skip
the in-progress epoch and the volatile rollback zone). For a preprod DB compared with
itself that is `block_no <= 4804649`, `epoch_no <= 291`. This is plain arithmetic in
Python, not a query.

**Step 3 - the `block` id-range.** The largest `block.id` at or below the cutoff block
(`compute_spine_ranges`, cutoff mode):

```sql
SELECT id FROM block WHERE block_no <= 4804649 ORDER BY block_no DESC, id DESC LIMIT 1;
-- preprod -> 4804652, so the block range is (0, 4804652)
```

Two subtleties. First, the range **starts at `0`, not at the first block id** - that
deliberately keeps the genesis / EBB rows whose `block_no` is `NULL` and which sort below
everything. Second, note it is `ORDER BY ... DESC LIMIT 1`, **not** `SELECT max(id) ...
WHERE block_no <= ...`. With a filter on the non-primary-key `block_no`, the planner would
turn `max(id)` into a scan of a huge slice of the table; the `ORDER BY ... LIMIT 1` form is
an **index seek** that jumps straight to the row in milliseconds
([primer 02](primers/02-indexes-and-table-scans.md) - the trick that turned 3 minutes into
1.8 seconds). Every step here uses that form.

**Step 4 - walk the spine to the other ranges** (`ranges._minmax`). Each remaining spine
range is the smallest and largest id whose foreign key falls in the previous range - two
index seeks, `ASC` and `DESC`. `tx` off the block range:

```sql
SELECT
  (SELECT id FROM tx WHERE block_id BETWEEN 0 AND 4804652 ORDER BY block_id ASC,  id ASC  LIMIT 1),
  (SELECT id FROM tx WHERE block_id BETWEEN 0 AND 4804652 ORDER BY block_id DESC, id DESC LIMIT 1);
-- preprod -> (1, 5990121)
```

then `tx_out` off the `tx` range (and, the same way, `pool_update` off `registered_tx_id`
and `gov_action_proposal` off `tx_id`):

```sql
SELECT
  (SELECT id FROM tx_out WHERE tx_id BETWEEN 1 AND 5990121 ORDER BY tx_id ASC,  id ASC  LIMIT 1),
  (SELECT id FROM tx_out WHERE tx_id BETWEEN 1 AND 5990121 ORDER BY tx_id DESC, id DESC LIMIT 1);
-- preprod -> (1, 20356046)
```

That is the **whole** boundary computation: five seeks' worth of ranges, once per database.
`--plan` prints the result as one line:

```
DB1 spine id-ranges: {'block': (0, 4804652), 'tx': (1, 5990121),
                      'tx_out': (1, 20356046), 'pool_update': (1, 1273),
                      'gov_action_proposal': (1, 100)}
```

The two databases get *different* numbers here (id drift), but each pair selects the same
slice of chain. Every table in the examples below then drops the one range its anchor
points at into a `BETWEEN` - `tx_out` uses the `tx` range, `pool_relay` the `pool_update`
range, `ma_tx_out` the `tx_out` range, and so on. Epoch-written tables (`epoch_stake`, ...)
skip this entirely and bound on `epoch_no <= 291`; accumulator tables (`multi_asset`, ...)
carry `WHERE TRUE`.

## Example 1: a simple table with one translation - `pool_relay`

```sql
SELECT count(*) AS n,
       coalesce(sum(('x'||substr(h,1,15))::bit(60)::bigint::numeric),0) AS s1,
       coalesce(sum(('x'||substr(h,17,15))::bit(60)::bigint::numeric),0) AS s2
FROM (
  SELECT md5(ROW(t0."dns_name", t0."dns_srv_name", t0."ipv4", t0."ipv6",
                 t0."port", j2."hash", j1."cert_index")::text) AS h
  FROM "pool_relay" t0
  LEFT JOIN "pool_update" j1 ON t0."update_id" = j1."id"
  LEFT JOIN "tx" j2 ON j1."registered_tx_id" = j2."id"
  WHERE t0."update_id" BETWEEN 1 AND 36747
) q
```

Read it from the inside out - that is the order the ideas stack up.

**The `FROM` and the joins (foreign-key translation).** `pool_relay` is aliased `t0`. A
relay belongs to a pool registration, so the tool joins `pool_update` (alias `j1`) on
`t0."update_id" = j1."id"`, and then `tx` (alias `j2`) on `j1."registered_tx_id" =
j2."id"`. Why: `update_id` is a surrogate id that **drifts** between the two databases
([primer 05](primers/05-surrogate-ids-sequences-and-drift.md)), so it must not be hashed
raw. Instead the tool follows it to the registration transaction and pulls that
transaction's **`hash`** (`j2."hash"`) - a natural key that is identical in both
databases. The aliases `j1`, `j2` are handed out by the idempotent `JoinBuilder` in
`sql.py`; a table reached twice would be joined only once.

**The row fingerprint.** `md5(ROW(...)::text) AS h` glues this relay's meaningful fields
into one string and hashes it: the relay's own plain columns (`dns_name`, `dns_srv_name`,
`ipv4`, `ipv6`, `port`), plus the **translated** values (`j2."hash"` for the transaction,
`j1."cert_index"` from the pool update). Note `port` is hashed directly - that is the
column whose signed-16-bit overflow the [case study](08-case-study-pool-relay-port.md)
caught; any change to it changes `h`.

**The bound predicate (the id-range window).** `WHERE t0."update_id" BETWEEN 1 AND 36747`
restricts the table to the common chain boundary. `36747` is *this database's*
`pool_update` id at the cutoff block; the other database uses its own number for the same
chain point ([how it works, idea 3](03-how-it-works.md)). It is a plain indexed
`BETWEEN`, so no join to `block` and no full scan.

**The set hash (the outer query).** Each row produced one md5 string `h`. The outer query
turns the whole set of them into three numbers:

- `count(*) AS n` - how many rows (catches added/dropped rows).
- `sum(('x'||substr(h,1,15))::bit(60)::bigint::numeric)` - take hex characters 1-15 of
  each md5 (a 60-bit chunk), read them as a number, and **sum** across all rows.
- the second `sum(...)` does the same with hex characters 17-31 (the other 60-bit chunk).

Summing is the trick from [primer 03](primers/03-hashing-and-fingerprints.md): addition
ignores row order and does not cancel duplicates, so two databases that hold the same set
of relays produce the same `(n, s1, s2)` no matter how the rows are physically stored.
Two 60-bit chunks (not the full 128 bits) keep each sum inside a signed `bigint` after the
bit cast. Only those three numbers travel back over the network.

## Example 2: a giant with many translations - `tx_out`

```sql
SELECT count(*) AS n, coalesce(sum(...),0) AS s1, coalesce(sum(...),0) AS s2
FROM (
  SELECT md5(ROW(t0."address", t0."address_has_script", j1."hash", t0."data_hash",
                 t0."index", j2."hash", t0."payment_cred", j3."hash",
                 j4."hash_raw", j5."hash", t0."value")::text) AS h
  FROM "tx_out" t0
  LEFT JOIN "tx" j1 ON t0."consumed_by_tx_id" = j1."id"
  LEFT JOIN "datum" j2 ON t0."inline_datum_id" = j2."id"
  LEFT JOIN "script" j3 ON t0."reference_script_id" = j3."id"
  LEFT JOIN "stake_address" j4 ON t0."stake_address_id" = j4."id"
  LEFT JOIN "tx" j5 ON t0."tx_id" = j5."id"
  WHERE t0."tx_id" BETWEEN 1 AND 120373367
) q
```

Same shape, more translations. A `tx_out` row carries five foreign keys, and each is
translated to a natural key: the consuming transaction's `hash` (`j1`), the inline datum's
`hash` (`j2`), the reference script's `hash` (`j3`), the stake address's `hash_raw`
(`j4`), and the creating transaction's `hash` (`j5`). The plain columns (`address`,
`value`, `index`, `payment_cred`, ...) are hashed as-is. The window is on `tx_id` (the
spine column for outputs). Notice the **same table can be joined twice** under different
aliases (`tx` is both `j1` via `consumed_by_tx_id` and `j5` via `tx_id`) - two different
relationships, two aliases.

### The cheap numeric proof (giants only)

`tx_out` and `ma_tx_out` are [giant tables](06-how-each-table-is-compared.md), so by
default (no `--full`) they also get a fast sum/min/max check on their numeric value
column. For `ma_tx_out` (value column `quantity`) the tool also runs:

```sql
SELECT coalesce(sum(t0."quantity"::numeric),0),
       coalesce(min(t0."quantity"::numeric),0),
       coalesce(max(t0."quantity"::numeric),0)
FROM "ma_tx_out" t0
  LEFT JOIN "multi_asset" j1 ON t0."ident" = j1."id"
  LEFT JOIN "tx_out" j2 ON t0."tx_out_id" = j2."id"
  WHERE t0."tx_out_id" BETWEEN 1 AND 346347398
```

This catches corruption in the totals (a wrong amount shifts the sum, min, or max) much
faster than a full per-row hash of a billion rows. Only numeric columns are ever summed -
the tool refuses to sum a `jsonb` column like `datum.value`, which is the "value-column
guard" in `planning.py`.

### Tiering in the plan output

For `ma_tx_out`, `--plan` also prints:

```
ma_tx_out  [giant]
  columns dropped (FK over depth budget): ['tx_out.tx_id->tx (depth>1)', 'tx_out_id->tx_out']
```

That is tiering in action ([how it works, idea 4](03-how-it-works.md)): a giant table
only resolves foreign keys one level deep by default, so the deeper chain
`ma_tx_out -> tx_out -> tx` is dropped rather than joined a billion times. `--full` raises
the budget and hashes everything. The dropped columns are reported, not silently ignored.

## Example 3: the bucketed variant (`--localize buckets`)

When a giant mismatches and you localize with `--localize buckets`, the tool runs the
**same** row hash and joins, but adds a bucket key and groups by it, so one scan produces
a hash per chain window instead of re-scanning many times
([performance](07-performance-and-scaling.md)). The shape (from `sql.py:hash_sql_bucketed`):

```sql
SELECT bkt, count(*) AS n, coalesce(sum(...),0) AS s1, coalesce(sum(...),0) AS s2
FROM (
  SELECT width_bucket(t0."tx_id", ARRAY[<id at block 0>, <id at block W>, ...]::bigint[]) AS bkt,
         md5(ROW(... same columns ...)::text) AS h
  FROM "tx_out" t0
  LEFT JOIN "tx" j5 ON t0."tx_id" = j5."id"
  ...
  WHERE t0."tx_id" BETWEEN 1 AND 120373367
) q GROUP BY bkt
```

`width_bucket(value, thresholds)` is a built-in that returns which bucket a value falls
into. The `thresholds` array is *this database's* `tx_id` at each block edge (computed by
`ranges.bucket_boundary_ids`); the other database has its own array, but **bucket `k` is
the same block range on both** - the same id-drift-proof trick as the windows. Buckets
whose `(n, s1, s2)` differ between the two databases are the windows that contain the
difference.

## The point

Every table's check is one query of the same anatomy: **bound** it to the common chain
window, **translate** its drifting foreign keys to natural keys via LEFT JOINs, **hash**
each row, and **sum** the hashes into a tiny order-independent fingerprint. `--plan` lets
you see precisely that query for any table before you trust its result - which is the
whole reason the tool builds SQL as plain strings rather than hiding it inside an ORM.

**Next:** back to the [docs index](README.md) · [start here](00-start-here.md).
