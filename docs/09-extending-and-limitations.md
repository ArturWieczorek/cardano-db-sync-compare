# 09 - Extending and limitations

> **What's in here:** how to keep the tool working as db-sync's schema evolves,
> small ways to make it faster if you control the database, and an honest list of
> what it deliberately does *not* do.
>
> **Prerequisites:** [how each table is compared](06-how-each-table-is-compared.md).

## Schema drift between versions is handled automatically

The two databases can be at **different schema versions** (a new release may add
or drop columns). The tool reads both schemas at startup and compares only the
**columns the two databases share**, per table. Columns present in only one
database are reported, not hashed - so a new column doesn't cause a false
mismatch, and you still get told it exists. New whole **tables** present in only
one database are listed and skipped.

(In the validation run the two versions happened to have identical columns even
though their internal schema-version numbers differed - the extra migrations were
data/index changes, not column changes. The tool would have coped either way.)

## Adding a new table or foreign key

When db-sync adds tables or columns, you may need to teach the tool about them.
First confirm the new shape against the upstream **source of truth** - the
[schema reference](https://github.com/IntersectMBO/cardano-db-sync/blob/master/doc/schema.md),
the [schema source](https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/src/Cardano/Db/Schema),
and the [migrations](https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/test/schema),
at the git **tag matching the db-sync version under comparison** (`master` runs
ahead of releases). Then teach the tool: everything lives in
`db_sync_comparator/registries.py` ([doc 06](06-how-each-table-is-compared.md)):

- **A new foreign-key column** → add it to `GLOBAL_FK` (if the name is
  unambiguous) or `FK_MAP` (if the same name means different things in different
  tables). If you forget, the tool won't silently hash a drifting id - it will
  flag the column as `UNMAPPED` and exclude it, which is your cue to map it.
- **A new table that others point at** → add its natural key to `NATURAL_KEYS`.
- **A new table to bound to the chain** → add an entry to `ANCHORS` (tie it to a
  transaction, an epoch, a pool update, etc.). If you don't, the tool treats it as
  an *accumulator* (compared whole, count differences informational) - a safe
  default.
- **A new non-chain table** (network-fetched or per-instance) → add it to
  `EXCLUDED_TABLES` with a reason.

After any change, run `--plan` and confirm there are **zero `UNMAPPED`** columns
and that each table's anchor and dropped/translated columns look right.

## Making windowed comparison faster (if you own the DB)

A few anchor columns aren't indexed (`redeemer.tx_id`, `collateral_tx_out.tx_id`),
so `--block-range` and Phase-2 localization fall back to full scans on them
([doc 07](07-performance-and-scaling.md)). On a throwaway comparison database you
can add indexes to make those windows fast:

```sql
CREATE INDEX ON redeemer (tx_id);
CREATE INDEX ON collateral_tx_out (tx_id);
```

Don't do this casually on a production database - extra indexes cost write speed
and disk. For a full release comparison it makes little difference anyway (the
whole table is read either way).

## Limitations (by design)

- **Off-chain / per-instance tables are not compared.** Network-fetched metadata
  and per-database bookkeeping aren't a function of the chain
  ([doc 06](06-how-each-table-is-compared.md)). Compare those out of band if you
  need to.
- **The two TxOut variants can't be compared against each other.** Core (address
  inline in `tx_out`) and Address (`use_address_table`, address in a separate
  `address` table reached via `address_id`) have *different columns*, so a
  Core-vs-Address run is not meaningful. Two databases that use the **same**
  variant compare correctly - both are supported
  ([doc 06 §3](06-how-each-table-is-compared.md)).
- **Accumulator count differences are informational, not failures.** When one
  database is synced further it has seen more distinct tokens / addresses / pools.
  The tool flags the delta but doesn't fail on it. If you need to be strict, equalize
  the tips first or restrict with `--cutoff-block`.
- **Tiered giants have a small default blind spot.** The billion-row tables get
  cheaper proofs unless you pass `--full`
  ([doc 07](07-performance-and-scaling.md)).
- **The id-range window assumes settled history.** It stays a margin below the tip
  to stay out of the rollback zone ([doc 07](07-performance-and-scaling.md)); don't
  point the cutoff at the very tip.
- **A difference in a genesis governance row has no block window.** The genesis
  committee and constitution have no block, so Phase 2 reports them as one line,
  `rows with no block (gov_action_proposal_id IS NULL, the genesis row): ...`
  (JSON `localized_no_block`), rather than a block range ([doc 06](06-how-each-table-is-compared.md)). Query the
  row with `gov_action_proposal_id IS NULL` directly.
- **Lifecycle epochs are compared only up to the cutoff epoch.** db-sync fills
  `gov_action_proposal.ratified/enacted/dropped/expired_epoch` in at later epoch
  boundaries, so values above the cutoff epoch depend on how far each database has
  synced and are hashed as NULL ([doc 06](06-how-each-table-is-compared.md)). A
  disagreement in the `--epoch-margin` epochs just below the lower tip is therefore
  not seen. To close that window, run `--tables gov_action_proposal --epoch-margin 0`
  as a separate run: a margin of 0 makes the lower database's current (still
  running) epoch part of the comparison, which is safe for these four columns but
  can show false differences in the epoch-bounded tables (`epoch_stake`,
  `reward`, ...), so don't use it for the whole run. Never pass a negative margin.
- **`tx_out.consumed_by_tx_id` has the same issue in consumed / prune databases.**
  With db-sync's `tx_out` option set to `consumed`, `prune` or `bootstrap`, an
  output created below the cutoff gets `consumed_by_tx_id` filled in when it is
  spent, possibly in a block only the further-synced database has. That would show
  as a `tx_out` `HASH_DIFF`. With the default (`enable`) the column is always NULL,
  so default databases are not affected. Not handled yet, because no consumed-mode
  databases were available to test against.
- **It detects, it doesn't explain.** The tool tells you *which table* and *which
  block range* differ; a human reads the actual rows to judge whether it's a
  regression or an intended change (the [case study](08-case-study-pool-relay-port.md)
  shows the drill-in).
- **MD5 is used for speed, not security.** Fine here - nobody is adversarially
  crafting rows; we just need a fast change-detecting fingerprint
  ([primer 03](primers/03-hashing-and-fingerprints.md)).

## Verifying a change to the tool

1. `python -m py_compile db_sync_comparator/*.py` - it imports cleanly; `make check` runs lint+types+tests.
2. `--plan` against two real databases - 0 `UNMAPPED` columns, every table
   classified sensibly.
3. `--block-range` on a historical window - finishes fast and reports `MATCH` for
   tables you expect to match.
4. A known-difference test (like the [pool-relay case](08-case-study-pool-relay-port.md))
   - confirm it still flags and localizes.

---

That's the whole tool. Back to the [start](00-start-here.md).
