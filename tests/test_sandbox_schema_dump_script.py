"""scripts/sandbox/dump_prod_schema.sh: its prod-side refusals (controller D-2), driven through FAKE
pg_dump / psql binaries on PATH, so no database is needed and every refusal path is reached.

The fakes print what the test configures and log their argv and the libpq environment, which is how
the tests see that the password never reaches argv and that --lock-wait-timeout is passed.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sandbox/dump_prod_schema.sh"
STAMP = "20261125T2300Z"
PASSWORD = "pw-NEVER-on-argv"
DSN = "postgresql://dumper:%s@192.0.2.25:5432/pivota?sslmode=require" % PASSWORD
TRAILER = "\n--\n-- PostgreSQL database dump complete\n--\n"
GOOD_SCHEMA = (
    "--\n-- PostgreSQL database dump\n--\n\\restrict k1\nSET statement_timeout = 0;\n"
    "CREATE TABLE public.a (id integer);\nCREATE TABLE public.b (id integer);\n\\unrestrict k1" + TRAILER
)
LEDGER_ROWS = ("INSERT INTO public.schema_migrations (id, applied_at) VALUES ('001_taxonomy.sql', "
               "'2026-01-01 00:00:00+00');")

FAKE_PG_DUMP = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG/pg_dump.argv"
env | grep -E '^PG' | sort >> "$FAKE_LOG/pg_dump.env"
[ -n "${PGPASSFILE:-}" ] && cat "$PGPASSFILE" >> "$FAKE_LOG/pgpass"
if [ "$1" = "--version" ]; then echo "$FAKE_PG_DUMP_VERSION"; exit 0; fi
cat "$FAKE_SCHEMA"
"""
FAKE_PSQL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG/psql.argv"
case "$*" in
  *has_table_privilege*) echo "$FAKE_WRITABLE" ;;
  *server_version_num*) echo "$FAKE_SERVER_NUM" ;;
  *string_agg*) echo "$FAKE_LEDGERS" ;;
  *"from public.schema_migrations"*) cat "$FAKE_LEDGER_ROWS" ;;
  *"from public.startup_sql_migrations"*) : ;;
  *information_schema.tables*) echo "$FAKE_LIVE" ;;
esac
"""


@pytest.fixture
def harness(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("pg_dump", FAKE_PG_DUMP), ("psql", FAKE_PSQL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log"
    log.mkdir()

    def run(schema=GOOD_SCHEMA, ledger_rows=LEDGER_ROWS, live="2", ledgers="schema_migrations", **env_extra):
        (tmp_path / "schema.sql").write_text(schema)
        (tmp_path / "rows.sql").write_text(ledger_rows + ("\n" if ledger_rows else ""))
        env = {"PATH": "%s:/usr/bin:/bin" % bin_dir, "HOME": str(tmp_path), "PIVOTA_ENV": "production",
               "DATABASE_URL": DSN, "STAMP": STAMP, "SCHEMA_DUMP_LOCAL_OUT_DIR": str(tmp_path / "drop"),
               "FAKE_LOG": str(log), "FAKE_SCHEMA": str(tmp_path / "schema.sql"),
               "FAKE_LEDGER_ROWS": str(tmp_path / "rows.sql"), "FAKE_LIVE": live, "FAKE_LEDGERS": ledgers,
               "FAKE_PG_DUMP_VERSION": "pg_dump (PostgreSQL) 17.6 (Debian 17.6-1.pgdg120+1)",
               "FAKE_SERVER_NUM": "170006", "FAKE_WRITABLE": "none"}
        env.update(env_extra)
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    run.log = log
    run.drop = tmp_path / "drop"
    return run


def test_a_good_dump_is_written_with_the_dsn_never_on_argv(harness):
    done = harness()
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in (harness.drop / STAMP).iterdir()) == ["_MANIFEST.json", "ledgers.sql.gz", "schema.sql.gz"]
    argv = (harness.log / "pg_dump.argv").read_text() + (harness.log / "psql.argv").read_text()
    assert PASSWORD not in argv and "postgresql://" not in argv and "dumper" not in argv
    assert PASSWORD not in done.stdout + done.stderr
    assert "--lock-wait-timeout=10s" in (harness.log / "pg_dump.argv").read_text()
    env = (harness.log / "pg_dump.env").read_text()
    assert "PGHOST=192.0.2.25" in env and "PGUSER=dumper" in env and "PGSSLMODE=require" in env
    assert "PGAPPNAME=sandbox-schema-dump" in env and "statement_timeout=120s" in env
    assert PASSWORD not in env  # only in the passfile
    assert (harness.log / "pgpass").read_text().strip().endswith(":" + PASSWORD)


@pytest.mark.parametrize("statement", [
    "CREATE USER MAPPING FOR dumper SERVER other OPTIONS (user 'u', password 'p');",
    "CREATE SERVER other FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host 'x');",
    "GRANT SELECT ON public.a TO reader;",
    "REVOKE ALL ON public.a FROM PUBLIC;",
    "CREATE SUBSCRIPTION s CONNECTION 'host=x password=p' PUBLICATION p;",
    "create user mapping for u server s options (password 'p');",
    "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO r;",
    "SET ROLE postgres;",
    "DO $$ BEGIN NULL; END $$;",
    "CREATE FOREIGN TABLE ft (a int) SERVER s;",
    "COPY public.a (id) FROM stdin;",
])
def test_a_forbidden_statement_never_leaves_prod(harness, statement):
    done = harness(schema=GOOD_SCHEMA.replace("\\unrestrict k1", statement + "\n\\unrestrict k1"))
    assert done.returncode == 3, done.stdout + done.stderr
    assert "forbidden statement" in done.stderr and "'p'" not in done.stderr
    assert not harness.drop.exists()


@pytest.mark.parametrize("literal", ["'owner@person.example'", "'sk_live_abc'", "'shpat_x'", "'" + "ab" * 16 + "'"])
def test_a_literal_secret_never_leaves_prod(harness, literal):
    done = harness(schema=GOOD_SCHEMA.replace("\\unrestrict k1", "CREATE TABLE public.c (a text DEFAULT %s);\n\\unrestrict k1" % literal))
    assert done.returncode == 3 and "looks like a secret or a person" in done.stderr
    assert literal.strip("'") not in done.stdout + done.stderr
    assert not harness.drop.exists()


def test_a_truncated_schema_never_leaves_prod(harness):
    done = harness(schema=GOOD_SCHEMA.replace(TRAILER, "\n"))
    assert done.returncode == 3 and "completion trailer" in done.stderr
    assert not harness.drop.exists()


def test_a_table_shortfall_never_leaves_prod(harness):
    done = harness(live="3")  # the dump has 2 CREATE TABLEs, the "source" 3
    assert done.returncode == 3 and "fewer tables" in done.stderr
    assert not harness.drop.exists()


def test_an_unreadable_live_count_is_a_refusal(harness):
    done = harness(live="")
    assert done.returncode == 3 and "count the source" in done.stderr


@pytest.mark.parametrize("row", [
    "INSERT INTO public.schema_migrations (id, applied_at, extra) VALUES ('a', 'b', 'c');",
    "INSERT INTO public.orders (id) VALUES ('1');",
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES ('a', 'b'); DROP TABLE x;",
    "INSERT INTO public.schema_migrations (id, applied_at) VALUES (E'a', 'b');",
])
def test_a_ledger_line_that_is_not_an_explicit_column_insert_never_leaves_prod(harness, row):
    done = harness(ledger_rows=row)
    assert done.returncode == 3 and "ledger dump" in done.stderr
    assert not harness.drop.exists()


@pytest.mark.parametrize("meta", ["\\connect other", "\\! id"])
def test_a_foreign_meta_command_never_leaves_prod(harness, meta):
    done = harness(schema=GOOD_SCHEMA.replace("\\unrestrict k1", meta + "\n\\unrestrict k1"))
    assert done.returncode == 3 and "meta-command" in done.stderr


@pytest.mark.parametrize("env,match", [
    ({"PIVOTA_ENV": "sandbox"}, "PIVOTA_ENV=production"),
    ({"PIVOTA_ENV": ""}, "PIVOTA_ENV=production"),
    ({"STAMP": "now"}, "STAMP"),
    ({"SCHEMA_DUMP_LOCK_WAIT": "forever"}, "SCHEMA_DUMP_LOCK_WAIT"),
    ({"SCHEMA_DUMP_DEADLINE_SECONDS": "10m"}, "SCHEMA_DUMP_DEADLINE_SECONDS"),
    ({"DATABASE_URL": "postgresql://u:p@/db?host=/cloudsql/x"}, "not of the form"),
    ({"DATABASE_URL": "postgresql://u:p@h/db?sslmode=require&host=/tmp"}, "not of the form"),
    ({"SCHEMA_DUMP_LOCAL_OUT_DIR": "", "SANDBOX_SCHEMA_GCS_URI": "gs://other-bucket/schema"}, "sandbox drop bucket"),
    ({"SCHEMA_DUMP_LOCAL_OUT_DIR": "", "SANDBOX_SCHEMA_GCS_URI": "gs://pivota-sandbox-catalog-drop/../x"}, "'..'"),
])
def test_bad_settings_are_refused_before_any_database_call(harness, env, match):
    done = harness(**env)
    assert done.returncode == 2 and match in done.stderr, done.stderr
    assert not (harness.log / "pg_dump.argv").exists() or "--schema-only" not in (harness.log / "pg_dump.argv").read_text()
    assert PASSWORD not in done.stderr


def test_a_dump_never_overwrites_a_stamp(harness):
    assert harness().returncode == 0
    again = harness()
    assert again.returncode != 0 and "never overwrites" in again.stderr


def test_the_check_subcommand_runs_only_the_refusals(tmp_path):
    schema, ledgers = tmp_path / "s.sql", tmp_path / "l.sql"
    schema.write_text(GOOD_SCHEMA)
    ledgers.write_text(LEDGER_ROWS + "\n-- sandbox ledger dump complete\n")
    ok = subprocess.run(["bash", str(SCRIPT), "check", str(schema), str(ledgers)], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin"})
    assert ok.returncode == 0 and "CHECK OK" in ok.stdout
    schema.write_text(GOOD_SCHEMA.replace("\\unrestrict k1", "GRANT ALL ON a TO b;\n\\unrestrict k1"))
    bad = subprocess.run(["bash", str(SCRIPT), "check", str(schema), str(ledgers)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert bad.returncode == 3


def test_the_bucket_is_the_same_one_the_python_jobs_pin():
    from config.sandbox_identity import SANDBOX_DROP_BUCKET

    assert 'SANDBOX_DROP_BUCKET="%s"' % SANDBOX_DROP_BUCKET in SCRIPT.read_text()


def test_no_timeout_binary_means_no_prod_run(harness, tmp_path):
    """Outside local test mode, a missing coreutils `timeout` is a refusal (every prod call has a deadline)."""
    if os.path.exists("/usr/bin/timeout") or os.path.exists("/bin/timeout"):
        pytest.skip("this host has coreutils timeout on /usr/bin or /bin")
    done = harness(SCHEMA_DUMP_LOCAL_OUT_DIR="", SANDBOX_SCHEMA_GCS_URI="gs://pivota-sandbox-catalog-drop/schema")
    assert done.returncode == 2 and "timeout is required" in done.stderr


def test_pg_dumps_own_restrict_key_is_not_a_secret(harness):
    key = "F3TsolvOl5pCPhpgaXeCjBbfIaJ7fz6kwAb6nHRoD5BbjffG0OGvUuCamceG4Nh"
    done = harness(schema=GOOD_SCHEMA.replace("k1", key))
    assert done.returncode == 0, done.stderr


def test_the_manifest_records_pg_dump_s_version_and_the_server_s(harness):
    import gzip  # noqa: F401
    import json

    done = harness()
    assert done.returncode == 0, done.stderr
    manifest = json.loads((harness.drop / STAMP / "_MANIFEST.json").read_text())
    assert manifest["pg_dump_version"] == "17.6" and manifest["server_version_num"] == 170006


@pytest.mark.parametrize("version,server", [
    ("pg_dump (PostgreSQL) 18.1", "170006"),   # newer pg_dump than the server: its SETs would not apply
    ("pg_dump (PostgreSQL) 16.9", "170006"),
    ("pg_dump (PostgreSQL) 17.6", "160004"),
])
def test_a_pg_dump_of_another_major_version_never_dumps(harness, version, server):
    done = harness(FAKE_PG_DUMP_VERSION=version, FAKE_SERVER_NUM=server)
    assert done.returncode == 3 and "is not the server's major version" in done.stderr, done.stderr
    assert not (harness.log / "pg_dump.argv").read_text().replace("--version\n", "").strip()  # never dumped
    assert not harness.drop.exists()


def test_an_unreadable_server_version_never_dumps(harness):
    done = harness(FAKE_SERVER_NUM="")
    assert done.returncode == 3 and "server's version" in done.stderr
    assert not harness.drop.exists()


def test_a_bare_carriage_return_never_leaves_prod(harness):
    done = harness(schema=GOOD_SCHEMA.replace("\\unrestrict k1", "-- note\rCREATE ROLE x;\n\\unrestrict k1"))
    assert done.returncode == 3 and "carriage return" in done.stderr
    assert not harness.drop.exists()


def test_a_refusal_message_does_not_carry_the_exit_code(harness):
    done = harness(schema=GOOD_SCHEMA.replace("\\unrestrict k1", "GRANT SELECT ON public.a TO r;\n\\unrestrict k1"))
    assert done.returncode == 3
    assert done.stderr.strip().endswith("GRANT ,") and ", 3" not in done.stderr


#: One sample per secret kind; the dump (bash) and the apply (Python) must each refuse every one.
SECRET_SAMPLES = (
    "owner@person.example", "sk_live_abc", "sk-ant-api03-abcdefghijklmnopqrstu", "sk-proj-abcdefghijklmnopqr",
    "shpat_x", "ghp_abc", "github_pat_11ABCDEFG", "xoxb-123", "whsec_abc",
    "AKIA" + "ABCDEFGHIJKLMNOP", "AIza" + "B" * 35, "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig",
    "postgresql://u:hunter2@db/x", "host=db password=hunter2", "+14155550123", "415-555-0123",
    "ab" * 16, "QUJD" * 11,
)


@pytest.mark.parametrize("sample", SECRET_SAMPLES)
def test_every_secret_kind_is_refused_by_the_dump_and_by_the_apply(harness, sample):
    from jobs import sandbox_schema_apply as A

    statement = "CREATE TABLE public.c (a text DEFAULT '%s');" % sample
    done = harness(schema=GOOD_SCHEMA.replace("\\unrestrict k1", statement + "\n\\unrestrict k1"))
    assert done.returncode == 3 and "looks like a secret or a person" in done.stderr, (sample, done.stderr)
    assert sample not in done.stdout + done.stderr
    with pytest.raises(A.SchemaRefused, match="secret or a person"):
        A.validate_schema_sql(GOOD_SCHEMA.replace("\\unrestrict k1", statement + "\n\\unrestrict k1"))


@pytest.mark.parametrize("answer,match", [
    ("role", "superuser or can create roles"),
    ("table", "owns or may write a table"),
    ("", "could not check the credential"),
    ("something else", "could not check the credential"),
])
def test_a_credential_that_could_write_never_dumps(harness, answer, match):
    """infra T6: prod's full DATABASE_URL supplied by mistake must fail closed, before any pg_dump."""
    done = harness(FAKE_WRITABLE=answer)
    assert done.returncode == 3 and match in done.stderr, done.stderr
    assert not (harness.log / "pg_dump.argv").read_text().replace("--version\n", "").strip()  # never dumped
    assert not harness.drop.exists()


def test_the_privilege_check_runs_before_the_dump_and_is_read_only(harness):
    done = harness()
    assert done.returncode == 0, done.stderr
    calls = (harness.log / "psql.argv").read_text()
    check = next(line for line in calls.splitlines() if "has_table_privilege" in line)
    assert all(word not in check.lower() for word in ("insert into", "update ", "delete from", "truncate ", "grant", "create "))
    for kind in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert "'%s'" % kind in calls
    assert "relowner" in calls and "rolsuper" in calls
