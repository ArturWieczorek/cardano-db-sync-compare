# Primer 01 — Databases in 2 minutes

> **What's in here:** a quick refresher so we all use the same words. If you're
> comfortable with tables, rows, primary keys and foreign keys, skim this and
> move on.
>
> **Prerequisites:** none.

## A table is a spreadsheet

A database **table** is just a spreadsheet with a fixed set of named **columns**
and any number of **rows**. Here is a tiny slice of db-sync's `block` table:

| id | block_no | hash         | epoch_no | time                |
|----|----------|--------------|----------|---------------------|
| 1  | (none)   | `0x5f20...`  | (none)   | 2017-09-23 21:44:51 |
| 2  | 0        | `0x89d9...`  | 0        | 2017-09-23 21:44:51 |
| 3  | 1        | `0xf0f7...`  | 0        | 2017-09-23 21:45:02 |

Each **row** is one record (here, one block of the blockchain). Each **column**
holds one field of that record. A **value** can be a number, text, a timestamp, a
chunk of raw bytes (like that `hash`), or empty — written `NULL`, meaning "no
value".

## Querying

You read a table with SQL. The only shapes you need for these docs:

```sql
SELECT block_no, hash FROM block WHERE epoch_no = 5;   -- pick columns + filter rows
SELECT count(*) FROM block;                            -- how many rows
SELECT min(block_no), max(block_no) FROM block;        -- smallest / largest value
```

## Primary key (PK): the row's unique ticket

Almost every db-sync table has a column called **`id`** that is the **primary
key** — a number that is unique for every row in that table. Think of it as a
cloakroom ticket: row `id = 42` is one specific row, and no other row in that
table has ticket 42. The database guarantees it's unique.

Crucially for this whole project: in db-sync, `id` is **assigned by the database**
as rows arrive, not taken from the blockchain. Hold that thought — it's the
subject of [primer 05](05-surrogate-ids-sequences-and-drift.md).

## Foreign key (FK): a cell that points at another table

A **foreign key** is a column whose value is the *primary key of a row in another
table* — a cross-reference. For example, db-sync's `tx` table (transactions) has
a column `block_id`. If a transaction row has `block_id = 3`, that means "this
transaction is in the block whose `id` is 3" (the third row of our table above).

| id  | block_id | hash (of the transaction) |
|-----|----------|---------------------------|
| 10  | 3        | `0xaa01...`               |
| 11  | 3        | `0xbb02...`               |

So `block.id` (a primary key) and `tx.block_id` (a foreign key) link the two
tables. This is how db-sync connects transactions to blocks, outputs to
transactions, and so on — a big web of tables pointing at each other by `id`.

> **One surprise, important later:** db-sync does **not** ask PostgreSQL to
> *enforce* these foreign keys (it would make loading the chain too slow). They
> are foreign keys "by convention" — the column `block_id` clearly means
> `block.id`, but the database itself isn't policing it. We'll come back to this
> in [primer 05](05-surrogate-ids-sequences-and-drift.md) and
> [how each table is compared](../06-how-each-table-is-compared.md).

## The words we'll reuse

- **row / column / value / NULL**
- **query** — a `SELECT` that reads data
- **primary key (PK)** — the unique `id` of a row
- **foreign key (FK)** — a column holding another table's PK

Everything else (indexes, hashing, sequences) we teach as we go.

**Next:** [Indexes and table scans →](02-indexes-and-table-scans.md)
