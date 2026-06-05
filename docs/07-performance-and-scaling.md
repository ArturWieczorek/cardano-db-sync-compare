# 07 — Performance and scaling

> **What's in here:** what actually makes the tool fast or slow on a 500 GB
> database, the knobs you have, and the honest caveats.
>
> **Prerequisites:** [primer 02 (indexes)](primers/02-indexes-and-table-scans.md)
> and [how it works](03-how-it-works.md).

## The golden rule: indexes decide everything

From [primer 02](primers/02-indexes-and-table-scans.md): if the column the tool
filters on is **indexed**, bounding a table to a chain window is a quick seek; if
it isn't, the database falls back to reading the **whole table**.

Most of db-sync's chain-backbone columns (`tx.block_id`, `tx_out.tx_id`,
`ma_tx_out.tx_out_id`, `tx_in.tx_in_id`) **are** indexed, so windowed comparison
of those is fast. A few anchor columns are **not** indexed — notably
`redeemer.tx_id` and `collateral_tx_out.tx_id`. The tool detects this and prints:

```
note: anchor column not indexed (window bound = seq scan): ada_pots, pot_transfer, ...
```

For those tables a *narrow window* still scans the whole table.

### Why this barely matters for a full run

A full release comparison reads essentially the entire history of each table
anyway. So an unindexed anchor isn't extra cost there — the table would be fully
scanned regardless. The unindexed-anchor penalty only bites in `--block-range`
mode (where you *wanted* to touch only a sliver) and in Phase-2 localization. If
you control the comparison database and want fast windows on those tables, you can
add an index (see [extending](09-extending-and-limitations.md)).

## Hash joins are the right tool for a full run

Translating foreign keys to natural keys ([how it works, idea 1](03-how-it-works.md))
means joining each table to the ones it points at (`tx`, `datum`, `stake_address`,
…). For a **full** run — where almost every row participates — PostgreSQL builds an
in-memory hash of the referenced table and streams through: a **hash join**, which
is efficient at that scale. It only *looks* slow if you run it over a tiny window,
because it still builds the whole hash up front. That asymmetry is expected and is
why the tiering and the full-vs-window distinction exist.

`--work-mem` (default `256MB`) controls how much memory each session may use for
those hashes before spilling to disk; bigger is faster up to the point your server
has the RAM.

## Parallelism

`--workers N` (default 4) compares N tables at once, each on its own pair of
connections, using the server's cores. More workers finish sooner but multiply
memory use (each can hold up to `work_mem`-sized hashes) and disk I/O. On a busy
or memory-tight server, fewer workers is safer.

## Tiering on the giants

By default the billion-row tables get the cheaper check
([how it works, idea 4](03-how-it-works.md)); `--full` makes them exhaustive. The
practical guidance:

- Routine release check / time-boxed CI → default (tiered). Catches dropped rows,
  wrong totals, and value corruption fast.
- Formal sign-off where you want every column proven on every table → `--full`,
  budget the extra hours.

## Real numbers (from validation)

These are *uncontaminated* timings from the validation run; treat them as rough
shape, not promises (your hardware, cache state, and `work_mem` change them a
lot):

- `block` full set-fingerprint (13.3M rows): **~22 s**.
- A 10,000-block window across all block-anchored tables: a couple of minutes,
  dominated by the unindexed-anchor tables that fall back to full scans.
- `withdrawal` over full history (11.6M rows, with translation joins): **~8 min**.

For a **full mainnet run**, the giants (`ma_tx_out` 1.1B, `epoch_stake`/`reward`
~450M, `tx_out`/`tx_in` ~340M) dominate; expect **a few hours**, more with
`--full`. Tune with `--workers` and `--work-mem`, and validate on a
`--block-range` slice first.

## Resource usage on a full mainnet run (measured)

The comparison is **read-only on the data** — it never writes to either database,
so it can't corrupt them or grow them permanently. But a full run is heavy in
three ways, and one of them surprises people:

- **Temporary disk — large and fluctuating.** Translating foreign keys to natural
  keys hash-joins the giant tables against `tx`/`datum`/`stake_address`. When a
  join's hash table exceeds `work_mem`, PostgreSQL spills it to **temp files**
  under `base/pgsql_tmp`. On a real run this generated **hundreds of GB of temp
  I/O cumulatively** — measured via `pg_stat_database.temp_bytes`, ~478 GB on one
  database and ~255 GB on the other — with **tens of GB live at peak**. Because
  each query's temp is created during the scan and released when it finishes,
  **free disk space visibly rises and falls** throughout the run. This is the
  usual cause of "why is my disk fluctuating?". Mitigate by keeping ample free
  space (tens of GB; more with more `--workers` or `--full`), capping it with
  PostgreSQL's `temp_file_limit`, or moving it off the data disk via a dedicated
  `temp_tablespaces`.
- **RAM is mostly server-side.** The client process is tiny (~10–40 MB; the run
  measured ~11 MB resident) because all the work happens inside PostgreSQL. The
  server's peak is roughly `workers × (concurrent hash/sort ops) × work_mem`, plus
  `shared_buffers`, plus OS page cache for the scans. Raising `--work-mem` cuts
  temp-disk spill at the cost of RAM; the two trade off directly.
- **CPU + disk read bandwidth.** Sustained sequential reads of the whole ~500 GB
  plus `md5` across cores — expect the run to saturate disk reads.

Quick way to see the temp pressure yourself, during or after a run:

```sql
SELECT datname, temp_files, pg_size_pretty(temp_bytes)
FROM pg_stat_database WHERE datname LIKE 'mainnet%';
```

**Rule of thumb:** tight on disk → raise `--work-mem`, lower `--workers`; tight on
RAM → keep `--work-mem` modest, lower `--workers` (and make sure there's temp
disk); plenty of both → more `--workers` for speed. Avoid `--full` on mainnet
unless you have the disk and the hours — it deep-joins the 1.1B-row `ma_tx_out`
and multiplies temp usage.

## The honest caveat: the near-tip rollback zone

The id-range window ([how it works, idea 3](03-how-it-works.md)) assumes row
`id`s run in chain order, so that "ticket numbers `a`–`b`" exactly equals "blocks
`x`–`y`". That holds for **settled history**. In the last few thousand blocks —
the rollback zone — a database may have re-inserted rows out of strict order, so a
tight id-range there could include or exclude a few neighbouring rows. The tool
guards against this by comparing only up to the **common boundary minus a margin**
(`--epoch-margin`, default 2 epochs), keeping the comparison in settled territory.
Don't set the cutoff right at the tip.

For block-anchored tables there is the matching `--block-margin` knob (default 0):
set it to pull the block cutoff back below the lower tip by roughly the security
parameter `k` (≈2160 on mainnet) when one database's tip sits in the volatile
rollback zone. (In the validation run this wasn't the cause of any mismatch —
the only block-anchored difference, in `tx_out`, was a real pointer-address
encoding fix at block ~7M, not a near-tip artifact — but the knob is there when
you do compare close to a tip.)

**Next:** [Case study: the pool-relay port bug →](08-case-study-pool-relay-port.md)
