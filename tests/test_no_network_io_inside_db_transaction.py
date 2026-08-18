"""A DB transaction must not span a network call.

THE INCIDENT THIS PREVENTS (2026-08-18). The backend wedged: `/health` 503 with
`db_ok:false`, every `/api/canonical/products` request 504 in a flat 4.0s, live
PDPs 404, and the scheduler log full of `maximum number of running instances
reached (1)`. It took a redeploy to clear, and it was the second time.

The database was innocent, and that is the part worth remembering. Postgres
showed **23 connections, every one IDLE, zero active queries, zero
idle-in-transaction**, and the exact COUNT the route was "timing out" on
returned in **0.03s** over a direct asyncpg connection from inside the same
container. Nothing was waiting on the database, and nothing was waiting for a
free pool slot either.

WHY A NETWORK CALL INSIDE A TRANSACTION DOES THIS. The prod pin is
`databases==0.7.0`, which parks ONE `Connection` object in a `ContextVar` that
every task in the context inherits (`databases/core.py`), and that object owns
an `asyncio.Lock` (`self._query_lock`, `self._connection_lock`). So a task that
holds the connection across non-DB I/O does not merely occupy one of the 20 pool
slots — it SERIALIZES every sibling task behind an in-process lock. Pool size is
irrelevant (the connections were idle); an acquire timeout would not have helped
either (nobody was blocked in `acquire`), and in any case `databases` calls
`pool.acquire()` with no timeout at all, so exhaustion queues forever rather
than failing fast. The only thing that actually bounds the damage is not holding
the connection across the call.

WHAT THIS GATE DOES. It walks the `async with database.transaction()` /
`database.connection()` blocks in the serving tree and fails when one contains an
`await` that looks like network or thread-hop I/O. It is a static check, so it
cannot see everything — a network call behind an indirection it cannot name will
pass. That is accepted: it catches the shape that actually bit us twice, and it
catches it at review time instead of at 504 time.

Fix the finding by HOISTING the call above the `async with`, not by widening the
allowlist. The 2026-08-18 fix did exactly that in
`services/catalog_sync_service.ingest_standard_products`, where an LLM category
classification sat inside a per-product transaction on a path reachable from
`routes/universal_product_sync.py` — i.e. one HTTP request could open N
transactions, each spanning an LLM round-trip.
"""

from __future__ import annotations

import ast
from pathlib import Path
from collections import Counter
from typing import Iterator, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# Trees whose code runs inside the long-lived web/scheduler process. Ops CLIs
# under scripts/ are deliberately out of scope: they are single-shot, they do
# not share a context with request handlers, and one of them
# (`retailer_ingest/single_writer_lock`) pins a connection ON PURPOSE to hold a
# session-scoped advisory lock.
SCANNED_TREES = ("services", "routes", "jobs", "db")

# Substrings that mark an await as network or thread-hop I/O. Deliberately
# narrow — matching on bare `.get(`/`.post(` would flag dict and DB access.
NETWORK_MARKERS = (
    "httpx",
    "aiohttp",
    "requests.",
    "openai",
    "anthropic",
    "stripe",
    "asyncio.to_thread",
    "run_in_executor",
    "classify_via_llm",
    "_with_llm_fallback",
    "urlopen",
)

# ---------------------------------------------------------------------------
# THE RATCHET — measured 2026-08-18, and it is DEBT, not a standard.
# ---------------------------------------------------------------------------
# 24 sites across 9 files hold a DB transaction open across a Stripe round-trip,
# several of them on live request paths: the Stripe webhook handler
# (`routes/billing_routes._handle_checkout_session_completed`), PSP connect on
# three surfaces, refunds, and settlement transfers. Any one of them can wedge
# the process the way 2026-08-18 wedged it, because the held Connection is
# shared across tasks on the 0.7.0 pin.
#
# They are NOT fixed here on purpose. Every one is a money path where the
# transaction exists to keep the DB row and the Stripe object consistent, and
# rewriting refund/settlement/webhook semantics as a drive-by inside an
# availability fix is how you turn one outage into two. Each needs its own
# change: create the Stripe object first, then take a SHORT transaction for the
# DB write, with an idempotency key and a reconciliation path for the window in
# between.
#
# So this is a ratchet, per FILE so that fixing one site cannot silently pay for
# a new one somewhere else. The numbers may only go DOWN. A new file, or a file
# getting worse, fails.
BASELINE = {
    "routes/admin_api.py": 1,
    "routes/admin_partners.py": 1,
    "routes/billing_routes.py": 3,
    "routes/manage_integrations.py": 1,
    "routes/merchant_api_extensions.py": 1,
    "services/billing/credit_overage_billing.py": 5,
    "services/invoice_generation_service.py": 7,
    "services/refund_service.py": 1,
    "services/settlement_file_service.py": 4,
}


def _iter_python_files() -> Iterator[Path]:
    for tree in SCANNED_TREES:
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "test" in path.name:
                continue
            yield path


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """Innermost def/async def containing `lineno` — a stabler label than a
    line number when reporting a finding."""
    best = "<module>"
    best_line = -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and node.lineno > best_line:
                best, best_line = node.name, node.lineno
    return best


def _findings() -> List[Tuple[str, str, str, int, str]]:
    out: List[Tuple[str, str, str, int, str]] = []
    for path in _iter_python_files():
        try:
            source = path.read_text()
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            ctx = " ".join(ast.unparse(item.context_expr) for item in node.items)
            if "transaction()" not in ctx and "connection()" not in ctx:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Await):
                    continue
                rendered = ast.unparse(inner)
                for marker in NETWORK_MARKERS:
                    if marker in rendered:
                        out.append(
                            (rel, _enclosing_function(tree, inner.lineno), marker,
                             inner.lineno, rendered[:120])
                        )
                        break
    return out


def _counts_by_file() -> Counter:
    return Counter(f[0] for f in _findings())


def test_no_new_network_io_inside_a_db_transaction() -> None:
    counts = _counts_by_file()
    regressions = []
    for path, n in sorted(counts.items()):
        allowed = BASELINE.get(path, 0)
        if n > allowed:
            where = [f for f in _findings() if f[0] == path]
            detail = "\n".join(
                f"      L{line} in {func}() — {marker!r}: {src}"
                for _p, func, marker, line, src in where
            )
            regressions.append(
                f"  {path}: {n} > {allowed} allowed\n{detail}"
            )
    assert not regressions, (
        "A database transaction now spans a network call in a place it did not "
        "before.\n\n"
        "On the `databases==0.7.0` prod pin the Connection is shared across "
        "tasks via a ContextVar and guarded by an asyncio.Lock, so holding it "
        "across network I/O serializes every sibling task and wedges the "
        "process — 2026-08-18: 23 connections all idle, zero active queries, "
        "total outage, cleared only by redeploy.\n\n"
        "HOIST the call above the `async with`. Do not raise BASELINE.\n\n"
        + "\n".join(regressions)
    )


def test_the_ratchet_only_goes_down() -> None:
    """A baseline entry that overshoots is a fix nobody banked.

    Without this, hoisting a call leaves the old headroom sitting there for the
    next regression to occupy silently.
    """
    counts = _counts_by_file()
    slack = [
        f"  {path}: baseline {allowed}, actual {counts.get(path, 0)} — lower it"
        for path, allowed in sorted(BASELINE.items())
        if counts.get(path, 0) < allowed
    ]
    assert not slack, "BASELINE has stale headroom:\n" + "\n".join(slack)


def test_the_gate_can_actually_see_a_violation() -> None:
    """The detector must fail on the real shape, or the ratchet is decoration.

    Guards the failure this repo keeps meeting: a gate that runs, asserts
    something, and would stay green through the regression it is named for.
    """
    tree = ast.parse(
        "async def f():\n"
        "    async with database.transaction():\n"
        "        await stripe_client.v1.invoices.create(params={})\n"
    )
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith):
            ctx = " ".join(ast.unparse(i.context_expr) for i in node.items)
            if "transaction()" in ctx:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Await) and any(
                        m in ast.unparse(inner) for m in NETWORK_MARKERS
                    ):
                        hits.append(ast.unparse(inner))
    assert hits, "detector failed to flag a network call inside a transaction"


def test_the_hoisted_catalog_sync_call_stays_hoisted() -> None:
    """The 2026-08-18 fix, pinned.

    `ingest_standard_products` opens a transaction PER PRODUCT and is reachable
    from `routes/universal_product_sync.py`, so one HTTP request could open N
    transactions each spanning an LLM round-trip. The classification is pure and
    reads only off `product`, so it belongs above the `async with`.
    """
    src = (REPO_ROOT / "services" / "catalog_sync_service.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith):
            ctx = " ".join(ast.unparse(i.context_expr) for i in node.items)
            if "transaction()" not in ctx:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Await) and "fold_category" in ast.unparse(inner):
                    raise AssertionError(
                        "fold_category_with_llm_fallback is back inside a "
                        f"transaction (line {inner.lineno}) — it makes an LLM "
                        "call and will serialize every sibling task behind the "
                        "shared Connection."
                    )
