"""The `card_rail_outcomes` vocabularies in Python and in SQL must not drift.

Deliberately NOT in the Postgres gate: it only reads two files, so gating it behind a database
would mean the drift it guards goes unchecked on every ordinary CI run — which is exactly when
someone adds a failure_reason to the Python list and forgets the CHECK.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/199_card_rail_outcomes.sql"


def test_the_python_vocabularies_match_the_check_constraints():
    """Two copies of a vocabulary is a drift hazard: if the Python list gains a value the CHECK
    does not, the route returns a clean 4xx locally and the database raises a 500 in production.
    Parsed out of the migration text so this fails the moment either side moves."""
    from db.card_rail_outcomes import FAILURE_REASONS, OUTCOMES, REPORTERS

    sql = _MIGRATION.read_text()
    for name, values in (
        ("ck_card_rail_outcome", OUTCOMES),
        ("ck_card_rail_reported_by", REPORTERS),
        ("ck_card_rail_failure_reason", FAILURE_REASONS),
    ):
        start = sql.index(name)
        block = sql[start:sql.index(")", sql.index("IN (", start))]
        for value in values:
            assert f"'{value}'" in block, f"{value!r} missing from {name} in migration 199"
        quoted = {t.strip().strip("'") for t in block[block.index("IN (") + 4:].split(",")}
        quoted = {q for q in quoted if q and not q.startswith("\n")}
        assert quoted == set(values), (
            f"{name} and its Python list disagree: "
            f"SQL-only={quoted - set(values)}, python-only={set(values) - quoted}"
        )
