"""Every statement the ops scripts send must be PLANNABLE by Postgres.

THE DEFECT THIS CLOSES. The first production `--apply` of
`scripts/remediate_unpublished_crawl_rows.py` died at PREPARE with
`could not determine data type of parameter $2`, having written nothing.
`jsonb_build_object` is variadic `"any"`, so Postgres cannot infer a bind
parameter's type from its position; the statement is rejected before it is
planned, on every row, so the runner was simply dead. Fixed in #1703 by wrapping
the params in `CAST(:x AS text)`.

WHY NOTHING CAUGHT IT — the same three-part gap, again:

* `tests/test_remediate_unpublished_crawl_rows.py` records SQL strings against a
  fake connection. A fake never asks Postgres to plan anything, so a statement
  that cannot be prepared looks identical to one that can. This is the same
  reason `tests/test_databases_textclause_params.py` exists — read its module
  docstring; the argument is verbatim the same, only the engine differs. That
  class raises inside `databases._build_query` before the driver is touched, so
  SQLite reproduces it. This one needs a real Postgres PREPARE.
* The dialect gate (`.github/workflows/postgres-dialect-gate.yml`) collects
  `tests/test_*_postgres.py`; the remediation script's test file matches no set
  it runs, so it never executed against Postgres at all.
* The regression test #1703 added is a TEXTUAL pin on those specific casts. It
  catches deleting them. It cannot catch the next statement written with the
  same flaw — which is exactly the "an assertion has to be authored against a
  defect you have already met" argument in the workflow header.

WHAT THIS FILE DOES INSTEAD. It takes the statements the scripts actually build
— captured by driving the real functions, or by calling the real query builders,
never by lifting a copy into the test — and asks Postgres to PREPARE each one. A
statement that cannot be planned fails the gate. No foreknowledge of the defect
is needed, which is the whole point.

PREPARE, NOT EXECUTE. `conn.prepare()` is Parse+Describe: it resolves every
table, column, operator and parameter type and plans the statement, then stops.
Nothing is written and no parameter VALUES are needed — which matters, because
inventing values for `INSERT INTO catalog_skus (...)` would trip NOT NULL
constraints and report a schema-fixture problem as a dialect defect. Type
inference happens at Parse and is value-independent, so PREPARE is exactly the
right knife: it sees the whole failure class and nothing else.

`:name` -> `$n` is done by compiling the SQL through SQLAlchemy's postgresql
dialect (the same compiler `databases` uses) rather than a hand-rolled regex,
so `'{}'::jsonb` stays a cast and does not become a bind named `jsonb`.

KNOWN LIMIT, stated rather than discovered later: PREPARE resolves names against
THIS database's schema, so the gate is only as truthful as the fixture. Column
TYPES are load-bearing here in a way they are not for a row-semantics test — a
stub column typed `text` where production has `jsonb` would change whether a
cast is required. The lightweight DDL below mirrors migrations 044 / 098.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` for tables
`db.catalog` owns; `CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS`
only for tables it does not. Never hand-roll DDL for a table `db.catalog` owns —
see the warning in tests/test_canonical_feed_tombstoned_flag_postgres.py, where
a three-column stub under a real table's name broke every sibling gate.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)


# ---------------------------------------------------------------------------
# schema fixture
# ---------------------------------------------------------------------------
# Only tables `db.catalog` does NOT own. Types mirror migrations 044
# (external_product_seeds) and 098 (index_pipeline_state) because PREPARE
# resolves parameter types THROUGH the column types — see KNOWN LIMIT above.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (id text);
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS market text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS tool text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_url text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS canonical_url text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS domain text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS image_url text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS attached_product_key text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seller_ref text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_kind text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data jsonb;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS updated_at timestamptz;
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS pivota_signature_id text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS merchant_id text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS pipeline_stage text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_detail text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS content_quality_score real;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS quality_scored_at timestamptz;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS last_extracted_at timestamptz;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS description_length integer;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS has_image boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS has_price boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS updated_at timestamptz;
CREATE TABLE IF NOT EXISTS product_group_members (product_group_id text);
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS merchant_id text;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS platform text;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS platform_product_id text;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS is_primary boolean;
"""


@pytest.fixture(scope="module")
def prepare() -> Callable[[str], None]:
    """Yield `prepare(sql)` -> raises if Postgres cannot plan the statement.

    One asyncpg connection on a private loop for the whole module. The tests are
    sync so there is no running loop to collide with, and nothing here is
    written: `prepare` is Parse+Describe and stops.
    """
    import asyncpg
    import db.catalog  # noqa: F401  (registers the catalog tables on the shared MetaData)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(stmt))
    engine.dispose()

    loop = asyncio.new_event_loop()
    pg = loop.run_until_complete(asyncpg.connect(DATABASE_URL.replace("+asyncpg", "")))

    def _prepare(sql: str) -> None:
        loop.run_until_complete(pg.prepare(_to_positional(sql)))

    try:
        yield _prepare
    finally:
        loop.run_until_complete(pg.close())
        loop.close()


# ---------------------------------------------------------------------------
# `:name` -> `$n`
# ---------------------------------------------------------------------------
_PYFORMAT = re.compile(r"%\((\w+)\)s")


def _to_positional(sql: str) -> str:
    """Render `:name` binds as `$n` for asyncpg.

    Compiled through SQLAlchemy's postgresql dialect — the same compiler
    `databases` drives — rather than a regex over `:\\w+`, so `'{}'::jsonb` stays
    a cast instead of becoming a bind named `jsonb`.
    """
    from sqlalchemy import text as sa_text
    from sqlalchemy.dialects import postgresql

    rendered = sa_text(sql).compile(dialect=postgresql.dialect(paramstyle="pyformat")).string
    order: List[str] = []
    for match in _PYFORMAT.finditer(rendered):
        if match.group(1) not in order:
            order.append(match.group(1))
    numbered = {name: f"${i}" for i, name in enumerate(order, start=1)}
    return _PYFORMAT.sub(lambda m: numbered[m.group(1)], rendered).replace("%%", "%")


# ---------------------------------------------------------------------------
# statement collectors
#
# Each returns [(origin, sql)]. Statements are DRIVEN out of the real functions
# or produced by the real query builders — never a copy pasted into this file.
# A lifted copy tests the copy, which is how the original defect shipped (see
# tests/test_pdp_scope_backfill_postgres.py's header on the same point).
# ---------------------------------------------------------------------------
class _Recorder:
    """A `databases.Database` stand-in that records instead of executing."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.statements: List[Tuple[str, str]] = []

    def _record(self, method: str, query: Any) -> None:
        self.statements.append((f"{self.origin} -> database.{method}()", str(query)))

    async def execute(self, query: Any, values: Any = None) -> None:
        self._record("execute", query)

    async def fetch_all(self, query: Any, values: Any = None) -> List[Any]:
        self._record("fetch_all", query)
        return []

    async def fetch_one(self, query: Any, values: Any = None) -> None:
        self._record("fetch_one", query)
        return None

    async def fetch_val(self, query: Any, values: Any = None) -> None:
        self._record("fetch_val", query)
        return None


def _drive(module: Any, coro_factory: Callable[[], Any], origin: str,
           **extra_patches: Any) -> List[Tuple[str, str]]:
    """Run an async entry point with the module's `database` global recording.

    try/finally restores every patched global — these are module-level names
    shared with the rest of the session.
    """
    recorder = _Recorder(origin)
    saved = {"database": module.database}
    module.database = recorder
    for name, value in extra_patches.items():
        saved[name] = getattr(module, name)
        setattr(module, name, value)
    try:
        asyncio.run(coro_factory())
    finally:
        for name, value in saved.items():
            setattr(module, name, value)
    return recorder.statements


async def _noop_recompute(content_key: str, reason: str | None = None) -> bool:
    """Stand-in for services.index_pipeline_state_service.recompute_serving_eligibility.

    It owns a different module's `database` global; this gate is scoped to the
    SQL the SCRIPT itself sends.
    """
    return True


def _collect_remediate() -> List[Tuple[str, str]]:
    """The script that broke prod. Every statement, driven through the real
    functions — the `jsonb_build_object` UPDATE among them."""
    import scripts.remediate_unpublished_crawl_rows as module

    row = {
        "seed_id": "seed_probe",
        "seed_status": "active",
        "destination_url": "https://example.com/products/probe",
        "product_key": "prod::probe",
        "content_key": "ck_probe",
        "suppression_reason": None,
        "suppressed_at": None,
        "rows_on_key": 5,
    }
    revert_row = dict(
        row,
        # 'active' is the branch that also touches external_product_seeds; the
        # 'inactive' branch is a strict subset of these statements.
        suppression_metadata={"script": module.SCRIPT_NAME, "prior_seed_status": "active"},
    )

    async def _go() -> None:
        await module._load_candidates(None)
        await module._withdraw([dict(row)])
        await module._revert([revert_row])
        await module._drive_revert(
            argparse.Namespace(host=None, apply=False, show=5, revert=True)
        )

    return _drive(
        module, _go, "remediate_unpublished_crawl_rows",
        recompute_serving_eligibility=_noop_recompute,
    )


def _collect_seed_content_audit() -> List[Tuple[str, str]]:
    import scripts.run_seed_content_audit as module

    origin = "run_seed_content_audit"
    return [
        # Both branches: the default one carries `seed_data->'review_summary'->>
        # 'auditor' != :auditor_version`, a bind compared against a `->>`
        # extraction — the same family of position Postgres must infer through.
        (f"{origin}._select_sql(force=False)",
         module._select_sql(force=False, limit=10, offset=5)),
        (f"{origin}._select_sql(force=True)",
         module._select_sql(force=True, limit=10, offset=5)),
        (f"{origin}.UPDATE_SEED_SQL", module.UPDATE_SEED_SQL),
        (f"{origin}.UPDATE_CATALOG_PRODUCTS_SQL", module.UPDATE_CATALOG_PRODUCTS_SQL),
    ]


def _collect_source_pdp_content_repair() -> List[Tuple[str, str]]:
    import scripts.source_pdp_content_repair as module

    origin = "source_pdp_content_repair"
    return [
        (f"{origin}.build_candidate_query(limit=10)",
         module.build_candidate_query(limit=10)[0]),
        (f"{origin}.build_candidate_query(limit=0)",
         module.build_candidate_query(limit=0)[0]),
        (f"{origin}.UPDATE_DESCRIPTION_SQL", module.UPDATE_DESCRIPTION_SQL),
    ]


def _collect_source_pdp_offer_image_repair() -> List[Tuple[str, str]]:
    import scripts.source_pdp_offer_image_repair as module

    origin = "source_pdp_offer_image_repair"
    return [
        (f"{origin}.build_candidate_query(scoped)",
         module.build_candidate_query(limit=10, include_upstream_blockers=False)[0]),
        (f"{origin}.build_candidate_query(unscoped)",
         module.build_candidate_query(limit=0, include_upstream_blockers=True)[0]),
        (f"{origin}.EXISTING_SKU_QUERY", module.EXISTING_SKU_QUERY),
        (f"{origin}.INSERT_REPAIR_SKU_SQL", module.INSERT_REPAIR_SKU_SQL),
        (f"{origin}.INSERT_REPAIR_OFFER_SQL", module.INSERT_REPAIR_OFFER_SQL),
        (f"{origin}.UPDATE_CATALOG_IMAGE_SQL", module.UPDATE_CATALOG_IMAGE_SQL),
    ]


# module path -> collector. The completeness guard below reads this same list,
# so a script cannot be registered here and left half-covered in silence.
_COVERED_SCRIPTS: Dict[str, Callable[[], List[Tuple[str, str]]]] = {
    "scripts/remediate_unpublished_crawl_rows.py": _collect_remediate,
    "scripts/run_seed_content_audit.py": _collect_seed_content_audit,
    "scripts/source_pdp_content_repair.py": _collect_source_pdp_content_repair,
    "scripts/source_pdp_offer_image_repair.py": _collect_source_pdp_offer_image_repair,
}

# What each collector yields TODAY, not a slack lower bound. Guards the failure
# the workflow header calls out by name: a gate that runs and asserts nothing is
# green while testing nothing. Deleting a driven call site — or a collector that
# quietly stops driving one — drops the count below these and turns the gate red
# instead of shrinking its coverage in silence. Adding statements is fine; only
# losing them needs a deliberate edit here, with the reason.
_MIN_STATEMENTS = {
    "scripts/remediate_unpublished_crawl_rows.py": 11,
    "scripts/run_seed_content_audit.py": 4,
    "scripts/source_pdp_content_repair.py": 3,
    "scripts/source_pdp_offer_image_repair.py": 6,
}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", sorted(_COVERED_SCRIPTS), ids=lambda p: Path(p).stem)
def test_every_statement_the_script_sends_can_be_planned(prepare, script: str) -> None:
    statements = _COVERED_SCRIPTS[script]()

    minimum = _MIN_STATEMENTS[script]
    assert len(statements) >= minimum, (
        f"{script} yielded {len(statements)} statement(s), expected at least "
        f"{minimum}. Either a call site was removed (update _MIN_STATEMENTS with "
        "the reason) or the collector stopped driving it — a collector that "
        "yields nothing makes this gate green without planning anything."
    )

    failures: List[str] = []
    for origin, sql in statements:
        try:
            prepare(sql)
        except Exception as exc:  # noqa: BLE001 — every planning failure is in scope
            body = "\n".join("    " + line for line in sql.strip().splitlines())
            failures.append(f"  {origin}\n    !! {type(exc).__name__}: {exc}\n{body}")

    assert not failures, (
        f"{len(failures)} statement(s) in {script} cannot be PREPAREd by Postgres.\n"
        "`IndeterminateDatatypeError: could not determine data type of parameter $n` "
        "means a bind sits where Postgres cannot infer its type from position — "
        "inside a variadic \"any\" function such as jsonb_build_object, or compared "
        "against another untyped expression. Wrap it: `CAST(:x AS text)`.\n"
        + "\n".join(failures)
    )


def test_the_gate_fails_on_an_untypeable_bind_param(prepare) -> None:
    """A gate that passes both before and after the fix is worthless.

    This is the #1703 statement with and without its casts. The bad form is the
    literal SQL that died in production; if it PREPAREs here, the harness above
    proves nothing and every green run in this file is a false negative.
    """
    fixed = (
        "UPDATE catalog_products "
        "   SET suppression_metadata = COALESCE(suppression_metadata, '{}'::jsonb) "
        "       || jsonb_build_object("
        "            'script', CAST(:script AS text), "
        "            'prior_seed_status', CAST(:prior AS text)), "
        "       updated_at = NOW() "
        " WHERE source_ref = :id AND suppressed_at IS NULL"
    )
    broken = fixed.replace("CAST(:script AS text)", ":script").replace(
        "CAST(:prior AS text)", ":prior"
    )

    prepare(fixed)  # must plan

    with pytest.raises(Exception) as caught:
        prepare(broken)
    assert "could not determine data type of parameter" in str(caught.value), (
        "expected the production failure — asyncpg IndeterminateDatatypeError — "
        f"but got {type(caught.value).__name__}: {caught.value}"
    )


def test_the_cast_renderer_does_not_eat_postgres_cast_syntax() -> None:
    """`'{}'::jsonb` must survive as a cast, not become a bind named `jsonb`.

    A regex over `:\\w+` gets this wrong, and it would get it wrong SILENTLY —
    the statement would still PREPARE, just with a parameter that production
    never sends, so the gate would be planning SQL nobody runs.
    """
    got = _to_positional(
        "UPDATE t SET meta = COALESCE(meta, '{}'::jsonb) || CAST(:v AS jsonb) "
        "WHERE k = :k AND meta->>'s' = CAST(:v AS text)"
    )
    assert "'{}'::jsonb" in got
    assert ":v" not in got and ":k" not in got
    # `:v` appears twice and must reuse one placeholder, as `databases` does.
    assert got.count("$1") == 2 and "$2" in got and "$3" not in got


# ---------------------------------------------------------------------------
# completeness guard — covers the NEXT statement, not just today's
# ---------------------------------------------------------------------------
_SQL_CONSTANT = re.compile(r"(?:SQL|QUERY)$")


def _module_level_sql_constants(path: Path) -> List[Tuple[str, str]]:
    """Module-level `NAME = "...sql..."` constants whose name ends in SQL/QUERY."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: List[Tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and _SQL_CONSTANT.search(target.id):
                found.append((target.id, node.value.value))
    return found


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.parametrize("script", sorted(_COVERED_SCRIPTS), ids=lambda p: Path(p).stem)
def test_no_sql_constant_in_a_covered_script_escapes_the_gate(script: str) -> None:
    """Adding a new SQL constant to a covered script must not silently opt out.

    Without this, the gate covers the statements someone remembered to register
    on the day they wrote it — the same "authored against a defect you have
    already met" trap the workflow header warns about, one level up.

    A constant counts as covered when it is named in a collected statement's
    origin, or when its text turns up inside one (that is how the DRIVEN
    statements and the `.format()`-rendered CANDIDATE_QUERY templates register).
    """
    path = REPO_ROOT / script
    statements = _COVERED_SCRIPTS[script]()
    origins = " ".join(origin for origin, _ in statements)
    bodies = [_normalize(sql) for _, sql in statements]

    constants = _module_level_sql_constants(path)
    assert constants, (
        f"{script} is registered as covered but declares no module-level SQL "
        "constant — either the AST scan broke or the wrong file is registered."
    )

    uncovered: List[str] = []
    for name, raw in constants:
        if name in origins:
            continue
        value = _normalize(raw)
        # A `.format()` template never appears verbatim in its rendered output,
        # so match on the fixed head that precedes the first substitution.
        head = value.split("{", 1)[0].strip() if "{" in value else value
        if head and any(head in body for body in bodies):
            continue
        uncovered.append(name)

    assert not uncovered, (
        f"{script} defines SQL constant(s) no collector in this file reaches: "
        + ", ".join(sorted(uncovered))
        + ". Add them to the collector (preferably by driving the function that "
        "sends them, so the test cannot drift from the call site) — otherwise "
        "they ship unplanned, which is exactly how #1703 reached production."
    )
