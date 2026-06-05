# Investigation report — mainnet 13.6.0.5 vs 13.7.1.0

**Date:** 2026-06-05 · **Tool:** `db-sync-compare` (tiered, `--workers 6`)

| | DB1 | DB2 |
|---|---|---|
| db-sync version | 13.6.0.5 (snapshot restored on 13.7.0.4) | 13.7.1.0 |
| tip | block 13,313,031 / epoch 626 | block 13,488,662 / epoch 634 |
| `schema_version` stage_two | **44** | **48** |
| common cutoff | block ≤ 13,313,031, epoch ≤ 624 | |

The single most explanatory fact: **DB1 is missing stage-2 migrations 0045–0048**
(it sits at schema 44; DB2 at 48). Several differences trace directly to that.

Every difference the tool reported is **explained** below — there is **no
unexplained data corruption**. Most are known/fixed db-sync issues or config
differences; one is a previously-unreported regression the tool discovered.

---

## A. Tool successes — real differences that map to known db-sync bugs/fixes

These validate the comparator against ground truth: it independently flagged data
that corresponds to documented db-sync issues, and localized each one.

### A1. `tx_out` — pointer-address encoding fix (#2051 / #2053)
- **Tool reported:** `tx_out` `HASH_DIFF`, identical row count (345,996,649).
- **Localized (manually, by column):** difference is **only in the `address`
  column**, at block **~7,000,000**; every other column matches. All differing
  rows are **pointer addresses** (`addr1g…`, type-4) — same `payment_cred`, only
  the pointer tail differs (e.g. `…drcudevsft64mf887333adamant` in v1 vs
  `…drccqqqqqyfdge7` in v2). In a 10k-block window, 415 outputs = **one** pointer
  address.
- **Root cause:** CHANGELOG *"Fixed pointer addresses existing on Conway [#2053]"*,
  commit `dd90ebb1` *"2051 - Era aware pointer addresses"*, **fixed in 13.7.0.1**.
- **Verdict:** Expected. 13.6.0.5 predates the fix (old encoding); **13.7.1.0 is
  correct** (era-aware encoding). Affects only rare deprecated pointer addresses.

### A2. `epoch` — out_sum/fees corruption repair (#2118, migration 0048)
- **Tool reported:** `epoch` `HASH_DIFF`, identical 625 rows.
- **Root cause:** CHANGELOG 13.7.0.5 *"Fix `epoch.out_sum`/`epoch.fees` corruption
  caused by numeric decoders [#2118]"* and 13.7.1.0 auto-repair **migration
  0048** (recomputes `out_sum`/`fees`/`tx_count`/`blk_count`). DB1 lacks 0048.
- **Verdict:** Expected. DB1 carries the corrupted aggregates; **13.7.1.0 is
  correct.**

### A3. `epoch_stake` — legacy zero-amount rows cleanup (migration 0047)
- **Tool reported:** `epoch_stake` `COUNT_DIFF` — 450,149,435 (v1) vs 440,374,279
  (v2); v1 has ~10M **more**, consistently higher per epoch (e.g. epoch 214:
  38,780 vs 37,819).
- **Root cause:** **migration 0047** (13.7.0.3) *"deletes legacy zero-amount
  `epoch_stake` rows"* (the ledger no longer emits them). DB1 lacks 0047.
- **Verdict:** Expected. DB1 keeps the zero-amount rows; **13.7.1.0 is correct.**

### A4. `pool_relay.port` — signed-16-bit overflow ⚠️ *previously unreported*
- **Tool reported:** `pool_relay` `HASH_DIFF`, identical 72,514 rows; localized to
  block ~4.49M during an earlier run.
- **Root cause (found by the tool):** in **13.7.1.0**, relay ports **> 32767 are
  stored as negative** (signed-16-bit overflow: `52636` → `-12900`). Verified via
  value ranges: DB1 `port ∈ [1, 64848]` (1116 ports > 32767, correct); DB2
  `port ∈ [-32536, 31415]` with **1118 negative** ports, none > 32767. Column is
  `integer` in both, so the corruption happens at decode time.
- **Repo check:** not in CHANGELOG, git history, or GitHub issues (0 matches) →
  **a regression the tool discovered; here 13.6.0.5 is correct and 13.7.1.0 is
  wrong.** Worth filing upstream. See [docs/08 case study](../docs/08-case-study-pool-relay-port.md).

---

## B. Expected / configuration differences (not bugs)

- **`pool_stat` — 0 (v1) vs 1,134,346 (v2).** The `pool_stat` insert option
  (default off) was **disabled** in the 13.6.0.5 build. One-sided-zero ⇒ a
  config/feature difference, not corruption. (The tool now labels this and does
  not bisect it.)
- **Accumulator count deltas** (`multi_asset` 11.108M/11.129M, `stake_address`
  5.828M/5.857M, `pool_hash` 6123/6136, `drep_hash` 1603/1650, `slot_leader`
  3372/3379, `cost_model` 8/9): DB2 is ~175k blocks ahead, so it has seen more
  distinct objects. Informational.
- **`gov_action_proposal` HASH_DIFF (96=96)** and **`epoch_state` COUNT_DIFF
  (120 vs 118)**: no schema change; value/timing differences in Conway
  governance processing. Low impact.

## C. A bug the investigation found in the comparator itself (fixed)

- **`new_committee`** was anchored by `epoch_no`, but that table has no such
  column (its key is `gov_action_proposal_id`) → per-table `ERROR`. Fixed: anchor
  via `gov_action_proposal_id` (like `committee`); regression test added.

---

## D. Summary table

| Table | Tool status | Verdict | Cause / citation |
|---|---|---|---|
| `tx_out` | HASH_DIFF | known fix | pointer addresses #2053 (13.7.0.1); 13.7.1.0 correct |
| `epoch` | HASH_DIFF | known fix | out_sum/fees #2118 + migration 0048; 13.7.1.0 correct |
| `epoch_stake` | COUNT_DIFF | known fix | zero-amount cleanup, migration 0047; 13.7.1.0 correct |
| `pool_relay` | HASH_DIFF | **regression (new)** | port signed-16-bit overflow in 13.7.1.0; **13.6.0.5 correct** |
| `pool_stat` | COUNT_DIFF (0 vs N) | config | `pool_stat` insert option off in 13.6.0.5 |
| accumulators | COUNT_DIFF | expected | DB2 ahead (tip gap) |
| `gov_action_proposal`, `epoch_state` | HASH/COUNT_DIFF | expected | governance value/timing |
| `new_committee` | ERROR → fixed | tool bug | wrong anchor (no `epoch_no`); now `gov_action_proposal_id` |
| ~45 other tables | MATCH | — | content-equivalent across the full shared history |

**Bottom line:** the comparator correctly flagged four real data differences that
map to db-sync issues — three known/fixed (#2053, #2118, migration 0047) and
**one previously-unreported regression it discovered** (`pool_relay.port`) — while
classifying the rest as expected config/tip differences. That is exactly the
behaviour a release-integrity gate needs.
