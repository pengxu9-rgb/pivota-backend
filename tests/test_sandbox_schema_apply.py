"""jobs/sandbox_schema_apply.py: the checks that need no database.

Includes every bypass the controller reproduced against the first (regex) re-check (D-3), each of
which must now be REFUSED by the tokenizer + statement allowlist.
"""

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
    "\\restrict abc123\n--\n-- PostgreSQL database dump\n--\n"
    "SET statement_timeout = 0;\nSET standard_conforming_strings = on;\n"
    "SELECT pg_catalog.set_config('search_path', '', false);\n"
    "CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;\n"
    "CREATE DOMAIN public.currency_code AS character(3) CONSTRAINT c CHECK ((VALUE ~ '^[A-Z]{3}$'::text));\n"
    "CREATE TABLE public.t (id integer NOT NULL, note text DEFAULT 'it''s; fine'::text);\n"
    "CREATE FUNCTION public.f() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n"
    "INSERT INTO public.audit VALUES (1);\nGRANT SELECT ON t TO x;\nRETURN NEW;\nEND $$;\n"
    "CREATE FUNCTION public.g() RETURNS text LANGUAGE sql AS $_$ SELECT 'COPY x; DO' $_$;\n"
    "/* a block /* nested */ comment */\n"
    "ALTER TABLE ONLY public.t ADD CONSTRAINT t_pkey PRIMARY KEY (id);\n\\unrestrict abc123" + TRAILER
)


def _schema(statement: str) -> str:
    return OK_SCHEMA.replace("\\unrestrict abc123", statement + "\n\\unrestrict abc123")


def test_a_clean_schema_passes_and_loses_only_the_meta_lines():
    sql = A.validate_schema_sql(OK_SCHEMA)
    assert "\\restrict" not in sql and "\\unrestrict" not in sql
    assert "INSERT INTO public.audit" in sql  # inside a function body: kept, and not a refusal
    assert "'^[A-Z]{3}$'" in sql and "it''s; fine" in sql


def test_statements_split_only_at_top_level_semicolons():
    parts = A.split_statements("CREATE TABLE a (x text DEFAULT ';');\nCREATE FUNCTION f() AS $$ a; b; $$;", "t")
    assert [sk.split()[1] for _, sk in parts] == ["TABLE", "FUNCTION"]


@pytest.mark.parametrize("statement,match", [
    # Each of these was ACCEPTED by the first re-check (controller, D-3).
    ("SELECT '$$';\nGRANT ALL ON t TO r;", "does not emit"),
    ("CREATE TABLE x (a text DEFAULT E'\\'');\nGRANT ALL ON t TO r;", "E'...'"),
    ("CREATE TABLE y (id int); GRANT ALL ON y TO r;", "does not emit"),
    ("DO $$ BEGIN EXECUTE 'CREATE USER MAPPING FOR u SERVER s'; END $$;", "does not emit"),
    ("SET ROLE postgres;", "SET ROLE"),
    ("SET SESSION AUTHORIZATION postgres;", "SET ROLE"),
    ("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO r;", "does not emit"),
    ("CREATE FOREIGN TABLE ft (a int) SERVER s OPTIONS (password 'p');", "does not emit"),
    # And the rest of the never-list.
    ("CREATE SERVER s FOREIGN DATA WRAPPER postgres_fdw;", "does not emit"),
    ("CREATE USER MAPPING FOR u SERVER s OPTIONS (password 'p');", "does not emit"),
    ("CREATE SUBSCRIPTION s CONNECTION 'host=x' PUBLICATION p;", "does not emit"),
    ("CREATE PUBLICATION p FOR ALL TABLES;", "does not emit"),
    ("CREATE ROLE r LOGIN PASSWORD 'p';", "does not emit"),
    ("ALTER ROLE r PASSWORD 'p';", "does not emit"),
    ("ALTER SYSTEM SET x = 1;", "does not emit"),
    ("GRANT ALL ON t TO r;", "does not emit"),
    ("REVOKE ALL ON t FROM r;", "does not emit"),
    ("COPY public.t (id) FROM stdin;", "does not emit"),
    ("INSERT INTO public.t VALUES (1);", "does not emit"),
    ("COMMENT ON TABLE public.t IS 'x';", "does not emit"),
    ("create database other;", "does not emit"),
    ("ALTER TABLE public.t OWNER TO someone;", "ownership"),
    ("SET standard_conforming_strings = off;", "standard_conforming_strings"),
    ("CREATE EXTENSION dblink;", "connects out"),
    ("CREATE EXTENSION postgres_fdw;", "connects out"),
    ("CREATE EXTENSION pg_cron;", "connects out"),
    ("CREATE FUNCTION h() RETURNS int LANGUAGE plpython3u AS $$ return 1 $$;", "untrusted language"),
    ("CREATE TABLE z (a text DEFAULT U&'\\0041');", "U&"),
    ("CREATE TABLE z (a text DEFAULT 'unterminated);", "unterminated literal"),
    ("CREATE FUNCTION k() AS $x$ never closed;", "unterminated"),
    ("CREATE TABLE z (a int)", "no ';'|meta-command inside a statement"),
])
def test_every_known_bypass_and_forbidden_kind_is_refused(statement, match):
    with pytest.raises(A.SchemaRefused, match=match):
        A.validate_schema_sql(_schema(statement))


@pytest.mark.parametrize("literal", [
    "'owner@person.example'", "'sk_live_abc123'", "'shpat_0123'", "'" + "a1" * 16 + "'",
    "'" + "QUJD" * 11 + "'", "'xoxb-123'", "'ghp_abc'",
])
def test_a_literal_that_looks_like_a_secret_or_a_person_is_refused(literal):
    with pytest.raises(A.SchemaRefused, match="secret or a person"):
        A.validate_schema_sql(_schema("CREATE TABLE s (a text DEFAULT %s);" % literal))
    body = "CREATE FUNCTION s() RETURNS text LANGUAGE sql AS $$ SELECT %s $$;" % literal
    with pytest.raises(A.SchemaRefused, match="secret or a person"):
        A.validate_schema_sql(_schema(body))


@pytest.mark.parametrize("line", ["\\connect other", "\\! rm -rf /", "\\copy t from 'x'", "\\restrict bad key"])
def test_any_other_psql_meta_command_is_refused(line):
    with pytest.raises(A.SchemaRefused, match="meta-command"):
        A.validate_schema_sql(_schema(line))


def test_a_backslash_line_inside_a_function_body_is_body_text_not_a_meta_command():
    A.validate_schema_sql(_schema("CREATE FUNCTION r() RETURNS text LANGUAGE sql AS $$\n\\d\nSELECT 1 $$;"))


def test_a_truncated_schema_is_refused():
    with pytest.raises(A.SchemaRefused, match="trailer"):
        A.validate_schema_sql("CREATE TABLE a (id int);\n")


LEDGERS = (
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('001_taxonomy.sql', '2026-01-01 00:00:00+00');\n"
    "INSERT INTO public.startup_sql_migrations (filename, checksum, applied_at) VALUES ('001_a.sql', 'c1', '2026-01-01 00:00:00+00');\n"
    + A.LEDGER_TRAILER + "\n"
)


def test_ledger_inserts_pass_and_are_counted():
    sql, rows = A.validate_ledgers_sql(LEDGERS)
    assert rows == 2 and "schema_migrations" in sql


@pytest.mark.parametrize("bad", [
    "SET x = 0; GRANT ALL ON t TO r;",  # the controller's .* span
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ((SELECT secret FROM vault), now());",
    "SET role postgres;",
    "INSERT INTO public.orders (id) VALUES ('1');",
    "INSERT INTO public.agents (api_key) VALUES ('k');",
    "INSERT INTO public.schema_migrations (id, applied_at, extra) VALUES ('a', 'b', 'c');",
    "INSERT INTO public.schema_migrations (id) VALUES ('a'); DROP TABLE orders;",
    "DELETE FROM public.schema_migrations;",
    "SELECT pg_catalog.setval('public.orders_id_seq', 4, true);",
])
def test_anything_else_in_the_ledger_file_is_refused(bad):
    with pytest.raises(A.SchemaRefused, match="ledger"):
        A.validate_ledgers_sql(bad + "\n" + LEDGERS)


def test_a_ledger_file_without_its_trailer_is_refused():
    with pytest.raises(A.SchemaRefused, match="trailer"):
        A.validate_ledgers_sql(LEDGERS.replace(A.LEDGER_TRAILER, ""))


def _drop(tmp_path, *, schema=OK_SCHEMA, ledgers=LEDGERS, folder="20261125T2300Z", **manifest_changes):
    store, stamp = LocalDirStore(tmp_path), folder
    files = {}
    for part, text in (("schema", schema), ("ledgers", ledgers)):
        data = gzip.compress(text.encode())
        store.put("%s/%s.sql.gz" % (stamp, part), data)
        files[part] = {"file": "%s/%s.sql.gz" % (stamp, part), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {"format_version": A.FORMAT_VERSION, "kind": "schema", "stamp": stamp, "tables_live": 1,
                "ledger_rows": 2, "files": files}
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
    ({"format_version": 1}, "schema manifest"),
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


async def test_a_ledger_count_that_disagrees_with_the_manifest_is_refused_before_any_db_work(tmp_path):
    class NoDb:
        def __getattr__(self, name):
            raise AssertionError("touched the database")

    with pytest.raises(A.SchemaRefused, match="ledger file row count"):
        await A.apply_schema(NoDb(), _drop(tmp_path, ledger_rows=3))


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


def test_a_statement_with_no_semicolon_at_the_very_end_is_refused():
    with pytest.raises(A.SchemaRefused, match="no ';'"):
        A.validate_schema_sql(OK_SCHEMA.replace(TRAILER, "\nCREATE TABLE z (a int)" + TRAILER))


def test_a_doubled_quote_stays_inside_its_literal():
    parts = A.split_statements("CREATE TABLE a (x text DEFAULT 'it''s; GRANT'); CREATE TABLE b (y int);", "t")
    assert [sk for _, sk in parts] == ["CREATE TABLE A (X TEXT DEFAULT '?')", "CREATE TABLE B (Y INT)"]
