# 03 - How it works

> **What's in here:** the five ideas the tool is built on, each with the analogy
> that makes it stick. This is the heart of the docs.
>
> **Prerequisites:** all five [primers](primers/05-surrogate-ids-sequences-and-drift.md).

The tool answers "do these two databases hold the same chain data?" with five
ideas working together.

---

## Idea 1 - Compare by meaning, not by id

From [primer 05](primers/05-surrogate-ids-sequences-and-drift.md): the `id`
numbers and the foreign keys drift between databases, so we **drop the `id`** and
**translate every foreign key to the natural key** of the row it points at.

Worked example - the `ma_tx_out` table (which records "this output holds this much
of this token"). Its real columns include `tx_out_id` (which output) and `ident`
(which token), both surrogate ids that drift. The tool rewrites them:

```
ma_tx_out.ident       →  the multi_asset's (policy, asset_name)        [natural]
ma_tx_out.tx_out_id   →  the tx_out's (transaction hash, output index) [natural]
```

and that last one chains further: the output's natural key needs *its*
transaction's hash, so the tool follows `ma_tx_out → tx_out → tx` and reads
`tx.hash`. The fingerprint is then computed over these **meaningful** values plus
the row's own real data (the quantity). Both databases now produce the same
fingerprint for the same token-in-output fact, drift and all.

The exact natural key for every table, and the foreign-key map, are in
[how each table is compared](06-how-each-table-is-compared.md).

---

## Idea 2 - Fingerprint inside the database, not in Python

From [primer 03](primers/03-hashing-and-fingerprints.md): each database computes
an **order-independent set-fingerprint** of the table (MD5 each translated row →
sum the numbers), and we compare the two fingerprints. The query runs on the
server, next to the data; only a row count and two numbers come back.

This is what makes 500 GB feasible. In the validation run it fingerprinted the
13.3-million-row `block` table in ~22 seconds with nothing but a count and two
numbers crossing the wire.

---

## Idea 3 - Compare only the slice both databases have

From [problem 1](02-the-three-hard-problems.md): the databases are at different
tips, so we compare only up to the **common boundary** (the lower tip).

The challenge is applying that boundary *cheaply*. We can't filter most tables by
`block_no` directly - they don't have a `block_no` column, and joining all the way
up to the `block` table to filter would force a full scan
([primer 02](primers/02-indexes-and-table-scans.md)). Instead the tool uses an
**id-range window**.

**The deli-ticket-range analogy.** Tickets are handed out in order
([primer 05](primers/05-surrogate-ids-sequences-and-drift.md)), so all the rows
belonging to a stretch of chain have ticket numbers in one **contiguous range**.
"All transactions in blocks 0-13,313,031" is the same as "all `tx` rows with
`id` between *(first such ticket)* and *(last such ticket)*". The tool computes
those two boundary ticket numbers once per database, then bounds each table with a
simple, index-friendly `BETWEEN`:

```
WHERE ma_tx_out.tx_out_id BETWEEN <low> AND <high>
```

It finds the boundary ticket numbers by walking the chain's backbone -
`block → tx → tx_out` - with the index-seek trick from
[primer 02](primers/02-indexes-and-table-scans.md) (the one that turned 3 minutes
into 1.8 seconds). Because each database has *its own* drifted ticket numbers, the
low/high values differ between the two databases - but they select the **same set
of chain facts**, which is the whole point.

> A subtlety made honest: this assumes ticket numbers run in chain order, which is
> true for settled history. Near the very tip (the rollback zone) it can wobble,
> so the comparison stays a safe margin below the tip. See
> [extending and limitations](09-extending-and-limitations.md).

---

## Idea 4 - Do cheap proofs before expensive ones (tiering)

A handful of tables are enormous (`ma_tx_out` ~1.1 billion rows;
`epoch_stake`/`reward` ~450 million). Fully translating and fingerprinting every
column of every row of those is the most expensive thing the tool can do.

So by default these **giant tables** get a *tiered* check: the row count, plus a
sum/min/max of their main numeric column (e.g. the quantity), plus a fingerprint
over the cheap-to-translate columns - enough to catch real corruption quickly. If
you want the exhaustive, every-column-translated fingerprint on the giants too,
pass `--full`. Small and medium tables always get the full treatment. Details in
[performance and scaling](07-performance-and-scaling.md).

---

## Idea 5 - When fingerprints differ, zoom in

There are two phases, and they answer two different questions. **Phase 1 asks "is
there a difference?"** - it compares the one fingerprint per table from Idea 2 and
returns yes/no. **Phase 2 (localization) asks "*where* is the difference?"** -
because "table `tx_out` differs *somewhere* in 13 million blocks" is useless for
follow-up; you need "it differs in blocks 5,566,408-5,567,872" so you can pull
those few rows and see what actually changed.

If a table's two fingerprints don't match, the tool then **localizes** the
difference to a narrow stretch of chain, by **binary search** - the "guess my
number" game.

Split the chain range in half. Fingerprint each half in both databases. The
half whose fingerprints match is identical - ignore it. Recurse into the half
that still differs. A few rounds of halving shrink "somewhere in 13 million
blocks" down to "blocks 4,490,224-4,491,848". (This halving-and-discarding is the
idea behind a *Merkle tree* diff, and behaves like weighing coins on a balance to
find the odd one out.) The tool prints those narrow windows so you can pull the
actual rows and see what changed - exactly how the
[pool-relay port bug](08-case-study-pool-relay-port.md) was found.

That halving re-scans the data at each step, which is slow on a giant table, so
there's an opt-in one-pass alternative (`--localize buckets`) that hashes ~1000
fixed chain windows in a single scan and compares them - same answer, far less
work. Details and trade-offs in
[performance and scaling](07-performance-and-scaling.md#localizing-a-mismatch---localize-bisect-vs---localize-buckets).

> Localization (Phase 2) only runs when you compare the whole chain. If you pass
> `--block-range LO:HI` you have already pointed at a narrow window, so there is
> nothing to narrow and Phase 2 is skipped - see [running it](05-running-it.md).

---

## Putting it together: the run

1. Read both schemas; figure out which tables and columns the two databases share.
2. Find the common boundary (lower tip) and compute each database's id-range
   windows by walking `block → tx → tx_out`.
3. **Phase 1** - for every table, in parallel: build the "compare by meaning"
   fingerprint query, bound it to the window, run it on both databases, compare
   the counts and fingerprints.
4. **Phase 2** - for any table that differed, binary-search the chain range to
   pinpoint where.
5. Print a summary (and optionally a JSON report); exit `0` if equivalent, `1` if
   not.

For the same run narrated at the level of the actual code and functions, see
[the code, end to end](11-the-code-end-to-end.md).

**Next:** [What I used and why →](04-what-i-used-and-why.md)
