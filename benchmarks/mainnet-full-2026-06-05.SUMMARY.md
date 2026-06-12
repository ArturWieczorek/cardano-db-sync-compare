# Full mainnet comparison - measured benchmark

- Date (UTC): 2026-06-05
- DB1 (13.6.0.5) tip: block 13313031, epoch 626
- DB2 (13.7.1.0) tip: block 13488662, epoch 634
- Common cutoff: block <= 13313031, epoch <= 624
- Mode: tiered (default), --workers 6
- **Total wall-clock: 9h42m01s (34921s)**
- Tables: 45 match, 7 discrepancies, 6 accumulator count-deltas (informational), 1 errors

> **Every difference is explained - see the full root-cause writeup in [INVESTIGATION-13.6.0.5-vs-13.7.1.0.md](INVESTIGATION-13.6.0.5-vs-13.7.1.0.md).** Highlights: the tool caught three known/fixed db-sync issues (pointer addresses #2053, epoch out_sum/fees #2118, epoch_stake zero-amount cleanup) and one **previously-unreported regression it discovered** (`pool_relay.port` signed-16-bit overflow in 13.7.1.0).

## Findings & root causes

| table | status | root cause |
|---|---|---|
| cost_model | COUNT_DIFF | EXPECTED: accumulator; tip gap. |
| drep_hash | COUNT_DIFF | EXPECTED: accumulator; tip gap. |
| epoch | HASH_DIFF | KNOWN FIX: out_sum/fees corruption #2118, repaired by migration 0048. DB1 (schema 44) lacks it. 13.7.1.0 correct. |
| epoch_stake | COUNT_DIFF | KNOWN FIX: legacy zero-amount rows removed by migration 0047. DB1 (schema 44) keeps them. 13.7.1.0 correct. |
| epoch_state | COUNT_DIFF | EXPECTED: small governance value/timing delta. |
| gov_action_proposal | HASH_DIFF | EXPECTED: Conway governance value/timing; no schema change. |
| multi_asset | COUNT_DIFF | EXPECTED: accumulator; DB2 is ~175k blocks ahead (tip gap). |
| new_committee | ERROR | TOOL BUG (fixed): wrong anchor (no epoch_no); now anchored via gov_action_proposal_id. |
| pool_hash | COUNT_DIFF | EXPECTED: accumulator; tip gap. |
| pool_relay | HASH_DIFF | REGRESSION (tool-discovered, unreported): ports >32767 stored negative (signed-16-bit) in 13.7.1.0. 13.6.0.5 correct. Worth filing upstream. |
| pool_stat | COUNT_DIFF | CONFIG: pool_stat insert option off in 13.6.0.5 (0 rows) -> feature difference, not corruption. |
| slot_leader | COUNT_DIFF | EXPECTED: accumulator; tip gap. |
| stake_address | COUNT_DIFF | EXPECTED: accumulator; tip gap. |
| tx_out | HASH_DIFF | KNOWN FIX: pointer-address encoding #2051/#2053 (fixed 13.7.0.1). Only addr1g/addr1y pointer addresses differ, ~block 7M. 13.7.1.0 correct. |

## Slowest tables

| table | status | rows (db1/db2) | seconds |
|---|---|---|---|
| ma_tx_out | MATCH | 1123883162/1123883162 | 8221.6 |
| tx_in | MATCH | 334791414/334791414 | 6498.9 |
| tx_out | HASH_DIFF | 345996649/345996649 | 5363.4 |
| collateral_tx_in | MATCH | 29470129/29470129 | 3850.1 |
| reward | MATCH | 415153735/415153735 | 3052.7 |
| tx_metadata | MATCH | 136431432/136431432 | 2522.1 |
| epoch_stake | COUNT_DIFF | 450149435/440374279 | 2378.5 |
| datum | MATCH | 34007902/34007902 | 1543.3 |
| withdrawal | MATCH | 11596065/11596065 | 1490.2 |
| delegation | MATCH | 3495913/3495913 | 1392.1 |
| collateral_tx_out | MATCH | 13580528/13580528 | 1352.7 |
| extra_key_witness | MATCH | 36054207/36054207 | 1331.2 |
| reference_tx_in | MATCH | 30940090/30940090 | 1228.8 |
| redeemer_data | MATCH | 1768868/1768868 | 1137.5 |
| tx | MATCH | 120243441/120243441 | 1058.5 |

## All tables by time

| table | status | seconds |
|---|---|---|
| ma_tx_out | MATCH | 8221.6 |
| tx_in | MATCH | 6498.9 |
| tx_out | HASH_DIFF | 5363.4 |
| collateral_tx_in | MATCH | 3850.1 |
| reward | MATCH | 3052.7 |
| tx_metadata | MATCH | 2522.1 |
| epoch_stake | COUNT_DIFF | 2378.5 |
| datum | MATCH | 1543.3 |
| withdrawal | MATCH | 1490.2 |
| delegation | MATCH | 1392.1 |
| collateral_tx_out | MATCH | 1352.7 |
| extra_key_witness | MATCH | 1331.2 |
| reference_tx_in | MATCH | 1228.8 |
| redeemer_data | MATCH | 1137.5 |
| tx | MATCH | 1058.5 |
| stake_registration | MATCH | 967.6 |
| redeemer | MATCH | 887.4 |
| ma_tx_mint | MATCH | 193.4 |
| delegation_vote | MATCH | 170.0 |
| stake_deregistration | MATCH | 146.3 |
| block | MATCH | 141.6 |
| script | MATCH | 97.9 |
| voting_procedure | MATCH | 71.0 |
| multi_asset | COUNT_DIFF | 67.5 |
| voting_anchor | MATCH | 54.1 |
| treasury | MATCH | 22.5 |
| stake_address | COUNT_DIFF | 15.8 |
| pool_metadata_ref | MATCH | 14.5 |
| pool_owner | MATCH | 13.6 |
| reward_rest | MATCH | 10.1 |
| drep_registration | MATCH | 8.2 |
| reserve | MATCH | 6.4 |
| pool_update | MATCH | 3.6 |
| epoch_param | MATCH | 2.8 |
| pool_retire | MATCH | 2.3 |
| pool_stat | COUNT_DIFF | 1.7 |
| ada_pots | MATCH | 1.6 |
| drep_distr | MATCH | 1.6 |
| gov_action_proposal | HASH_DIFF | 0.9 |
| pool_relay | HASH_DIFF | 0.8 |
| param_proposal | MATCH | 0.5 |
| epoch_state | COUNT_DIFF | 0.2 |
| drep_hash | COUNT_DIFF | 0.2 |
| pool_hash | COUNT_DIFF | 0.2 |
| epoch | HASH_DIFF | 0.1 |
| slot_leader | COUNT_DIFF | 0.1 |
| epoch_stake_progress | MATCH | 0.1 |
| tx_cbor | MATCH | 0.1 |
| treasury_withdrawal | MATCH | 0.0 |
| committee_member | MATCH | 0.0 |
| committee_registration | MATCH | 0.0 |
| new_committee | ERROR | 0.0 |
| committee | MATCH | 0.0 |
| committee_de_registration | MATCH | 0.0 |
| constitution | MATCH | 0.0 |
| cost_model | COUNT_DIFF | 0.0 |
| event_info | MATCH | 0.0 |
| pot_transfer | MATCH | 0.0 |
| committee_hash | MATCH | 0.0 |
