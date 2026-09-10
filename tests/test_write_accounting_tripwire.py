"""Tripwire: `databases`.execute() returns NOTHING useful for a write, so a caller that CONSUMES
its return value is reading a number that does not exist.

`databases` over asyncpg gives no rowcount for UPDATE/DELETE/INSERT — `execute()` resolves to None
(or the last inserted id, never a count). Raw asyncpg's own `execute` DOES return a status string
like "UPDATE 3", which is why this only flags the shared `database` instance. Every hit is a place
where a declined write is indistinguishable from a landed one, and where a report says "deleted 12"
because someone assigned `deleted_2pct` and believed it.

That class shipped twice in one day on 2026-09-09: the relationship-graph backfill reported
`declined_by_guard: 0` unconditionally because the call site discarded the landed flag, and the
review before that had to point out `matched` was counting intentions rather than changes.

THE FIX at a call site is `RETURNING <col>` + `fetch_val`/`fetch_one`, then test the result.

This is a RATCHET, not a clean-up mandate. 51 sites existed when it was written; the watermark
stops the 52nd. Lower it whenever you convert one — the test tells you when it is stale, because a
watermark that drifts above reality silently stops protecting anything.
"""

from __future__ import annotations

import ast
import pathlib
import re

# Measured 2026-09-09 across the repo. LOWER THIS when you convert a site; never raise it.
WATERMARK = 51

_SKIP_PARTS = {".claude", "node_modules", ".venv", "__pycache__", ".git", "build", "dist"}
_WRITE_SQL = re.compile(r"\b(UPDATE|DELETE|INSERT)\b", re.IGNORECASE)
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
            scope_of[child] = inner if inner is not scope else scope
            walk(child, inner)

    scope_of[tree] = tree
    walk(tree, tree)
    return scope_of


def _violations():
    """(path, lineno, variable) for each CONSUMED return value of a shared-database write."""
    found = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
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
            if not _WRITE_SQL.search(_sql_text(call)):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not targets:
                continue
            name = targets[0]
            scope = scope_of.get(node, tree)
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
                found.append((str(path.relative_to(_REPO_ROOT)), node.lineno, name))
    return sorted(set(found))


def test_no_new_reads_of_a_nonexistent_write_rowcount():
    violations = _violations()
    assert len(violations) <= WATERMARK, (
        "%d sites consume the return of database.execute() on a write, above the watermark of %d.\n"
        "`databases` over asyncpg returns NO rowcount for UPDATE/DELETE/INSERT, so this value is "
        "not a count and a declined write looks exactly like a landed one.\n"
        "Use `RETURNING <col>` + fetch_val/fetch_one and test the result.\n"
        "New or moved sites:\n  %s"
        % (
            len(violations),
            WATERMARK,
            "\n  ".join("%s:%d (%s)" % v for v in violations[-8:]),
        )
    )


def test_the_watermark_is_not_stale():
    """A ratchet that drifts above reality silently stops protecting anything: once the watermark
    exceeds the real count, several new violations can land before it trips."""
    violations = _violations()
    assert len(violations) >= WATERMARK - 5, (
        "Only %d violations remain but WATERMARK is %d — lower it to %d so the ratchet keeps "
        "biting. (Slack of 5 so ordinary refactors do not fail the build.)"
        % (len(violations), WATERMARK, len(violations))
    )


def test_the_scanner_actually_detects_the_pattern():
    """The scanner is the mechanism under test; a scan that silently matched nothing would make
    both tests above pass forever. Pin that it finds real, known sites."""
    violations = _violations()
    assert violations, "scanner found nothing at all — it is broken, not the codebase clean"
    files = {v[0] for v in violations}
    assert any(f.startswith("services/claim_state.py") for f in files), files
    assert any(f.startswith("routes/") for f in files), files
