"""Tests for the citation-operator migration apply/verify admin endpoint.

The SQL splitter is the bug-prone part: a leading `-- header` comment block
before a CREATE statement previously caused the whole statement (table and all)
to be discarded. These tests read the REAL migration files and assert every
CREATE TABLE survives, plus exercise the apply/verify endpoints with a fake DB.
"""
from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.admin_apply_citation_operator as m
from utils.auth import require_admin_or_key


def test_splitter_keeps_every_create_table() -> None:
    creates = set()
    for f in m._MIGRATION_FILES:
        stmts = m._split_sql_statements((m._MIGRATIONS_DIR / f).read_text())
        for s in stmts:
            assert not s.strip().startswith("--")          # no comment-only statements
            if "CREATE TABLE" in s.upper():
                creates.add(s.split("IF NOT EXISTS")[1].split("(")[0].strip())
    assert creates == set(m._CITATION_TABLES)               # all 5 tables present


def test_splitter_excludes_down_block_and_bind_tokens() -> None:
    for f in m._MIGRATION_FILES:
        for s in m._split_sql_statements((m._MIGRATIONS_DIR / f).read_text()):
            assert "DROP TABLE" not in s.upper()            # commented-out DOWN block excluded
            assert not re.search(r":[a-zA-Z_]", s)          # no :word the DB lib binds


def _client(monkeypatch, *, present_tables):
    executed = []
    async def fake_execute(stmt, *a, **k):
        executed.append(stmt)
    async def fake_fetch_all(query, values=None):
        return [{"table_name": t} for t in present_tables]
    monkeypatch.setattr(m.database, "execute", fake_execute)
    monkeypatch.setattr(m.database, "fetch_all", fake_fetch_all)
    app = FastAPI()
    app.include_router(m.router)
    app.dependency_overrides[require_admin_or_key] = lambda: {"role": "admin"}
    return TestClient(app), executed


def test_apply_executes_all_statements(monkeypatch) -> None:
    client, executed = _client(monkeypatch, present_tables=m._CITATION_TABLES)
    resp = client.post("/admin/migrations/apply-citation-operator")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    # 146 (11) + 147 (2) + 148 (2) = 15 statements executed.
    assert len(executed) == 15
    assert sum(s["statements_executed"] for s in body["migrations"]) == 15


def test_verify_reports_missing_when_absent(monkeypatch) -> None:
    client, _ = _client(monkeypatch, present_tables=[])
    resp = client.get("/admin/migrations/verify-citation-operator")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_applied"
    assert set(body["tables_missing"]) == set(m._CITATION_TABLES)


def test_verify_reports_applied_when_present(monkeypatch) -> None:
    client, _ = _client(monkeypatch, present_tables=m._CITATION_TABLES)
    resp = client.get("/admin/migrations/verify-citation-operator")
    assert resp.json()["status"] == "applied"
