"""The catalog export's per-run role steps (jobs/sandbox_catalog_export_role.py, role design r3 R3/R6)
against a real PostgreSQL:

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_sandbox_copy_test \\
      pytest tests/test_sandbox_catalog_export_role_postgres.py

Each test uses its own schema and its own throwaway roles, dropped afterwards. PostgreSQL 15 has no
large-object ACL function, so these run with require_pg17=False (checks 7 and 8b are then reduced and
say so); the refusal of an older server is itself tested.
"""

import os
import secrets
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL (see docstring)")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
if _IS_PG:
    _dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in _dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip("refusing to create roles in database %r - throwaway only" % _dbname, allow_module_level=True)

from jobs import sandbox_catalog_export_role as R  # noqa: E402

SCHEMA = "xrole_src"
TABLES = ["cat_a", "cat_b", "cat_optional_absent"]


@pytest.fixture
async def env():
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    tag = secrets.token_hex(4)
    role, other = "sbx_xrole_%s" % tag, "sbx_xrole_other_%s" % tag
    await conn.execute("DROP SCHEMA IF EXISTS %s CASCADE; CREATE SCHEMA %s" % (SCHEMA, SCHEMA))
    await conn.execute("CREATE TABLE %s.cat_a (id int PRIMARY KEY, price numeric, currency text)" % SCHEMA)
    await conn.execute("CREATE TABLE %s.cat_b (id int PRIMARY KEY)" % SCHEMA)
    await conn.execute("CREATE TABLE %s.not_catalog (secret text)" % SCHEMA)
    await conn.execute("CREATE ROLE %s NOLOGIN NOINHERIT" % role)
    await conn.execute("ALTER ROLE %s SET default_transaction_read_only = on" % role)
    await conn.execute("GRANT USAGE ON SCHEMA %s TO %s" % (SCHEMA, role))
    await conn.execute("CREATE ROLE %s NOLOGIN" % other)
    try:
        yield conn, role, other
    finally:
        await conn.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        for name in (role, other):
            await conn.execute("DROP OWNED BY %s" % name)
            await conn.execute("DROP ROLE %s" % name)
        await conn.close()


async def _readable(conn, role):
    return sorted(r[0] for r in await conn.fetch(R._readable_sql(), role))


async def _grant(conn, role):
    await R.grant(conn, TABLES, role=role, schema=SCHEMA, require_pg17=False)


async def test_grant_then_revoke_gives_exactly_the_catalog_tables_and_then_nothing(env, capsys):
    conn, role, _ = env
    await _grant(conn, role)
    assert await _readable(conn, role) == ["xrole_src.cat_a", "xrole_src.cat_b"]
    out = capsys.readouterr().out
    assert "PRESENT catalog_tables=2 of=3" in out and "GRANT_DONE" in out
    assert "READ_4 security_definer_executable" in out and "CHECK_8B SKIPPED" in out
    await R.revoke(conn, TABLES, role=role, schema=SCHEMA)
    assert await _readable(conn, role) == []
    assert "REVOKE_DONE" in capsys.readouterr().out


async def _refused(conn, role, label):
    with pytest.raises(R.Stop, match=label):
        await _grant(conn, role)
    # rolled back: the role reads none of the catalog tables
    assert not [t for t in await _readable(conn, role) if t.startswith("xrole_src.cat_")]


async def test_a_role_that_already_reads_something_is_refused(env):
    conn, role, _ = env
    await conn.execute("GRANT SELECT ON %s.not_catalog TO %s" % (SCHEMA, role))
    await _refused(conn, role, "PRE_B")


async def test_a_public_grant_on_a_catalog_table_is_refused(env):
    conn, role, _ = env
    await conn.execute("GRANT SELECT ON %s.cat_b TO PUBLIC" % SCHEMA)
    with pytest.raises(R.Stop, match="PRE_B"):  # PUBLIC's grant makes the role read cat_b already
        await _grant(conn, role)
    await conn.execute("REVOKE SELECT ON %s.cat_b FROM PUBLIC" % SCHEMA)
    await conn.execute("GRANT SELECT (id) ON %s.cat_a TO PUBLIC" % SCHEMA)
    with pytest.raises(R.Stop, match="PRE_B"):
        await _grant(conn, role)


async def test_check_c_catches_a_public_grant_even_on_a_column(env, monkeypatch):
    """PRE_C on its own (PRE_B neutralised): a column-level PUBLIC grant on a catalog table."""
    conn, role, _ = env
    await conn.execute("GRANT SELECT (id) ON %s.cat_a TO PUBLIC" % SCHEMA)
    real = R._must_be_empty

    async def skip_b(c, label, sql, *args):
        if label.startswith("PRE_B"):
            return None
        return await real(c, label, sql, *args)

    monkeypatch.setattr(R, "_must_be_empty", skip_b)
    with pytest.raises(R.Stop, match="PRE_C"):
        await _grant(conn, role)
    # rolled back: no grant of ours (cat_a stays readable through PUBLIC's column grant only)
    assert not await conn.fetchval("SELECT has_table_privilege($1, 'xrole_src.cat_b', 'SELECT')", role)


async def test_a_catalog_table_owned_by_another_role_is_refused(env):
    conn, role, other = env
    await conn.execute("ALTER TABLE %s.cat_b OWNER TO %s" % (SCHEMA, other))
    await _refused(conn, role, "CHECK_0")


async def test_membership_in_another_role_is_refused(env):
    conn, role, other = env
    await conn.execute("GRANT %s TO %s" % (other, role))
    await _refused(conn, role, "CHECK_1")


async def test_a_write_privilege_is_refused(env):
    conn, role, _ = env
    await conn.execute("GRANT INSERT ON %s.cat_a TO %s" % (SCHEMA, role))
    await _refused(conn, role, "CHECK_2")


async def test_create_on_a_schema_is_refused(env):
    conn, role, _ = env
    await conn.execute("GRANT CREATE ON SCHEMA %s TO %s" % (SCHEMA, role))
    await _refused(conn, role, "CHECK_3")


async def test_another_role_setting_is_refused(env):
    conn, role, _ = env
    await conn.execute("ALTER ROLE %s SET statement_timeout = '0'" % role)
    await _refused(conn, role, "CHECK_5")


async def test_owning_anything_is_refused(env):
    conn, role, _ = env
    await conn.execute("CREATE TYPE %s.owned_t AS (x int)" % SCHEMA)
    await conn.execute("ALTER TYPE %s.owned_t OWNER TO %s" % (SCHEMA, role))
    with pytest.raises(R.Stop, match="CHECK_[26]"):
        await _grant(conn, role)


async def test_a_catalog_name_that_is_a_view_is_refused(env):
    conn, role, _ = env
    await conn.execute("CREATE VIEW %s.cat_optional_absent AS SELECT * FROM %s.not_catalog" % (SCHEMA, SCHEMA))
    await _refused(conn, role, "CHECK_8C")


async def test_row_level_security_on_a_catalog_table_is_refused(env):
    conn, role, _ = env
    await conn.execute("ALTER TABLE %s.cat_a ENABLE ROW LEVEL SECURITY" % SCHEMA)
    await _refused(conn, role, "CHECK_10")


async def test_force_row_level_security_alone_is_refused_too(env):
    conn, role, _ = env
    await conn.execute("ALTER TABLE %s.cat_a FORCE ROW LEVEL SECURITY" % SCHEMA)
    await _refused(conn, role, "CHECK_10")


async def test_a_missing_role_and_an_older_server_are_refused(env):
    conn, role, _ = env
    with pytest.raises(R.Stop, match="does not exist"):
        await R.grant(conn, TABLES, role=role + "_nope", schema=SCHEMA, require_pg17=False)
    if int(await conn.fetchval("SHOW server_version_num")) < 170000:
        with pytest.raises(R.Stop, match="older than PostgreSQL 17"):
            await R.grant(conn, TABLES, role=role, schema=SCHEMA)


async def test_revoke_with_login_still_on_is_a_stop_but_the_revoke_stays(env):
    conn, role, _ = env
    await _grant(conn, role)
    await conn.execute("ALTER ROLE %s LOGIN" % role)
    with pytest.raises(R.Stop, match="login"):
        await R.revoke(conn, TABLES, role=role, schema=SCHEMA)
    assert await _readable(conn, role) == []


async def test_revoke_leaves_another_grantors_grant_and_says_so(env):
    """REVOKE removes only the same grantor's grants: one made by another role survives, and the
    re-read must stop on it."""
    conn, role, other = env
    await _grant(conn, role)
    await conn.execute("GRANT SELECT ON %s.cat_a TO %s WITH GRANT OPTION" % (SCHEMA, other))
    await conn.execute("GRANT USAGE ON SCHEMA %s TO %s" % (SCHEMA, other))
    await conn.execute("SET ROLE %s" % other)
    try:
        await conn.execute("GRANT SELECT ON %s.cat_a TO %s" % (SCHEMA, role))
    finally:
        await conn.execute("RESET ROLE")
    with pytest.raises(R.Stop, match="readable"):
        await R.revoke(conn, TABLES, role=role, schema=SCHEMA)
    assert await _readable(conn, role) == ["xrole_src.cat_a"]


async def test_revoke_with_a_session_still_open_is_a_stop(env):
    import urllib.parse

    import asyncpg

    conn, role, _ = env
    password = secrets.token_hex(16)
    await conn.execute("ALTER ROLE %s LOGIN PASSWORD '%s'" % (role, password))
    url = urllib.parse.urlsplit(DATABASE_URL)
    netloc = "%s:%s@%s" % (role, password, url.hostname + (":%d" % url.port if url.port else ""))
    session = await asyncpg.connect(urllib.parse.urlunsplit(url._replace(netloc=netloc)))
    try:
        await conn.execute("ALTER ROLE %s NOLOGIN PASSWORD NULL" % role)  # NOLOGIN does not end it
        with pytest.raises(R.Stop, match="sessions"):
            await R.revoke(conn, TABLES, role=role, schema=SCHEMA)
    finally:
        await session.close()


def test_main_without_a_database_url_stops(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert R.main(["grant"]) == 3
    assert "STOP DATABASE_URL is not set" in capsys.readouterr().out


def test_main_reports_a_stop_as_exit_3(monkeypatch, capsys):
    async def stop(mode, dsn):
        raise R.Stop("CHECK_0 not_owned_by_current_user")

    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(R, "_run", stop)
    assert R.main(["grant"]) == 3
    assert "rolled back: the role holds nothing new" in capsys.readouterr().out
    assert R.main(["revoke"]) == 3
    assert "the REVOKE is committed" in capsys.readouterr().out


async def test_a_grant_that_did_not_take_is_refused(env):
    """Check 9 is the last word on the grants: here one GRANT is silently dropped (as a non-owner's
    GRANT would be, with only a WARNING), and the run must stop, not export without that table."""
    conn, role, _ = env

    class DropOneGrant:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def execute(self, sql, *args):
            if sql.startswith("GRANT SELECT") and '"cat_b"' in sql:
                return "GRANT"
            return await self._inner.execute(sql, *args)

    with pytest.raises(R.Stop, match="CHECK_9"):
        await R.grant(DropOneGrant(conn), TABLES, role=role, schema=SCHEMA, require_pg17=False)
    assert await _readable(conn, role) == []  # rolled back: cat_a's grant is gone too


async def test_a_non_superuser_owner_grants_and_revokes_beside_a_table_it_does_not_own(env, capsys):
    """The review's probe: the R3/R6 one-offs run as the app role, which is not a superuser. With a
    table in the schema that role cannot touch, `REVOKE ... ON ALL TABLES IN SCHEMA` fails outright and
    rolls every revoke back; revoking the catalog tables by name works, and the re-read still runs."""
    import urllib.parse

    import asyncpg

    conn, role, _ = env
    owner = role + "_owner"
    password = secrets.token_hex(16)
    await conn.execute("CREATE ROLE %s LOGIN NOSUPERUSER PASSWORD '%s'" % (owner, password))
    as_owner = None
    try:
        await conn.execute("GRANT USAGE ON SCHEMA %s TO %s" % (SCHEMA, owner))
        for t in ("cat_a", "cat_b"):
            await conn.execute("ALTER TABLE %s.%s OWNER TO %s" % (SCHEMA, t, owner))
        # not_catalog stays the superuser's: the owner holds no privilege at all on it
        url = urllib.parse.urlsplit(DATABASE_URL)
        netloc = "%s:%s@%s" % (owner, password, url.hostname + (":%d" % url.port if url.port else ""))
        as_owner = await asyncpg.connect(urllib.parse.urlunsplit(url._replace(netloc=netloc)))
        assert not await as_owner.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")

        await R.grant(as_owner, TABLES, role=role, schema=SCHEMA, require_pg17=False)
        assert await _readable(conn, role) == ["xrole_src.cat_a", "xrole_src.cat_b"]
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the old, schema-wide form
            await as_owner.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA %s FROM %s" % (SCHEMA, role))
        await R.revoke(as_owner, TABLES, role=role, schema=SCHEMA)
        assert await _readable(conn, role) == []
        assert "REVOKED tables=2 of=2" in capsys.readouterr().out
    finally:
        if as_owner is not None:
            await as_owner.close()
        await conn.execute("REASSIGN OWNED BY %s TO CURRENT_USER" % owner)
        await conn.execute("DROP OWNED BY %s" % owner)
        await conn.execute("DROP ROLE %s" % owner)


async def test_a_catalog_table_the_revoker_cannot_touch_is_a_stop_and_the_rest_are_revoked(env, capsys):
    """One table's REVOKE failing (here: owned by a role the revoker has no privilege on) does not undo
    the others, and the run stops."""
    import urllib.parse

    import asyncpg

    conn, role, other = env
    owner = role + "_owner"
    password = secrets.token_hex(16)
    await conn.execute("CREATE ROLE %s LOGIN NOSUPERUSER PASSWORD '%s'" % (owner, password))
    as_owner = None
    try:
        await conn.execute("GRANT USAGE ON SCHEMA %s TO %s" % (SCHEMA, owner))
        await conn.execute("ALTER TABLE %s.cat_a OWNER TO %s" % (SCHEMA, owner))
        # cat_a is the revoker's own: a superuser's GRANT on it is recorded as the owner's, so it is revoked
        await conn.execute("GRANT SELECT ON %s.cat_a TO %s" % (SCHEMA, role))
        # cat_b stays the superuser's: the revoker holds no privilege on it, so its REVOKE fails
        await conn.execute("GRANT SELECT ON %s.cat_b TO %s" % (SCHEMA, role))
        url = urllib.parse.urlsplit(DATABASE_URL)
        netloc = "%s:%s@%s" % (owner, password, url.hostname + (":%d" % url.port if url.port else ""))
        as_owner = await asyncpg.connect(urllib.parse.urlunsplit(url._replace(netloc=netloc)))
        with pytest.raises(R.Stop, match="revoke cat_b"):
            await R.revoke(as_owner, TABLES, role=role, schema=SCHEMA)
        out = capsys.readouterr().out
        assert "REVOKE_FAILED cat_b InsufficientPrivilegeError" in out and "REREAD readable" in out
        assert "xrole_src.cat_a" not in await _readable(conn, role)
    finally:
        if as_owner is not None:
            await as_owner.close()
        await conn.execute("REASSIGN OWNED BY %s TO CURRENT_USER" % owner)
        await conn.execute("DROP OWNED BY %s" % owner)
        await conn.execute("DROP ROLE %s" % owner)


def test_main_reports_a_database_error_as_exit_3_with_its_sqlstate_only(monkeypatch, capsys):
    import asyncpg

    async def denied(mode, dsn):
        raise asyncpg.InsufficientPrivilegeError("permission denied for table secret_name on host-x")

    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(R, "_run", denied)
    assert R.main(["revoke"]) == 3
    out = capsys.readouterr().out
    assert "STOP InsufficientPrivilegeError sqlstate=42501" in out
    assert "secret_name" not in out and "host-x" not in out
