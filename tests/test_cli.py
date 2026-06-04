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
