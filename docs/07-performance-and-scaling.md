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

## The honest caveat: the near-tip rollback zone

The id-range window ([how it works, idea 3](03-how-it-works.md)) assumes row
`id`s run in chain order, so that "ticket numbers `a`–`b`" exactly equals "blocks
`x`–`y`". That holds for **settled history**. In the last few thousand blocks —
the rollback zone — a database may have re-inserted rows out of strict order, so a
tight id-range there could include or exclude a few neighbouring rows. The tool
guards against this by comparing only up to the **common boundary minus a margin**
(`--epoch-margin`, default 2 epochs), keeping the comparison in settled territory.
Don't set the cutoff right at the tip.

**Next:** [Case study: the pool-relay port bug →](08-case-study-pool-relay-port.md)
