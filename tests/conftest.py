"""Shared test fixtures.

`pythonpath = ["."]` in pyproject.toml makes `import db_sync_comparator` work
from the repo root; this file adds a small factory for building synthetic
`TableSchema`s so the planning/SQL logic can be tested without a database.
"""

from __future__ import annotations

import pytest

from db_sync_comparator.model import TableSchema


@pytest.fixture
def make_schema():
    def _make(
        name: str,
        columns: list[str],
        coltypes: dict[str, str] | None = None,
        pk: list[str] | None = None,
        generated: set[str] | None = None,
    ) -> TableSchema:
        types = {c: "text" for c in columns}
        if coltypes:
            types.update(coltypes)
        return TableSchema(
            name=name,
            columns=list(columns),
            coltypes=types,
            pk=pk if pk is not None else (["id"] if "id" in columns else []),
            generated=set(generated or set()),
            fks={},
        )

    return _make
