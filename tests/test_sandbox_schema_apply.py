"""jobs/sandbox_schema_apply.py: the checks that need no database."""

from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from jobs import sandbox_schema_apply as A
from jobs.sandbox_catalog_copy import LocalDirStore
from tests.test_sandbox_guard import GOOD, _clean_platform_env  # noqa: F401 (autouse fixture)
from tests.sandbox_test_addresses import stand_in_addresses  # noqa: F401 (autouse: hashed addresses -> RFC 5737 stand-ins)

TRAILER = "\n--\n-- PostgreSQL database dump complete\n--\n"
OK_SCHEMA = (
    "\\restrict abc123\nSET statement_timeout = 0;\n"
    "CREATE TABLE public.t (id integer NOT NULL);\n"
    "CREATE FUNCTION public.f() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n"
    "INSERT INTO public.audit VALUES (1);\nGRANT SELECT ON t TO x;\nRETURN NEW;\nEND $$;\n"
    "CREATE FUNCTION public.g() RETURNS text LANGUAGE sql AS $_$ SELECT 'COPY x' $_$;\n"
    "ALTER TABLE ONLY public.t ADD CONSTRAINT t_pkey PRIMARY KEY (id);\n\\unrestrict abc123" + TRAILER
)


def test_a_clean_schema_passes_and_loses_only_the_meta_lines():
    sql = A.validate_schema_sql(OK_SCHEMA)
    assert "\\restrict" not in sql and "\\unrestrict" not in sql
    assert "INSERT INTO public.audit" in sql  # inside a function body: kept, and not a refusal


@pytest.mark.parametrize("statement", [
    "CREATE SERVER s FOREIGN DATA WRAPPER postgres_fdw;",
    "CREATE USER MAPPING FOR u SERVER s OPTIONS (password 'p');",
    "CREATE SUBSCRIPTION s CONNECTION 'host=x password=p' PUBLICATION p;",
    "CREATE PUBLICATION p FOR ALL TABLES;",
    "CREATE ROLE r LOGIN PASSWORD 'p';",
    "ALTER ROLE r PASSWORD 'p';",
    "ALTER SYSTEM SET x = 1;",
    "GRANT ALL ON t TO r;",
    "REVOKE ALL ON t FROM r;",
    "COPY public.t (id) FROM stdin;",
    "INSERT INTO public.t VALUES (1);",
    "create database other;",
])
def test_a_statement_that_can_hold_a_credential_or_rows_is_refused(statement):
    with pytest.raises(A.SchemaRefused, match="forbidden statement"):
        A.validate_schema_sql("CREATE TABLE a (id int);\n" + statement + "\n" + TRAILER)


@pytest.mark.parametrize("line", ["\\connect other", "\\! rm -rf /", "\\copy t from 'x'", "\\restrict bad key"])
def test_any_other_psql_meta_command_is_refused(line):
    with pytest.raises(A.SchemaRefused, match="meta-command"):
        A.validate_schema_sql("CREATE TABLE a (id int);\n" + line + "\n" + TRAILER)


def test_a_truncated_schema_is_refused():
    with pytest.raises(A.SchemaRefused, match="trailer"):
        A.validate_schema_sql("CREATE TABLE a (id int);\n")


LEDGERS = (
    "\\restrict k1\nSET client_encoding = 'UTF8';\nSELECT pg_catalog.set_config('search_path', '', false);\n"
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('001_taxonomy.sql', '2026-01-01');\n"
    "INSERT INTO public.startup_sql_migrations (filename, checksum) VALUES ('001_a.sql', 'c1');\n"
    "SELECT pg_catalog.setval('public.startup_sql_migrations_id_seq', 4, true);\n\\unrestrict k1\n" + TRAILER
)


def test_ledger_inserts_pass_and_are_counted():
    sql, rows = A.validate_ledgers_sql(LEDGERS)
    assert rows == 2 and "\\restrict" not in sql


@pytest.mark.parametrize("bad", [
    "INSERT INTO public.orders (id) VALUES (1);",
    "INSERT INTO public.agents (api_key) VALUES ('k');",
    "SELECT pg_catalog.setval('public.orders_id_seq', 4, true);",
    "DELETE FROM public.schema_migrations;",
    "CREATE TABLE x (id int);",
    "INSERT INTO public.schema_migrations (id) VALUES ('a'); DROP TABLE orders;",
    "INSERT INTO public.schema_migrations (id) VALUES ('a'); DROP TABLE orders(x);",
])
def test_anything_else_in_the_ledger_file_is_refused(bad):
    with pytest.raises(A.SchemaRefused, match="ledger"):
        A.validate_ledgers_sql(LEDGERS.replace(TRAILER, "\n" + bad + "\n" + TRAILER))


def _drop(tmp_path, *, schema=OK_SCHEMA, ledgers=LEDGERS, folder="20261125T2300Z", **manifest_changes):
    store, stamp = LocalDirStore(tmp_path), folder
    files = {}
    for part, text in (("schema", schema), ("ledgers", ledgers)):
        data = gzip.compress(text.encode())
        store.put("%s/%s.sql.gz" % (stamp, part), data)
        files[part] = {"file": "%s/%s.sql.gz" % (stamp, part), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {"format_version": 1, "kind": "schema", "stamp": stamp, "tables_live": 1, "ledger_rows": 2,
                "files": files}
    manifest.update(manifest_changes)
    store.put("%s/_MANIFEST.json" % stamp, json.dumps(manifest).encode())
    return store


def test_the_newest_complete_dump_is_loaded(tmp_path):
    store = _drop(tmp_path)
    store.put("20261126T0000Z/schema.sql.gz", b"partial, no manifest")
    stamp, manifest, schema, ledgers = A._load(store, None)
    assert stamp == "20261125T2300Z" and "CREATE TABLE public.t" in schema


@pytest.mark.parametrize("changes,match", [
    ({"kind": "catalog"}, "schema manifest"),
    ({"format_version": 2}, "schema manifest"),
    ({"stamp": "20261124T0000Z"}, "stamp does not match"),
])
def test_a_wrong_manifest_is_refused(tmp_path, changes, match):
    with pytest.raises(A.SchemaRefused, match=match):
        A._load(_drop(tmp_path, **changes), None)


def test_a_tampered_file_is_refused(tmp_path):
    store = _drop(tmp_path)
    (tmp_path / "20261125T2300Z" / "schema.sql.gz").write_bytes(gzip.compress(b"CREATE ROLE evil;" + TRAILER.encode()))
    with pytest.raises(A.SchemaRefused, match="sha256"):
        A._load(store, None)


def test_a_file_outside_its_stamp_is_refused(tmp_path):
    store = _drop(tmp_path)
    path = tmp_path / "20261125T2300Z" / "_MANIFEST.json"
    manifest = json.loads(path.read_text())
    manifest["files"]["schema"]["file"] = "20261124T0000Z/schema.sql.gz"
    path.write_text(json.dumps(manifest))
    with pytest.raises(A.SchemaRefused, match="outside its stamp"):
        A._load(store, None)


def test_no_dump_at_all_is_refused(tmp_path):
    with pytest.raises(A.SchemaRefused, match="no complete schema dump"):
        A._load(LocalDirStore(tmp_path), None)


def _env(**changes):
    env = dict(GOOD, DATABASE_URL="postgresql://u:pw@198.51.100.5:5432/pivota", CLOUD_RUN_JOB="sandbox-schema-apply",
               SANDBOX_SCHEMA_GCS_URI="gs://pivota-sandbox-catalog-drop/schema")
    env.pop("K_SERVICE", None)
    env.update({k: v for k, v in changes.items() if v is not None})
    for k, v in changes.items():
        if v is None:
            env.pop(k, None)
    return env


def test_the_sandbox_with_its_own_db_passes():
    A.check_environment(_env())


@pytest.mark.parametrize("changes,match", [
    ({"PIVOTA_ENV": "production"}, "only in the sandbox"),
    ({"PIVOTA_ENV": "staging"}, "only in the sandbox"),
    ({"DATABASE_URL": "postgresql://u:pw@192.0.2.25/pivota"}, "sandbox guard"),
    ({"DATABASE_URL": "postgresql://u:pw@203.0.113.70/pivota"}, "private range"),
    ({"GOOGLE_CLOUD_PROJECT": "pivota-staging-x"}, "sandbox guard"),
    ({"GOOGLE_CLOUD_PROJECT": "other-project"}, "GOOGLE_CLOUD_PROJECT"),
])
def test_it_refuses_anywhere_but_the_sandbox_db(changes, match):
    with pytest.raises(A.SchemaRefused, match=match) as excinfo:
        A.check_environment(_env(**changes))
    assert "pw" not in str(excinfo.value)


def test_main_refuses_with_exit_2_outside_the_sandbox(monkeypatch, capsys):
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    assert A.main([]) == 2
    assert "REFUSED" in capsys.readouterr().err


async def test_a_ledger_count_that_disagrees_with_the_manifest_is_refused_before_any_db_work(tmp_path):
    class NoDb:
        def __getattr__(self, name):
            raise AssertionError("touched the database")

    with pytest.raises(A.SchemaRefused, match="ledger file row count"):
        await A.apply_schema(NoDb(), _drop(tmp_path, ledger_rows=3))
