"""Tripwire: `databases`.execute() returns NOTHING useful for a write, so a caller that CONSUMES
its return value is reading a number that does not exist.

`databases` over asyncpg gives no rowcount for UPDATE/DELETE/INSERT — `execute()` resolves to None
(or the last inserted id, never a count). Raw asyncpg's own `execute` DOES return a status string
like "UPDATE 3", which is why this only flags the shared `database` instance. A hit is a place where a declined
write is indistinguishable from a landed one, and where a report can say "deleted 12" because
someone assigned `deleted_2pct` and believed it.

RETURNING is excluded ONLY when the value is used as a value. `databases` implements execute() as
fetchval — the FIRST COLUMN of the FIRST ROW — so `INSERT ... RETURNING id` genuinely resolves to
the id and is the fix this tripwire recommends. But `DELETE ... RETURNING 1` accumulated into a
total resolves to the literal 1 forever, which is the SAME defect wearing the recommended fix's
clothes. The first version of this file excluded RETURNING wholesale and so certified
routes/products_cache_maintenance.py — which reported at most 2 duplicates removed however many it
deleted — as correct. Review caught it; both sites are fixed (fetch_all + len) in the same commit.

That class shipped twice in one day on 2026-09-09: the relationship-graph backfill reported
`declined_by_guard: 0` unconditionally because the call site discarded the landed flag, and the
review before that had to point out `matched` was counting intentions rather than changes.

THE FIX DEPENDS ON WHAT YOU WANTED, and getting this wrong is how the products_cache bug happened:

  * a LANDED FLAG ("did anything change?") -> `RETURNING <col>` + `fetch_val`/`fetch_one`, then test
    the result for None.
  * a ROW COUNT ("how many changed?") -> `RETURNING 1` + `fetch_all`, then `len(rows)`; or
    `WITH d AS (DELETE ... RETURNING 1) SELECT count(*) FROM d` + `fetch_val`.

`fetch_val` on a count is the SAME defect this file catches: it returns the first column of the
first row, so it answers 1 for any non-empty result. An earlier version of this docstring
recommended it for both cases.

This is a RATCHET, not a clean-up mandate. 47 sites existed when it was written; the watermark
stops the 48th. It is asserted EXACTLY, in both directions: raising the number is as much a change
as adding a call site, and an earlier version that allowed a band of 5 would have let the watermark
itself drift from 47 to 52 with every test still green.
"""

from __future__ import annotations

import ast
import functools
import pathlib
import re
import warnings

# MEASURED, not estimated: 47 on 2026-09-09, after excluding RETURNING sites whose value is not
# consumed as a number (see _used_arithmetically).
# Two of the 47 live in tests/test_wallet_admin_missing_row_postgres.py, which consumes the value
# deliberately to PIN this behaviour — left in the count rather than special-cased, because an
# exclusion list that grows is how a ratchet stops meaning anything.
# LOWER THIS when you convert a site; never raise it.
WATERMARK = 47

_SKIP_PARTS = {".claude", "node_modules", ".venv", "__pycache__", ".git", "build", "dist"}
_WRITE_SQL = re.compile(r"\b(UPDATE|DELETE|INSERT)\b", re.IGNORECASE)
# `databases` implements execute() as fetchval on the asyncpg backend, so a statement with
# RETURNING DOES resolve to the returned value — but to ONE value, not a count. See
# _used_arithmetically for why that is an exclusion and not a blanket pass.
_RETURNING_SQL = re.compile(r"\bRETURNING\b", re.IGNORECASE)
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _sql_text(call: ast.Call) -> str:
    """Best-effort literal SQL from the call's arguments (constants and f-string literals)."""
    parts = []
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            parts.append(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            parts.extend(v.value for v in arg.values if isinstance(v, ast.Constant))
    return " ".join(parts)


def _is_shared_database_execute(call: ast.Call) -> bool:
    """`database.execute(...)` on the shared `databases` instance — NOT a raw asyncpg connection,
    whose execute() genuinely returns a status string."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
        return False
    receiver = func.value
    name = (
        receiver.id
        if isinstance(receiver, ast.Name)
        else receiver.attr
        if isinstance(receiver, ast.Attribute)
        else ""
    )
    return name == "database"


def _enclosing_scope_map(tree):
    """node -> innermost enclosing function (or the module).

    Walking functions AND the module separately finds each in-function assignment twice, and the
    module pass then searches the WHOLE FILE for later references — so a `result` in an unrelated
    function downstream counts as a use. Precision matters here: an imprecise scanner produces a
    watermark that is mostly noise, and a noisy ratchet gets raised instead of lowered.
    """
    scope_of = {}

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            scope_of[child] = inner
            walk(child, inner)

    scope_of[tree] = tree
    walk(tree, tree)
    return scope_of


def _used_arithmetically(name, scope, after_line):
    """Is `name` ACCUMULATED — added, subtracted, summed?

    Deliberately narrow. It catches the accumulator shape (`total += x`, `a + x`, `sum([...])`) and
    NOT every numeric read: `return {"deleted": x}` and `return f"deleted {x}"` are the same defect
    and are missed, while `if x > 0` and `1 if x else 0` are legitimate landed-flag reads of
    fetchval that must not be flagged. Widening this to "used as a number" would need a way to tell
    those two apart, which the AST alone does not give. A ratchet over a known shape, not a proof.

    This is what separates a correct `RETURNING id` from the defect. `databases.execute()` is
    `fetchval`: the FIRST COLUMN of the FIRST ROW. Asking for an id back and using it as an id is
    right. Asking for `RETURNING 1` and ADDING it to a running total is the same defect as reading a
    rowcount that does not exist — the answer is the literal 1, so the accumulator reports the number
    of STATEMENTS, not the number of rows. routes/products_cache_maintenance.py did exactly that and
    the first version of this file pinned it as correct.
    """
    for node in ast.walk(scope):
        reads = None
        if isinstance(node, ast.AugAssign) and isinstance(node.op, (ast.Add, ast.Sub)):
            reads = node.value
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            reads = node
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sum"
        ):
            reads = node
        if reads is None or getattr(node, "lineno", 0) <= after_line:
            continue
        for ref in ast.walk(reads):
            if isinstance(ref, ast.Name) and ref.id == name and isinstance(ref.ctx, ast.Load):
                return True
    return False


def _violations_in_source(source, path):
    """The whole rule, over one module's text. Separated so tests can probe it with fixtures
    instead of depending on whichever real call sites happen to exist today."""
    found = []
    try:
        with warnings.catch_warnings():
            # Some repo files carry invalid escape sequences; parsing them is not this test's
            # business to report, and the warning noise hides real output.
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return found
    scope_of = _enclosing_scope_map(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Await) and isinstance(value.value, ast.Call)):
            continue
        call = value.value
        if not _is_shared_database_execute(call):
            continue
        sql = _sql_text(call)
        if not _WRITE_SQL.search(sql):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not targets:
            continue
        name = targets[0]
        scope = scope_of.get(node, tree)
        if _RETURNING_SQL.search(sql) and not _used_arithmetically(name, scope, node.lineno):
            # RETURNING resolves through execute(), and the value is used as a value, not a count.
            continue
        # Only a CONSUMED value is a defect. An assignment nobody reads is merely misleading,
        # and flagging it would bury the real ones.
        consumed = any(
            isinstance(ref, ast.Name)
            and ref.id == name
            and isinstance(ref.ctx, ast.Load)
            and ref.lineno > node.lineno
            for ref in ast.walk(scope)
        )
        if consumed:
            found.append((str(path), node.lineno, name))
    return found


@functools.lru_cache(maxsize=1)
def _violations():
    """(path, lineno, variable) for each CONSUMED return value of a shared-database write.

    Cached: four tests call this and each call walks every .py in the repo (~5s).
    """
    found = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        found.extend(
            _violations_in_source(
                path.read_text(encoding="utf-8", errors="ignore"),
                path.relative_to(_REPO_ROOT),
            )
        )
    return tuple(sorted(set(found)))


def test_no_new_reads_of_a_nonexistent_write_rowcount():
    violations = _violations()
    assert len(violations) <= WATERMARK, (
        "%d sites consume the return of database.execute() on a write, above the watermark of %d.\n"
        "`databases` over asyncpg returns NO rowcount for UPDATE/DELETE/INSERT, so this value is "
        "not a count and a declined write looks exactly like a landed one.\n"
        "For a LANDED FLAG: `RETURNING <col>` + fetch_val/fetch_one, then test for None.\n"
        "For a ROW COUNT: `RETURNING 1` + fetch_all, then len(rows) — fetch_val answers 1 for any\n"
        "non-empty result, which is the same defect wearing the fix's clothes.\n"
        "New or moved sites:\n  %s"
        % (
            len(violations),
            WATERMARK,
            "\n  ".join("%s:%d (%s)" % v for v in violations[-8:]),
        )
    )


def test_the_watermark_is_exact():
    """EXACT, not a band. The first version allowed WATERMARK - 5, which meant the number itself
    could be raised from 47 to 52 and every test still passed — five new violations could then land
    silently. A ratchet whose own setting is unpinned is not a ratchet. When this fails because the
    count legitimately moved, change the number and say why in the commit."""
    violations = _violations()
    assert len(violations) == WATERMARK, (
        "%d sites consume the return of a shared-database write; WATERMARK says %d.\n"
        "If you FIXED some, lower it to %d. If you added some, that is the defect: `databases` over "
        "asyncpg returns NO rowcount for UPDATE/DELETE/INSERT, so a declined write looks exactly "
        "like a landed one. Use `RETURNING <col>` + fetch_val/fetch_one and test the result.\n"
        "Current sites (last 8):\n  %s"
        % (
            len(violations),
            WATERMARK,
            len(violations),
            "\n  ".join("%s:%d (%s)" % v for v in violations[-8:]),
        )
    )


def test_the_scanner_actually_detects_the_pattern():
    """The scanner is the mechanism under test; a scan that silently matched nothing would make
    both tests above pass forever. Pin that it finds real, known sites."""
    violations = _violations()
    assert violations, "scanner found nothing at all — it is broken, not the codebase clean"
    files = {v[0] for v in violations}
    assert any(f.startswith("services/claim_state.py") for f in files), files
    assert any(f.startswith("routes/") for f in files), files


_FIXTURE_CORRECT = """
async def log_it(self):
    row_id = await self.database.execute("INSERT INTO t (a) VALUES (1) RETURNING id", {})
    return row_id
"""

_FIXTURE_ARITHMETIC = """
async def compact(self):
    deleted = await self.database.execute("DELETE FROM t WHERE x RETURNING 1", {})
    removed = 0
    removed += int(deleted or 0)
    return removed
"""

_FIXTURE_NO_RETURNING = """
async def touch(self):
    n = await self.database.execute("UPDATE t SET a = 1", {})
    return n
"""


def test_a_returning_value_used_as_a_value_is_not_flagged():
    """`databases` implements execute() as fetchval, so `RETURNING id` genuinely does resolve to the
    id — that is the FIX this tripwire recommends, and flagging it would make the ratchet noise."""
    assert _violations_in_source(_FIXTURE_CORRECT, "f.py") == []


def test_a_returning_value_ADDED_TO_A_TOTAL_is_flagged():
    """The control, and a real defect this file used to certify as correct.

    fetchval returns the first column of the first row, so `RETURNING 1` is the literal 1 forever.
    Accumulating it counts STATEMENTS, not rows. routes/products_cache_maintenance.py reported at
    most 2 duplicates removed however many it deleted; the previous version of this test asserted
    those two lines "must not be flagged". Both are fixed (fetch_all + len) in the same commit."""
    hits = _violations_in_source(_FIXTURE_ARITHMETIC, "f.py")
    assert [h[2] for h in hits] == ["deleted"], hits


def test_a_write_with_no_returning_at_all_is_flagged():
    """The other control: without it, a scanner that flagged nothing would pass both tests above."""
    assert [h[2] for h in _violations_in_source(_FIXTURE_NO_RETURNING, "f.py")] == ["n"]


def test_the_real_returning_call_sites_stay_unflagged():
    """The two surviving in-repo RETURNING consumers, both `INSERT ... RETURNING id` used as an id.
    (The two `RETURNING 1` sites in products_cache_maintenance.py no longer call execute() at all.)"""
    violations = {(str(v[0]), v[1]) for v in _violations()}
    for path, line in (
        ("services/agent_integration_bridge.py", 86),
        ("services/payment_routing_service.py", 1377),
    ):
        assert not any(p == path and abs(l - line) <= 4 for p, l in violations), (
            "%s:%d returns an id and uses it as an id; it must not be flagged" % (path, line)
        )
