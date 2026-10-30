"""scripts/ops/measure_setting_spray_refile.py on the production dialect: its candidate SQL (PostgreSQL
regex), its READ ONLY transaction and statement timeout, GATE_OPEN only after both, and the drift line.

    DATABASE_URL=postgresql://127.0.0.1:5432/<tag>_test .venv/bin/python -m pytest tests/test_measure_setting_spray_refile_postgres.py

ISOLATION. A per-process scratch schema built from the model (catalog_products, category_taxonomy) plus
the one index_pipeline_state column it reads, dropped at teardown; `public` is never touched.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL -- see the module docstring")

_REPO = Path(__file__).resolve().parent.parent
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"measure_setting_spray_test_{os.getpid()}"
LEAF = "beauty/makeup/face/setting-spray"

_spec = importlib.util.spec_from_file_location("measure_setting_spray_refile",
                                               _REPO / "scripts/ops/measure_setting_spray_refile.py")
measure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure)


def _url() -> str:
    url = DATABASE_URL
    return "postgresql://" + url[len("postgres://"):] if url.startswith("postgres://") else url


ROWS = [
    # key, stored path, title, merchant type, domain, serving
    ("k_toner", "beauty/skincare/tone/toner", "Makeup Fixing Mist", "", "shop.example", True),
    ("k_gap", "beauty/makeup/setting-spray", "All Nighter Setting Spray", "", "b.example", True),
    ("k_typed", "beauty", "Life Lock Hydrating Setting Spray", "Setting Spray", "shop.example", False),
    ("k_powder", "beauty/makeup/face/powder", "Translucent Powder", "Setting Spray and Powder", "c.example", True),
    ("k_combined", "beauty/makeup/face/powder", "Never Touch Up Setting Spray", "Setting Spray and Powder", "c.example", True),
    ("k_brow", "beauty", "Brow Setting Spray", "", "shop.example", True),
    # the phrase only in the merchant type, or only in the category column
    ("k_typefix", "beauty", "Dewy Glow 100ml", "Make-Up Setting Fixer", "shop.example", True),
    ("k_cat", "beauty", "Dewy Glow 50ml", "", "shop.example", True),
    # not candidates: no setting phrase, and "setting" is not a word boundary inside "Upsetting"
    ("k_mist", "beauty/skincare/tone/toner", "Hydrating Face Mist", "", "shop.example", True),
    ("k_word", "beauty", "Upsetting Sprayer Bottle", "", "shop.example", True),
]


@pytest.fixture(scope="module", autouse=True)
def _schema():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create a scratch schema in database {dbname!r} -- throwaway only")
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    admin = create_engine(_url())
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    engine = create_engine(_url(), connect_args={"options": f"-csearch_path={_SCHEMA}"})
    metadata.create_all(engine, tables=[metadata.tables["catalog_products"], metadata.tables["category_taxonomy"]])
    import sys
    sys.path.insert(0, str(_REPO / "scripts"))
    from seed_category_taxonomy import _desired

    # the table as it stands BEFORE the seeder: every row but the setting-spray leaf and its alias
    desired = [r for r in _desired() if r["path"] not in (LEAF, "beauty/makeup/setting-spray")]
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE index_pipeline_state (content_key text PRIMARY KEY, serving_eligible boolean)"))
        for record in sorted(desired, key=lambda r: r.get("alias_of") is not None):
            conn.execute(text("INSERT INTO category_taxonomy (path, label, is_leaf, alias_of) VALUES (:p, :l, :f, :a)"),
                         {"p": record["path"], "l": record["label"], "f": record["is_leaf"], "a": record.get("alias_of")})
        for key, path, title, ptype, domain, serving in ROWS:
            conn.execute(text("INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, "
                              "content_key, title, product_type, category, category_path, source_domain) "
                              "VALUES (:k, 'm', 'shopify', :k, :k, :t, :p, :cat, :c, :d)"),
                         {"k": key, "t": title, "p": ptype, "c": path, "d": domain,
                          "cat": "Makeup Fixing Spray" if key == "k_cat" else None})
            conn.execute(text("INSERT INTO index_pipeline_state VALUES (:k, :s)"), {"k": key, "s": serving})
    engine.dispose()
    yield
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
    admin.dispose()


def _collect():
    import asyncpg

    async def run():
        conn = await asyncpg.connect(_url(), server_settings={"search_path": _SCHEMA})
        try:
            return await measure.collect(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_gate_open_follows_the_read_only_and_timeout_preconditions():
    lines = _collect()
    checks = [line["check"] for line in lines if isinstance(line, dict) and line.get("kind") == "precondition"]
    assert checks == ["image_has_leaf", "read_only", "statement_timeout"]
    assert lines.count("GATE_OPEN") == 1
    gate = lines.index("GATE_OPEN")
    assert gate == 3 and [line["check"] for line in lines[:gate]] == checks
    assert not any(isinstance(line, dict) and line.get("kind") == "error" for line in lines)


def test_the_candidate_sql_selects_by_postgres_regex_and_the_resolvers_decide():
    lines = _collect()
    [params] = [line for line in lines if isinstance(line, dict) and line["kind"] == "params"]
    assert params["candidates"] == 8            # every row but the face mist and "Upsetting"
    keys = {line["product_key"]: line for line in lines if isinstance(line, dict) and line["kind"] == "key"}
    assert set(keys) == {"k_toner", "k_gap", "k_typed", "k_combined", "k_brow", "k_typefix", "k_cat"}
    assert keys["k_typefix"]["feed"] == LEAF and keys["k_cat"]["row"] == LEAF
    assert keys["k_toner"]["row"] == LEAF and keys["k_gap"]["row"] == LEAF
    assert keys["k_typed"]["feed"] == LEAF                   # a typed shelf is what the drain reads
    assert keys["k_combined"]["shelf"] == "combined_spray_powder" and keys["k_combined"]["feed"] == LEAF
    assert keys["k_brow"]["row"] == "" and keys["k_brow"]["from"] == "beauty"   # a brow fixer is not the leaf
    moves = [line for line in lines if isinstance(line, dict) and line["kind"] == "from_to"
             and line["source_domain"] == "c.example" and line["resolver"] == "feed"]
    assert {(m["from"], m["to"]) for line in moves for m in line["moves"]} == {
        ("beauty/makeup/face/powder", "beauty/makeup/face/powder"), ("beauty/makeup/face/powder", LEAF)}


def test_the_drift_before_the_seeder_is_the_leaf_alone():
    [drift] = [line for line in _collect() if isinstance(line, dict) and line["kind"] == "drift"]
    assert LEAF in drift["setting_spray_entries"]
    assert drift["count_excluding_setting_spray_leaf"] == 0


def test_it_refuses_to_measure_on_an_image_without_the_leaf(monkeypatch):
    import services.category_path_aliases as aliases

    monkeypatch.setattr(aliases, "TAXONOMY_LEAVES", frozenset(p for p in aliases.TAXONOMY_LEAVES if p != LEAF))
    lines = _collect()
    assert "GATE_OPEN" not in lines
    assert lines[-1]["kind"] == "error"
