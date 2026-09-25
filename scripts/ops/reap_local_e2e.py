"""Run the whole Reap agentic purchase machine on a laptop, against Reap's SANDBOX.

    purchase route -> ledger -> poller -> Reap sandbox -> completed + attribution edge

Everything on OUR side is local: a SQLite file (default) or a Postgres on localhost, a uvicorn on
127.0.0.1, and the poller's own step function driven by hand in this process. The only thing that
leaves the machine is the poller's calls to `sandbox.api.reap.global`. A HUMAN approves on Reap's
hosted page; this script prints that page's URL and never opens it. Pivota never sees card data.

── THE OPERATOR'S SEQUENCE ──────────────────────────────────────────────────────────────────

    PY=.venv/bin/python            # the repo's own interpreter (uvicorn, jwt, aiosqlite)
    $PY scripts/ops/reap_local_e2e.py seed  --reset [--seed-enrollment <reap enrollment uuid>]
    $PY scripts/ops/reap_local_e2e.py serve                       # terminal 1, leave running
    $PY scripts/ops/reap_local_e2e.py run                         # terminal 2: purchase + poll

`run` is `purchase` then `poll`; both exist separately. `--dry-run` on `purchase`/`poll`/`run`
swaps Reap for an in-process fake (no network, no key) and posts the purchase through the real
app in-process — it is how this script is validated. `serve` itself never holds the real key
(it never calls Reap), so `serve` + `purchase` + `poll --dry-run` rehearses the HTTP path without
the sandbox key.

── WHAT IT REFUSES ──────────────────────────────────────────────────────────────────────────

  * any DATABASE_URL that is not a SQLite FILE or a Postgres on localhost / 127.0.0.1 / ::1
    (`check_local_database_url`). A dotted host, a private IP, userinfo in front of a remote host,
    a `?host=` override, a multi-host list and a host-less URL (which libpq would resolve from
    PGHOST), and an authority with more than one `@` are all refused. Every catalog/ledger WRITER
    in this file re-checks the URL `db.database` actually bound (`_bound_database_url_check`) as
    its first statement, so the guard sits in the write path, not only in the CLI.
  * any REAP_API_BASE_URL that is not exactly the sandbox host (`check_sandbox_base_url`).
  * egress to any host but the sandbox from the poll process: every httpx client the Reap client
    builds goes through `_RecordingTransport`, which refuses other hosts.

── CREDENTIALS ──────────────────────────────────────────────────────────────────────────────

The Reap key is LOADED at runtime, by `poll`/`run` ONLY, from `~/.config/pivota/reap_sandbox.env`
(or $REAP_SANDBOX_ENV) by `_load_env_file`, adapted from scripts/reap_agentic_sandbox_probe.py.
The file is the ONLY source: a `REAP_API_KEY` exported in the shell makes every command refuse,
and an env file whose base URL is not the sandbox has its key refused. `serve` gets a
placeholder. The key is never printed, never written to the state file, and the Authorization
header is redacted in the JSON call log. The
agent API key and buyer JWT that `seed` prints are LOCAL test credentials for a local database.

The environment of `serve` and of this process is built from an ALLOWLIST (`_harness_env`), not
inherited, and this process's own `os.environ` is CLEARED before it is replaced: a shell that
exports a production DATABASE_URL, REDIS_URL, SENTRY_DSN, a Cloud Run marker or libpq's
PGHOST/PGHOSTADDR/PGSERVICE (which psycopg2 honours even beside host=localhost) leaks none of it.

State files: the state dir is 0700 and must be ours; every file is written 0600 via a temp file
and a rename; the SQLite file is created 0600 before anything writes to it.

See docs/runbooks/reap_agentic_purchase.md, "Local end-to-end run against the sandbox".
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import re
import secrets
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── constants ────────────────────────────────────────────────────────────────────────────────

#: EXACT hostnames, never a substring or suffix test. `sandbox.api.reap.global.evil.com`,
#: `api.reap.global/sandbox` and `prod.api.reap.global` must all refuse. A subset of
#: `rc.SIMULATE_CHECKOUT_SANDBOX_HOSTS` (a test pins that), because the simulate header this run
#: depends on is only emitted for those hosts.
SANDBOX_HOSTS = frozenset({"sandbox.api.reap.global"})
DEFAULT_REAP_BASE_URL = "https://sandbox.api.reap.global"

#: The only database hosts a local run may name. Compared EXACTLY after `urlsplit` lowercases and
#: un-brackets them, so `localhost.evil.com`, `127.0.0.1.nip.io` and `10.25.0.2` refuse.
LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SQLITE_SCHEMES = frozenset({"sqlite", "sqlite+aiosqlite"})
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgres"})
#: Query parameters libpq/asyncpg read as a connection TARGET. Any of them lets a URL whose
#: authority says localhost connect somewhere else.
_HOST_OVERRIDE_PARAMS = frozenset({"host", "hostaddr", "service", "port"})

#: Under `$TMPDIR` (per-user on macOS). With TMPDIR unset the harness REFUSES rather than fall
#: back to a shared `/tmp`; pass `--state-dir`.
STATE_DIR_NAME = "pivota-reap-local-e2e"
DEFAULT_ENV_FILE = "~/.config/pivota/reap_sandbox.env"

#: THE refusal table for `check_local_database_url`: ONE list, owned here. The harness's tests
#: parametrize over it, and the ADR-011 catalog tripwire probes ALL of it before it exempts this
#: script's catalog fixture write — so a URL added here is enforced in both places at once.
REFUSED_DATABASE_URLS = (
    ("postgresql://10.25.0.2/pivota", "private ip"),
    ("postgresql://10.25.0.2:5432/pivota", "private ip with port"),
    ("postgresql://db.internal/pivota", "a dotted hostname"),
    ("postgresql://localhost.localdomain/pivota", "a dotted hostname that starts with localhost"),
    ("postgresql://127.0.0.1.nip.io/pivota", "a dotted hostname that starts with 127.0.0.1"),
    ("postgresql://local/pivota", "a substring of an allowed host"),
    ("postgresql://pivota:pw@34.120.1.9:5432/pivota", "prod-looking cloud sql ip"),
    ("postgresql://pivota:secret@prod-db.pivota.cc/pivota", "prod-looking hostname"),
    ("postgresql://localhost@10.25.0.2/pivota", "userinfo that says localhost, remote host"),
    ("postgresql://localhost:pw@10.25.0.2/pivota", "userinfo:password, remote host"),
    ("postgresql://a@10.25.0.2@localhost/pivota", "two @ in the authority"),
    ("postgresql://u:p@x@localhost/pivota", "two @ in the authority, localhost last"),
    ("postgresql://localhost:5432,10.25.0.2/pivota", "multi-host list led by localhost"),
    ("postgresql://localhost,10.25.0.2/pivota", "multi-host list without ports"),
    ("postgresql://localhost/pivota?host=10.25.0.2", "a ?host= override"),
    ("postgresql://localhost/pivota?hostaddr=10.25.0.2", "a ?hostaddr= override"),
    ("postgresql://localhost/pivota?service=prod", "a ?service= override"),
    ("postgresql://localhost/pivota?HOST=10.25.0.2", "an upper-case ?HOST= override"),
    ("postgresql:///pivota", "no host: libpq would read PGHOST"),
    ("postgresql://:5432/pivota", "an empty host with a port"),
    ("postgresql://localhost:notaport/pivota", "malformed port"),
    ("sqlite://evilhost/x.db", "sqlite with an authority"),
    ("sqlite+aiosqlite:///:memory:", "in-memory sqlite"),
    ("sqlite:///", "sqlite with no file"),
    ("mysql://localhost/pivota", "unsupported scheme"),
    ("", "empty"),
)
ACCEPTED_DATABASE_URLS = (
    "sqlite+aiosqlite:////tmp/x/local.db",
    "sqlite:///relative.db",
    "sqlite+aiosqlite:///./pivota.db",
    "postgresql://localhost/pivota_local",
    "postgresql://LOCALHOST:5432/pivota_local",
    "postgresql://127.0.0.1/pivota_local",
    "postgresql://[::1]:5432/pivota_local",
    "postgres://me:pw@localhost/pivota_local",
    "postgresql://me@127.0.0.1:5433/pivota_local?sslmode=disable",
)
DRY_RUN_PLACEHOLDER_KEY = "dry-run-placeholder-not-a-reap-key"

JWT_ISSUER = "https://local-e2e.pivota.test"
JWT_AUDIENCE = "pivota-local-e2e"
JWT_KID = "local-e2e-1"

#: The one product measured quotable in Reap's sandbox on 2026-09-25 (fashion merchants only).
DEFAULTS = {
    "merchant_domain": "fashionnova.com",
    "merchant_id": "m_local_fashionnova",
    "source_product_id": "maven-lipstick-snatched",
    "product_title": "Maven Lipstick - Snatched",
    "variant_title": "OS",
    "brand": "Fashion Nova",
    "category": "lipstick",
    "price": "1.98",
    "currency": "USD",
    "market": "US",
    "buyer_ref": "pivota-probe-buyer-001",
    "consent_version": "reap-agentic-v1",
    "email": "local-e2e-buyer@example.com",
}

#: A Delaware address; the sandbox quoted $6.99 shipping to one on 2026-09-25.
DEFAULT_ADDRESS = {
    "firstName": "Local",
    "lastName": "Probe",
    "phone": "+13025550100",
    "addressLine1": "1000 N West St",
    "city": "Wilmington",
    "region": "DE",
    "postalCode": "19801",
    "country": "US",
}

NO_PROXY_HOSTS = "sandbox.api.reap.global,localhost,127.0.0.1,::1"

#: Passed through from the operator's shell into `serve` and into this process. EVERYTHING ELSE
#: IS DROPPED — see `_harness_env`.
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM",
    "TMPDIR", "TZ", "VIRTUAL_ENV", "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
)

_PURCHASE_PATH = "/agent/v2/commerce/reap/purchases"
_TERMINAL = frozenset({"completed", "failed", "refused", "expired"})


class HarnessRefused(RuntimeError):
    """A safety refusal. `main` prints it and exits 2; nothing has been sent anywhere."""


# ── the two guards ───────────────────────────────────────────────────────────────────────────


def check_local_database_url(raw: Any) -> str:
    """Return `raw` if it names a LOCAL database, else raise `HarnessRefused`.

    Accepted: `sqlite:///<file>` / `sqlite+aiosqlite:///<file>` (no authority, not `:memory:` —
    the `databases` library opens a connection per query, so an in-memory DB loses its tables);
    `postgresql://` / `postgres://` whose host is exactly localhost, 127.0.0.1 or ::1.
    """
    text = str(raw or "").strip()
    if not text:
        raise HarnessRefused("DATABASE_URL is empty")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise HarnessRefused("DATABASE_URL does not parse as a URL") from None
    scheme = parts.scheme.lower()
    if parts.netloc.count("@") > 1:
        # `a@b@host`: urlsplit takes the LAST `@`, other parsers the first. Where parsers
        # disagree about which host a URL names, fail closed rather than pick one.
        raise HarnessRefused("DATABASE_URL has more than one '@' in its authority")

    if scheme in _SQLITE_SCHEMES:
        if parts.netloc:
            # `sqlite://host/x.db` — SQLite has no hosts; an authority here is a typo or a trick.
            raise HarnessRefused("a SQLite DATABASE_URL must have no host (sqlite:///path/to.db)")
        path = parts.path.lstrip("/")
        if not path or path == ":memory:" or path.startswith(":memory:"):
            raise HarnessRefused("a SQLite DATABASE_URL must name a FILE, not :memory:")
        return text

    if scheme in _POSTGRES_SCHEMES:
        # ONE membership test covers three shapes that each look like they need their own arm:
        # a missing host ('' — libpq would fill it from PGHOST, which this guard cannot see), a
        # multi-host list (`localhost,10.0.0.2` is not a member; `localhost:5432,10.0.0.2` parses
        # as host `localhost` with a malformed port and is refused just below), and userinfo in
        # front of a remote host (`hostname` is the part after the `@`).
        host = (parts.hostname or "").lower()
        if host not in LOCAL_DB_HOSTS:
            raise HarnessRefused(
                f"DATABASE_URL host {host or '(none)'!r} is not local; only "
                f"{sorted(LOCAL_DB_HOSTS)} are allowed, named explicitly"
            )
        try:
            parts.port  # noqa: B018 — raises on a malformed port
        except ValueError:
            raise HarnessRefused("DATABASE_URL has a malformed port") from None
        overrides = {k.lower() for k in parse_qs(parts.query, keep_blank_values=True)}
        if overrides & _HOST_OVERRIDE_PARAMS:
            raise HarnessRefused(
                "DATABASE_URL carries a connection-target query parameter "
                f"({sorted(overrides & _HOST_OVERRIDE_PARAMS)}); refusing"
            )
        return text

    raise HarnessRefused(
        f"DATABASE_URL scheme {scheme or 'none'!r} is not sqlite or postgresql; refusing"
    )


def check_sandbox_base_url(raw: Any) -> str:
    """Return the normalised sandbox base URL, or raise `HarnessRefused`.

    https, no userinfo, host EXACTLY one of `SANDBOX_HOSTS`, default port, no path/query/fragment.
    """
    text = str(raw or "").strip()
    if not text:
        raise HarnessRefused("REAP_API_BASE_URL is empty")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise HarnessRefused("REAP_API_BASE_URL does not parse") from None
    if parts.scheme.lower() != "https":
        raise HarnessRefused("REAP_API_BASE_URL must be https")
    if "@" in parts.netloc:
        raise HarnessRefused("REAP_API_BASE_URL must not carry userinfo")
    host = (parts.hostname or "").lower()
    if host not in SANDBOX_HOSTS:
        raise HarnessRefused(
            f"REAP_API_BASE_URL host {host!r} is not the Reap sandbox {sorted(SANDBOX_HOSTS)}"
        )
    if port not in (None, 443):
        raise HarnessRefused("REAP_API_BASE_URL must use the default https port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise HarnessRefused("REAP_API_BASE_URL must be a bare origin")
    return f"https://{host}"


# ── credentials: loaded, never printed ───────────────────────────────────────────────────────


def _load_env_file() -> Dict[str, str]:
    """The sandbox env file, and ONLY the file. Values are never printed.

    Adapted from scripts/reap_agentic_sandbox_probe.py::_load_env (not on main), with one
    deliberate difference: the probe let a shell `REAP_*` win over the file. Here the file is the
    only source of a key — a `REAP_API_KEY` exported in a shell is of unknown provenance (it may be
    a production key), and `main` refuses to run while one is set rather than ignore it.
    """
    env: Dict[str, str] = {}
    path = Path(os.environ.get("REAP_SANDBOX_ENV") or DEFAULT_ENV_FILE).expanduser()
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


_SECRET_HEADER_NAMES = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
    "x-agent-user-jwt",
})
_SECRET_BODY_KEYS = frozenset({"authorization", "apikey", "api_key"})


def redact(obj: Any) -> Any:
    """Headers and bodies with every credential-bearing key replaced. Same key set as the probe's
    `_redact`, plus the header names this harness itself sends."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if str(k).lower() in (_SECRET_HEADER_NAMES | _SECRET_BODY_KEYS)
                else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


# ── environment ──────────────────────────────────────────────────────────────────────────────


def _harness_env(
    *,
    database_url: str,
    reap_base_url: str,
    reap_api_key: Optional[str],
    state_dir: Path,
    shell: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The COMPLETE environment for `serve` and for this process. Allowlisted, not inherited."""
    shell = os.environ if shell is None else shell
    env = {k: shell[k] for k in _ENV_PASSTHROUGH if shell.get(k)}
    env.update({
        "DATABASE_URL": database_url,
        "PIVOTA_ENV": "development",
        "REAP_AGENTIC_ENABLED": "1",
        "REAP_API_BASE_URL": reap_base_url,
        "REAP_AGENTIC_SIMULATE_CHECKOUT": "COMPLETED",
        # No scheduler jobs in `serve`: the poller is driven by hand from `poll`.
        "AUDIT_WORKER_ENABLED": "false",
        # The schema is built by `seed`; skip the long DDL tail of `startup()`.
        "SKIP_HEAVY_STARTUP_INIT": "true",
        # The buyer JWT `seed` minted is verified against the JWKS it wrote.
        "AGENT_USER_JWKS_FILE": str(state_dir / "jwks.json"),
        "AGENT_USER_JWT_ISSUER": JWT_ISSUER,
        "AGENT_USER_JWT_AUDIENCE": JWT_AUDIENCE,
        "NO_PROXY": NO_PROXY_HOSTS,
        "no_proxy": NO_PROXY_HOSTS,
        "MVP_EVENTS_FILE": str(state_dir / "mvp_events.jsonl"),
        "PYTHONUNBUFFERED": "1",
    })
    if reap_api_key:
        env["REAP_API_KEY"] = reap_api_key
    return env


def _apply_env(env: Mapping[str, str]) -> None:
    os.environ.clear()
    os.environ.update(env)


def _serve_env_keys_for_display(env: Mapping[str, str]) -> List[str]:
    return [f"{k}={'<never printed>' if k == 'REAP_API_KEY' else v}"
            for k, v in sorted(env.items()) if k not in _ENV_PASSTHROUGH]


# ── state ────────────────────────────────────────────────────────────────────────────────────


def _write_private(path: Path, text: str) -> None:
    """0600, written to a fresh temp file beside `path` and RENAMED over it.

    The temp file is opened `O_CREAT|O_EXCL|O_NOFOLLOW`, so it cannot be a pre-planted file or a
    symlink; `os.replace` then swaps the directory entry, so a symlink planted AT `path` is
    replaced, never followed. The file is never visible with wider bits or partial content.
    """
    directory = path.parent
    tmp = directory / f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(str(tmp), str(path))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _resolve_state_dir(raw: Optional[str], environ: Optional[Mapping[str, str]] = None) -> Path:
    """`--state-dir` as an ABSOLUTE path, or `$TMPDIR/pivota-reap-local-e2e`. With neither, refuse:
    the only other default is a shared `/tmp`, where another user can pre-create the directory."""
    environ = os.environ if environ is None else environ
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw)))
    tmpdir = (environ.get("TMPDIR") or "").strip()
    if not tmpdir:
        raise HarnessRefused(
            "TMPDIR is unset, so the default state dir would be a shared /tmp; pass --state-dir"
        )
    return Path(os.path.abspath(tmpdir)) / STATE_DIR_NAME


def _ensure_state_dir(path: Path, *, create: bool) -> Path:
    """The state dir must be a real directory, OURS, and mode 0700. Created 0700 when `create`;
    an existing one owned by someone else, a symlink, or one with group/other bits is refused."""
    if create and not os.path.lexists(str(path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(str(path), 0o700)
        os.chmod(str(path), 0o700)  # mkdir's mode is filtered by the umask
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        raise HarnessRefused(f"state dir {path} does not exist; run `seed` first") from None
    import stat as _stat

    if not _stat.S_ISDIR(st.st_mode):
        raise HarnessRefused(f"state dir {path} is not a directory (or is a symlink)")
    if st.st_uid != os.getuid():
        raise HarnessRefused(f"state dir {path} is owned by uid {st.st_uid}, not you")
    if st.st_mode & 0o077:
        raise HarnessRefused(
            f"state dir {path} has mode {oct(st.st_mode & 0o777)}; it must be 0700"
        )
    return path


def _sqlite_file(database_url: str) -> Optional[Path]:
    match = re.match(r"^sqlite(?:\+aiosqlite)?:///(.+)$", database_url)
    return Path(match.group(1)) if match else None


def _absolutize_sqlite_url(database_url: Optional[str]) -> Optional[str]:
    """A relative SQLite path made absolute NOW, before the harness `chdir`s to the repo root —
    otherwise `sqlite:///local.db` would silently name a different file after the chdir."""
    if not database_url:
        return database_url
    match = re.match(r"^(sqlite(?:\+aiosqlite)?):///(.+)$", database_url)
    if not match or match.group(2).startswith("/") or match.group(2).startswith(":memory:"):
        return database_url
    return f"{match.group(1)}:///{os.path.abspath(match.group(2))}"


def _precreate_sqlite_file(database_url: str) -> None:
    """Create the SQLite file 0600 BEFORE anything writes to it (O_CREAT|O_EXCL|O_NOFOLLOW when
    new); an existing one must be a regular file and is chmodded 0600."""
    path = _sqlite_file(database_url)
    if path is None:
        return
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except FileExistsError:
        if os.path.islink(str(path)) or not os.path.isfile(str(path)):
            raise HarnessRefused(f"{path} exists and is not a regular file") from None
    os.chmod(str(path), 0o600)


def _load_state(state_dir: Path) -> Dict[str, Any]:
    path = state_dir / "state.json"
    if not path.exists():
        raise HarnessRefused(f"no {path}; run `seed` first")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(state_dir: Path, state: Mapping[str, Any]) -> None:
    _write_private(state_dir / "state.json", json.dumps(state, indent=2, sort_keys=True) + "\n")


def _default_database_url(state_dir: Path) -> str:
    return f"sqlite+aiosqlite:///{state_dir / 'local.db'}"


def _bound_database_url_check() -> None:
    """After `db.database` is imported: the URL it actually BOUND must still be local. Settings
    can read a `.env` file, so the URL we set is not proof of the URL in use."""
    from db.database import DATABASE_URL

    check_local_database_url(DATABASE_URL)


def _is_postgres() -> bool:
    from db.database import IS_POSTGRES

    return bool(IS_POSTGRES)


# ── the buyer JWT (local issuer, RS256) ──────────────────────────────────────────────────────


def _new_signing_key() -> tuple:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt import algorithms

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    jwk = json.loads(algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": JWT_KID, "use": "sig", "alg": "RS256"})
    return pem, {"keys": [jwk]}


def mint_buyer_jwt(state_dir: Path, state: Mapping[str, Any], *, ttl_seconds: int = 3600) -> str:
    import jwt

    pem = (state_dir / "signing_key.pem").read_text(encoding="ascii")
    now = int(time.time())
    claims = {
        "iss": JWT_ISSUER,
        "sub": state["buyer_subject"],
        "aud": JWT_AUDIENCE,
        # The buyer's session id. Carried for realism; the verifier derives agent_user_ref from
        # `iss` + `sub` and ignores `sid`.
        "sid": state["buyer_session_id"],
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": JWT_KID})


def agent_user_ref_for(state: Mapping[str, Any]) -> str:
    """What `services.agent_user_jwt` will derive from the JWT: `<iss>:<sub>`."""
    return f"{JWT_ISSUER}:{state['buyer_subject']}"


# ── seed ─────────────────────────────────────────────────────────────────────────────────────


def _load_steps_module():
    """scripts/ops/reap_purchase_steps.py, for `_checked_id` — reused rather than duplicated."""
    spec = importlib.util.spec_from_file_location(
        "reap_purchase_steps", REPO_ROOT / "scripts" / "ops" / "reap_purchase_steps.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def build_schema() -> None:
    """The schema the way the app builds it at boot: `metadata.create_all` over every table `main`
    registers, then the schema-guard self-heal (which is what creates the reap_agentic_* tables —
    production skips db/migrations)."""
    _bound_database_url_check()  # IN the write path: DDL follows
    import main  # noqa: F401 — registers every Table on `metadata`
    from db.database import engine, metadata
    from db.schema_guard import ensure_required_schema_light

    metadata.create_all(engine, checkfirst=True)
    await ensure_required_schema_light()


async def seed_rows(state_dir: Path, opts: Mapping[str, Any]) -> Dict[str, Any]:
    """Insert everything a purchase needs. Returns the state dict (also saved by the caller).

    Idempotent per key: rows this harness owns are deleted by their keys before insert, so a
    second `seed` on a Postgres database replaces them rather than colliding.

    THE GUARD IS THE FIRST STATEMENT, not the caller's job. This function writes catalog_products
    through the process-global `database`; the ADR-011 tripwire exempts this file from the
    five-door rule only because the write itself refuses a non-local binding. A caller that
    forgot the CLI's checks still cannot make it write anywhere else.
    """
    _bound_database_url_check()
    from db.database import database
    from db.agents import create_agent
    from db.buyer_vault import hash_agent_user_ref
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as routes_reap

    merchant_domain = opts["merchant_domain"]
    merchant_id = opts["merchant_id"]
    product_key = f"prod::{merchant_id}::shopify::{opts['source_product_id']}"
    variant_key = f"sku::{product_key}::v1"
    offer_id = f"off_{variant_key}"
    variant_title = opts["variant_title"] or None

    for sql, params in (
        ("DELETE FROM catalog_offers WHERE offer_id = :k", {"k": offer_id}),
        ("DELETE FROM catalog_skus WHERE sku_key = :k", {"k": variant_key}),
        ("DELETE FROM catalog_products WHERE product_key = :k", {"k": product_key}),
        ("DELETE FROM catalog_merchants WHERE merchant_id = :k", {"k": merchant_id}),
        (
            "DELETE FROM reap_agentic_eligibility WHERE merchant_domain = :d AND market_country = :m",
            {"d": merchant_domain, "m": opts["market"]},
        ),
    ):
        await database.execute(sql, params)

    await database.execute(
        """
        INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status,
                                       source_system)
        VALUES (:mid, :name, 'shopify', 'active', 'reap_local_e2e')
        """,
        {"mid": merchant_id, "name": merchant_domain},
    )
    await database.execute(
        """
        INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,
                                      title, brand, category, product_type, source_domain)
        VALUES (:pk, :mid, 'shopify', :spid, :title, :brand, :category, :ptype, :domain)
        """,
        {
            "pk": product_key, "mid": merchant_id, "spid": opts["source_product_id"],
            "title": opts["product_title"], "brand": opts["brand"],
            "category": opts["category"], "ptype": opts["category"], "domain": merchant_domain,
        },
    )
    await database.execute(
        """
        INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
                                  source_product_id, source_variant_id, title, currency)
        VALUES (:sk, :pk, :mid, 'shopify', :spid, 'v1', :title, :currency)
        """,
        {
            "sk": variant_key, "pk": product_key, "mid": merchant_id,
            "spid": opts["source_product_id"], "title": variant_title,
            "currency": opts["currency"],
        },
    )
    await database.execute(
        """
        INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency,
                                    merchant_effective_price)
        VALUES (:oid, :sk, :pk, :mid, :currency, :price)
        """,
        {
            "oid": offer_id, "sk": variant_key, "pk": product_key, "mid": merchant_id,
            "currency": opts["currency"], "price": opts["price"],
        },
    )
    await database.execute(
        """
        INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                              market_country, enabled)
        VALUES (:d, :merchant_row, '', :m, :enabled)
        """,
        {
            "d": merchant_domain, "merchant_row": ledger.ELIGIBILITY_MERCHANT_ROW,
            "m": opts["market"], "enabled": True,
        },
    )

    # The agent, through the repo's own writer: `agents.api_key` holds the plaintext key, which
    # `get_agent_by_key` reads when there is no hash-key table (SQLite, or a fresh local
    # Postgres). A local Postgres that DOES have `api_keys` gets the hash row too.
    agent = await create_agent(
        agent_name="reap-local-e2e", agent_type="custom", description="local e2e harness"
    )
    if _is_postgres():
        present = await database.fetch_one("SELECT to_regclass('public.api_keys') AS t")
        if present and dict(present).get("t"):
            import hashlib

            await database.execute(
                """
                INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
                VALUES (:a, 'reap-local-e2e', :h, :p, 'active')
                """,
                {
                    "a": agent["agent_id"],
                    "h": hashlib.sha256(agent["api_key"].encode()).hexdigest(),
                    "p": agent["api_key"][:12],
                },
            )

    pem, jwks = _new_signing_key()
    _write_private(state_dir / "signing_key.pem", pem)
    _write_private(state_dir / "jwks.json", json.dumps(jwks, indent=2))

    state: Dict[str, Any] = {
        "seeded_at": datetime.now(timezone.utc).isoformat(),
        "database_url": opts["database_url"],
        "agent_id": agent["agent_id"],
        "agent_api_key": agent["api_key"],
        "buyer_subject": f"local-buyer-{secrets.token_hex(4)}",
        "buyer_session_id": f"sess_{secrets.token_hex(8)}",
        "merchant_domain": merchant_domain,
        "merchant_id": merchant_id,
        "product_key": product_key,
        "variant_key": variant_key,
        "product_title": opts["product_title"],
        "variant_title": variant_title,
        "brand": opts["brand"],
        "category": opts["category"],
        "price": opts["price"],
        "currency": opts["currency"],
        "market": opts["market"],
        "consent_version": opts["consent_version"],
        "email": opts["email"],
        "seeded_enrollment": None,
        "purchases": [],
    }

    enrollment_id = opts.get("seed_enrollment")
    if enrollment_id:
        # REUSE AN ENROLLMENT THAT IS ALREADY ACTIVE AT REAP, so no card has to be typed again.
        # The chain the route and the poller will walk, written here in the same shapes:
        #   buyer_identity_links (agent, hash(<iss>:<sub>)) -> buyer_id      [route's own minting]
        #   reap_agentic_buyer_refs buyer_id -> reap_buyer_ref               [the Reap owner.id]
        #   reap_agentic_enrollments buyer_ref, status 'active', reap_enrollment_id  [ledger API]
        # `_step_resolving` then finds `get_active_enrollment(buyer_ref)` and goes straight to
        # 'quoting'; the checkout is created against that reap_enrollment_id.
        buyer_ref = opts["buyer_ref"]
        user_hash = hash_agent_user_ref(agent_user_ref_for(state))
        buyer_id = await routes_reap._buyer_id_for(
            agent_id=agent["agent_id"], agent_user_ref_hash=user_hash
        )
        await database.execute(
            "DELETE FROM reap_agentic_buyer_refs WHERE buyer_id = :b OR reap_buyer_ref = :r",
            {"b": buyer_id, "r": buyer_ref},
        )
        await database.execute(
            """
            INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref, consent_version,
                                                 consented_at)
            VALUES (:b, :r, :cv, :at)
            """,
            {
                "b": buyer_id, "r": buyer_ref, "cv": opts["consent_version"],
                "at": ledger._bind_dt(datetime.now(timezone.utc)),
            },
        )
        existing = await ledger.get_enrollment_by_reap_id(enrollment_id)
        if existing is not None:
            await database.execute(
                "DELETE FROM reap_agentic_enrollments WHERE id = :id", {"id": existing["id"]}
            )
        pending = await ledger.upsert_pending_enrollment(
            buyer_ref=buyer_ref, agent_id=agent["agent_id"], reap_enrollment_id=enrollment_id,
            reap_status="ACTIVE",
        )
        active = await ledger.mark_enrollment_active(
            pending["id"], reap_enrollment_id=enrollment_id, reap_status="ACTIVE"
        )
        if active is None:
            raise HarnessRefused("the seeded enrollment could not be marked active")
        state["seeded_enrollment"] = {
            "enrollment_id": active["id"],
            "reap_enrollment_id": enrollment_id,
            "buyer_ref": buyer_ref,
            "buyer_id": buyer_id,
        }
    return state


# ── Reap calls: recorded, redacted, sandbox-only ─────────────────────────────────────────────


class CallLog:
    """Every Reap call, redacted, rewritten to disk after each call so a crash keeps the record."""

    def __init__(self, path: Optional[Path], secret: Optional[str]):
        self.path = path
        self._secret = secret or ""
        self.entries: List[Dict[str, Any]] = []

    def add(self, entry: Dict[str, Any]) -> None:
        self.entries.append(redact(entry))
        self.flush()

    def text(self) -> str:
        text = json.dumps({"calls": self.entries}, indent=2, default=str)
        if self._secret and self._secret in text:
            # Belt and braces: a key that reached the log by some path `redact` did not know.
            text = text.replace(self._secret, "<redacted>")
        return text

    def flush(self) -> None:
        if self.path is not None:
            _write_private(self.path, self.text() + "\n")


def _json_or_text(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return raw[:2000].decode("utf-8", "replace")


def _make_recording_transport(inner_factory: Callable[[], Any], log: CallLog):
    import httpx

    class _RecordingTransport(httpx.AsyncBaseTransport):
        """Refuses every host but the sandbox, and records each exchange with credentials
        redacted. Wraps a real transport, or the dry-run fake's MockTransport."""

        def __init__(self) -> None:
            self._inner = inner_factory()

        async def handle_async_request(self, request):
            host = (request.url.host or "").lower()
            if host not in SANDBOX_HOSTS:
                raise HarnessRefused(f"refusing egress to {host!r}: only the Reap sandbox")
            started = time.monotonic()
            body = request.content if hasattr(request, "content") else b""
            entry: Dict[str, Any] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "method": request.method,
                "url": str(request.url),
                "request_headers": dict(request.headers),
                "request_body": _json_or_text(body),
            }
            try:
                response = await self._inner.handle_async_request(request)
                # `aread` DECODES (gzip etc.), so the response handed back below must not
                # claim an encoding any more — its content-encoding/length headers are dropped.
                content = await response.aread()
            except Exception as exc:
                entry.update(error=type(exc).__name__,
                             elapsed_ms=int((time.monotonic() - started) * 1000))
                log.add(entry)
                raise
            entry.update(
                status=response.status_code,
                response_body=_json_or_text(content),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            log.add(entry)
            headers = [
                (k, v) for k, v in response.headers.multi_items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            ]
            return httpx.Response(response.status_code, headers=headers, content=content,
                                  request=request)

        async def aclose(self) -> None:
            await self._inner.aclose()

    return _RecordingTransport


@contextlib.contextmanager
def route_reap_calls(inner_factory: Callable[[], Any], log: CallLog):
    """Every `httpx.AsyncClient` built inside this block goes through `_RecordingTransport`.

    The Reap client builds a fresh `httpx.AsyncClient` per call and passes no transport, so
    replacing the class for the duration of the poll is the one seam that sees every call. An
    explicit transport also disables httpx's env-proxy mounts, which is what this Mac's proxy
    needs (it drops reap.global); NO_PROXY is set as well.
    """
    import httpx

    real = httpx.AsyncClient
    transport_cls = _make_recording_transport(inner_factory, log)

    class _HarnessClient(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport_cls()
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = _HarnessClient
    try:
        yield
    finally:
        httpx.AsyncClient = real


# ── the dry-run fake Reap ────────────────────────────────────────────────────────────────────


class FakeReapSandbox:
    """Reap's sandbox, as the Reap client sees it over HTTP, for ONE seeded product.

    Payload shapes are the repo's own fixtures (tests/test_reap_agentic_client.py FENTY_* /
    SINGLE_VARIANT_*, tests/test_reap_agentic_purchase.py LIVE_QUOTE, ENROLLMENT_*, CHECKOUT_*),
    re-keyed to the seeded product. It sits UNDER the real client — search, match, option
    resolution, quote verification, the simulate header and the hosted-URL allowlist all run for
    real — which is why it is a transport and not a replacement for the six client functions.

    The HUMAN is simulated too, and only on the reads: an enrollment turns ACTIVE on its second
    GET, and a checkout is approved on its second GET. What happens after approval mirrors the
    sandbox measured on 2026-09-25: PROCESSING then COMPLETED with an orderId IF the checkout was
    created with `X-Simulate-Checkout: COMPLETED`, FAILED otherwise.
    """

    def __init__(self, state: Mapping[str, Any], *, shipping: str = "6.99"):
        self.domain = state["merchant_domain"]
        self.title = state["product_title"]
        self.variant_title = state.get("variant_title") or ""
        self.price = Decimal(str(state["price"]))
        self.currency = state["currency"]
        self.shipping = Decimal(shipping)
        self.product_id = f"prd_{uuid.uuid4().hex}"
        self.variant_id = f"var_{uuid.uuid4().hex[:16]}"
        self.enrollments: Dict[str, Dict[str, Any]] = {}
        self.checkouts: Dict[str, Dict[str, Any]] = {}
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.seen_paths: List[str] = []

    @staticmethod
    def _iso(delta_seconds: int) -> str:
        at = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
        return at.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _money(self, amount: Decimal) -> Dict[str, Any]:
        return {"amount": float(amount), "currency": self.currency}

    def _variant(self) -> Dict[str, Any]:
        options = [{"name": "Size", "value": self.variant_title}] if self.variant_title else []
        return {
            "id": self.variant_id, "name": self.variant_title or "Default",
            "options": options, "price": self._money(self.price),
            "available": True, "requiresShipping": True, "media": [],
        }

    def handler(self, request):
        import httpx

        path = request.url.path
        self.seen_paths.append(f"{request.method} {path}")
        if not str(request.headers.get("authorization", "")).startswith("Bearer "):
            return httpx.Response(401, json={"error": {"code": "UNAUTHORIZED"}})
        body = _json_or_text(request.content) if request.method == "POST" else None
        body = body if isinstance(body, dict) else {}

        if request.method == "POST" and path == "/agentic/products/search":
            return httpx.Response(200, json={
                "id": f"srch_{uuid.uuid4().hex[:8]}",
                "products": [{
                    "id": self.product_id, "merchant": {"name": self.domain},
                    "name": self.title, "available": True,
                    "priceRange": {"min": self._money(self.price), "max": self._money(self.price)},
                }],
                "pagination": {"nextCursor": None, "hasNextPage": False, "returnedCount": 1},
                "warnings": [],
            })
        if request.method == "POST" and path == "/agentic/products/details":
            options = ([{"name": "Size", "values": [
                {"optionId": "opt_os", "label": self.variant_title, "available": True},
            ]}] if self.variant_title else [])
            return httpx.Response(200, json={"products": [{
                "id": self.product_id, "merchant": {"name": self.domain}, "name": self.title,
                "media": [], "options": options, "defaultVariant": self._variant(),
            }], "errors": []})
        if request.method == "POST" and path == "/agentic/products/variant":
            return httpx.Response(200, json=self._variant())
        if request.method == "POST" and path == "/agentic/quotes":
            items = body.get("items") or [{}]
            quantity = int((items[0] or {}).get("quantity") or 1)
            subtotal = self.price * quantity
            quote_id = f"qt_{uuid.uuid4().hex[:16]}"
            final = subtotal + self.shipping
            self.quotes[quote_id] = {"final": final}
            return httpx.Response(200, json={
                "id": quote_id,
                "expiresAt": self._iso(300),
                "amountBreakdown": {
                    "itemsSubtotal": self._money(subtotal),
                    "shipping": self._money(self.shipping),
                    "tax": {"amount": {"amount": 0, "currency": self.currency}},
                    "discounts": [], "additionalCharges": [],
                    "finalAmount": self._money(final),
                },
                "shippingOptions": [{
                    "id": "ship_std", "name": "Standard", "selected": True,
                    "price": self._money(self.shipping),
                }],
            })
        if request.method == "POST" and path == "/agentic/enrollments":
            enrollment_id = str(uuid.uuid4())
            self.enrollments[enrollment_id] = {"status": "REQUIRES_ACTION", "reads": 0}
            return httpx.Response(200, json={
                "id": enrollment_id, "status": "REQUIRES_ACTION", "source": "EXTERNAL",
                "owner": body.get("owner"),
                "nextAction": {"type": "REDIRECT",
                               "url": f"https://pay.prava.space/enroll/{enrollment_id}",
                               "expiresAt": self._iso(900)},
            })
        if request.method == "GET" and path.startswith("/agentic/enrollments/"):
            enrollment_id = path.rsplit("/", 1)[-1]
            record = self.enrollments.setdefault(enrollment_id, {"status": "ACTIVE", "reads": 0})
            record["reads"] += 1
            if record["status"] != "ACTIVE" and record["reads"] >= 2:
                record["status"] = "ACTIVE"  # the simulated human finished card entry
            if record["status"] == "ACTIVE":
                return httpx.Response(200, json={
                    "id": enrollment_id, "status": "ACTIVE",
                    "paymentMethod": {"type": "CARD", "network": "VISA", "last4": "4242"},
                    "nextAction": None,
                })
            return httpx.Response(200, json={
                "id": enrollment_id, "status": "REQUIRES_ACTION",
                "nextAction": {"type": "REDIRECT",
                               "url": f"https://pay.prava.space/enroll/{enrollment_id}",
                               "expiresAt": self._iso(600)},
            })
        if request.method == "POST" and path == "/agentic/checkouts":
            enrollment = self.enrollments.get(str(body.get("enrollmentId") or ""))
            if enrollment is not None and enrollment["status"] != "ACTIVE":
                return httpx.Response(409, json={"error": {"code": "ENROLLMENT_NOT_ACTIVE"}})
            quote = self.quotes.get(str(body.get("quoteId") or ""))
            if quote is None:
                return httpx.Response(409, json={"error": {"code": "QUOTE_EXPIRED"}})
            checkout_id = f"chk_{uuid.uuid4().hex[:16]}"
            simulate = request.headers.get("x-simulate-checkout") == "COMPLETED"
            self.checkouts[checkout_id] = {"reads": 0, "simulate": simulate,
                                           "final": quote["final"]}
            return httpx.Response(200, json={
                "id": checkout_id, "status": "REQUIRES_ACTION",
                "quoteId": body.get("quoteId"), "enrollmentId": body.get("enrollmentId"),
                "amount": self._money(quote["final"]),
                "nextAction": {"type": "REDIRECT",
                               "url": f"https://pay.prava.space/checkout/{checkout_id}",
                               "expiresAt": self._iso(900)},
            })
        if request.method == "GET" and path.startswith("/agentic/checkouts/"):
            checkout_id = path.rsplit("/", 1)[-1]
            record = self.checkouts.get(checkout_id)
            if record is None:
                return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
            record["reads"] += 1
            if record["reads"] == 1:
                status = "REQUIRES_ACTION"     # the buyer has not approved yet
            elif not record["simulate"]:
                status = "FAILED"              # approved, but the sandbox fails without the header
            elif record["reads"] == 2:
                status = "PROCESSING"          # approved; the simulated merchant order is placing
            else:
                status = "COMPLETED"
            payload: Dict[str, Any] = {"id": checkout_id, "status": status, "nextAction": None}
            if status == "REQUIRES_ACTION":
                payload["nextAction"] = {"type": "REDIRECT",
                                         "url": f"https://pay.prava.space/checkout/{checkout_id}",
                                         "expiresAt": self._iso(600)}
            if status == "COMPLETED":
                payload["orderId"] = f"ord_dry_{checkout_id[4:12]}"
                payload["finalAmount"] = self._money(record["final"])
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "path": path}})


# ── purchase ─────────────────────────────────────────────────────────────────────────────────


def purchase_body(state: Mapping[str, Any], opts: Mapping[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "merchant_domain": state["merchant_domain"],
        "product_key": state["product_key"],
        "variant_key": state["variant_key"],
        "quantity": int(opts.get("quantity") or 1),
        "buyer": {
            "email": opts.get("email") or state["email"],
            "shipping_address": dict(DEFAULT_ADDRESS),
            "consent_version": state["consent_version"],
        },
        "idempotency_key": f"local-e2e-{uuid.uuid4().hex[:16]}",
    }
    if opts.get("return_url"):
        body["return_url"] = opts["return_url"]
    return body


def _purchase_headers(state_dir: Path, state: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "X-API-Key": state["agent_api_key"],
        "X-Agent-User-JWT": mint_buyer_jwt(state_dir, state),
        "Content-Type": "application/json",
    }


async def post_purchase(
    state_dir: Path, state: Mapping[str, Any], opts: Mapping[str, Any], *, in_process: bool,
    base_url: str = "http://127.0.0.1:8765",
) -> Dict[str, Any]:
    """POST the purchase. `in_process` talks to the real app over ASGI (dry run); otherwise to the
    `serve` process over HTTP. Either way the REAL auth dependencies run: the agent key is looked
    up in `agents`, the JWT is verified against the JWKS `seed` wrote."""
    import httpx

    if in_process:
        _bound_database_url_check()  # the in-process app writes the ledger
    body = purchase_body(state, opts)
    headers = _purchase_headers(state_dir, state)
    if in_process:
        from main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local-e2e") as http:
            resp = await http.post(_PURCHASE_PATH, json=body, headers=headers)
    else:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0, trust_env=False) as http:
            resp = await http.post(_PURCHASE_PATH, json=body, headers=headers)
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {"raw": resp.text[:500]}
    return {"status": resp.status_code, "body": payload}


# ── poll ─────────────────────────────────────────────────────────────────────────────────────


def _fmt(value: Any) -> str:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _money_minor(value: Any, currency: Any) -> str:
    if value is None:
        return "-"
    return f"{Decimal(int(value)) / 100:.2f} {currency or ''}".strip()


async def attribution_edges(order_id: str) -> Dict[str, Any]:
    """The edge row(s) `_close_attribution` wrote: `commerce_attribution_edges`, keyed by
    (merchant_id, external_order_id)."""
    from db.database import database

    try:
        rows = await database.fetch_all(
            """
            SELECT edge_id, merchant_id, click_id, external_order_id, agent_id, state, source,
                   gross_attributed_gmv_cents, currency, converted_at, metadata
              FROM commerce_attribution_edges
             WHERE external_order_id = :oid
            """,
            {"oid": order_id},
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "rows": []}
    out = []
    for raw in rows:
        row = dict(raw)
        meta = row.get("metadata")
        if isinstance(meta, str):
            with contextlib.suppress(Exception):
                row["metadata"] = json.loads(meta)
        out.append({k: (_fmt(v) if isinstance(v, datetime) else v) for k, v in row.items()})
    return {"error": None, "rows": out}


async def poll_until_terminal(
    purchase_id: str,
    *,
    interval: float,
    timeout: float,
    out: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Drive `run_reap_agentic_purchase_poll` (the job's own tick — sweeps, claim, `advance`,
    release) every `interval` seconds and narrate the row until it is terminal or `timeout`.

    The row's own `next_poll_at` still decides when it is due, so the cadence is the real one:
    'awaiting_approval' is looked at every 30 s and 'processing' every 15 s.
    """
    import db.reap_agentic_ledger as ledger
    import jobs.reap_agentic_purchase_poll as job
    import routes.agent_commerce_reap as routes_reap
    import services.reap_agentic_client as rc

    _bound_database_url_check()  # every tick writes the ledger (and, on completion, the edge)
    started = time.monotonic()
    transitions: List[Dict[str, Any]] = []
    last_state: Optional[str] = None
    last_error: Optional[str] = None
    shown_urls: set = set()
    view: Dict[str, Any] = {}

    def stamp() -> str:
        return f"[+{time.monotonic() - started:6.1f}s]"

    while True:
        report = await job.run_reap_agentic_purchase_poll()
        if report.errors:
            out(f"{stamp()} poll tick reported errors={report.errors} (see the server/poll log)")
        row = await ledger.get_purchase_internal(purchase_id)
        if row is None:
            raise HarnessRefused(f"purchase {purchase_id} is not in this database")
        view = ledger.public_purchase_view(row) or {}
        state = str(view.get("state") or "")
        if state != last_state:
            out(f"{stamp()} {last_state or '(start)'} -> {state}")
            transitions.append({"state": state, "at_s": round(time.monotonic() - started, 1)})
            last_state = state
        code = view.get("last_error_code")
        if code and code != last_error:
            out(f"{stamp()}   last_error_code={code}")
            last_error = code

        hosted = view.get("hosted_url")
        if state == "needs_enrollment" and hosted and hosted not in shown_urls:
            shown_urls.add(hosted)
            out("=" * 78)
            out("CARD ENTRY — a HUMAN opens this on Reap's page and types their OWN card.")
            out(f"    {hosted}")
            out(f"page expires : {_fmt(view.get('hosted_url_expires_at'))}")
            out("=" * 78)
        if state == "awaiting_approval" and hosted and hosted not in shown_urls:
            shown_urls.add(hosted)
            deadline = routes_reap.approval_deadline(
                view.get("reap_quote_expires_at"), view.get("hosted_url_expires_at")
            )
            out("=" * 78)
            out("APPROVE — a HUMAN opens this on Reap's page. Nothing is charged in the sandbox.")
            out(f"    {hosted}")
            out(f"quote        : {_money_minor(view.get('quoted_total_minor'), view.get('currency'))}"
                f" (items {_money_minor(view.get('our_price_minor'), view.get('currency'))}"
                f" x {view.get('quantity')}, shipping "
                f"{_money_minor(view.get('shipping_minor'), view.get('currency'))}, tax "
                f"{_money_minor(view.get('tax_minor'), view.get('currency'))})")
            out(f"quote expires: {_fmt(view.get('reap_quote_expires_at'))}")
            out(f"page expires : {_fmt(view.get('hosted_url_expires_at'))}")
            if deadline is not None:
                left = (deadline - datetime.now(timezone.utc)).total_seconds()
                out(f"APPROVE BEFORE {_fmt(deadline)}  ({int(left)} s from now). The checkout goes"
                    " FAILED 1-10 s after the QUOTE expires, not the page.")
            out("=" * 78)

        if state in _TERMINAL:
            break
        if time.monotonic() - started >= timeout:
            out(f"{stamp()} timeout after {timeout:.0f}s in state {state!r}; the row is left as is")
            break
        await asyncio.sleep(interval)

    result: Dict[str, Any] = {"purchase_id": purchase_id, "state": last_state,
                              "transitions": transitions, "view": view}
    if last_state == "completed":
        order_id = str(view.get("reap_order_id") or "")
        result["order_reference"] = order_id
        edges = await attribution_edges(order_id) if order_id else {"error": None, "rows": []}
        result["attribution_edges"] = edges
    elif last_state in _TERMINAL:
        reason = view.get("refusal_reason") or view.get("last_error_code")
        result["explanation"] = rc.explain_refusal(reason) if reason else None
    return result


def _print_result(result: Mapping[str, Any], out: Callable[[str], None] = print) -> None:
    view = dict(result.get("view") or {})
    out("\n--- LEDGER ROW (public columns only) " + "-" * 40)
    out(json.dumps({k: _fmt(v) if isinstance(v, datetime) else v for k, v in view.items()},
                   indent=2, default=str))
    if result.get("state") == "completed":
        out(f"\nORDER REFERENCE : {result.get('order_reference')}  (Reap's orderId; the merchant "
            "order is SIMULATED in the sandbox)")
        edges = result.get("attribution_edges") or {}
        rows = edges.get("rows") or []
        out("\n--- ATTRIBUTION EDGE (commerce_attribution_edges) " + "-" * 26)
        if rows:
            out(json.dumps(rows, indent=2, default=str))
        else:
            out("NO EDGE ROW for this order.")
            if edges.get("error"):
                out(f"  the read failed: {edges['error']}")
            if not _is_postgres():
                out("  On SQLite this is EXPECTED: the close statement "
                    "(services.commerce_attribution_service._CLOSE_EXTERNAL_CONVERSION_SQL)\n"
                    "  is Postgres-only ('[]'::jsonb), so `_close_attribution` logs "
                    "error_type=OperationalError\n"
                    "  and returns False. Re-run with --database-url "
                    "postgresql://localhost/<db> to see the edge.")
            else:
                out(f"  last_error_code={view.get('last_error_code')!r} names why "
                    "(see the runbook's 'three codes that suppress the attribution edge').")
    elif result.get("explanation"):
        out(f"\nWHY: {result['explanation']}")


# ── commands ─────────────────────────────────────────────────────────────────────────────────


def _resolve_database_url(args, state: Optional[Mapping[str, Any]] = None) -> str:
    url = args.database_url or (state or {}).get("database_url") or _default_database_url(
        Path(args.state_dir)
    )
    return check_local_database_url(url)


def _resolve_base(args) -> str:
    """`--reap-base-url`, else the shell's REAP_API_BASE_URL, else the sandbox — and whichever it
    is must BE the sandbox. A shell exporting a production base is refused, not ignored."""
    return check_sandbox_base_url(
        args.reap_base_url or os.environ.get("REAP_API_BASE_URL") or DEFAULT_REAP_BASE_URL
    )


def _resolve_reap(args, *, need_key: bool) -> tuple:
    """(base, key). ONLY `poll`/`run` load the real key, and only from the env file: `serve`
    never calls Reap (the routes write the ledger; the scheduler registers no jobs), so it gets
    the placeholder, which is enough to arm the route. A dry run never reads the env file."""
    base = _resolve_base(args)
    if getattr(args, "dry_run", False) or not need_key:
        return base, DRY_RUN_PLACEHOLDER_KEY
    loaded = _load_env_file()
    for name in ("REAP_API_BASE_URL", "REAP_API_BASE"):
        if loaded.get(name):
            try:
                check_sandbox_base_url(loaded[name])
            except HarnessRefused:
                # The key sitting next to a non-sandbox base may not be a sandbox key.
                raise HarnessRefused(
                    f"{name} in the env file is not the sandbox; refusing to use the key that "
                    "came with it"
                ) from None
    key = (loaded.get("REAP_API_KEY") or "").strip()
    if not key:
        raise HarnessRefused(
            f"REAP_API_KEY not found in {DEFAULT_ENV_FILE} (or $REAP_SANDBOX_ENV). "
            "Use --dry-run to exercise the harness without it."
        )
    return base, key


def _prepare_process(args, database_url: str, base: str, key: Optional[str]) -> Dict[str, str]:
    """Replace this process's environment and chdir to the repo. EVERY check runs BEFORE the
    first mutation, so a refusal leaves the caller's environment and cwd exactly as they were."""
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        raise HarnessRefused("internal: the state dir must be absolute before the chdir")
    database_url = check_local_database_url(database_url)
    base = check_sandbox_base_url(base)
    env = _harness_env(database_url=database_url, reap_base_url=base, reap_api_key=key,
                       state_dir=state_dir)
    _apply_env(env)
    sys.path.insert(0, str(REPO_ROOT))
    os.chdir(str(REPO_ROOT))
    _bound_database_url_check()
    return env


def cmd_seed(args) -> int:
    state_dir = Path(args.state_dir)
    # EVERY REFUSAL BEFORE THE FIRST FILESYSTEM EFFECT. `--reset` deletes files; a refused URL
    # must not cost the operator their state dir.
    database_url = check_local_database_url(args.database_url or _default_database_url(state_dir))
    try:
        price = Decimal(str(args.price))
    except InvalidOperation:
        raise HarnessRefused(f"--price {args.price!r} is not a decimal") from None
    if price <= 0 or price.as_tuple().exponent < -2:
        raise HarnessRefused("--price must be positive with at most two decimals")
    base = _resolve_base(args)
    enrollment = (args.seed_enrollment or "").strip() or None
    if enrollment:
        # `services.reap_agentic_client` does not import `db.database`, so this cannot bind the
        # database before `_prepare_process` has set DATABASE_URL.
        sys.path.insert(0, str(REPO_ROOT))
        import services.reap_agentic_client as rc

        enrollment = _load_steps_module()._checked_id(rc, enrollment, what="enrollment", uuid=True)

    _ensure_state_dir(state_dir, create=True)
    if args.reset:
        for name in ("local.db", "state.json", "jwks.json", "signing_key.pem"):
            with contextlib.suppress(FileNotFoundError):
                (state_dir / name).unlink()
    _precreate_sqlite_file(database_url)
    _prepare_process(args, database_url, base, DRY_RUN_PLACEHOLDER_KEY)

    opts = {
        "database_url": database_url,
        "merchant_domain": args.merchant_domain,
        "merchant_id": args.merchant_id,
        "source_product_id": args.source_product_id,
        "product_title": args.product_title,
        "variant_title": args.variant_title,
        "brand": args.brand,
        "category": args.category,
        "price": str(price),
        "currency": DEFAULTS["currency"],
        "market": DEFAULTS["market"],
        "consent_version": args.consent_version,
        "email": args.email,
        "seed_enrollment": enrollment,
        "buyer_ref": args.buyer_ref,
    }

    async def _go() -> Dict[str, Any]:
        from db.database import database

        await database.connect()
        try:
            await build_schema()
            return await seed_rows(state_dir, opts)
        finally:
            await database.disconnect()

    state = asyncio.run(_go())
    _save_state(state_dir, state)
    jwt_token = mint_buyer_jwt(state_dir, state, ttl_seconds=24 * 3600)
    print("\nSEEDED (LOCAL database, LOCAL test credentials only)")
    print(f"  database        : {database_url}")
    print(f"  state dir       : {state_dir}")
    print(f"  merchant        : {state['merchant_domain']} / market {state['market']} (eligible)")
    print(f"  product_key     : {state['product_key']}")
    print(f"  variant_key     : {state['variant_key']}  (title {state['variant_title']!r})")
    print(f"  price           : {state['price']} {state['currency']}")
    print(f"  agent_id        : {state['agent_id']}")
    print(f"  X-API-Key       : {state['agent_api_key']}")
    print(f"  X-Agent-User-JWT: {jwt_token}")
    print(f"  agent_user_ref  : {agent_user_ref_for(state)}  (sid {state['buyer_session_id']})")
    if state["seeded_enrollment"]:
        se = state["seeded_enrollment"]
        print(f"  enrollment      : ACTIVE, reap id {se['reap_enrollment_id']}, buyer_ref "
              f"{se['buyer_ref']}  -> no card entry; the purchase goes straight to quoting")
    else:
        print("  enrollment      : none -> the purchase will stop at needs_enrollment and print "
              "a card-entry URL")
    print("\nNext: `serve` in one terminal, `run` in another.")
    return 0


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def serve_command(args) -> tuple:
    """(argv, env) for `serve`, with every refusal made. Split from `cmd_serve` so the refusals
    and the environment can be tested without exec'ing uvicorn."""
    state_dir = _ensure_state_dir(Path(args.state_dir), create=False)
    state = _load_state(state_dir)
    database_url = _resolve_database_url(args, state)
    # NEVER the real key: `serve` makes no Reap call. See `_resolve_reap`.
    base, key = _resolve_reap(args, need_key=False)
    if args.host not in _LOOPBACK_HOSTS:
        raise HarnessRefused(f"serve binds loopback only ({', '.join(_LOOPBACK_HOSTS)})")
    env = _harness_env(database_url=database_url, reap_base_url=base, reap_api_key=key,
                       state_dir=state_dir)
    argv = [sys.executable, "-m", "uvicorn", "main:app", "--host", args.host,
            "--port", str(args.port)]
    return argv, env


def cmd_serve(args) -> int:
    argv, env = serve_command(args)
    print("serve: environment (allowlisted; nothing else from your shell is passed through)")
    for line in _serve_env_keys_for_display(env):
        print(f"  {line}")
    print("  (REAP_API_KEY is a placeholder: `serve` never calls Reap; only `poll` loads the key)")
    print(f"\nuvicorn main:app on http://{args.host}:{args.port}  (Ctrl-C to stop)\n")
    sys.stdout.flush()
    os.chdir(str(REPO_ROOT))
    os.execve(sys.executable, argv, env)
    return 0  # pragma: no cover


async def _purchase(args, state_dir: Path, state: Dict[str, Any]) -> Optional[str]:
    opts = {"quantity": args.quantity, "email": args.email, "return_url": args.return_url}
    base_url = f"http://127.0.0.1:{args.port}"
    resp = await post_purchase(state_dir, state, opts, in_process=args.dry_run, base_url=base_url)
    body = resp["body"] if isinstance(resp["body"], dict) else {}
    purchase_id = body.get("purchase_id")
    print(f"POST {_PURCHASE_PATH} -> {resp['status']}")
    print(json.dumps(body, indent=2))
    if resp["status"] != 202 or not purchase_id:
        print("\nThe route refused; nothing was opened. `detail.error` is the reason code "
              "(docs/reap_agentic_routes.md).")
        return None
    state.setdefault("purchases", []).append(purchase_id)
    state["last_purchase_id"] = purchase_id
    _save_state(state_dir, state)
    print(f"\npurchase_id {purchase_id}  state {body.get('status')}")
    return purchase_id


def _fast_schedule() -> None:
    """Dry run only: shrink the per-state poll intervals so the fake walks in seconds."""
    import services.reap_agentic_purchase as svc

    for name in list(svc.POLL_INTERVALS):
        svc.POLL_INTERVALS[name] = 1


async def _poll(args, state_dir: Path, state: Mapping[str, Any], purchase_id: str,
                key: str) -> Dict[str, Any]:
    import httpx

    log_path = Path(args.reap_log)  # made absolute by `main`, before the chdir
    log = CallLog(log_path, key)
    if args.dry_run:
        fake = FakeReapSandbox(state)
        inner: Callable[[], Any] = lambda: httpx.MockTransport(fake.handler)  # noqa: E731
        if args.fast:
            _fast_schedule()
    else:
        inner = lambda: httpx.AsyncHTTPTransport()  # noqa: E731
    print(f"poll: every {args.interval}s, timeout {args.timeout}s; Reap calls -> {log_path}"
          f"{' (FAKE Reap, no network)' if args.dry_run else ''}")
    with route_reap_calls(inner, log):
        result = await poll_until_terminal(purchase_id, interval=args.interval,
                                           timeout=args.timeout)
    result["reap_calls"] = len(log.entries)
    result["reap_log"] = str(log_path)
    return result


def _run_common(args, *, do_purchase: bool, do_poll: bool) -> int:
    state_dir = _ensure_state_dir(Path(args.state_dir), create=False)
    state = _load_state(state_dir)
    database_url = _resolve_database_url(args, state)
    base, key = _resolve_reap(args, need_key=do_poll)
    _prepare_process(args, database_url, base, key)
    if do_poll and not args.dry_run:
        print(f"REAP_API_KEY : loaded from the env file (value never printed); base {base}")

    async def _go() -> int:
        from db.database import database

        await database.connect()
        try:
            purchase_id = getattr(args, "purchase_id", None) or None
            if do_purchase:
                purchase_id = await _purchase(args, state_dir, state)
                if not purchase_id:
                    return 3
            if not do_poll:
                return 0
            purchase_id = purchase_id or state.get("last_purchase_id")
            if not purchase_id:
                raise HarnessRefused("no purchase id: run `purchase` first or pass --purchase-id")
            result = await _poll(args, state_dir, state, purchase_id, key)
            _print_result(result)
            print(f"\n{result['reap_calls']} Reap call(s) recorded, Authorization redacted: "
                  f"{result['reap_log']}")
            if args.result_json:
                _write_private(Path(args.result_json),
                               json.dumps(result, indent=2, default=str))
            return 0 if result.get("state") == "completed" else 4
        finally:
            await database.disconnect()

    return asyncio.run(_go())


def cmd_purchase(args) -> int:
    return _run_common(args, do_purchase=True, do_poll=False)


def cmd_poll(args) -> int:
    return _run_common(args, do_purchase=False, do_poll=True)


def cmd_run(args) -> int:
    return _run_common(args, do_purchase=True, do_poll=True)


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", default=None,
                        help=f"where the SQLite file, state.json and the JWKS live (0700; "
                             f"default $TMPDIR/{STATE_DIR_NAME}; refused if TMPDIR is unset)")
    common.add_argument("--database-url",
                        help="a LOCAL database; default sqlite+aiosqlite:///<state-dir>/local.db. "
                             "Postgres on localhost/127.0.0.1/::1 only.")
    common.add_argument("--reap-base-url", default=None,
                        help="default $REAP_API_BASE_URL, else the sandbox; either way it must "
                             "be exactly https://sandbox.api.reap.global")
    common.add_argument("--port", type=int, default=8765)
    sub = ap.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", parents=[common], help="build the schema and seed the rows")
    seed.add_argument("--reset", action="store_true",
                      help="delete the state dir's SQLite file and state first")
    seed.add_argument("--seed-enrollment", metavar="REAP_ENROLLMENT_UUID",
                      help="write an ACTIVE ledger enrollment bound to this Reap enrollment id, "
                           "so no card entry is needed")
    seed.add_argument("--buyer-ref", default=DEFAULTS["buyer_ref"],
                      help="the Reap owner.id the seeded enrollment belongs to")
    seed.add_argument("--merchant-domain", default=DEFAULTS["merchant_domain"])
    seed.add_argument("--merchant-id", default=DEFAULTS["merchant_id"])
    seed.add_argument("--source-product-id", default=DEFAULTS["source_product_id"])
    seed.add_argument("--product-title", default=DEFAULTS["product_title"])
    seed.add_argument("--variant-title", default=DEFAULTS["variant_title"],
                      help="must equal Reap's option label EXACTLY; '' for a no-variant product")
    seed.add_argument("--brand", default=DEFAULTS["brand"])
    seed.add_argument("--category", default=DEFAULTS["category"])
    seed.add_argument("--price", default=DEFAULTS["price"], help="major units, e.g. 1.98")
    seed.add_argument("--consent-version", default=DEFAULTS["consent_version"])
    seed.add_argument("--email", default=DEFAULTS["email"])
    seed.set_defaults(func=cmd_seed)

    serve = sub.add_parser("serve", parents=[common], help="run uvicorn on loopback")
    serve.add_argument("--host", default="127.0.0.1")
    serve.set_defaults(func=cmd_serve)

    def _purchase_args(p):
        p.add_argument("--quantity", type=int, default=1)
        p.add_argument("--email", default=None, help="buyer email (default: the seeded one)")
        p.add_argument("--return-url", default=None,
                       help="must be https on REAP_RETURN_URL_HOSTS; default the rail's own")

    def _poll_args(p):
        p.add_argument("--interval", type=float, default=5.0)
        p.add_argument("--timeout", type=float, default=900.0)
        p.add_argument("--reap-log", default=None,
                       help="JSON log of every Reap call (default ./reap_local_e2e_<ts>.json)")
        p.add_argument("--fast", action="store_true",
                       help="dry run only: 1-second per-state poll intervals")
        p.add_argument("--result-json", default=None, help=argparse.SUPPRESS)

    for name, func, helptext in (
        ("purchase", cmd_purchase, "POST the purchase to the local server"),
        ("poll", cmd_poll, "drive the poller in-process until terminal"),
        ("run", cmd_run, "purchase, then poll"),
    ):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--dry-run", action="store_true",
                       help="fake Reap in-process, no network, no key; the purchase goes "
                            "through the real app in-process")
        if name in ("purchase", "run"):
            _purchase_args(p)
        if name in ("poll", "run"):
            _poll_args(p)
        if name == "poll":
            p.add_argument("--purchase-id", default=None)
        p.set_defaults(func=func)
    return ap


def _absolutize_paths(args, environ: Mapping[str, str]) -> None:
    """Every path the operator typed, made absolute against THEIR cwd, before anything chdirs."""
    args.state_dir = str(_resolve_state_dir(args.state_dir, environ))
    args.database_url = _absolutize_sqlite_url(args.database_url)
    if hasattr(args, "reap_log"):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.reap_log = os.path.abspath(args.reap_log or f"reap_local_e2e_{stamp}.json")
    if getattr(args, "result_json", None):
        args.result_json = os.path.abspath(args.result_json)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "fast", False) and not getattr(args, "dry_run", False):
        print("REFUSED: --fast is a dry-run option; the real run keeps the real cadence.")
        return 2
    try:
        if os.environ.get("REAP_API_KEY"):
            # A key exported in a shell has no provenance; it may be a production key. It is
            # never used, and it is not silently ignored either.
            raise HarnessRefused(
                "REAP_API_KEY is set in your shell. This harness takes the key ONLY from "
                f"{DEFAULT_ENV_FILE} (or $REAP_SANDBOX_ENV); unset it and re-run"
            )
        _absolutize_paths(args, os.environ)
        return int(args.func(args))
    except HarnessRefused as exc:
        print(f"REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
