"""Plain data structures shared across the package.

These carry no behaviour and import nothing from the rest of the package, so
every other module can depend on them without creating cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TableSchema:
    """One table as introspected from a live database."""

    name: str
    columns: list[str]  # in ordinal order
    coltypes: dict[str, str]  # column -> SQL data type
    pk: list[str]  # primary-key columns
    generated: set[str]  # GENERATED ALWAYS columns
    fks: dict[str, tuple[str, str]]  # declared FKs: local_col -> (ref_table, ref_col)


@dataclass
class TablePlan:
    """How a single table will be compared (the generated-SQL ingredients)."""

    name: str
    kind: str  # 'normal' | 'giant' | 'accumulator' | 'excluded'
    reason: str = ""  # why excluded (when kind == 'excluded')
    select_exprs: list[str] = field(default_factory=list)  # normalized columns to hash
    joins: str = ""  # FK-translation LEFT JOINs
    anchor_kind: str = "none"  # 'idrange' | 'epoch' | 'none'
    spine: str | None = None  # spine table whose id-range bounds this one
    anchor_col: str | None = None  # column on t0 carrying the spine FK
    epoch_expr: str | None = None  # epoch expression for epoch-anchored tables
    value_col: str | None = None  # numeric column for the cheap sum/min/max proof
    skipped_cols: list[str] = field(default_factory=list)  # columns dropped (deep FK / unmapped)
    extra_cols: dict[str, list[str]] = field(default_factory=dict)  # version-only columns


@dataclass
class TableResult:
    """The outcome of comparing one table across the two databases."""

    name: str
    kind: str
    n1: int = 0
    n2: int = 0
    h1: tuple = (0, 0)
    h2: tuple = (0, 0)
    value1: tuple | None = None
    value2: tuple | None = None
    status: str = "PENDING"  # MATCH | COUNT_DIFF | HASH_DIFF | VALUE_DIFF | ERROR
    note: str = ""
    seconds: float = 0.0
    skipped_cols: list[str] = field(default_factory=list)
    schema_drift: dict = field(default_factory=dict)
    localized: list[str] = field(default_factory=list)
    verify: dict = field(default_factory=dict)  # optional accumulator subset-check result
