"""A DB transaction must not span a network call.

WHAT THIS IS, AND WHAT IT IS NOT. It is not the explanation for the 2026-08-18
backend wedge. An earlier version of this file claimed to be, and that claim was
disproven by execution against the pinned library — the correction is recorded
below because a wrong root cause in a gate file outlives the person who wrote
it. **That outage is still unexplained.**

WHAT IS ACTUALLY WRONG with a network call inside `async with
database.transaction():`, verified on `databases==0.7.0` (the prod pin):

1. It holds a pooled connection AND an open Postgres transaction for the full
   duration of the call. `services/catalog_sync_service.ingest_standard_products`
   opened one PER PRODUCT and awaited an LLM classification inside it, on a path
   reachable from `routes/universal_product_sync.py` — so one HTTP request could
   pin N connections in series, each for an LLM round-trip, holding row locks
   the whole time.
2. Worse, and less obvious: when the `Connection` IS inherited (0.7.0 parks it
   in a `ContextVar`, so any task spawned from a context that already touched
   the DB gets the same object), a sibling task's queries silently JOIN the
   holder's open transaction. Measured: while a holder sat inside a transaction
   "calling the network", a sibling's `SELECT count(*)` returned the holder's
   UNCOMMITTED row. If the holder then rolls back, the sibling's writes go with
   it, and nothing raises.

WHAT WAS CLAIMED AND IS FALSE, so nobody re-derives it: that the holder
SERIALIZES its siblings behind `Connection`'s `asyncio.Lock`s. It does not.
`_query_lock` is taken per query, not for the block, and `Transaction.start()`
releases `_transaction_lock` before `__aenter__` returns. Measured: a sibling
query completed in 0.000s while the holder slept 1.0s inside its transaction.
The outage evidence argues the same way — a task parked inside a transaction
shows up as `idle in transaction` in `pg_stat_activity`, and the outage snapshot
had 23 connections, all plain `idle`, zero in transaction.

WHAT THIS GATE DOES. It walks `async with database.transaction()` /
`database.connection()` blocks in the serving tree and fails when one contains an
`await` that reaches a network client — directly, via `asyncio.to_thread` /
`run_in_executor`, or through one level of same-module helper. It is static, so
it misses plenty: a transaction bound to a variable or opened by decorator, a
`gather()` of network coroutines, an `async for` over a stream, an aliased
import, or a helper more than one level deep. None of those shapes exist in the
tree today; the check catches the ones that do and catches them at review time.

Fix a finding by HOISTING the call above the `async with` — not by widening
NETWORK_MARKERS or raising BASELINE.
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
    "stripe_client",
    "asyncio.to_thread",
    "run_in_executor",
    "classify_via_llm",
    "_with_llm_fallback",
    "urlopen",
)

# ---------------------------------------------------------------------------
# THE RATCHET — measured 2026-08-18, and it is DEBT, not a standard.
# ---------------------------------------------------------------------------
# 7 awaits across 2 files hold a DB transaction open across a Stripe round-trip.
# Both are in the monetization lane and neither is live today (see BASELINE),
# so this is debt with no current exposure — not an emergency, and not a
# standard either: the held Connection is shared across tasks on the 0.7.0
# pin, so if either lane is promoted it can wedge the process.
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
# getting worse, fails. Keep the counts HONEST: see _callee_texts for why an
# over-count is worse than useless.
BASELINE = {
    # Monetization lane only. Neither is reachable in production today: the T7
    # billing cron is registered PAUSED (next_run_time=None) in
    # services/audit_scheduler.py, and create_overage_invoice has no production
    # caller. "Paused" is one authenticated admin resume away, though
    # (routes/admin_scheduler_jobs.py exposes it), so this is debt with a short
    # fuse, not debt that can be forgotten.
    #
    # Each is a money path where the transaction exists to keep the DB row and
    # the Stripe object consistent, so each needs its own change — Stripe object
    # first, then a SHORT transaction for the write, with an idempotency key and
    # a reconciliation path for the window between. That is why they are counted
    # here rather than rewritten inside an availability fix.
    "services/billing/credit_overage_billing.py": 2,
    "services/invoice_generation_service.py": 4,
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


def _callee_texts(node: ast.Await) -> List[str]:
    """What is being CALLED — never the argument text.

    Matching the whole `ast.unparse(await_node)` is the trap this function
    exists to avoid: an `await db.execute("... stripe_customer_id ...")` renders
    its SQL into the string, so a substring test for "stripe" flags a plain
    database write as a network call. The first version of this gate did exactly
    that and reported 24 violations across 9 files — including the Stripe
    webhook handler, refunds and PSP connect — when the true number was 7 in 2.
    Inflated counts are not a harmless overestimate here: they hand each file
    headroom a REAL violation can then occupy without failing the ratchet.

    For `asyncio.to_thread(fn, ...)` / `run_in_executor(None, fn, ...)` the
    callee is the wrapper, so the arguments are inspected too — that is where
    the blocking Stripe SDK call actually is.
    """
    out: List[str] = []
    value = node.value
    if isinstance(value, ast.Call):
        rendered = ast.unparse(value.func)
        out.append(rendered)
        if "to_thread" in rendered or "run_in_executor" in rendered:
            out.extend(ast.unparse(arg) for arg in value.args)
    else:
        out.append(ast.unparse(value))
    return out


def _local_async_defs(tree: ast.AST) -> dict:
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _leaf_marker(texts: List[str]) -> str:
    for marker in NETWORK_MARKERS:
        if any(marker in t for t in texts):
            return marker
    return ""


def _marker_via_local_helper(texts: List[str], defs: dict) -> str:
    """Resolve ONE level through a same-module helper.

    Name matching alone cannot separate `_create_stripe_invoice` (which does
    `asyncio.to_thread(stripe_client.v1...)`) from
    `_stripe_customer_id_for_merchant` (two SELECTs, no Stripe API) — both carry
    "stripe" in the name. So markers match real client symbols only, and a bare
    local callee is resolved by looking at what ITS body calls. One level is
    enough for the shapes present and keeps this a static check.
    """
    for text in texts:
        fn = defs.get(text)
        if fn is None:
            continue
        for inner in ast.walk(fn):
            if isinstance(inner, ast.Await):
                found = _leaf_marker(_callee_texts(inner))
                if found:
                    return found
    return ""


def _findings() -> List[Tuple[str, str, str, int, str]]:
    out: List[Tuple[str, str, str, int, str]] = []
    for path in _iter_python_files():
        try:
            source = path.read_text()
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        defs = _local_async_defs(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            ctx = " ".join(ast.unparse(item.context_expr) for item in node.items)
            if "transaction()" not in ctx and "connection()" not in ctx:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Await):
                    continue
                texts = _callee_texts(inner)
                marker = _leaf_marker(texts) or _marker_via_local_helper(texts, defs)
                if marker:
                    out.append(
                        (rel, _enclosing_function(tree, inner.lineno), marker,
                         inner.lineno, texts[0][:100])
                    )
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
        "A transaction that spans a network call holds a pooled connection and an "
        "open Postgres transaction — with its row locks — for the whole call, "
        "and on the databases==0.7.0 pin a sibling task that inherited the same "
        "Connection will silently JOIN that transaction and lose its writes if "
        "the holder rolls back.\n\n"
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
    """Drive `_callee_texts` ITSELF, and pin the false positive it exists for.

    The first version of this test re-implemented detection inline with
    `any(m in ast.unparse(node))` — the very matching the detector had just
    stopped using — so it passed even with `_callee_texts` stubbed to return [].
    A self-test that does not call the thing it certifies is decoration.
    """
    def scan(src: str) -> List[str]:
        tree = ast.parse(src)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            ctx = " ".join(ast.unparse(i.context_expr) for i in node.items)
            if "transaction()" not in ctx:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Await) and any(
                    m in t for t in _callee_texts(inner) for m in NETWORK_MARKERS
                ):
                    found.append(ast.unparse(inner))
        return found

    wrapped = (
        "async def f():\n"
        "    async with database.transaction():\n"
        "        await asyncio.to_thread(stripe_client.v1.invoices.create)\n"
    )
    direct = (
        "async def f():\n"
        "    async with database.transaction():\n"
        "        await httpx_client.post(url)\n"
    )
    # The shape that produced 24 phantom findings: a DB write whose SQL merely
    # NAMES a stripe column. Must stay ignored.
    sql_only = (
        "async def f():\n"
        "    async with database.transaction():\n"
        "        await db.execute('UPDATE m SET stripe_customer_id = :c', v)\n"
    )
    assert scan(wrapped), "missed a Stripe call handed to asyncio.to_thread"
    assert scan(direct), "missed a direct httpx call"
    assert not scan(sql_only), "false positive: flagged a plain DB write"


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
