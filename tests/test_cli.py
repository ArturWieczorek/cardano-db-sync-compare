"""Tests for argument parsing."""

from __future__ import annotations

import pytest

from db_sync_comparator.cli import build_arg_parser, parse_block_range


def test_parse_block_range():
    assert parse_block_range("8000000:8010000") == (8000000, 8010000)
    assert parse_block_range(None) is None


def test_arg_parser_defaults():
    args = build_arg_parser().parse_args(["--db1", "dbname=a", "--db2", "dbname=b"])
    assert args.db1 == "dbname=a"
    assert args.workers == 4
    assert args.full is False
    assert args.giant_fk_depth == 1
    assert args.epoch_margin == 2
    assert args.work_mem == "256MB"
    assert args.json_path is None


def test_arg_parser_requires_both_dbs():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--db1", "dbname=a"])


def test_localize_mismatch_records_what_the_localizer_found(monkeypatch):
    # The Phase 2 wrapper must pass the windows and the "stopped early" flag on to
    # the result (and so to the JSON report). No database: the localizer is faked.
    from db_sync_comparator import cli
    from db_sync_comparator.model import Localization, TablePlan, TableResult

    windows = ["block_no 1..1: content differs (db1 n=1, db2 n=1)"]
    monkeypatch.setattr(cli, "localize", lambda *a, **k: Localization(list(windows), truncated=True))
    plan = TablePlan("tx_out", "normal", anchor_kind="idrange", spine="tx", anchor_col="tx_id")
    r = TableResult("tx_out", "normal", status="HASH_DIFF")
    cli._localize_mismatch(r, plan, None, None, 5, 0, "bisect", 1024, 0)
    assert r.localized == windows
    assert r.localized_truncated is True
    assert r.localize_error == ""


def test_plan_shows_the_genesis_condition_only_in_cutoff_mode(capsys):
    # --plan must print the SQL Phase 1 really runs: with the NULL-anchor
    # condition in cutoff mode, and without it for a --block-range run.
    from db_sync_comparator import cli
    from db_sync_comparator.model import TablePlan

    plan = TablePlan(
        "committee",
        "normal",
        select_exprs=['t0."quorum_numerator"'],
        anchor_kind="idrange",
        spine="gov_action_proposal",
        anchor_col="gov_action_proposal_id",
        null_anchor=("epoch_state", "committee_id", "epoch_no"),
    )
    ranges = {"gov_action_proposal": (1, 9)}
    cli._print_plan([plan], ranges, 300, False)
    out = capsys.readouterr().out
    assert 'IS NULL AND EXISTS (SELECT 1 FROM "epoch_state" na' in out
    assert "rows with NULL gov_action_proposal_id (genesis)" in out
    cli._print_plan([plan], ranges, 300, True)
    assert "IS NULL" not in capsys.readouterr().out
