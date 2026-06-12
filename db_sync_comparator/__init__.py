"""Content-equivalence comparator for two cardano-db-sync PostgreSQL databases.

The package is split into small, single-responsibility modules whose
dependencies point inward toward :mod:`db_sync_comparator.model` and
:mod:`db_sync_comparator.registries` (no import cycles):

* ``model``      - the dataclasses (``TableSchema``, ``TablePlan``, ``TableResult``).
* ``registries`` - the hand-built cardano-db-sync schema knowledge.
* ``sql``        - pure SQL generation (set-hash, FK translation, bounds).
* ``schema``     - live-database schema introspection.
* ``planning``   - turn two schemas into a per-table comparison plan.
* ``ranges``     - per-database id-range windows for the common chain boundary.
* ``db``         - connection handling.
* ``compare``    - run the hashes and localize mismatches.
* ``report``     - human summary and JSON report.
* ``cli``        - argument parsing and orchestration (``main``).

Importing this top-level package does **not** import psycopg, so the pure-logic
modules can be unit-tested without a database driver installed.
"""

__version__ = "0.1.0"
