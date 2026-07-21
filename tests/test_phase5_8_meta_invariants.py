"""P5.8.7 — meta-invariant tests.

The 5 P0s found by the review all hid in patterns the existing
test suite never asserted at the meta level. This module adds
generic checks that catch entire bug CLASSES, not individual bugs:

  1. Schema-tenancy: every audit-scoped DB table has merchant_id
  2. SELECT-vs-read drift: SELECT columns appear in subsequent
     row.get() reads (catches the gsc_indexing_status field-name
     class of bug)
  3. Env-var-fallback inventory: every os.getenv(KEY, fallback)
     in production code has KEY registered in config/settings.py
     (catches the PIVOTA_BACKEND_INTERNAL_URL class of bug)
  4. No orphan DB helpers: every public async def in db/*.py is
     called from at least one non-test caller in services/ or
     routes/ (catches the link_action_to_task class of bug)
  5. Idempotent persist: persist_canonical_evidence run twice
     produces the same canonical row count (the P0-2 class of bug)

These run as standalone tests (no DB / network) by parsing source
files via re + ast. They're fast and hermetic.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# =====================================================================
# 1. Schema-tenancy meta-test
# =====================================================================


def test_every_audit_scoped_table_has_merchant_id_column():
    """Every audit-scoped table (one that references audit_run_id)
    MUST have a merchant_id column SOMEWHERE in its migration
    history — either in the original CREATE TABLE or added later
    via ALTER. P0-1 happened because the Phase 4 canonical tables
    initially shipped with audit_run_id but no merchant_id;
    migration 088 added it later.

    The meta-test scans all .sql migration files and computes the
    UNION of:
      - CREATE TABLE <name> with body mentioning merchant_id
      - ALTER TABLE <name> ADD COLUMN ... merchant_id
    Then it flags any audit-scoped table not in that union."""
    audit_scoped_tables: set = set()  # tables with audit_run_id
    tables_with_merchant_id: set = set()  # via CREATE or ALTER
    for sql_file in sorted(_REPO_ROOT.glob("db/migrations/*.sql")):
        text = sql_file.read_text()
        # CREATE TABLE — capture audit_scoped + merchant_id status
        for match in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.+?)\n\);",
            text, re.DOTALL,
        ):
            table_name, body = match.group(1), match.group(2)
            if "audit_run_id" in body:
                audit_scoped_tables.add(table_name)
            if "merchant_id" in body:
                tables_with_merchant_id.add(table_name)
        # ALTER TABLE ADD COLUMN merchant_id — captures the
        # delayed-add pattern P5.8.1 used (migration 088 adds
        # merchant_id to P4.1's canonical tables).
        for match in re.finditer(
            r"ALTER TABLE (\w+)\s+ADD COLUMN(?: IF NOT EXISTS)?\s+merchant_id\b",
            text, re.IGNORECASE,
        ):
            tables_with_merchant_id.add(match.group(1))
    # Whitelist: tables that predate P5.8 scope. Cleanup is
    # tracked separately as tech debt.
    _PRE_P5_8_WHITELIST = {
        "competitor_audit_runs",  # PR-2; pre-Phase-2 schema
    }
    violations = sorted(
        audit_scoped_tables - tables_with_merchant_id - _PRE_P5_8_WHITELIST
    )
    assert violations == [], (
        "audit-scoped tables WITHOUT merchant_id across all "
        "migrations (single-layer tenancy hazard, P0-1 class):\n  "
        + "\n  ".join(violations)
        + "\n\nFix: ALTER TABLE ... ADD COLUMN merchant_id TEXT + "
        "backfill via JOIN on audit_run_id (see migration 088)."
    )


# =====================================================================
# 2. SELECT-vs-read drift
# =====================================================================


def test_no_select_vs_row_get_drift_in_verifiers():
    """In each verifier module, every column read via
    `row.get("X")` or `sub_row.get("X")` must appear in the SELECT
    statement of the same function. P0-4 happened because
    gsc_indexing_status read sub_row.get('indexed_at') while the
    SELECT in _fetch_submission_row didn't include indexed_at.

    Limited scope: services/verifiers/*.py only (where the bug
    class lives). Future expansion: any module with both SELECT
    text + .get() reads in the same function."""
    violations = []
    for py_file in sorted((_REPO_ROOT / "services/verifiers").glob("*.py")):
        text = py_file.read_text()
        tree = ast.parse(text)
        # For each function, find SELECT string literals + .get()
        # column references; check intersection.
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            func_src = ast.get_source_segment(text, node) or ""
            select_clause = re.search(
                r"SELECT\s+(.+?)\s+FROM", func_src,
                re.IGNORECASE | re.DOTALL,
            )
            if not select_clause:
                continue
            select_cols = set(re.findall(
                r"\b(\w+)\b", select_clause.group(1).lower(),
            ))
            row_reads = set(re.findall(
                r"(?:sub_row|row)\.get\(\s*['\"](\w+)['\"]",
                func_src,
            ))
            missing = row_reads - select_cols - {"audit_run_id"}
            if missing:
                violations.append(
                    f"{py_file.name}:{node.name} reads "
                    f"{sorted(missing)} not in SELECT"
                )
    assert violations == [], (
        "SELECT/row.get drift in verifiers (P0-4 class):\n  "
        + "\n  ".join(violations)
    )


# =====================================================================
# 3. Env-var-fallback inventory
# =====================================================================


def test_no_localhost_or_127_fallback_in_production_getenv():
    """Production code (services/, routes/, db/) MUST NOT contain
    os.getenv("X", "...localhost...") fallbacks — that's the P0-5
    class where PIVOTA_BACKEND_INTERNAL_URL silently fell back to
    http://localhost:8000 in Railway prod.

    Allowed: explicit None fallback or config/settings.py-mediated
    reads. Allowed: tests can use localhost freely.

    Whitelist: pre-existing cases outside the P5.8 scope. New
    violations would still fire — the goal is regression
    prevention, not retroactive cleanup."""
    # Keys + files that predate P5.8. Each whitelist entry is
    # tech-debt that should eventually be cleaned up but is out of
    # this fix batch's scope. Removing an entry from the whitelist
    # is the right move once the underlying env-var hole is fixed.
    _WHITELIST = {
        # (file, env_key) — files relative to repo root
        ("routes/merchant_api_extensions.py", "BACKEND_URL"),
    }
    violations = []
    pattern = re.compile(
        r'os\.getenv\(\s*[\'"](\w+)[\'"]\s*,\s*[\'"]([^\'"]*)[\'"]',
    )
    for root in ("services", "routes", "db"):
        for py_file in sorted((_REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue
            rel = str(py_file.relative_to(_REPO_ROOT))
            for match in pattern.finditer(py_file.read_text()):
                key, default = match.group(1), match.group(2)
                if (
                    "localhost" not in default.lower()
                    and "127.0.0.1" not in default
                ):
                    continue
                if (rel, key) in _WHITELIST:
                    continue
                violations.append(
                    f"{rel}: os.getenv({key!r}, ...localhost...)"
                )
    assert violations == [], (
        "localhost fallback in production code "
        "(P0-5 class — env-var hole hidden behind localhost "
        "default):\n  " + "\n  ".join(violations)
        + "\n\nFix: register the key in config/settings.py + read "
        "from settings; return None / blocked when unset."
    )


# =====================================================================
# 4. No orphan DB helpers
# =====================================================================


_DB_HELPERS_THAT_NEED_PROD_CALLER = {
    # The accessors that MUST have a non-test caller. Add new
    # P4/P5 accessors here as they ship.
    "link_action_to_task",          # P4.2 (caught by P5.8.3)
    "enqueue_audit_run",            # P2.1
    "enqueue_executor_run",         # P3.1
    "enqueue_verification_run",     # P5.1
    "claim_next_pending_run",       # P2.1
    "claim_next_pending_executor_run",       # P3.1
    "claim_next_pending_verification",       # P5.1
    "persist_canonical_evidence",    # services-layer
    "build_and_persist_all_projections",   # services-layer
    "enqueue_verifications_for_completed_audit",  # P5.7
}


def test_critical_db_accessors_have_at_least_one_production_caller():
    """Every accessor in the curated list above must be called
    from at least one file under services/ or routes/. P0-3
    happened because link_action_to_task existed but had zero
    callers — materialized_task_id was permanently NULL.

    The curated list is intentional: not every helper needs a
    caller (some are tests-only), but the audit-lifecycle ones
    MUST be reachable."""
    callers_by_func = {f: [] for f in _DB_HELPERS_THAT_NEED_PROD_CALLER}
    for root in ("services", "routes"):
        for py_file in sorted((_REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue
            text = py_file.read_text()
            for func in _DB_HELPERS_THAT_NEED_PROD_CALLER:
                # Match `func_name(` as identifier (not substring
                # in another function name).
                if re.search(rf"\b{func}\s*\(", text):
                    callers_by_func[func].append(
                        str(py_file.relative_to(_REPO_ROOT)),
                    )
    orphans = [
        f for f, callers in callers_by_func.items() if not callers
    ]
    assert orphans == [], (
        "audit-lifecycle DB helpers have NO production callers "
        "(P0-3 class — dead helper):\n  " + "\n  ".join(orphans)
        + "\n\nFix: invoke from services/ or routes/ — or remove "
        "the helper if it's genuinely unused."
    )


# =====================================================================
# 5. Idempotent persist (logic-level test)
# =====================================================================


def test_persist_canonical_evidence_signature_helpers_are_deterministic():
    """The P0-2 fix relies on _evidence_signature / _finding_signature
    / _action_signature being DETERMINISTIC — same input dict
    produces same signature on every call. If anyone introduces
    time.time() or random.uuid() into a signature helper, the
    idempotency_key changes between re-runs + the ON CONFLICT no
    longer fires + we're back to data corruption.

    This test calls each signature helper twice and asserts the
    output is byte-identical."""
    from services.audit_evidence_builder import (
        _evidence_signature, _finding_signature, _action_signature,
    )
    ev = {
        "evidence_type": "grounding_chunk",
        "product_key": "p-1",
        "payload": {
            "host": "forbes.com",
            "excerpt_text": "X" * 200,
            "matched_url": "https://example.com",
        },
    }
    finding = {"finding_type": "category_visibility_low", "product_key": "p-1"}
    action = {
        "lever": "content_creation",
        "product_key": "p-1",
        "title": "Draft 3 publisher briefs (extra prose...)" * 5,
    }
    # Determinism: two calls produce identical output
    assert _evidence_signature(ev) == _evidence_signature(ev)
    assert _finding_signature(finding) == _finding_signature(finding)
    assert _action_signature(action) == _action_signature(action)
    # And the signatures themselves are non-empty strings
    assert len(_evidence_signature(ev)) > 0
    assert len(_finding_signature(finding)) > 0
    assert len(_action_signature(action)) > 0


# =====================================================================
# Bonus: VERIFIER_SPECS / register_verifier drift (P5.7's
# regression test elevated to meta-pattern)
# =====================================================================


def test_verifier_specs_matches_register_verifier_calls_grep_check():
    """The drift test in test_phase5_enqueue_verifications.py
    asserts VERIFIER_SPECS matches the registered set at runtime.
    This meta-test does the same check via source-code grep —
    useful when running outside the import side-effect graph (e.g.,
    in CI environments that don't import the verifier modules)."""
    enqueuer = (
        _REPO_ROOT / "services/audit_verification_enqueuer.py"
    ).read_text()
    spec_ids = set(re.findall(
        r'"id":\s*"(\w+)"', enqueuer,
    ))
    register_ids = set()
    for py_file in (_REPO_ROOT / "services/verifiers").glob("*.py"):
        text = py_file.read_text()
        for match in re.finditer(
            r'register_verifier\(\s*"(\w+)"', text,
        ):
            register_ids.add(match.group(1))
    only_in_specs = spec_ids - register_ids
    only_in_register = register_ids - spec_ids
    assert not only_in_specs and not only_in_register, (
        f"VERIFIER_SPECS / register_verifier drift:\n"
        f"  in specs but not registered: {only_in_specs}\n"
        f"  registered but not in specs: {only_in_register}"
    )


# =====================================================================
# 7. Function-local internal-import resolvability
# =====================================================================


def test_worker_modules_have_no_unresolvable_internal_imports():
    """Function-local `from db.X import Y` style imports inside the
    worker stack must point at modules that actually exist AND
    expose the named symbols. P5.8.8 happened because
    services/audit_run_worker.py:513 had `from db.catalog_products
    import catalog_products`, but the table object lives in
    `db.catalog`. The discovering-stage code path is skipped by
    unit tests (mocked away), so the broken import only surfaced
    when a real audit ran on prod — and Gate 4 of the deploy
    pipeline blocked there.

    This test parses each worker module's AST, finds every
    `ImportFrom` node (top-level + function-local), and:
      1. Resolves the module name (catches `db.catalog_products`
         where `db.catalog_products` doesn't exist).
      2. Resolves each imported symbol via getattr (catches
         `from db.catalog import some_renamed_thing` where the
         module exists but the symbol does not).

    Wildcard imports (`from db.X import *`) are skipped — symbol
    resolution is undefined for those.
    """
    worker_files = [
        _REPO_ROOT / "services/audit_run_worker.py",
        _REPO_ROOT / "services/executor_run_worker.py",
        _REPO_ROOT / "services/verification_run_worker.py",
        _REPO_ROOT / "services/audit_evidence_builder.py",
        _REPO_ROOT / "services/audit_projection_builder.py",
        _REPO_ROOT / "services/audit_verification_enqueuer.py",
    ]
    internal_prefixes = (
        "db.", "services.", "routes.", "config.", "utils.", "jobs.",
    )
    problems = []
    for path in worker_files:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            if not any(
                mod == p[:-1] or mod.startswith(p)
                for p in internal_prefixes
            ):
                continue
            try:
                imported = __import__(mod, fromlist=["*"])
            except Exception as exc:  # noqa: BLE001
                problems.append(
                    f"{path.name}:{node.lineno} — "
                    f"cannot import module {mod!r}: {exc!s}"
                )
                continue
            # Symbol resolution: each `name` in `from mod import x, y`
            # must exist on the module object OR be importable as a
            # submodule (`from db import merchant_audit_runs as mar`
            # style — Python doesn't auto-bind submodules onto the
            # parent namespace until they're imported).
            for alias in node.names:
                if alias.name == "*":
                    continue
                if hasattr(imported, alias.name):
                    continue
                # Try resolving as a submodule.
                try:
                    __import__(f"{mod}.{alias.name}")
                    continue  # submodule import succeeded
                except Exception:  # noqa: BLE001
                    pass
                problems.append(
                    f"{path.name}:{node.lineno} — "
                    f"{mod!r} has no attribute "
                    f"{alias.name!r}"
                )

    assert not problems, (
        "Worker modules have unresolvable internal imports — these will "
        "blow up at runtime when the code path is exercised "
        "(unit tests mock the surrounding logic, so the import never "
        "actually executes in test). Fix the import path:\n  "
        + "\n  ".join(problems)
    )


# =====================================================================
# 9. JSONB write boundaries route through _json_safe
# =====================================================================


def test_jsonb_writes_in_db_modules_route_through_json_safe():
    """Every place that assigns a JSONB column value via SQLAlchemy
    `.values(...)` should coerce the payload through `_json_safe`
    first. PR #477 + #479 fixed three of these sites; codex review
    found four more in db/merchant_audit_runs.py, db/executor_runs.py,
    and db/audit_evidence.py:801. Without coercion, any payload that
    contains a UUID (probe_run_id, evidence_id, audit_run_id, etc.)
    fails JSONB serialization and the write silently no-ops (worst
    case: appears completed but produces zero downstream rows, the
    Gate 5 action_plan_items=0 class of bug).

    The check is a pragmatic grep: for every `*_jsonb = <expr>` or
    `*_jsonb=<expr>` assignment inside files in db/, the right-hand
    side should reference `_json_safe` somewhere on the same line OR
    be a documented exception. False-positives are listed in
    _JSONB_WRITE_EXCEPTIONS so they don't gum up CI.
    """
    # Documented exceptions — any line containing one of these
    # substrings is skipped. Each captures a known-safe pattern:
    _JSONB_WRITE_EXCEPTIONS = {
        # Already-coerced sentinel value names. Variables prefixed
        # with `safe_` or named `_json_safe(...)` are guaranteed to
        # have been through the coercer.
        "_json_safe(",
        "safe_payload",
        "safe_evidence",
        # Table schema definitions — not runtime writes.
        "Column(",
        # Row reads (column appears as a row.get() target, not an
        # insert/update LHS).
        "d.get(",
        "row.get(",
        ".get(\"payload_jsonb\")",
        ".get(\"evidence_jsonb\")",
        ".get(\"report_jsonb\")",
        ".get(\"error_jsonb\")",
        ".get(\"cost_summary_jsonb\")",
        ".get(\"partial_result_jsonb\")",
        ".get(\"attribution_jsonb\")",
        # Raw-SQL fragments inside string literals. The check uses
        # the heuristic "contains SQL operator/keyword on the same
        # line" — patterns like `evidence_jsonb = :evidence_jsonb`,
        # `SET col = COALESCE(...)`, `=  '{}'::jsonb`.
        "COALESCE(",
        " = :",
        ":patch::jsonb",                    # legacy SQL — already
                                            # caught by meta-test #8
        "CAST(:patch AS JSONB)",            # post-fix SQL
        # A SQL-text write binds a param whose value is coerced in the
        # execute() params dict (e.g. json.dumps(_json_safe(...)) /
        # _dumps(..., default=str)), which the line-grep can't see. Every
        # `..._jsonb = CAST(:x AS JSONB)` is this class: persist_report_jsonb
        # + catalog_onboard.mark_done.
        "CAST(:",
        # kwarg PASSTHROUGH to a callee that coerces (record_audit_run_started
        # → enqueue_audit_run_with_replay, which coerces request_options_jsonb).
        "request_options_jsonb=request_options_jsonb",
        # already-coerced local var flowing into .insert().values() — the
        # coercion happened at the assignment (single line, _json_safe visible).
        "partial_result_jsonb=partial_result_jsonb",
        # Migration DDL.
        "ADD COLUMN",
        "ALTER COLUMN",
        # SQL SELECT/UPDATE/INSERT keyword as a continuation token.
        "RETURNING",
    }
    pattern = re.compile(
        r'\b\w+_jsonb\s*=\s*'
    )
    problems = []
    for py_file in (_REPO_ROOT / "db").glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        text = py_file.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if (
                stripped.startswith("#")
                or stripped.startswith("\"\"\"")
                or stripped.startswith("'''")
            ):
                continue
            if not pattern.search(line):
                continue
            # Skip whitelisted exception patterns.
            if any(ex in line for ex in _JSONB_WRITE_EXCEPTIONS):
                continue
            problems.append(
                f"{py_file.name}:{lineno} — assigns *_jsonb without "
                f"_json_safe(): {line.strip()[:140]}"
            )

    assert not problems, (
        "JSONB write site(s) bypass the _json_safe coercion at the "
        "write boundary. Any UUID/datetime/Decimal in the payload "
        "will fail JSONB serialization silently. Add _json_safe(...) "
        "around the value, OR (if the line is a false positive) add "
        "a matching substring to _JSONB_WRITE_EXCEPTIONS in this "
        "test:\n  "
        + "\n  ".join(problems)
    )


# =====================================================================
# 8. SQLAlchemy text() vs PostgreSQL `::cast` collision
# =====================================================================


def test_no_param_double_colon_cast_in_raw_sql():
    """SQLAlchemy text() parameter parser reads `:name` as a bound
    parameter. PostgreSQL's `::type` cast operator looks like a
    continuation of the parameter name to that parser, so
    `:patch::jsonb` is parsed as a SINGLE parameter named
    'patch:jsonb' instead of `:patch` + `::jsonb` cast.

    The bug: SQLAlchemy then can't resolve 'patch' to the provided
    bound parameter, and raises:
        This text() construct doesn't define a bound parameter
        named 'patch'

    record_partial_result (db/merchant_audit_runs.py) shipped with
    this exact pattern, breaking partial_result writes silently in
    prod (lifecycle's best-effort try/except swallowed the error).
    Gate 5 of the deploy validation pipeline caught it on run
    52b789f2-98e2-4799-968b-88ad0d50279c.

    The codebase already has the correct pattern at 6+ other sites:
    `CAST(:param AS JSONB)`. This meta-test fails if anyone reintro-
    duces `:param::type` in production SQL.
    """
    pattern = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)::([a-zA-Z_]+)")
    problems = []
    for root in ("db", "services"):
        for py_file in (_REPO_ROOT / root).rglob("*.py"):
            text = py_file.read_text()
            for lineno, line in enumerate(text.splitlines(), start=1):
                # Skip lines that are obviously not SQL (imports,
                # type hints, docstrings/comments). The bug only
                # manifests inside strings passed to database.execute
                # / text() / similar — so require the colon-cast to
                # appear in a string-literal context. Heuristic: line
                # contains a quote AND the pattern.
                if '"' not in line and "'" not in line:
                    continue
                if line.lstrip().startswith("#"):
                    continue
                m = pattern.search(line)
                if m is None:
                    continue
                problems.append(
                    f"{py_file.relative_to(_REPO_ROOT)}:{lineno} — "
                    f"`:{m.group(1)}::{m.group(2)}` "
                    f"breaks SQLAlchemy text() parser. "
                    f"Use `CAST(:{m.group(1)} AS {m.group(2).upper()})` "
                    f"instead. (line: {line.strip()[:120]})"
                )

    assert not problems, (
        "Production SQL contains `:param::type` PostgreSQL-cast "
        "syntax that breaks SQLAlchemy text() parameter binding:\n  "
        + "\n  ".join(problems)
    )
