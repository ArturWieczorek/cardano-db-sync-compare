# 07 - Performance and scaling

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
of those is fast. A few anchor columns are **not** indexed - notably
`redeemer.tx_id` and `collateral_tx_out.tx_id`. The tool detects this and prints:

```
note: anchor column not indexed (window bound = seq scan): ada_pots, pot_transfer, ...
```

For those tables a *narrow window* still scans the whole table.

### Why this barely matters for a full run

A full release comparison reads essentially the entire history of each table
anyway. So an unindexed anchor isn't extra cost there - the table would be fully
scanned regardless. The unindexed-anchor penalty only bites in `--block-range`
mode (where you *wanted* to touch only a sliver) and in Phase-2 localization. If
you control the comparison database and want fast windows on those tables, you can
add an index (see [extending](09-extending-and-limitations.md)).

## Hash joins are the right tool for a full run

Translating foreign keys to natural keys ([how it works, idea 1](03-how-it-works.md))
means joining each table to the ones it points at (`tx`, `datum`, `stake_address`,
...). For a **full** run - where almost every row participates - PostgreSQL builds an
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

The comparison is **read-only on the data** - it never writes to either database,
so it can't corrupt them or grow them permanently. But a full run is heavy in
three ways, and one of them surprises people:

- **Temporary disk - large and fluctuating.** Translating foreign keys to natural
  keys hash-joins the giant tables against `tx`/`datum`/`stake_address`. When a
  join's hash table exceeds `work_mem`, PostgreSQL spills it to **temp files**
  under `base/pgsql_tmp`. On a real run this generated **hundreds of GB of temp
  I/O cumulatively** - measured via `pg_stat_database.temp_bytes`, ~478 GB on one
  database and ~255 GB on the other - with **tens of GB live at peak**. Because
  each query's temp is created during the scan and released when it finishes,
  **free disk space visibly rises and falls** throughout the run. This is the
  usual cause of "why is my disk fluctuating?". Mitigate by keeping ample free
  space (tens of GB; more with more `--workers` or `--full`), capping it with
  PostgreSQL's `temp_file_limit`, or moving it off the data disk via a dedicated
  `temp_tablespaces`.
- **RAM is mostly server-side.** The client process is tiny (~10-40 MB; the run
  measured ~11 MB resident) because all the work happens inside PostgreSQL. The
  server's peak is roughly `workers × (concurrent hash/sort ops) × work_mem`, plus
  `shared_buffers`, plus OS page cache for the scans. Raising `--work-mem` cuts
  temp-disk spill at the cost of RAM; the two trade off directly.
- **CPU + disk read bandwidth.** Sustained sequential reads of the whole ~500 GB
  plus `md5` across cores - expect the run to saturate disk reads.

Quick way to see the temp pressure yourself, during or after a run:

```sql
SELECT datname, temp_files, pg_size_pretty(temp_bytes)
FROM pg_stat_database WHERE datname LIKE 'mainnet%';
```

**Rule of thumb:** tight on disk → raise `--work-mem`, lower `--workers`; tight on
RAM → keep `--work-mem` modest, lower `--workers` (and make sure there's temp
disk); plenty of both → more `--workers` for speed. Avoid `--full` on mainnet
unless you have the disk and the hours - it deep-joins the 1.1B-row `ma_tx_out`
and multiplies temp usage.

## Localizing a mismatch: `--localize bisect` vs `--localize buckets`

When a table's content differs, Phase 2 tells you *where* on the chain. There are
two algorithms; the default is unchanged, the second is an opt-in speed-up for
giant tables.

**Two flags that are easy to confuse - they are not rivals.** `--localize`
*picks the method* (`bisect` or `buckets`). `--localize-buckets N` is *a dial for
the buckets method only* - how many windows to chop the chain into (default 1024,
capped 5000); it does nothing under `bisect`. So `--localize bisect` runs the
guessing-game halving; `--localize buckets` runs the one-pass method with 1024
windows; `--localize buckets --localize-buckets 4000` runs the same one-pass
method but with a finer, 4000-window ruler.

### Why this matters (a real cost)

In the mainnet run, the only differing giant was `tx_out` (346M rows, the pointer
addresses). Pinpointing it with the default method took a large chunk of the
**9h42m** total - because of *how* the default works.

A measured back-to-back comparison on a 23.2M-row `tx_out` slice
(`--cutoff-block 6000000`, both algorithms, single worker) makes the gap concrete:
**both localized to the same two regions**, but the localization phase took
**~95 min with `bisect` vs ~8 min with `buckets` (≈11×)** - the full numbers are in
[`benchmarks/buckets-vs-bisect-2026-06-05.md`](../benchmarks/buckets-vs-bisect-2026-06-05.md).

### `bisect` (default) - simple, but re-scans

Think of the "guess my number" game: hash the whole block range, then hash each
half, keep the half that differs, halve again, and so on until the window is
narrow. It's tiny code and reuses the normal hashing - but **each level re-runs a
hash query over the data.** A row at block 7M gets scanned again at *every* level
it falls inside, so a 346M-row table effectively gets scanned many times. Fine for
small/medium tables; slow on a giant.

**Exactly when does it stop?** For each window it looks at, one of three things
happens: (1) the two DBs hash **equal** there -> that stretch matches, discard it;
(2) they **differ** and the window is already **narrow enough** -> print it as a
result and stop digging this branch; (3) they differ and the window is still wide
-> split in half and examine both halves. "Narrow enough" means the window is
`min_width` blocks wide or less - **2000 blocks** for id-range tables, and **0**
for epoch tables (so it narrows all the way to a single epoch).

**Careful with the "8":** the code's `max_leaves = 8` is **not** the search depth -
it's the maximum number of *differing result windows* ("leaves") it will report. The
halving goes as deep as needed: to shrink preprod's ~4.8M-block chain down to a
2000-block window takes about **12 halvings**, not 8. For a single difference you get
**one** leaf (one ~2000-block window, i.e. pinpointing to ~0.04% of the chain) after
~12 levels down one branch. The `8` cap only bites when corruption is **scattered
across many places** - then it reports the first 8 differing windows as a
representative sample rather than listing every one. And note the axis is **block
numbers, not rows**: a 2000-block window can hold many rows, and the number of
halvings depends on how many *blocks* the chain has, not how many rows the table has.

### `buckets` (opt-in) - one pass

`--localize buckets` instead chops the chain into ~1000 fixed windows up front
(e.g. ~13k blocks each on mainnet) and computes the hash of **every** window in a
**single scan** of the table (one `GROUP BY`), on each database. Then it just
lines up the ~1000 window-hashes - the windows whose hashes differ are your
answer. **No re-scans.**

The analogy: instead of repeatedly cutting a deck in half to find one marked card
(re-handling cards each cut), you deal the whole deck into ~1000 labelled piles
**once**, then glance at which pile's checksum differs.

```bash
db-sync-compare --db1 ... --db2 ... --localize buckets
db-sync-compare --db1 ... --db2 ... --localize buckets --localize-buckets 2000   # finer windows
```

### How it stays correct across the two databases

Buckets are defined by **block ranges**, but each row is filed into its bucket by
its cheap **id** column (the same `tx_id`/`tx_out_id` the id-range windows use, so
no extra joins). For each database we precompute the id at each block edge - the
boundaries differ between the databases (id drift), but **bucket *k* = the same
block range on both**, so comparing bucket *k* to bucket *k* compares the same
slice of chain. (Same trick, and same correctness property, as the id-range
windowing in [doc 03](03-how-it-works.md).)

### Why buckets only works on id-range tables (no jargon)

Picture the chain as a very long **shelf** marked off with block numbers 0, 1, 2,
... up to millions. Every row of a table "sits" somewhere on that shelf, at the
block it belongs to. Buckets wants to slice the shelf into ~1000 equal **sections**,
fingerprint each section in one sweep, and compare section-by-section - the sections
that differ are the answer.

The catch: to drop each row into the right section, the tool must know **where on
the shelf that row sits.** But rows aren't stored with a block number - they carry a
**ticket number** (the row's `id`, handed out in order). So the tool has to translate
each section boundary, which is a *block number*, into a *ticket number*, because
ticket numbers are what the rows actually carry. (Those translated boundary values -
"the ticket number where each section begins" - are the only thing the word
*threshold* meant above; a *window* or *section* is just a range of blocks.)

Whether the tool can do that translation is exactly what splits the table kinds:

- **id-range tables** (`tx_out`, `pool_relay`, ...) carry a ticket number that climbs
  in chain order, and the tool can work out "which ticket number matches block X" by
  walking the backbone `block -> tx -> tx_out`. So it can mark the section boundaries
  and bucket them. **Buckets works.**
- **epoch tables** (`epoch_stake`, ...) are labelled by **epoch number**, not a
  ticket number - and there are only a few hundred epochs ever, so slicing them into
  1000 sections is pointless (plain `bisect` lands on the exact epoch in a handful of
  steps). The tool **falls back to `bisect`** for them.
- **accumulator tables** (`multi_asset`, ...) have **no position on the shelf at
  all** (no block, no epoch - see [doc 06](06-how-each-table-is-compared.md)). You
  can't sort them into sections, so **neither** buckets nor bisect applies; they're
  compared whole and never localized.

So `--localize buckets` really means "use buckets where a table carries a
chain-ordered ticket number, and `bisect` everywhere else" - which is why the tool
only bucketises id-range-anchored tables.

### When would I use `buckets` over `bisect`?

Short version: reach for `buckets` when the table that mismatched is **huge and
`bisect` is too slow**; stick with `bisect` (the default) for everything else.

| Situation | Use |
|---|---|
| Mismatch in a **giant** table (`tx_out`, `ma_tx_out`, `epoch_stake`), or localization is dragging / timing out | `--localize buckets` (raise `--localize-buckets` for a tighter window) |
| **Small/medium** table, or you want the **tightest** possible window | `bisect` (default - do nothing) |
| **Not sure** | Start with the default `bisect`; if it crawls on a big table, rerun with `buckets` |

Why the split: `bisect` re-scans the table on every halving (~12 rounds), which is
nothing on a small table but 12x the work on a billion-row one - exactly where the
one-pass `buckets` wins (measured ~11x faster above). The cost of `buckets` is
coarser resolution (a fixed window width) unless you raise `--localize-buckets`.
Most runs are fine on the default; you only reach for `buckets` once a *giant* table
actually mismatches and you don't want to wait for repeated full scans.

### Trade-offs and safety

- **Resolution:** `buckets` localizes to a fixed window width (~13k blocks at the
  default 1000); `bisect` adaptively narrows to ~2000 blocks. Raise
  `--localize-buckets` for finer windows (capped at 5000).
- **Resource use:** one scan instead of many → **less** CPU/IO and **less** temp
  disk than `bisect`. The per-bucket `GROUP BY` holds only ~1000 tiny rows
  (negligible RAM); the table's FK-translation joins behave exactly like one
  Phase-1 pass.
- **Non-authoritative:** like all localization, it only reports *where* a mismatch
  is - it never changes a table's `MATCH`/`DIFF` verdict or the exit code. So the
  choice of algorithm is purely about speed/precision, never correctness.
- **Only runs in cutoff mode.** Both algorithms are Phase 2, and Phase 2 is skipped
  entirely under `--block-range` (that mode is already a narrow window, so there is
  nothing to localize). To exercise either algorithm, run a normal/cutoff
  comparison - bound it with `--cutoff-block N` if you want it quick. `--localize`
  has no effect when `--block-range` is set.
- **Default stays `bisect`**, so existing runs are unchanged.

## Would a faster language (Rust, Go, C++) help? - no, the client isn't the bottleneck

A natural question: this tool is written in Python; would rewriting it in Rust
make a mainnet comparison "way faster"? **No - it would make essentially no
difference**, and the reason is the whole design.

Every expensive operation runs **inside PostgreSQL**: the `md5` hashing of
hundreds of millions / billions of rows, the FK-translation hash joins, the
~500 GB of sequential scans, and the temp-file spill I/O. The Python process only
builds SQL strings, opens connections, fires each query, casts two numbers out of
the result, and orchestrates threads. It then **blocks, waiting on the database.**

The measurement makes this concrete. On a full mainnet run, the Python client
used about **18 seconds of CPU across ~5 hours of wall-clock** (≈0.1% CPU) and
~13 MB RAM - i.e. it sat idle waiting on PostgreSQL **~99.9%** of the time. A Rust
rewrite would shave a few of those 18 seconds; against a multi-hour run that is
invisible.

Two clarifications:

- **The GIL is not a constraint here.** The workers are I/O-bound (waiting on the
  server), and psycopg releases the GIL during socket waits, so `--workers`
  parallelism already works. A compiled language's threading adds nothing the
  database isn't already gating.
- **The one case where the language *would* matter** is the *naive* design this
  tool rejects - pulling every row to the client and hashing it in-process. There,
  client CPU dominates and Rust would crush Python. But that design also ships
  terabytes over the wire, which is exactly why we hash server-side instead. Once
  the heavy work is in the database, the client language is irrelevant.

**What actually speeds it up** is all database-side (see the rest of this doc):
PostgreSQL parallel query (`max_parallel_workers_per_gather`, so one big hash scan
uses several cores), more RAM / faster disk, higher `work_mem` to cut temp spill,
and - the biggest algorithmic win - avoiding the Phase-2 re-scans by computing
per-range hashes in a single pass (a true Merkle tree) instead of re-hashing a
giant table at each bisection level.

## The honest caveat: the near-tip rollback zone

The id-range window ([how it works, idea 3](03-how-it-works.md)) assumes row
`id`s run in chain order, so that "ticket numbers `a`-`b`" exactly equals "blocks
`x`-`y`". That holds for **settled history**. In the last few thousand blocks -
the rollback zone - a database may have re-inserted rows out of strict order, so a
tight id-range there could include or exclude a few neighbouring rows. The tool
guards against this with two margins that pull the cutoff back below the lower
tip. They default differently on purpose - and the reason is the key to using them
correctly.

### Why the two margins default differently (`--block-margin` 0, `--epoch-margin` 2)

The two risks look similar but have a different *nature*: one is conditional, the
other is structural and unavoidable.

| | Newest-data risk near the tip | Correct default |
|---|---|---|
| **Block-anchored tables** (`tx`, `tx_out`, ...) | Present **only if** you stopped a database inside the live rollback zone *and* the two nodes had briefly diverged there | **0** + a knob to raise |
| **Epoch-written tables** (`epoch_stake`, `reward`, ...) | Present in **every** comparison, no matter how the nodes were stopped | **2** |

**`--block-margin` defaults to 0 - because the risk is conditional.** The cutoff is
already `min(tip1, tip2)`, so the faster database's extra blocks are dropped. If both
databases were stopped at a clean, agreed point (or restored from snapshots at the
same chain point), the shared history below the tip is identical and *any* margin
would just throw away thousands of perfectly comparable blocks. But **0 is a default,
not a guarantee**: if you stop two nodes while they are actively following the tip,
their last few blocks are genuinely in the rollback zone and can legitimately differ
(each node captured a momentary, possibly-different view of the volatile tip). In
that case you *should* raise `--block-margin` toward the security parameter `k`
(≈2160 on mainnet) to pull the cutoff back into settled territory. Two things keep 0
workable in practice: real rollbacks are almost always 1-2 blocks (2160 is the
theoretical *maximum* depth, rarely approached), and a genuine near-tip artifact
would be localized (Phase 2) to those last few blocks, so you would see it was a tip
effect rather than real corruption. (In the validation run this wasn't the cause of
any mismatch - the only block-anchored difference, in `tx_out`, was a real
pointer-address encoding fix at block ~7M, not a near-tip artifact - but the knob is
there when you compare close to a live tip.)

**`--epoch-margin` defaults to 2 - because the risk is structural.** The newest
epochs of the per-epoch tables are unreliable in *every* comparison, regardless of
how carefully the nodes are stopped, for two stacked reasons:

- **The in-progress epoch** hasn't ended, so its rows are only partially written.
- **Reward/stake calculation lags by ~2 epochs**: the rewards *earned* in epoch `e`
  are not computed and recorded until roughly two epochs later (the mark→set→go
  stake-snapshot pipeline). So the last one-to-two epochs of `reward`/`epoch_stake`
  can be absent in one database and mid-write in the other, purely as a function of
  when each stopped - not a real difference.

Holding back **2** epochs (not 1) steps past *both* the in-progress epoch and that
reward-calc lag, landing the comparison on fully-computed snapshots. Because this
unreliability can't be avoided at the node level, a nonzero default is always
correct - which is why epoch margin is nonzero where block margin is not.

Rule of thumb: leave the defaults for a settled/at-rest comparison; raise
`--block-margin` to ≈`k` when either database's tip sits in the live rollback zone.
Don't set the cutoff right at the tip.

**Next:** [Case study: the pool-relay port bug →](08-case-study-pool-relay-port.md)
