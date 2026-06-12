# Primer 08 - Column types and saving space

> **What's in here:** two more db-sync database details a newcomer meets: the custom
> *column types* db-sync invents (and why), and the `tx_out` option that lets operators
> trade blockchain history for disk space.
>
> **Prerequisites:** [primer 01](01-databases-in-2-minutes.md) and
> [primer 06](06-migrations-and-schema-stages.md).

## Part 1: custom column types (DOMAINs)

### What a DOMAIN is

PostgreSQL comes with built-in column types: `integer`, `text`, `numeric`, `bytea`
(raw bytes), and so on. A **DOMAIN** is a custom type *you* define as **a base type plus
a rule** (a `CHECK`). Once defined, any column of that domain automatically enforces the
rule - the database itself rejects a row that breaks it.

Analogy: a plain `numeric` column is a blank form field that accepts any number. A
domain is the same field with a printed rule next to it - "must be 0 or more, at most
this big" - and a clerk (the database) who refuses the form if you break the rule. You
write the rule once and never have to re-check it by hand.

### The ones db-sync defines

db-sync creates its domains in the stage-1 migrations (see
[primer 06](06-migrations-and-schema-stages.md)). Real examples from
`schema/migration-1-0001-20190730.sql`:

```sql
-- an amount of Lovelace (1 ADA = 1,000,000 Lovelace): never negative, fits a Word64
CREATE DOMAIN lovelace AS numeric (20, 0) CHECK (VALUE >= 0 AND VALUE <= 18446744073709551615);

-- a block or transaction hash: raw bytes, exactly 32 of them
CREATE DOMAIN hash32type AS bytea CHECK (octet_length (VALUE) = 32);

-- a stake address: raw bytes, exactly 29 of them
CREATE DOMAIN addr29type AS bytea CHECK (octet_length (VALUE) = 29);
```

and a more exotic one from `schema/migration-1-0004-20201026.sql`:

```sql
-- "Basically a Word64 with an extra sign bit" - used for asset quantities
CREATE DOMAIN int65type AS numeric (20, 0) CHECK (VALUE >= -18446744073709551615 AND VALUE <= 18446744073709551615);
```

Then the table definitions in stage 2 use these names directly, e.g. `block.hash` is
declared `hash32type NOT NULL`, so PostgreSQL guarantees every block hash is exactly 32
bytes.

### Why bother

Two reasons, both about correctness:

1. **The database enforces the rule for everyone.** Not just db-sync, but any tool that
   ever writes to the database, gets the same guarantee for free - a Lovelace amount can
   never be negative, a hash is always the right length.
2. **The rule documents intent.** Reading the schema, you can see that `lovelace` is a
   non-negative 64-bit-range amount. The comment in the migration even explains *why* it
   is `numeric(20,0)` and not a plain `bigint`: values like `pool_update.pledge` can
   exceed the maximum signed 64-bit integer, so a signed `bigint` would overflow.

On the Haskell side these domains pair with matching types
(`cardano-db/src/Cardano/Db/Types.hs`): `DbLovelace`, `DbWord64`, `DbInt65`. That is the
exact layer where the [pool_relay.port case study](../08-case-study-pool-relay-port.md)
found a bug - a value encoder that wrote an unsigned port through a signed 16-bit path -
so it is worth knowing this layer exists.

## Part 2: saving disk with the tx_out "consumed / prune" option

### Why tx_out is the problem child

From [primer 04](04-cardano-and-dbsync-for-this-tool.md), a **UTxO** is an unspent
transaction output - a chunk of funds waiting to be spent. db-sync records every output
ever created in the **`tx_out`** table, which makes it the **single largest table** in
the database. But most outputs eventually get *spent*, and once spent they are only of
historical interest. So db-sync offers a way to recognise spent outputs and, optionally,
delete them to reclaim disk.

### The five settings

This is chosen in the config file (`insert_options.tx_out`), not a command-line flag.
The values (see `cardano-db-sync/src/Cardano/DbSync/Config/Types.hs`) are:

| Setting | What it does to the database |
|---------|------------------------------|
| `enable` | Keep every output forever (the default, full history). |
| `disable` | Do not populate `tx_out` at all. |
| `consumed` | Keep every output, but stamp each one with *which transaction spent it* (a `consumed_by_tx_id` column). |
| `prune` | Like `consumed`, but also **delete** spent outputs once they are old enough to be safe, reclaiming disk. |
| `bootstrap` | Like `consumed`, but skip filling in old historical outputs during the initial sync (an even bigger time/space saving). |

### What "consumed" and "prune" actually do

- **Consumed** adds one fact per output: the id of the transaction that spent it. Nothing
  is deleted, so you still have full history, plus you can now ask "is this output spent,
  and by what?" cheaply. Mechanically it is an `UPDATE tx_out SET consumed_by_tx_id = ...`
  when db-sync sees the spend.
- **Prune** goes further: periodically it `DELETE`s outputs that were consumed before a
  safe block depth, shrinking `tx_out` toward roughly the *live* UTxO set. Because
  `ma_tx_out` (the multi-asset amounts on an output) is wired with `ON DELETE CASCADE`,
  deleting an output cleans up its asset rows automatically.

### The trade-off, and a one-way door

Pruning trades **history for disk**: you save a lot of space, but you can no longer query
the full historical set of outputs (an explorer showing "all UTxOs that ever existed at
this address" would be incomplete). That is the right choice for some operators and wrong
for others.

One caution worth knowing: turning pruning **on** is close to a one-way door. Once a
database has had spent outputs deleted, you cannot simply switch back to full history
without losing consistency, so db-sync remembers the choice and warns if you try to run
it a different way later. Pick the mode deliberately for a given database.

> For the comparison tool's purposes this matters too: two databases configured with
> *different* `tx_out` modes are not expected to match on `tx_out` row counts - one has
> pruned, the other has not. That is a configuration difference, not data corruption.
> See [how each table is compared](../06-how-each-table-is-compared.md).

**Next:** [Surrogate ids, sequences, and drift →](05-surrogate-ids-sequences-and-drift.md)
