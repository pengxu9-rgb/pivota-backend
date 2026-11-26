"""jobs/sandbox_schema_apply.py: the checks that need no database.

Includes every bypass the controller reproduced against the regex re-check (D-3) and against the text
tokenizer (the 77a881c7f review): each must be REFUSED by PostgreSQL's own parser + the node allowlist.
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


def test_a_clean_schema_passes_and_loses_only_the_meta_lines_and_the_skipped_sets():
    plan = A.validate_schema_sql(OK_SCHEMA)
    sql = "\n".join(plan.statements)
    assert "\\restrict" not in sql and "\\unrestrict" not in sql
    assert "INSERT INTO public.audit" in sql  # inside a function body: kept, and not a refusal
    assert "'^[A-Z]{3}$'" in sql and "it''s; fine" in sql
    # pg_dump's zero timeouts and scs=on are checked, never executed: the apply sets its own.
    assert "statement_timeout" not in sql and "standard_conforming_strings" not in sql
    assert "set_config('search_path', '', false)" in sql
    assert plan.extensions == frozenset({"pgcrypto"}) and plan.checked == len(plan.statements) + 2


def test_statements_split_only_at_top_level_semicolons():
    parts = A._parse("CREATE TABLE a (x text DEFAULT ';');\nCREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ a; b; $$;", "t")
    assert [type(node).__name__ for node, _ in parts] == ["CreateStmt", "CreateFunctionStmt"]
    assert parts[1][1].endswith("$$ a; b; $$")


@pytest.mark.parametrize("statement,match", [
    # Each of these was ACCEPTED by the first re-check (controller, D-3).
    ("SELECT '$$';\nGRANT ALL ON t TO r;", "outside pg_dump's preamble"),
    ("CREATE TABLE x (a text DEFAULT E'\\'');\nGRANT ALL ON t TO r;", "does not emit \\(GrantStmt\\)"),
    ("CREATE TABLE y (id int); GRANT ALL ON y TO r;", "does not emit \\(GrantStmt\\)"),
    ("DO $$ BEGIN EXECUTE 'CREATE USER MAPPING FOR u SERVER s'; END $$;", "does not emit \\(DoStmt\\)"),
    ("SET ROLE postgres;", "outside pg_dump's preamble"),
    ("SET SESSION AUTHORIZATION postgres;", "outside pg_dump's preamble"),
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
    ("CREATE EVENT TRIGGER e ON ddl_command_start EXECUTE FUNCTION f();", "does not emit"),
    ("CREATE RULE r AS ON INSERT TO public.t DO INSTEAD NOTHING;", "does not emit \\(RuleStmt\\)"),
    ("SECURITY LABEL ON TABLE public.t IS 'x';", "does not emit"),
    ("ALTER TABLE public.t OWNER TO someone;", "ownership"),
    ("ALTER TYPE public.k OWNER TO someone;", "ownership"),
    ("ALTER DOMAIN public.currency_code OWNER TO someone;", "ownership"),
    ("ALTER FUNCTION public.f() OWNER TO someone;", "ownership"),
    ("ALTER FOREIGN TABLE ft ADD COLUMN b int;", "ALTER of a OBJECT_FOREIGN_TABLE"),
    ("ALTER TABLE public.t SET TABLESPACE other;", "ALTER TABLE AT_SetTableSpace"),
    ("SET standard_conforming_strings = off;", "outside pg_dump's preamble"),
    ("SET check_function_bodies = true;", "outside pg_dump's preamble"),
    ("SET search_path = evil, public;", "outside pg_dump's preamble"),
    ("SET LOCAL lock_timeout = 0;", "outside pg_dump's preamble"),
    ("RESET ALL;", "outside pg_dump's preamble"),
    ("CREATE FUNCTION h() RETURNS int LANGUAGE plpython3u AS $$ return 1 $$;", "language other than"),
    ("CREATE TYPE public.b (INPUT = f, OUTPUT = g);", "needs C code"),
    ("CREATE TABLE z (a text DEFAULT 'unterminated);", "does not (scan|parse)"),
    ("CREATE FUNCTION k() AS $x$ never closed;", "does not (scan|parse)"),
    ("CREATE TABLE z (a int)", "no ';'"),
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


def test_a_restrict_line_inside_a_body_is_refused_never_blanked():
    with pytest.raises(A.SchemaRefused, match="inside a statement"):
        A.validate_schema_sql("CREATE FUNCTION r() RETURNS text LANGUAGE sql AS $$\n\\restrict zz\nSELECT 1 $$;" + TRAILER)
    with pytest.raises(A.SchemaRefused, match="more than one"):  # and with pg_dump's own pair around it
        A.validate_schema_sql(_schema("CREATE FUNCTION r() RETURNS text LANGUAGE sql AS $$\n\\restrict zz\nSELECT 1 $$;"))


@pytest.mark.parametrize("key", ["9abc", "0x1F", "1e5x", "abc9", "7"])
def test_pg_dumps_random_restrict_key_never_breaks_the_scan(key):
    # The key is random per dump; a digit-led one is "trailing junk" to the scanner if left in place.
    text = OK_SCHEMA.replace("\\restrict abc123", "\\restrict " + key).replace("\\unrestrict abc123", "\\unrestrict " + key)
    assert A.validate_schema_sql(text).statements


def test_more_than_one_restrict_pair_is_refused():
    with pytest.raises(A.SchemaRefused, match="more than one"):
        A.validate_schema_sql(_schema("\\restrict k2\nCREATE TABLE q (a int);\n\\unrestrict k2"))


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
    assert rows == 2 and len(sql) == 2 and "schema_migrations" in sql[0]


def test_a_null_ledger_value_is_accepted_and_an_empty_ledger_is_zero_rows():
    sql, rows = A.validate_ledgers_sql("INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', NULL);\n"
                                       + A.LEDGER_TRAILER + "\n")
    assert rows == 1
    assert A.validate_ledgers_sql(A.LEDGER_TRAILER + "\n") == ((), 0)


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
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', 'b'), ('c', 'd');",
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', 'b') ON CONFLICT DO NOTHING;",
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', 'b') RETURNING id;",
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', now());",
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', 'b'::text);",
    "INSERT INTO other.schema_migrations (id, applied_at) VALUES ('a', 'b');",
    "WITH x AS (DELETE FROM public.agents) INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', 'b');",
    # The controller's scs-off desync: with the parser this is one INSERT whose values are not two literals.
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('\\', ', now()); CREATE ROLE x; SELECT ') -- '\n;",
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
                "ledger_rows": 2, "pg_dump_version": "17.6", "server_version_num": 170006, "files": files}
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
    parts = A._parse("CREATE TABLE a (x text DEFAULT 'it''s; GRANT'); CREATE TABLE b (y int);", "t")
    assert [sql for _, sql in parts] == ["CREATE TABLE a (x text DEFAULT 'it''s; GRANT')", "CREATE TABLE b (y int)"]


# --- the 77a881c7f review: nine bypasses of the text tokenizer, now read by PostgreSQL's parser -------
MARK = "rv_pwned_role"
NINE_BYPASSES = [
    # 1. \r ends a -- comment for PostgreSQL.
    ("cr_line_comment", "CREATE TABLE t1 (a int);\n-- note\rCREATE ROLE %s;\nCREATE TABLE t2 (b int);" % MARK,
     "carriage return"),
    # 2. `$` and bytes >= 0x80 are identifier characters.
    ("dollar_in_identifier", "CREATE TABLE t1 (c$$$ int);\nCREATE ROLE %s;\nCREATE TABLE t2 (d$$ int);" % MARK,
     "CreateRoleStmt"),
    ("dollar_tag_in_identifier", "CREATE TABLE t1 (c$$q$ int);\nCREATE ROLE %s;\nCREATE TABLE t2 (d$q$ int);" % MARK,
     "CreateRoleStmt"),
    ("nonascii_before_dollar", "CREATE TABLE t1 (c€$$ int);\nCREATE ROLE %s;\nCREATE TABLE t2 (d$$ int);" % MARK,
     "CreateRoleStmt"),
    # 3. A non-ASCII dollar tag.
    ("nonascii_dollar_tag", "SET check_function_bodies = false;\nCREATE FUNCTION public.h() RETURNS int LANGUAGE sql "
     "AS $é$ SELECT 1 /* $é$; CREATE ROLE %s; -- */ $é$;\nCREATE TABLE t2 (b int);" % MARK, "CreateRoleStmt"),
    # 5. CREATE SCHEMA with schema elements.
    ("create_schema_grant", "CREATE SCHEMA s1 GRANT ALL ON SCHEMA public TO PUBLIC;", "carries elements"),
    ("create_schema_table", "CREATE SCHEMA s1 CREATE TABLE x (a int);", "carries elements"),
    ("create_schema_authorization", "CREATE SCHEMA s1 AUTHORIZATION postgres;", "AUTHORIZATION"),
    # 6. set_config hiding the setting name; SET session_authorization.
    ("set_config_role", "SELECT pg_catalog.set_config('role', 'pg_monitor', false);", "outside pg_dump's preamble"),
    ("set_config_scs_off", "SELECT pg_catalog.set_config('standard_conforming_strings', 'off', false);",
     "outside pg_dump's preamble"),
    ("set_session_authorization_guc", "SET session_authorization = 'postgres';", "outside pg_dump's preamble"),
    # 7. Code that runs at apply time.
    ("ctas_runs_function", "CREATE FUNCTION public.f() RETURNS int LANGUAGE plpgsql AS $$BEGIN EXECUTE "
     "'CREATE ROLE %s'; RETURN 1; END$$;\nCREATE TABLE public.t AS SELECT public.f() AS x;" % MARK, "CREATE TABLE AS"),
    ("matview_runs_function", "CREATE FUNCTION public.f() RETURNS int LANGUAGE plpgsql AS $$BEGIN EXECUTE "
     "'CREATE ROLE %s'; RETURN 1; END$$;\nCREATE MATERIALIZED VIEW public.mv AS SELECT public.f() AS x;" % MARK,
     "WITH DATA"),
    ("ledger_trigger", "CREATE TABLE public.schema_migrations (id text, applied_at text);\nCREATE FUNCTION public.trg() "
     "RETURNS trigger LANGUAGE plpgsql AS $$BEGIN EXECUTE 'CREATE ROLE %s'; RETURN NEW; END$$;\nCREATE TRIGGER x "
     "BEFORE INSERT ON public.schema_migrations FOR EACH ROW EXECUTE FUNCTION public.trg();" % MARK, "ledger table"),
    ("rule", "CREATE TABLE public.t9 (a int);\nCREATE RULE r AS ON INSERT TO public.t9 DO ALSO SELECT 1;", "RuleStmt"),
    # 8. Quoted language names.
    ("language_quoted_c", "CREATE FUNCTION public.g(int) RETURNS int LANGUAGE \"c\" AS 'int4inc', 'int4inc';",
     "language other than"),
    ("language_string_c", "CREATE FUNCTION public.g(int) RETURNS int LANGUAGE 'c' AS 'int4inc', 'int4inc';",
     "language other than"),
    ("language_internal_quoted", "CREATE FUNCTION public.g(int) RETURNS int LANGUAGE \"internal\" AS 'int4inc';",
     "language other than"),
    # 9. OWNER TO on any kind.
    ("alter_sequence_owner", "CREATE SEQUENCE public.s;\nALTER SEQUENCE public.s OWNER TO pg_monitor;", "ownership"),
    ("alter_view_owner", "ALTER VIEW public.v OWNER TO pg_monitor;", "ownership"),
    ("alter_index_owner", "ALTER INDEX public.i OWNER TO pg_monitor;", "ownership"),
    # And the reviewer's extras.
    ("nul_byte", "CREATE TABLE t1 (a int);\x00CREATE ROLE %s;" % MARK, "NUL byte"),
    ("bom", "﻿CREATE TABLE t1 (a int);", "byte-order mark"),
    ("digit_before_dollar", "CREATE TABLE t1 (a int DEFAULT 1$a$);", "does not (scan|parse)"),
]


@pytest.mark.parametrize("name,statement,match", NINE_BYPASSES, ids=[c[0] for c in NINE_BYPASSES])
def test_every_tokenizer_bypass_is_refused_by_the_parser(name, statement, match):
    with pytest.raises(A.SchemaRefused, match=match):
        A.validate_schema_sql(_schema(statement))


def test_a_u_escaped_extension_name_is_read_as_the_server_reads_it():
    # 4. U&"dbl\0069nk" is dblink. The parser names it; the apply then refuses it, because extensions are
    # pre-created by the admin and dblink is not one of them (tested against Postgres).
    plan = A.validate_schema_sql(_schema('CREATE EXTENSION U&"dbl\\0069nk";'))
    assert plan.extensions == frozenset({"pgcrypto", "dblink"})


def test_the_dumps_zero_timeouts_are_checked_but_never_executed():
    plan = A.validate_schema_sql(_schema("SET lock_timeout = 0;\nSET idle_in_transaction_session_timeout = 0;\n"
                                         "SET transaction_timeout = 0;"))
    assert not any("timeout" in statement for statement in plan.statements)


def test_a_create_table_as_is_refused_but_a_matview_with_no_data_is_kept():
    plan = A.validate_schema_sql(_schema("CREATE MATERIALIZED VIEW public.mv AS SELECT 1 AS x WITH NO DATA;"))
    assert any("WITH NO DATA" in statement for statement in plan.statements)


def test_a_trigger_on_a_table_that_is_not_a_ledger_is_kept():
    plan = A.validate_schema_sql(_schema("CREATE TRIGGER t BEFORE INSERT ON public.t FOR EACH ROW EXECUTE FUNCTION public.f();"))
    assert any(statement.startswith("CREATE TRIGGER") for statement in plan.statements)


def test_importing_the_web_worker_and_the_copy_job_does_not_load_the_parser():
    """pglast is for this job only (controller): main (web and worker run `uvicorn main:app`), the catalog
    copy and this module itself import without it; only validating a dump loads it."""
    import subprocess
    import sys as _sys

    code = ("import sys, main, jobs.sandbox_catalog_copy, jobs.sandbox_schema_apply, config.sandbox_guard; "
            "assert 'pglast' not in sys.modules, sorted(m for m in sys.modules if m.startswith('pglast'))")
    done = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]


def test_the_offline_check_reports_ok_and_refusals(tmp_path, capsys):
    schema, ledgers = tmp_path / "schema.sql", tmp_path / "ledgers.sql"
    schema.write_text(OK_SCHEMA)
    ledgers.write_text(LEDGERS)
    assert A.main(["check", str(schema), str(ledgers)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and '"ledger_rows": 2' in out and '"pgcrypto"' in out
    schema.write_text(_schema("GRANT ALL ON t TO r;"))
    assert A.main(["check", str(schema), str(ledgers)]) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert A.main(["check", str(schema)]) == 2


# --- the rework's re-review (controller B1, B3) ----------------------------------------------------
B1_FUNCTION = ("CREATE FUNCTION public.f() RETURNS int LANGUAGE plpgsql AS $$BEGIN EXECUTE 'GRANT ALL ON TABLE "
               "public.victim TO PUBLIC'; RETURN 1; END$$;\n")


@pytest.mark.parametrize("statement", [
    # The reviewer's reproduction: a partition bound runs the dump's function at create time.
    B1_FUNCTION + "CREATE TABLE public.p (a int) PARTITION BY LIST (a);\n"
    "CREATE TABLE public.p1 PARTITION OF public.p FOR VALUES IN (public.f());",
    B1_FUNCTION + "ALTER TABLE ONLY public.p ATTACH PARTITION public.p1 FOR VALUES IN (public.f());",
    "CREATE TABLE public.r1 PARTITION OF public.r FOR VALUES FROM (pg_catalog.now()) TO (MAXVALUE);",
    "CREATE TABLE public.r1 PARTITION OF public.r FOR VALUES FROM ((SELECT 1)) TO (2);",
    "CREATE TABLE public.r1 PARTITION OF public.r FOR VALUES FROM (1 + 1) TO (5);",
])
def test_a_partition_bound_must_be_a_constant(statement):
    with pytest.raises(A.SchemaRefused, match="partition bound that is not a constant"):
        A.validate_schema_sql(_schema(statement))


@pytest.mark.parametrize("statement", [
    "CREATE TABLE public.l1 PARTITION OF public.l FOR VALUES IN ('a', 'b');",
    "CREATE TABLE public.l2 PARTITION OF public.l FOR VALUES IN (-5, 7);",
    "CREATE TABLE public.l3 PARTITION OF public.l DEFAULT;",
    "CREATE TABLE public.r1 PARTITION OF public.r FOR VALUES FROM (MINVALUE) TO ('2024-01-01'::date);",
    "CREATE TABLE public.r2 PARTITION OF public.r FOR VALUES FROM ('2024-01-01') TO (MAXVALUE);",
    "ALTER TABLE ONLY public.p ATTACH PARTITION public.p1 FOR VALUES IN (true);",
    "CREATE TABLE public.h1 PARTITION OF public.h FOR VALUES WITH (modulus 4, remainder 0);",
])
def test_the_bounds_pg_dump_writes_are_kept(statement):
    assert A.validate_schema_sql(_schema(statement)).statements


# --- the 8155964d4 review: no dump-defined code is reachable at DDL time, by any route -------------------
DOMAIN_F = ("CREATE FUNCTION public.f(int) RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$BEGIN "
            "PERFORM public.g(); RETURN true; END$$;\n")


@pytest.mark.parametrize("statement", [
    # Each is a position the server evaluates (or constant-folds) while the statement runs.
    "CREATE INDEX i ON public.t ((public.f()));",
    "CREATE INDEX i ON public.t (a) WHERE (public.g() > 1);",
    "CREATE INDEX i ON public.t ((1 OPERATOR(public.===) 2));",
    "CREATE INDEX i ON public.t (((public.f())::public.k));",
    "CREATE TABLE public.q (a int) PARTITION BY RANGE ((public.f()));",
    "CREATE STATISTICS s ON (public.f()), (a) FROM public.t;",
    "ALTER TABLE public.t ADD CONSTRAINT x EXCLUDE USING gist (a WITH =) WHERE ((public.g() > 1));",
    # ... and those it does not evaluate today, refused anyway: no case analysis to get wrong.
    "CREATE INDEX i ON public.t USING btree (public.f(a));",
    "CREATE TABLE public.d (a int DEFAULT public.f());",
    "CREATE TABLE public.c (a int CHECK (public.positive(a)));",
    "CREATE TABLE public.c (a int GENERATED ALWAYS AS (public.f(a)) STORED);",
    "CREATE POLICY p ON public.t USING (public.f(a));",
    "CREATE TRIGGER t BEFORE INSERT ON public.t FOR EACH ROW WHEN (public.f(1)) EXECUTE FUNCTION public.tr();",
    "CREATE FUNCTION public.h(x int DEFAULT public.f(1)) RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
    "ALTER TABLE ONLY public.t ALTER COLUMN a SET DEFAULT public.f();",
    "CREATE TABLE public.c (a int CHECK (a = ANY (ARRAY[1]) AND a OPERATOR(public.<) 2));",
])
def test_a_dump_function_or_operator_in_any_expression_position_is_refused(statement):
    with pytest.raises(A.SchemaRefused, match="not pg_catalog's"):
        A.validate_schema_sql(_schema(statement))


@pytest.mark.parametrize("statement", [
    # A1: array_in -> domain_in runs the domain's CHECK while the DEFAULT is parsed.
    "CREATE TABLE public.t1 (a int[] DEFAULT '{1}'::public.d[]);",
    # A1': a composite's input runs its columns' inputs; in an index expression.
    "CREATE INDEX i ON public.t ((('(1)'::public.comp IS NOT NULL)));",
    "CREATE TABLE public.t1 (a int DEFAULT CAST('1' AS public.d));",
    "CREATE TABLE public.t1 (a int CHECK (a = (public.d '1')));",  # a typed literal is a cast
])
def test_a_cast_to_a_domain_or_composite_in_an_expression_is_refused(statement):
    with pytest.raises(A.SchemaRefused, match="cast to a type that is not pg_catalog's or an enum"):
        A.validate_schema_sql(_schema(statement))


@pytest.mark.parametrize("statement,match", [
    # A1/A5 at their root: a domain whose CHECK calls a dump function is refused where it is created, so
    # no input function of any type the dump creates can reach one (a bare literal coerced to a domain or
    # composite column, a bound coerced to a domain key).
    (DOMAIN_F + "CREATE DOMAIN public.d AS integer CONSTRAINT c CHECK (public.f(VALUE));", "not pg_catalog's"),
    ("CREATE DOMAIN public.d AS text CONSTRAINT c CHECK ((pg_catalog.query_to_xml('select 1', true, true, '') "
     "IS NOT NULL));", "runs SQL"),
    ("CREATE DOMAIN public.d AS text CONSTRAINT c CHECK ((pg_catalog.ts_stat('select 1') IS NOT NULL));", "runs SQL"),
    ("CREATE TABLE public.c (a text CHECK (pg_catalog.domain_in(a::cstring, 0::oid, -1) IS NOT NULL));", "runs SQL"),
    ("CREATE TABLE public.c (a text DEFAULT pg_catalog.set_config('role', 'x', true));", "runs SQL"),
    # A2: an aggregate's INITCOND runs its STYPE's input.
    ("CREATE AGGREGATE public.agg(integer) (SFUNC = int4pl, STYPE = public.d, INITCOND = '1');",
     "CREATE AGGREGATE that is not reviewed"),
    ("CREATE AGGREGATE public.agg(integer) (SFUNC = int4pl, STYPE = integer);", "CREATE AGGREGATE that is not reviewed"),
    # A3: ALTER DOMAIN ADD CONSTRAINT runs the new CHECK over the domain's uses.
    ("ALTER DOMAIN public.currency_code ADD CONSTRAINT c2 CHECK (public.f(1));", "ALTER DOMAIN"),
    ("ALTER DOMAIN public.currency_code ADD CONSTRAINT c2 CHECK ((VALUE IS NOT NULL));", "ALTER DOMAIN"),
    # A4: a validated CHECK added by ALTER is planned (constant-folded) on the spot, even on an empty table.
    ("ALTER TABLE ONLY public.t ADD CONSTRAINT c CHECK ((id > 0));", "named NOT VALID form"),
    ("ALTER TABLE ONLY public.t ADD CHECK ((id > 0)) NOT VALID;", "named NOT VALID form"),
    ("ALTER TABLE ONLY public.t ADD CONSTRAINT c CHECK (public.f(1)) NOT VALID;", "not pg_catalog's"),
    # A5: a partition key on a domain column (the bound is coerced to it).
    ("CREATE TABLE public.p (a public.d) PARTITION BY LIST (a);", "partition key column"),
    ("CREATE TABLE public.p (a integer) PARTITION BY LIST (b);", "partition key column"),
    # A6: ATTACH of a table whose CHECK calls a dump function: refused where that table is created.
    (DOMAIN_F + "CREATE TABLE public.p1 (a integer CONSTRAINT k CHECK (public.f(1)));\n"
     "ALTER TABLE ONLY public.p ATTACH PARTITION public.p1 FOR VALUES IN (1);", "not pg_catalog's"),
    # Ways to reach a dump function without naming it.
    ("CREATE OPERATOR public.=== (FUNCTION = public.f, LEFTARG = integer, RIGHTARG = integer);", "CREATE OBJECT_OPERATOR"),
    ("CREATE CAST (integer AS public.d) WITH FUNCTION public.f(integer) AS IMPLICIT;", "CREATE CAST"),
    ("CREATE TYPE public.r AS RANGE (subtype = integer, subtype_diff = public.f);", "canonical or subtype_diff"),
    ("CREATE TYPE public.r AS RANGE (subtype = integer, canonical = public.f);", "canonical or subtype_diff"),
])
def test_every_ddl_time_route_to_dump_code_is_refused(statement, match):
    with pytest.raises(A.SchemaRefused, match=match):
        A.validate_schema_sql(_schema(statement))


@pytest.mark.parametrize("statement", [
    # Audit bypass 1: a temp relation is searched before pg_catalog.
    "CREATE TEMPORARY VIEW pg_roles AS SELECT (0)::oid AS oid, ('x')::name AS rolname WHERE false;",
    "CREATE TEMP TABLE pg_attrdef (adrelid oid);",
    "CREATE TEMPORARY SEQUENCE public_s;",
    "CREATE VIEW pg_temp.pg_class AS SELECT 1 AS oid;",
    "CREATE TABLE pg_temp_3.x (a int);",
    "CREATE FUNCTION pg_temp.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
    "CREATE TABLE public.t2 (a int DEFAULT pg_temp.f());",
    "CREATE UNLOGGED TABLE public.u (a int);\nCREATE TEMP TABLE x (a int) ON COMMIT DROP;",
])
def test_temp_objects_and_pg_temp_names_are_refused(statement):
    with pytest.raises(A.SchemaRefused, match="TEMPORARY object or a pg_temp name"):
        A.validate_schema_sql(_schema(statement))


@pytest.mark.parametrize("statement", [
    "CREATE TYPE public.offer_state AS ENUM ('live', 'paused');\n"
    "CREATE TABLE public.o (state public.offer_state DEFAULT 'live'::public.offer_state NOT NULL, b text);\n"
    "CREATE INDEX i ON public.o USING btree (lower(b)) WHERE (state = 'live'::public.offer_state);",
    "CREATE INDEX i ON public.t USING gin (b public.gin_trgm_ops);",  # an extension's opclass: not an expression
    "CREATE TABLE public.q (a int) PARTITION BY RANGE (a);",
    "CREATE TABLE public.d (a int DEFAULT nextval('public.s'::regclass), b text DEFAULT (gen_random_uuid())::text);",
    "CREATE TABLE public.c (a public.currency_code, b numeric CHECK ((b >= (0)::numeric)));",  # a domain COLUMN
    "ALTER TABLE ONLY public.t ADD CONSTRAINT c CHECK ((id > 0)) NOT VALID;",  # pg_dump's form
    # Not evaluated when created: a view's query, a function's body, a trigger's function.
    "CREATE VIEW public.v AS SELECT public.f() AS x, ('(1)'::public.comp) AS y;",
    "CREATE MATERIALIZED VIEW public.mv AS SELECT public.f() AS x WITH NO DATA;",
    "CREATE FUNCTION public.h() RETURNS int LANGUAGE sql RETURN (public.f() + 1);",
    "CREATE TRIGGER t BEFORE INSERT ON public.t FOR EACH ROW EXECUTE FUNCTION public.f();",
    "CREATE TABLE public.r (a text DEFAULT 'pg_temp'::text);",  # a literal is not a name
])
def test_what_pg_dump_writes_for_columns_enums_and_bodies_is_kept(statement):
    assert A.validate_schema_sql(_schema(statement)).statements


def test_a_reviewed_name_is_allowed_and_the_dump_may_not_define_it(monkeypatch):
    monkeypatch.setattr(A, "EXPRESSION_NAMES_ALLOWED", frozenset({"public.uuid_generate_v4"}))
    A.validate_schema_sql(_schema("CREATE TABLE public.u (a uuid DEFAULT public.uuid_generate_v4());"))
    with pytest.raises(A.SchemaRefused, match="reviewed extension function's name"):
        A.validate_schema_sql(_schema("CREATE FUNCTION public.uuid_generate_v4(int) RETURNS uuid LANGUAGE sql "
                                      "AS $$ SELECT NULL::uuid $$;"))
    monkeypatch.setattr(A, "AGGREGATES_ALLOWED", frozenset({"public.agg"}))
    A.validate_schema_sql(_schema("CREATE AGGREGATE public.agg(integer) (SFUNC = int4pl, STYPE = integer);"))


def test_the_plan_records_declared_row_security():
    plan = A.validate_schema_sql(_schema("ALTER TABLE public.t ENABLE ROW LEVEL SECURITY;\n"
                                         "ALTER TABLE public.t FORCE ROW LEVEL SECURITY;"))
    assert plan.row_security == {"public.t"} and plan.forced_row_security == {"public.t"}


def test_a_security_definer_function_is_refused_and_invoker_is_kept():
    with pytest.raises(A.SchemaRefused, match="SECURITY DEFINER"):
        A.validate_schema_sql(_schema("CREATE FUNCTION public.h() RETURNS int LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$;"))
    A.validate_schema_sql(_schema("CREATE FUNCTION public.h() RETURNS int LANGUAGE sql SECURITY INVOKER AS $$ SELECT 1 $$;"))


def test_the_plan_counts_the_kinds_the_audit_expects():
    plan = A.validate_schema_sql(_schema("CREATE TRIGGER t BEFORE INSERT ON public.t FOR EACH ROW EXECUTE FUNCTION public.f();\n"
                                         "CREATE POLICY p ON public.t USING (true);"))
    assert plan.count("CreateTrigStmt") == 1 and plan.count("CreatePolicyStmt") == 1 and plan.count("RuleStmt") == 0
