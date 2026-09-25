"""WP4c — revoke on repoint, SQLite arm.

Every case lives in tests/reap_repoint_cases.py and is collected here AND in
tests/test_reap_buyer_link_repoint_postgres.py, so the two dialects run the SAME functions and
cannot drift. What is added HERE is the pair of checks that are about the PROCESS rather than
about the engine — the import graph of `routes/buyer_api`, and the absence of an outbound Reap
call from the hook — both of which need a fresh interpreter and therefore belong in exactly one
arm.

NEVER `TestClient`, and no `main`: the function under test is module-level and is called
directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.database import IS_POSTGRES  # noqa: E402

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason=(
        "the SQLite arm of the WP4c repoint hook; the Postgres arm is "
        "tests/test_reap_buyer_link_repoint_postgres.py"
    ),
)

from reap_repoint_cases import *  # noqa: E402,F401,F403


def _in_a_fresh_interpreter(source: str) -> str:
    """Run `source` in a child process with this repo on the path, and hand back its stdout.

    A FRESH INTERPRETER IS THE WHOLE POINT. Both assertions below are about what is in
    `sys.modules` after one import, and this test session has already imported half the repo —
    including, from the shared cases module, the very ledger whose ABSENCE is being asserted.
    Nothing measured in this process could mean anything.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # A throwaway SQLite file: `db.database` binds its singleton at import, and the child must
    # not touch the database this session is using.
    env["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(
        Path(env.get("TMPDIR", "/tmp")) / "wp4c_import_graph_probe.db"
    )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return result.stdout


def test_importing_buyer_api_does_not_pull_in_the_reap_rail():
    """`routes/buyer_api` IS A LIVE BUYER-FACING SURFACE. The hook it gained in WP4c imports
    `db.reap_agentic_ledger` LAZILY, inside the function, and that is not a style choice: a
    module-level import would put the ledger — and through it `services.reap_webhooks` and
    `services.reap_cart_link` — in this module's import graph, so an import-time failure or a
    slow import anywhere on the Reap rail would take the buyer's checkout down with it.

    Asserted on `sys.modules` after a bare import, in a fresh interpreter, because that is the
    only place the property is observable.
    """
    out = _in_a_fresh_interpreter(
        """
        import sys
        import routes.buyer_api  # noqa: F401
        leaked = sorted(
            name for name in sys.modules
            if name == "db.reap_agentic_ledger"
            or name.startswith("services.reap_")
        )
        print("LEAKED=" + ",".join(leaked))
        """
    )
    assert "LEAKED=" in out
    leaked = out.split("LEAKED=", 1)[1].strip()
    assert leaked == "", f"routes/buyer_api pulled the Reap rail into its import graph: {leaked}"


def test_the_lazy_import_still_resolves():
    """The other half of the same statement, and the reason it is not enough to grep for the
    absence of an import. A lazy import that names a function the ledger does not export is a
    hook that silently never runs — it raises inside the swallowing try and logs a failure
    nobody reads. The name is resolved here, from a fresh interpreter, exactly as the hook
    resolves it.
    """
    out = _in_a_fresh_interpreter(
        """
        import inspect
        from db.reap_agentic_ledger import retire_buyer_refs_for_buyer
        sig = inspect.signature(retire_buyer_refs_for_buyer)
        print("SIG=" + str(sig))
        """
    )
    assert "buyer_id" in out and "reason" in out


def test_the_hook_makes_no_partner_call():
    """STATED AS A PROPERTY OF THE SOURCE, not only as the `no_network` fixture's absence of a
    failure. The fixture proves no call happened on the paths the cases exercise; this proves the
    hook contains no call site at all, including on a branch no case reaches.

    `services.reap_agentic_client.revoke_enrollment` DOES exist (Reap offers
    `POST /agentic/enrollments/{id}/revoke`), and it is deliberately not called from here: this
    code runs on the hosted checkout's save path with a human waiting, and a partner POST can
    take up to the client's 25-second timeout before it gives up.
    """
    import ast
    import inspect
    import textwrap

    import routes.buyer_api as buyer_api

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(buyer_api._retire_reap_state_on_repoint))
    )
    # THE CODE, NOT THE PROSE. The hook's docstring NAMES `revoke_enrollment` in order to say
    # why it is not called, so a substring scan over the source would fail on the explanation
    # rather than on a call. Every identifier the function actually references is collected from
    # the syntax tree, and the docstring is a `Constant` that contributes none.
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    for forbidden in ("reap_agentic_client", "httpx", "revoke_enrollment", "requests", "urllib"):
        assert forbidden not in referenced, f"the repoint hook reaches for {forbidden}"
    # And the one import it DOES make is the lazy, data-layer one.
    assert "services" not in referenced
    assert "db" in referenced


# ── the fallback arm, SQLite only ────────────────────────────────────────────────────────────
#
# `_upsert_buyer_identity_link` has a second write path for "an environment without ON CONFLICT",
# and it repoints exactly as the first one does — so the hook has to be on it too. It is
# exercised HERE and not in the shared cases because it does not work on Postgres at all: see
# tests/test_reap_buyer_link_repoint_postgres.py::test_the_fallback_arm_is_unreachable_on_postgres,
# which pins the reason (no rowcount from `databases` 0.7.0 on that backend). A base property,
# not one WP4c introduced.


async def test_the_fallback_branch_repoints_and_retires(monkeypatch, retire_calls):
    """THE OTHER WRITE PATH. `_upsert_buyer_identity_link` has two of them — the `ON CONFLICT …
    DO UPDATE` upsert, and an update-then-insert fallback for an engine without it — and BOTH
    repoint. A hook on only one is a hook that does not exist on whichever dialect takes the
    other.

    The first arm is disabled by making its statement, and only its statement, raise.
    """
    real_execute = database.execute

    async def execute(query, *args, **kwargs):
        if "ON CONFLICT (agent_id, agent_user_ref_hash)" in str(query):
            raise RuntimeError("this engine has no ON CONFLICT")
        return await real_execute(query, *args, **kwargs)

    monkeypatch.setattr(database, "execute", execute)

    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    assert await upsert(REAL_BUYER) == ref_hash()

    assert retire_calls == [(MINTED_BUYER, "buyer_link_repointed")]
    assert await link_buyer_id(agent_id=AGENT, ref_hash=ref_hash()) == REAL_BUYER
    assert dict(await enrollment_row(enrollment_id))["status"] == "dead"
    assert await buyer_ref_count(MINTED_BUYER) == 0


async def test_the_fallback_insert_arm_retires_nothing(monkeypatch, retire_calls):
    """The third arm — the INSERT reached only when the fallback UPDATE matched nothing — creates
    a link rather than moving one. There is no old buyer, so there is nothing to retire, and a
    hook there would fire on every first sign-in."""
    real_execute = database.execute

    async def execute(query, *args, **kwargs):
        if "ON CONFLICT (agent_id, agent_user_ref_hash)" in str(query):
            raise RuntimeError("this engine has no ON CONFLICT")
        return await real_execute(query, *args, **kwargs)

    monkeypatch.setattr(database, "execute", execute)

    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    assert await upsert(REAL_BUYER) == ref_hash()

    assert retire_calls == []
    assert await link_buyer_id(agent_id=AGENT, ref_hash=ref_hash()) == REAL_BUYER
    assert dict(await enrollment_row(enrollment_id))["status"] == "active"
