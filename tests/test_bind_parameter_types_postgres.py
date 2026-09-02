"""Two statements Postgres could not type a bind parameter for.

Both became visible only when the PREPARE sweep learned to follow a literal
assigned to a function-local, and both are the #1588 defect class the whole
dialect gate exists for: SQLite types nothing, so a bind Postgres cannot resolve
is invisible to every other job in CI.

    db/audit_evidence.py::claim_next_pending_verification
        AmbiguousParameterError: could not determine data type of parameter $4

      `(:verifier_id IS NULL OR verifier_id = :verifier_id)` — the optional-filter
      idiom. `$4 IS NULL` gives Postgres nothing, and it will not carry the type
      back from the other arm of the OR, so the whole statement is unplannable.
      Not "slow" or "untyped": asyncpg raises before the query runs. This is the
      atomic claim for the verification worker — `FOR UPDATE SKIP LOCKED`, one row
      — so it has never claimed anything on Postgres.

    db/beneficiary_repo.py::BeneficiaryRepo.verify
        AmbiguousParameterError: inconsistent types deduced for parameter $1
        DETAIL: text versus character varying

      One bind reaching two places that disagree: `verify_status = :status`
      types it from the column (character varying), while `:status = 'verified'`
      inside the CASE types it as text. Postgres needs ONE type per parameter.

Both fixes are the CAST the gate's own failure message recommends, applied so
every use of the bind agrees. Note the beneficiary one needs the cast on BOTH
uses — casting only inside the CASE leaves the assignment typing it varchar and
the error is unchanged. Checked, not assumed.

WHY THIS FILE EXECUTES RATHER THAN TRUSTING THE SWEEP. PREPARE validates TYPES,
never VALUES. It cannot tell a claim that locks the right row from one that locks
any row, and it cannot see that a CASE still sets `verified_at` only for
'verified'. Adding a CAST is a change to what the statement MEANS as far as this
gate can see, so the semantics need their own coverage — and neither statement
had any: nothing in the suite executed either one, on any engine.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_bind_parameter_types_postgres.py

Never point this at prod — it writes.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN = uuid.uuid4().hex[:8]
AGENT = f"bindt_agent_{RUN}"

# verification_runs.verify_id is a UUID column, so these must be real UUIDs
# rather than readable slugs. Worth a word, because PREPARE never said so: the
# statement PLANNED happily with a text bind and failed at BIND time. That is
# the documented limit of the static sweep — it validates types, never values —
# and it is why this file executes.
ANY_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
NULL_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# db/audit_evidence.py — the optional-filter bind
# ---------------------------------------------------------------------------
@pytest.fixture
async def evidence_db():
    from db.audit_evidence import ensure_audit_evidence_tables
    from db.database import database

    await database.connect()
    # The module's own DDL, so this fixture cannot drift from the table the
    # claim actually updates.
    await ensure_audit_evidence_tables()
    try:
        yield database
    finally:
        await database.execute(
            "DELETE FROM verification_runs WHERE merchant_id = :m", {"m": AGENT}
        )
        await database.disconnect()


AUDIT_RUN = str(uuid.uuid4())


async def _pending(verify_id: str, *, verifier_id: str | None) -> None:
    from db.database import database

    await database.execute(
        """
        INSERT INTO verification_runs
            (verify_id, audit_run_id, merchant_id, verifier_id, status)
        VALUES (:verify_id, :audit_run_id, :merchant_id, :verifier_id, 'pending')
        """,
        {
            "verify_id": verify_id, "audit_run_id": AUDIT_RUN,
            "merchant_id": AGENT, "verifier_id": verifier_id,
        },
    )


async def test_the_verification_claim_runs_at_all(evidence_db):
    """The bind defect, at its bluntest: this raised before the fix.

    `AmbiguousParameterError` is not a wrong answer, it is a hard failure at
    Parse — so the claim returned nothing on Postgres for every call it has ever
    made, and the verification worker had no work to do.
    """
    from db.audit_evidence import claim_next_pending_verification
    from db.database import database

    await _pending(ANY_ID, verifier_id="ucp_probe")

    claimed = await claim_next_pending_verification(worker_id=f"w-{RUN}")

    assert claimed is not None, "the claim returned nothing — no row was locked"
    assert claimed["verify_id"] == ANY_ID

    # The claim's own effect, which "it returned a row" does not reach: the row
    # must actually be marked, and marked for THIS worker. Without these the
    # statement could RETURN the row while writing the wrong status.
    row = await database.fetch_one(
        "SELECT status, claimed_by_worker FROM verification_runs WHERE verify_id = :v",
        {"v": ANY_ID},
    )
    assert row["status"] == "claimed"
    assert row["claimed_by_worker"] == f"w-{RUN}"

    # ...and exactly one row was taken, not every pending row.
    still_pending = await database.fetch_one(
        "SELECT COUNT(*) AS c FROM verification_runs "
        "WHERE merchant_id = :m AND status = 'pending'",
        {"m": AGENT},
    )
    assert still_pending["c"] == 0, "more than the one seeded row existed"


async def test_a_verifier_id_filters_the_claim_to_that_verifier(evidence_db):
    """The conjunct the CAST is wrapped around, pinned by a row it must NOT take.

    Asserting only that the matching row IS claimed is true with or without the
    filter, which is the trap: it passes if the conjunct is deleted outright.
    """
    from db.audit_evidence import claim_next_pending_verification

    await _pending(OTHER_ID, verifier_id="commerce_checkout_probe")

    claimed = await claim_next_pending_verification(
        worker_id=f"w-{RUN}", verifier_id="a_verifier_that_matches_nothing"
    )
    assert claimed is None, f"claimed a row belonging to another verifier: {claimed}"

    # ...and the same call with the right verifier does take it, so the filter is
    # selective rather than simply broken.
    claimed = await claim_next_pending_verification(
        worker_id=f"w-{RUN}", verifier_id="commerce_checkout_probe"
    )
    assert claimed is not None
    assert claimed["verify_id"] == OTHER_ID


async def test_a_null_verifier_id_still_claims_anything(evidence_db):
    """The other arm of the same OR — `CAST(:verifier_id AS text) IS NULL`.

    This is the arm a careless cast breaks, and the default every caller uses.
    """
    from db.audit_evidence import claim_next_pending_verification

    await _pending(NULL_ID, verifier_id="ucp_probe")

    claimed = await claim_next_pending_verification(
        worker_id=f"w-{RUN}", verifier_id=None
    )
    assert claimed is not None
    assert claimed["verify_id"] == NULL_ID


# ---------------------------------------------------------------------------
# db/beneficiary_repo.py — one bind, two disagreeing types
# ---------------------------------------------------------------------------
@pytest.fixture
async def beneficiary_db():
    from db.database import database

    from sqlalchemy.schema import CreateIndex, CreateTable
    from sqlalchemy.dialects import postgresql

    from db.database import metadata
    import db.agents  # noqa: F401  — registers `agents` on metadata

    await database.connect()
    # agent_beneficiaries FKs to agents(agent_id), so the parent has to exist
    # first. Both come from the repo's own definitions — the model for `agents`,
    # migration 020 for the child — rather than being restated here.
    #
    # KNOWN FIDELITY GAP, stated rather than left to be discovered: migration 020
    # also carries a `CREATE FUNCTION ... $$ ... $$` and a `DO $$ ... $$` block,
    # and splitting the file on `;` shreds both, so the `trg_bene_updated_at`
    # trigger production has does not exist here. Nothing below depends on it —
    # the tests assert `updated_at` only through the statement's own
    # `updated_at = NOW()`, never through the trigger. The sibling
    # test_pdp_governance_now_expr_postgres.py splits its migration for the same
    # reason and says explicitly that ITS file has no dollar-quoted body; this
    # one does.
    agents_table = metadata.tables["agents"]
    # The table AND its declared indexes. Both are needed and the second is easy
    # to miss: `agent_id` is `unique=True, index=True`, which SQLAlchemy emits as
    # a separate CREATE UNIQUE INDEX rather than inline — so CreateTable alone
    # leaves agent_id non-unique and migration 020's foreign key is rejected with
    # "there is no unique constraint matching given keys".
    for statement in [CreateTable(agents_table)] + [
        CreateIndex(index) for index in agents_table.indexes
    ]:
        try:
            await database.execute(str(statement.compile(dialect=postgresql.dialect())))
        except Exception:
            pass
    for path in ("db/migrations/020_agent_beneficiaries.sql",):
        for statement in (REPO_ROOT / path).read_text(encoding="utf-8").split(";"):
            if statement.strip():
                try:
                    await database.execute(statement)
                except Exception:
                    pass
    have = await database.fetch_one(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_name = 'agent_beneficiaries'"
    )
    # NOT pytest.skip. postgres-dialect-gate.yml fails the whole job on ANY skip,
    # with the message "a skip here means this job is green while testing
    # nothing" — which would be a flatly wrong diagnosis for a fixture that could
    # not build its table. Fail loudly and accurately instead.
    assert have["c"], (
        "agent_beneficiaries could not be built from db/migrations/"
        "020_agent_beneficiaries.sql — its foreign key needs a UNIQUE agents"
        ".agent_id, which CreateTable alone does not emit. This is a fixture "
        "failure, not a reason to skip."
    )
    try:
        yield database
    finally:
        await database.execute(
            "DELETE FROM agent_beneficiaries WHERE agent_id = :a", {"a": AGENT}
        )
        # ...and the parent row this fixture inserted, which an earlier version
        # left behind to accumulate in any long-lived shared database.
        await database.execute(
            "DELETE FROM agents WHERE agent_id = :a", {"a": AGENT}
        )
        await database.disconnect()


async def _beneficiary() -> int:
    from db.database import database

    from db.agents import agents

    # Only the NOT NULL columns the model declares without a default.
    try:
        await database.execute(
            agents.insert().values(
                agent_id=AGENT,
                agent_name="bind-type gate fixture",
                agent_type="custom",
                api_key=f"bindt_key_{RUN}",
                api_key_hash=f"bindt_hash_{RUN}",
            )
        )
    except Exception:
        pass  # already inserted by an earlier test in this module
    row = await database.fetch_one(
        """
        INSERT INTO agent_beneficiaries (agent_id, method, currency, verify_status)
        VALUES (:agent_id, 'bank_wire', 'USD', 'pending')
        RETURNING id
        """,
        {"agent_id": AGENT},
    )
    return int(row["id"])


async def test_verify_marks_the_row_and_stamps_verified_at(beneficiary_db):
    """The statement ran at all — and the CASE still fires for 'verified'."""
    from db.beneficiary_repo import BeneficiaryRepo
    from db.database import database

    beneficiary_id = await _beneficiary()

    # NOTE: BeneficiaryRepo.verify returns a bare `True` without checking
    # rowcount, so its return value is an assertion that cannot fail — true even
    # with the whole WHERE clause deleted. Called for effect; the row below is
    # the actual guard.
    await BeneficiaryRepo().verify(AGENT, beneficiary_id, status="verified")

    row = await database.fetch_one(
        "SELECT verify_status, verified_at FROM agent_beneficiaries WHERE id = :i",
        {"i": beneficiary_id},
    )
    assert row["verify_status"] == "verified"
    assert row["verified_at"] is not None, "the CASE did not stamp verified_at"


async def test_a_non_verified_status_leaves_verified_at_alone(beneficiary_db):
    """The ELSE arm, which the test above cannot reach.

    Without this, a CASE rewritten to stamp NOW() unconditionally would pass.
    """
    from db.beneficiary_repo import BeneficiaryRepo
    from db.database import database

    beneficiary_id = await _beneficiary()

    # 'failed', not 'rejected': migration 020 constrains this column to
    # ('unverified', 'pending', 'verified', 'failed').
    await BeneficiaryRepo().verify(AGENT, beneficiary_id, status="failed")

    row = await database.fetch_one(
        "SELECT verify_status, verified_at FROM agent_beneficiaries WHERE id = :i",
        {"i": beneficiary_id},
    )
    assert row["verify_status"] == "failed"
    assert row["verified_at"] is None, "verified_at was stamped for a non-verified status"


async def test_verify_is_scoped_to_the_owning_agent(beneficiary_db):
    """`WHERE id = :id AND agent_id = :aid` — the security conjunct.

    Pinned by a call that must change nothing, because asserting the owner's own
    call succeeds is true with or without the agent_id check.
    """
    from db.beneficiary_repo import BeneficiaryRepo
    from db.database import database

    beneficiary_id = await _beneficiary()

    await BeneficiaryRepo().verify("some_other_agent", beneficiary_id, status="verified")

    row = await database.fetch_one(
        "SELECT verify_status FROM agent_beneficiaries WHERE id = :i",
        {"i": beneficiary_id},
    )
    assert row["verify_status"] == "pending", "another agent verified this beneficiary"
