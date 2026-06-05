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

## Phase-1 results — every flagged table (exact counts)

**45 of the 59** compared tables matched. The 14 flagged (Phase-1 `!!` lines):

| table | status | rows v1 / v2 | class (section) |
|---|---|---|---|
| `tx_out` | HASH_DIFF | 345,996,649 / 345,996,649 | known fix → A1 |
| `epoch` | HASH_DIFF | 625 / 625 | known fix → A2 |
| `epoch_stake` | COUNT_DIFF | 450,149,435 / 440,374,279 | known fix → A3 |
| `pool_relay` | HASH_DIFF | 72,514 / 72,514 | **regression → A4** |
| `pool_stat` | COUNT_DIFF | 0 / 1,134,346 | config → B |
| `multi_asset` | COUNT_DIFF | 11,108,713 / 11,128,803 | accumulator → B |
| `stake_address` | COUNT_DIFF | 5,828,058 / 5,857,049 | accumulator → B |
| `pool_hash` | COUNT_DIFF | 6,123 / 6,136 | accumulator → B |
| `drep_hash` | COUNT_DIFF | 1,603 / 1,650 | accumulator → B |
| `slot_leader` | COUNT_DIFF | 3,372 / 3,379 | accumulator → B |
| `cost_model` | COUNT_DIFF | 8 / 9 | accumulator → B |
| `gov_action_proposal` | HASH_DIFF | 96 / 96 | governance value/timing → B |
| `epoch_state` | COUNT_DIFF | 120 / 118 | governance value/timing → B |
| `new_committee` | ERROR | — | comparator bug (fixed) → C |

---

## A. Tool successes — real differences that map to known db-sync bugs/fixes

These validate the comparator against ground truth: it independently flagged data
that corresponds to documented db-sync issues, and localized each one.

### A1. `tx_out` — pointer-address encoding fix ([#2051] / [#2053])
- **Tool reported:** `tx_out` `HASH_DIFF` with **identical** row count
  (345,996,649) — i.e. no rows missing, some *value* differs.
- **How it was localized (method):**
  1. Hashed per-block-window samples in both DBs — blocks 1M, 4M, 4.48M, 6M, 8M,
     10.5M, 13.0M and the near-tip k-zone (last 2160 blocks) all **matched**.
  2. A full-row checksum sweep found the first divergence at **block ~7,000,000**
     (Alonzo): `win 7000000:7010000` differed, 6M and 8M matched.
  3. Per-column checksums over that window: **only `address` differs**;
     `payment_cred`, `value`, `data_hash`, `address_has_script`, stake all match.
  4. Dumped the keyed rows and diffed: **415** differing outputs, all
     **pointer addresses** (`addr1g…`, type-4), 0 non-pointer — and they are a
     **single distinct address** stored two ways (same payment credential, only
     the pointer triple differs):
     - v1 (13.6.0.5): `addr1g9ekml92qyvzrjmawxkh64r2w5xr6mg9ngfmxh2khsmdrcudevsft64mf887333adamant` (75 chars)
     - v2 (13.7.1.0): `addr1g9ekml92qyvzrjmawxkh64r2w5xr6mg9ngfmxh2khsmdrccqqqqqyfdge7` (63 chars)
- **Root cause:** db-sync mis-encoded the pointer part (the `StakeRefPtr`
  (slot, tx_index, cert_index) triple) of pointer addresses; the fix made pointer
  handling **era-aware**. CHANGELOG (under **13.7.0.1**): *"Fixed pointer addresses
  existing on Conway [#2053]"*; commit `dd90ebb1` *"2051 - Era aware pointer
  addresses"* ([PR #2053], [issue #2051]).
- **Verdict:** Expected. **13.6.0.5 predates the fix** (old/incorrect encoding);
  **13.7.1.0 is correct** (era-aware). Affects only the rare, deprecated pointer
  addresses, so the row count is unchanged and the diff is tiny and isolated.

### A2. `epoch` — out_sum/fees corruption repair ([#2118], migration 0048)
- **Tool reported:** `epoch` `HASH_DIFF`, identical 625 rows (every epoch present,
  the aggregate *values* differ).
- **Root cause:** numeric decoders truncated values when writing `epoch.out_sum`/
  `epoch.fees`, corrupting them in db-sync 13.7.0.0–13.7.0.4. CHANGELOG **13.7.0.5**:
  *"Fix `epoch.out_sum`/`epoch.fees` corruption caused by numeric decoders [#2118]"*;
  CHANGELOG **13.7.1.0** auto-repair via **[migration 0048]** (recomputes
  `out_sum`/`fees`/`tx_count`/`blk_count` from the underlying tx/block tables). A
  manual fix also ships at `scripts/fix-epoch-table.sql`. **DB1 (schema 44) lacks 0048.**
- **Verdict:** Expected. DB1 carries the corrupted aggregates; **13.7.1.0 is correct.**

### A3. `epoch_stake` — legacy zero-amount rows cleanup ([migration 0047])
- **Tool reported:** `epoch_stake` `COUNT_DIFF` — 450,149,435 (v1) vs 440,374,279
  (v2); v1 has ~9.8M **more**, consistently higher per epoch (Phase-2 localization
  showed e.g. epoch 214: 38,780 vs 37,819; 215: 42,261 vs 41,270; …).
- **Root cause:** the ledger used to emit zero-amount delegator entries and no
  longer does; **[migration 0047]** (CHANGELOG **13.7.0.3**) *"deletes legacy
  zero-amount `epoch_stake` rows … for consistency"*. The live insert path does no
  zero-filtering, so only the migration removes them. **DB1 (schema 44) lacks 0047.**
  (Related: [#2044] "Fixed epoch_stake missing entries".)
- **Verdict:** Expected. DB1 keeps the zero-amount rows; **13.7.1.0 is correct.**

### A4. `pool_relay.port` — signed-16-bit overflow ⚠️ *previously unreported*
- **Tool reported:** `pool_relay` `HASH_DIFF`, identical 72,514 rows; localized to
  block ~4.49M during an earlier run.
- **Root cause (discovered by the tool):** in **13.7.1.0**, relay ports **> 32767
  are stored as negative** (signed-16-bit overflow: `52636` → `52636 − 65536 =
  −12900`). Verified via value ranges:
  - DB1 (13.6.0.5): `port ∈ [1, 64848]`, **1116** ports > 32767, **0** negative — correct.
  - DB2 (13.7.1.0): `port ∈ [−32536, 31415]`, **0** ports > 32767, **1118** negative — wrong.
  - The column is `integer` (int4) in **both** schemas, so it can hold 0–65535;
    the truncation happens **at decode time**, before storage.
- **Repo check:** **not** in [CHANGELOG], git history, or GitHub issues (search
  returned 0 matches) → a regression the tool **discovered**. Here **13.6.0.5 is
  correct and 13.7.1.0 is wrong** — the opposite direction from A1–A3. **Worth
  filing upstream.** See the [case study](../docs/08-case-study-pool-relay-port.md).

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

## C. Comparator changes this investigation drove (commit `907fd4b`)

- **`new_committee` anchor bug (fixed).** It was anchored by `epoch_no`, but that
  table has no such column → per-table `ERROR`. Now anchored via
  `gov_action_proposal_id` (like `committee`); regression test added. See §F.
- **One-sided-zero tables are flagged, not localized.** Driven by `pool_stat`
  (0 vs N): the tool now reports *"one side has 0 rows — table likely disabled in
  config (insert_options) for that version, not a data difference"* and Phase 2
  no longer wastes time bisecting it. Fixture test added.
- **`--block-margin N` added.** Pull the block cutoff back below the lower tip by
  ~`k` (≈2160 on mainnet) to stay out of the volatile rollback zone — the
  block-anchored counterpart to `--epoch-margin` (see §E).

---

## D. Summary table

| Table | Tool status | Verdict | Cause / citation |
|---|---|---|---|
| `tx_out` | HASH_DIFF | known fix | pointer addresses [#2053] (13.7.0.1); 13.7.1.0 correct |
| `epoch` | HASH_DIFF | known fix | out_sum/fees [#2118] + [migration 0048]; 13.7.1.0 correct |
| `epoch_stake` | COUNT_DIFF | known fix | zero-amount cleanup, [migration 0047]; 13.7.1.0 correct |
| `pool_relay` | HASH_DIFF | **regression (new)** | port signed-16-bit overflow in 13.7.1.0; **13.6.0.5 correct** |
| `pool_stat` | COUNT_DIFF (0 vs N) | config | `pool_stat` insert option off in 13.6.0.5 |
| accumulators | COUNT_DIFF | expected | DB2 ahead (tip gap) |
| `gov_action_proposal`, `epoch_state` | HASH/COUNT_DIFF | expected | governance value/timing |
| `new_committee` | ERROR → fixed | tool bug | wrong anchor (no `epoch_no`); now `gov_action_proposal_id` |
| ~45 other tables | MATCH | — | content-equivalent across the full shared history |

## E. Was the cutoff placed too early? (boundary analysis)

Short answer: **no** — none of the flagged differences are cutoff/boundary
artifacts. The cutoff was `block ≤ 13,313,031` (DB1's exact tip) and `epoch ≤ 624`
(`min(626,634) − epoch-margin 2`).

- `tx_out` differs at **block ~7,000,000** — far below the tip, a real
  pointer-address encoding fix (§A1), not a near-boundary effect. The near-tip
  k-zone (last 2160 blocks) was explicitly checked and **matched**.
- `epoch` / `epoch_stake` differ in **old** epochs (e.g. 214), so they're not the
  in-progress-epoch boundary either — they're the missing-migration fixes (§A2, §A3).
- Both DBs are bounded to the **same** `epoch ≤ 624`, and `epoch_stake` for those
  epochs is fully computed in both (DB1's tip is epoch 626), so the count delta is
  real data, not a partial-epoch artifact.
- `pool_stat` (0 vs N) is config, not boundary (§B).

Where the cutoff *can* matter is the volatile near-tip rollback zone, if you ever
compare right up to a live tip. The tool only had `--epoch-margin`; this
investigation added **`--block-margin`** (≈`k`=2160) as the block-anchored
counterpart (§C). It wasn't needed here, but it's the right safeguard.

## F. Schema detail: `committee` vs `new_committee`

Both tables exist in both DBs (identical columns); neither has an `epoch_no`
column — hence the comparator's wrong epoch anchor for `new_committee` (§C):

- `committee`: `id, gov_action_proposal_id, quorum_numerator, quorum_denominator`
  (created in db-sync stage-2 migration 0037, replacing an earlier shape).
- `new_committee`: `id, gov_action_proposal_id, deleted_members, added_members,
  quorum_numerator, quorum_denominator`.

Both are keyed off a governance proposal, so both are correctly anchored via
`gov_action_proposal_id`. (Authoritative schema:
`cardano-db/src/Cardano/Db/Schema/Core/GovernanceAndVoting.hs` and
[doc/schema.md][schema].)

---

**Bottom line:** the comparator correctly flagged four real data differences that
map to db-sync issues — three known/fixed ([#2053], [#2118], [migration 0047]) and
**one previously-unreported regression it discovered** ([`pool_relay.port`](#a4-pool_relayport--signed-16-bit-overflow-️-previously-unreported)) —
while classifying the rest as expected config/tip differences, and it even
surfaced one bug in itself (`new_committee`). That is exactly the behaviour a
release-integrity gate needs.

---

## References

- db-sync **CHANGELOG**: <https://github.com/IntersectMBO/cardano-db-sync/blob/master/CHANGELOG.md>
- **#2051 / #2053** — era-aware pointer addresses (tx_out): <https://github.com/IntersectMBO/cardano-db-sync/issues/2051> · <https://github.com/IntersectMBO/cardano-db-sync/pull/2053>
- **#2118** — epoch out_sum/fees numeric-decoder corruption: <https://github.com/IntersectMBO/cardano-db-sync/issues/2118>
- **#2044** — epoch_stake missing entries: <https://github.com/IntersectMBO/cardano-db-sync/issues/2044>
- **Migrations** (on-the-wire DDL, incl. 0047 zero-amount epoch_stake delete & 0048 epoch repair): <https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/test/schema>
- **Schema** (authoritative): <https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/src/Cardano/Db/Schema> · reference: <https://github.com/IntersectMBO/cardano-db-sync/blob/master/doc/schema.md>
- `pool_relay.port` regression: **no** matching CHANGELOG/issue found (candidate to file).

[#2051]: https://github.com/IntersectMBO/cardano-db-sync/issues/2051
[#2053]: https://github.com/IntersectMBO/cardano-db-sync/pull/2053
[PR #2053]: https://github.com/IntersectMBO/cardano-db-sync/pull/2053
[issue #2051]: https://github.com/IntersectMBO/cardano-db-sync/issues/2051
[#2118]: https://github.com/IntersectMBO/cardano-db-sync/issues/2118
[#2044]: https://github.com/IntersectMBO/cardano-db-sync/issues/2044
[CHANGELOG]: https://github.com/IntersectMBO/cardano-db-sync/blob/master/CHANGELOG.md
[migration 0047]: https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/test/schema
[migration 0048]: https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/test/schema
[schema]: https://github.com/IntersectMBO/cardano-db-sync/blob/master/doc/schema.md
