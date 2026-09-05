"""Regression tests for three guards that read like protection but never ran.

1. routes/operations_routes.py called `check_permission(credentials, "operator")`
   as a bare statement at all ten call sites. `check_permission` RETURNS a bool
   and never raises, so the result was discarded and every route answered any
   authenticated caller. Separately, "operator" is a ROLE name and the map it is
   looked up in is keyed by role with PERMISSION values, so the string matched
   nothing and only the admin/super_admin early-returns would have passed.
2. orchestrator/payment_orchestrator.py called event_publisher with kwargs the
   publishers do not accept; the TypeError was swallowed by `except Exception`.
   That whole module was DELETED on 2026-08-31 -- it stood on the empty
   `psp.connectors.psp_manager` registry and could never reach a PSP -- so the
   section that pinned its two publisher call sites is gone with it. See
   tests/test_dead_psp_connectors_removed.py. dashboard.core's model repair
   (section 4 below) is NOT affected: routes/demo_data_routes.py still uses it.
3. config/production.py declared min_length=32 on the JWT secret, but nothing
   instantiated ProductionSettings (and it could not even be imported under
   Pydantic v2), so the live secret in config/settings.py was unchecked. That
   file is now deleted and the check lives where the secret does.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]

STRONG_SECRET = "x" * 64


# ---------------------------------------------------------------------------
# 1. Operations routes actually enforce authorization
# ---------------------------------------------------------------------------

ALLOWED_ROLES = ["super_admin", "admin", "employee", "operator"]
DENIED_ROLES = ["merchant", "agent", "outsourced", "viewer", "", "not_a_role"]


@pytest.fixture(scope="module")
def ops_client():
    from routes.operations_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _token(role):
    """Mint the claim set the REAL staff issuers emit.

    create_jwt_token(user_id, role) omits `email`, and every issuer that
    successfully mints a usable token includes it: scripts/mint_employee_jwt
    writes sub/email/role/user_id and routes/auth_routes writes the same at
    login.

    One caller does TRY the email-less shape — routes/dashboard_api's login —
    and produces nothing only because it passes an `expires_delta=` kwarg the
    3-arg create_jwt_token does not accept, so it raises TypeError into a 500.
    Worth stating precisely rather than "no issuer produces this shape": if that
    login is ever repaired it mints tokens that 401 against these ten routes and
    every other get_current_user route in the app.

    Using the thin helper meant these tests exercised a token production never
    successfully mints — and it 401s against the header dependency, which is how
    the discrepancy surfaced.
    """
    from utils.auth import create_access_token

    return create_access_token(
        {"sub": f"{role}@pivota.cc", "email": f"{role}@pivota.cc", "role": role}
    )


def _auth(role):
    """The credential goes in the HEADER now, not the query string."""
    return {"Authorization": f"Bearer {_token(role)}"}


# Every route in the file, so a newly added one that forgets the guard is caught
# by the DENIED cases below rather than shipping open.
#
# This list said that while covering EIGHT of the ten. Deleting the guard from
# the two it missed left all 83 tests green. They were almost certainly skipped
# because a naive body makes FastAPI answer 422 before the handler runs, so the
# guard is never reached: the PUT needs its two query params, and the welcome
# email needs a `status` body. Getting the request shape right is the whole
# difference between covering a route and appearing to.
OPS_REQUESTS = [
    ("POST", "/api/operations/agents/onboard",
     {"agent_name": "A", "contact_email": "a@x.com"}),
    ("POST", "/api/operations/merchants/onboard",
     {"merchant_name": "M", "contact_email": "m@x.com",
      "store_url": "https://m.example", "platform": "shopify"}),
    ("GET", "/api/operations/onboarding-queue", None),
    ("POST", "/api/operations/verify",
     {"entity_id": "AGENT_1", "entity_type": "agent",
      "verification_type": "api_test"}),
    ("GET", "/api/operations/verification-tasks", None),
    ("GET", "/api/operations/analytics", None),
    ("GET", "/api/operations/operations-log", None),
    ("GET", "/api/operations/dashboard-summary", None),
    ("PUT", "/api/operations/onboarding/AGENT_1/status?entity_id=AGENT_1&entity_type=agent",
     {"status": "pending"}),
    # entity_id/entity_type are bare annotations on the handler, so FastAPI
    # makes them QUERY params — a JSON body gets 422 before the guard runs.
    ("POST", "/api/operations/send-welcome-email?entity_id=AGENT_1&entity_type=agent",
     None),
]


@pytest.mark.parametrize("role", DENIED_ROLES)
@pytest.mark.parametrize("method,path,body", OPS_REQUESTS)
def test_operations_routes_deny_unprivileged_roles(ops_client, role, method, path, body):
    """The defect: all of these answered 200 for every one of these roles."""
    resp = ops_client.request(
        method, path, headers=_auth(role), json=body
    )
    assert resp.status_code == 403, (
        f"{method} {path} answered {resp.status_code} for role={role!r}; "
        "the operations surface is open again"
    )


@pytest.mark.parametrize("role", ALLOWED_ROLES)
def test_operations_routes_still_admit_staff(ops_client, role):
    """The mutant this kills: denying everyone, which would 'pass' the test
    above while breaking the operators the routes exist for."""
    resp = ops_client.get(
        "/api/operations/dashboard-summary", headers=_auth(role)
    )
    assert resp.status_code == 200, (
        f"role={role!r} got {resp.status_code}; staff locked out of operations"
    )


def test_require_permission_raises_and_check_permission_does_not():
    """The heart of the bug: one of these is a guard, the other is a predicate."""
    from utils.auth import (
        MANAGE_OPERATIONS,
        check_permission,
        require_permission,
    )

    # check_permission returns; used as a bare statement it enforces nothing.
    assert check_permission({"role": "merchant"}, MANAGE_OPERATIONS) is False

    with pytest.raises(HTTPException) as exc:
        require_permission({"role": "merchant"}, MANAGE_OPERATIONS)
    assert exc.value.status_code == 403


def test_a_role_name_never_resolves_as_a_permission():
    """The second half of the defect: "operator" was passed as a permission but
    is a ROLE name, and permission_map is keyed by role with permission values.
    It matched nothing, so only the admin/super_admin early-returns passed.

    Checked behaviourally against every role, because the map is a local.
    """
    from utils.auth import MANAGE_OPERATIONS, check_permission

    assert MANAGE_OPERATIONS == "manage_operations"

    every_role = ALLOWED_ROLES + ["merchant", "agent", "outsourced", "viewer"]
    for role_name in every_role:
        for caller in every_role:
            if caller in ("admin", "super_admin"):
                continue  # these early-return True for any non-super_admin string
            assert not check_permission({"role": caller}, role_name), (
                f"role name {role_name!r} resolved as a permission for "
                f"caller {caller!r} — roles and permissions are being conflated"
            )

    # And the real permission does resolve, for exactly the intended roles.
    for caller in ALLOWED_ROLES:
        assert check_permission({"role": caller}, MANAGE_OPERATIONS)
    for caller in ["merchant", "agent", "outsourced", "viewer"]:
        assert not check_permission({"role": caller}, MANAGE_OPERATIONS)


def test_no_bare_check_permission_statements_remain():
    """A bare `check_permission(...)` expression is authorization theatre."""
    offenders = []
    for path in (REPO_ROOT / "routes").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "check_permission"
            ):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                )
    assert not offenders, (
        "check_permission called as a statement (its bool return is discarded "
        "— use require_permission):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 3. The JWT secret length is enforced where the secret actually lives
# ---------------------------------------------------------------------------

def _run(snippet, extra_env):
    env = {
        k: v
        for k, v in os.environ.items()
        # CLOUD_RUN_* is scrubbed too. It was not, and that is the same blind
        # spot that let the Jobs shape through: a stray marker in the parent
        # environment would silently change which platform the child thinks it
        # is on.
        if not k.startswith(("RAILWAY_", "PIVOTA_", "K_", "JWT_", "CLOUD_RUN_"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )


# Signing a token is what the guard protects, so that is what these drive.
# They used to run `import config.settings`, because the check ran at import —
# which is exactly the defect being fixed here, so testing it that way would
# have pinned the bug rather than the behaviour.
_SIGN_A_TOKEN = "from utils.auth import create_access_token; create_access_token({'sub': 'x'})"

# VERIFYING is the half that matters most, and it was asserted only as a string
# inside the error message. Pointing decode_token back at the raw secret survived
# every test: a deployed host with no secret would refuse to MINT tokens while
# happily verifying ones forged from a repo checkout — the exact attack the guard
# exists to stop. The token is signed in a separate process that HAS a secret, so
# this drives verification and nothing else.
_VERIFY_A_TOKEN = (
    "import jwt, datetime;"
    "tok = jwt.encode({'sub':'x','exp': 9999999999}, 'your-super-secret-key', algorithm='HS256');"
    "from utils.auth import decode_token; decode_token(tok)"
)

# Importing the modules a batch job pulls in, and touching no token. This must
# SUCCEED on every deployed shape with no secret at all.
_BOOT_A_JOB = "import config.settings, db.database, utils.auth"


def _boot(extra_env):
    return _run(_SIGN_A_TOKEN, extra_env)


def _verify(extra_env):
    return _run(_VERIFY_A_TOKEN, extra_env)


DEPLOYED_SHAPES = [
    ({"RAILWAY_ENVIRONMENT": "production"}, "railway_prod"),
    ({"PIVOTA_ENV": "production"}, "pivota_env_prod_unmanaged"),
    ({"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "staging"}, "cloud_run_staging"),
    ({"K_SERVICE": "pivota-backend"}, "cloud_run_unresolved"),
    # The shape that was missing, and the one that mattered: every Cloud Run
    # JOB in pivota-prod carries these two markers AND PIVOTA_ENV=production.
    # config/platform.py's own docstring records this host family as having
    # been blind before 2026-08-26; the matrix reproduced the blindness.
    ({"CLOUD_RUN_JOB": "content-canonical-election",
      "CLOUD_RUN_EXECUTION": "exec-1",
      "PIVOTA_ENV": "production"}, "cloud_run_job"),
]


@pytest.mark.parametrize(
    "shape", [s for s, _ in DEPLOYED_SHAPES], ids=[i for _, i in DEPLOYED_SHAPES]
)
@pytest.mark.parametrize(
    "secret,ids", [(None, "unset"), ("short", "too_short"),
                   ("your-super-secret-key", "repo_default")],
)
def test_weak_jwt_secret_refuses_to_boot_on_a_real_server(shape, secret, ids):
    """Both platform conjuncts: staging and an unmanaged PIVOTA_ENV=production
    host are covered too, not just Railway prod."""
    env = dict(shape)
    if secret is not None:
        env["JWT_SECRET_KEY"] = secret
    result = _boot(env)
    assert result.returncode != 0, (
        f"signed a token with a weak JWT secret on {shape}: {result.stdout[-2000:]}"
    )
    assert "Refusing to sign or verify tokens" in result.stderr, result.stderr[-2000:]


@pytest.mark.parametrize(
    "shape", [s for s, _ in DEPLOYED_SHAPES], ids=[i for _, i in DEPLOYED_SHAPES]
)
def test_a_strong_secret_boots_on_every_deployed_shape(shape):
    """The mutant this kills: raising unconditionally, which would take prod
    down rather than protect it."""
    result = _boot({**shape, "JWT_SECRET_KEY": STRONG_SECRET})
    assert result.returncode == 0, result.stderr[-2000:]


def test_a_weak_secret_is_still_allowed_on_an_unmanaged_dev_host():
    """The suite and local dev run without secrets; the guard must not
    turn that into a hard failure."""
    result = _boot({})
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize(
    "shape", [s for s, _ in DEPLOYED_SHAPES], ids=[i for _, i in DEPLOYED_SHAPES]
)
def test_a_process_that_never_touches_a_token_starts_without_a_secret(shape):
    """The regression this guard shipped with, and the reason it now triggers
    on USE rather than on import.

    The check ran at the bottom of config/settings.py, and db/database.py
    imports that module — so every Cloud Run Job inherited it. All 19 jobs in
    pivota-prod set PIVOTA_ENV=production and none mounts JWT_SECRET_KEY, so
    nine of the eleven Python job entrypoints died at import on a check for a
    secret they never use. Silently: a cron job that stops running pages no one,
    and the jobs pin image SHAs, so the breakage would have landed on whatever
    later deploy rolled them rather than on the change that caused it.

    Moving it to utils/auth.py's import would not have helped either —
    jobs/external_referral_refresh.py loads that module transitively.
    """
    result = _run(_BOOT_A_JOB, shape)
    assert result.returncode == 0, (
        "a batch job that never signs or verifies a token must start without "
        f"JWT_SECRET_KEY on {shape}: {result.stderr[-2000:]}"
    )


def test_the_guard_still_fires_for_a_job_shape_that_does_touch_tokens():
    """The positive counterpart: exempting jobs by PLATFORM would have been the
    wrong fix. Nothing is exempt — only not-reading-the-secret is."""
    result = _run(_SIGN_A_TOKEN, {
        "CLOUD_RUN_JOB": "some-job", "CLOUD_RUN_EXECUTION": "e",
        "PIVOTA_ENV": "production",
    })
    assert result.returncode != 0
    assert "Refusing to sign or verify tokens" in result.stderr


def test_config_production_is_gone():
    """config/production.py declared `min_length=32` on the JWT secret and so
    read like the enforcement point, but nothing instantiated ProductionSettings
    and the module could not even be imported under Pydantic v2. It was a second
    answer to a question config/settings.py already answers, with the wrong one
    easier to find. Deleted; this test stops it coming back."""
    with pytest.raises(ModuleNotFoundError):
        __import__("config.production")

    assert not (REPO_ROOT / "config" / "production.py").exists()


# ---------------------------------------------------------------------------
# 4. dashboard.core's models support the shape their callers actually use
# ---------------------------------------------------------------------------
# Discovered while fixing the payment_orchestrator publisher calls (that section
# went with the module on 2026-08-31): process_order_payment died on
# OrderStatus.PAID and then on Order(...)/Payment(...), whose implemented
# signatures were (id, user_id, amount, currency) and (id, order_id, amount,
# psp). routes/demo_data_routes.py still depends on the repaired shapes.

def test_order_status_has_paid():
    from dashboard.core import OrderStatus

    assert OrderStatus.PAID.value == "paid"


def test_order_supports_both_construction_shapes():
    from dashboard.core import Order, OrderStatus

    # Legacy positional shape, used by DashboardCore.create_order.
    legacy = Order("o1", "u1", 12.5, "EUR")
    assert (legacy.user_id, legacy.amount, legacy.currency) == ("u1", 12.5, "EUR")
    # amount and total_amount are the same number under two names; both are read.
    assert legacy.total_amount == 12.5

    # Commerce shape, used by demo_data_routes.
    rich = Order(
        id="o2", merchant_id="M1", agent_id="A1", customer_email="c@x.com",
        total_amount=29.99, currency="USD", status=OrderStatus.PAID,
        items=[{"name": "T-Shirt", "quantity": 1}], payment_method="card",
        psp_used="stripe", metadata={"source": "demo"},
    )
    assert rich.total_amount == 29.99 and rich.amount == 29.99
    assert rich.status is OrderStatus.PAID


def test_payment_supports_both_construction_shapes():
    from dashboard.core import Payment, PSPType

    legacy = Payment("p1", "o1", 12.5, PSPType.STRIPE)
    assert legacy.fees == 0.0 and legacy.transaction_id is None

    rich = Payment(
        id="p2", order_id="o2", amount=29.99, currency="USD",
        psp=PSPType.STRIPE, status="succeeded", transaction_id="pi_1",
        fees=1.17, metadata={"source": "demo"},
    )
    assert rich.status == "succeeded" and rich.fees == 1.17


@pytest.mark.parametrize(
    "model,attrs",
    [
        ("Order", ["id", "merchant_id", "agent_id", "customer_email",
                   "total_amount", "amount", "currency", "status", "items",
                   "payment_method", "psp_used", "user_id",
                   "created_at", "updated_at", "metadata"]),
        ("Payment", ["id", "order_id", "amount", "currency", "psp", "status",
                     "transaction_id", "fees", "created_at", "updated_at",
                     "metadata"]),
    ],
)
def test_every_attribute_the_readers_access_exists(model, attrs):
    """routes/dashboard_api.py and routes/payment_routes.py read these back off
    instances. Each missing one was a latent AttributeError."""
    import dashboard.core as core

    inst = (core.Order(id="o") if model == "Order"
            else core.Payment(id="p", order_id="o", amount=1.0,
                              psp=core.PSPType.STRIPE))
    missing = [a for a in attrs if not hasattr(inst, a)]
    assert not missing, f"{model} is missing {missing}"


def test_demo_data_fixtures_construct():
    """routes/demo_data_routes.py builds three Orders and three Payments in the
    commerce shape; every one raised TypeError before the models were widened."""
    from datetime import datetime
    from dashboard.core import Order, OrderStatus, Payment, PSPType

    order = Order(
        id="order_demo_001", merchant_id="MERCH_001", agent_id="AGENT_001",
        customer_email="customer1@example.com", total_amount=29.99,
        currency="USD", status=OrderStatus.PAID,
        items=[{"name": "T-Shirt", "quantity": 1, "price": 29.99}],
        payment_method="card", psp_used="stripe",
        created_at=datetime.now(), updated_at=datetime.now(),
        metadata={"source": "demo"},
    )
    payment = Payment(
        id="payment_demo_001", order_id="order_demo_001", amount=29.99,
        currency="USD", psp=PSPType.STRIPE, status="succeeded",
        transaction_id="pi_stripe_demo_001", fees=1.17,
        created_at=datetime.now(), metadata={"source": "demo"},
    )
    assert order.status.value == "paid" and payment.transaction_id


# ---------------------------------------------------------------------------
# 4. Nothing reads the signing secret without going through the guard
# ---------------------------------------------------------------------------
#
# The authz half has test_no_bare_check_permission_statements_remain as its
# completeness guard; this is its counterpart. Three of the five use-site
# conversions were pinned by nothing — reverting merchant_store_connections,
# cafe24 or reviews_service to a raw `settings.jwt_secret_key` read left every
# suite green. A grep-shaped test kills all three at once, and would have caught
# services/outbound_links_service reading os.getenv("JWT_SECRET_KEY") directly,
# which escaped the guard entirely.

_SECRET_READ_ALLOWLIST = {
    # Defines the guard and the fallback it protects.
    "config/settings.py",
    # Has its OWN stricter inline guard: refuses unconditionally under 32 bytes
    # or on the repo literal, not only when deployed. Fails closed, so it is a
    # second correct answer rather than a bypass.
    "services/merchant_web_collector_service.py",
    # An operator CLI, not imported by the app (grep: no importers in routes/,
    # services/ or main.py). It sources the secret deliberately — from the env
    # or from Secret Manager via --secret — and runs on a laptop, where the
    # guard is a no-op anyway. Minting a token is its whole purpose.
    "scripts/mint_employee_jwt.py",
}


def _secret_readers():
    """Every module reading the signing secret other than through the guard.

    AST, not grep. A string search over the source flagged this test's own
    prose and a docstring mentioning the env var, and — worse in the other
    direction — a needle of "settings.jwt_secret_key" missed
    `getattr(settings, "jwt_secret_key", "")`, which is how one of these sites
    was actually written. Matching real attribute accesses and real getenv
    calls avoids both.
    """
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith((".venv/", "tests/", ".claude/")) or rel in _SECRET_READ_ALLOWLIST:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            # settings.jwt_secret_key
            if isinstance(node, ast.Attribute) and node.attr == "jwt_secret_key":
                offenders.append(f"{rel}:{node.lineno}  attribute read")
            elif isinstance(node, ast.Call):
                args = [a for a in node.args if isinstance(a, ast.Constant)]
                # getattr(settings, "jwt_secret_key", ...)
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and any(a.value == "jwt_secret_key" for a in args)
                ):
                    offenders.append(f"{rel}:{node.lineno}  getattr read")
                # os.getenv("JWT_SECRET_KEY") / os.environ.get(...)
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("getenv", "get")
                    and any(a.value == "JWT_SECRET_KEY" for a in args)
                ):
                    offenders.append(f"{rel}:{node.lineno}  env read")
            # os.environ["JWT_SECRET_KEY"]
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "JWT_SECRET_KEY"
            ):
                offenders.append(f"{rel}:{node.lineno}  env subscript")
    return sorted(set(offenders))


def test_the_signing_secret_is_only_read_through_the_guard():
    """A raw read signs or verifies with a key the guard never inspected.

    If you are adding a legitimate one, put it in _SECRET_READ_ALLOWLIST with
    the reason — do not widen the needles.
    """
    offenders = _secret_readers()
    assert not offenders, (
        "these read the JWT signing secret without require_jwt_secret():\n  "
        + "\n  ".join(offenders)
    )


def test_a_failed_verification_does_not_latch():
    """require_jwt_secret's docstring asserts this and nothing pinned it.

    Setting `_jwt_secret_verified = True` before the enforcement call — an easy
    refactor — makes the FIRST weak-secret call raise and every later one
    succeed, so a process that lost a race at startup would sign forgeable
    tokens for the rest of its life.
    """
    import config.settings as cs

    cs.reset_jwt_secret_verification()
    original = cs.settings.jwt_secret_key
    try:
        cs.settings.jwt_secret_key = cs.DEFAULT_JWT_SECRET_KEY
        for attempt in range(3):
            with pytest.raises(RuntimeError):
                with _deployed_host():
                    cs.require_jwt_secret()
            assert cs._jwt_secret_verified is False, (
                f"the failure latched on attempt {attempt + 1}"
            )
    finally:
        cs.settings.jwt_secret_key = original
        cs.reset_jwt_secret_verification()


import contextlib  # noqa: E402


@contextlib.contextmanager
def _deployed_host():
    """Make config.platform report a deployed, production host in-process."""
    from config import platform

    previous = os.environ.get("PIVOTA_ENV")
    os.environ["PIVOTA_ENV"] = "production"
    platform.reset_platform_state()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PIVOTA_ENV", None)
        else:
            os.environ["PIVOTA_ENV"] = previous
        platform.reset_platform_state()


# ---------------------------------------------------------------------------
# 5. Survivors found by adversarial review of 2eb4a2813
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "shape", [s for s, _ in DEPLOYED_SHAPES], ids=[i for _, i in DEPLOYED_SHAPES]
)
def test_verifying_a_token_also_refuses_on_a_weak_secret(shape):
    """The guard's message says "sign OR VERIFY"; only signing was tested.

    Pointing decode_token at the raw secret survived the whole suite. Under that
    mutant a deployed host refuses to mint tokens and still accepts ones forged
    with the repo literal, which is the attack this exists to stop.
    """
    result = _verify(shape)
    assert result.returncode != 0, (
        f"verified a forged token with a weak secret on {shape}: {result.stdout[-1500:]}"
    )
    assert "Refusing to sign or verify tokens" in result.stderr, result.stderr[-1500:]


def test_every_operations_route_is_in_the_denial_matrix():
    """OPS_REQUESTS says "every route in the file"; nothing checked that.

    Appending a new unguarded route to operations_routes passed the entire
    suite. This is the assertion that would have caught the original 8-of-10
    gap instead of it being found by review.
    """
    import routes.operations_routes as ops

    mounted = {
        (method, route.path)
        for route in ops.router.routes
        for method in (getattr(route, "methods", None) or set())
        if method not in ("HEAD", "OPTIONS")
    }
    covered = {(m, p.split("?")[0]) for m, p, _ in OPS_REQUESTS}
    # OPS_REQUESTS carries concrete ids where the route has a path param.
    normalised = set()
    for method, path in mounted:
        normalised.add((method, path))
    missing = {
        (m, p) for m, p in normalised
        if not any(m == cm and _paths_match(p, cp) for cm, cp in covered)
    }
    assert not missing, (
        "these operations routes are not in OPS_REQUESTS, so a missing guard on "
        f"them would ship green: {sorted(missing)}"
    )


def _paths_match(mounted_path: str, covered_path: str) -> bool:
    """`/onboarding/{entity_id}/status` vs the concrete `/onboarding/AGENT_1/status`."""
    a = [seg for seg in mounted_path.strip("/").split("/")]
    b = [seg for seg in covered_path.strip("/").split("/")]
    if len(a) != len(b):
        return False
    return all(x.startswith("{") or x == y for x, y in zip(a, b))


@pytest.mark.parametrize("method,path,body", OPS_REQUESTS)
@pytest.mark.parametrize("role", ALLOWED_ROLES)
def test_every_operations_route_still_admits_staff(ops_client, role, method, path, body):
    """The allow side was tested on ONE route in ten.

    Changing the permission string to one nobody holds locked operators out of
    nine operations routes and shipped green. A guard that denies everyone is as
    broken as one that denies no one.
    """
    resp = ops_client.request(
        method, path, headers=_auth(role), json=body
    )
    # `!= 403` alone was too weak: a mutant that made every staff token 401 —
    # locking staff out of all ten routes — kept this green.
    assert resp.status_code != 403, (
        f"{method} {path} denied {role!r}, who should be admitted"
    )
    assert resp.status_code != 401, (
        f"{method} {path} rejected {role!r}'s credential outright "
        f"({resp.json()}); staff are locked out, not merely denied"
    )


# The two orchestrator survivors that lived here -- the publish_payment_result
# status-polarity check and the "the repaired publisher actually reaches the
# metrics store" call -- went with orchestrator/payment_orchestrator.py on
# 2026-08-31. Their replacement is the deletion contract in
# tests/test_dead_psp_connectors_removed.py.


# ---------------------------------------------------------------------------
# 6. The credential travels in a header, not the URL
# ---------------------------------------------------------------------------
#
# utils.auth.verify_jwt_token has the signature `verify_jwt_token(token: str)`,
# so `Depends(verify_jwt_token)` made FastAPI bind `token` as a required QUERY
# parameter. Every call to these ten staff routes therefore wrote a live,
# admin-capable 24-hour JWT into the URL.
#
# WHERE IT ACTUALLY LEAKED, corrected after review — the first version of this
# comment named two channels that do not apply and missed the one that does:
#
#   * uvicorn's ACCESS LOG. infra/gcp/Dockerfile and main.py both start uvicorn
#     with no --no-access-log and no --log-config, so it writes the request line
#     including the query string VERBATIM to stdout, which on Cloud Run is
#     Cloud Logging. This is the real channel; middleware/rate_limiter quotes
#     sampled prod uvicorn access lines, so it is demonstrably live.
#   * Cloud Run's httpRequest.requestUrl, recorded by the platform regardless of
#     what the app does.
#
#   NOT the app's structured logger — middleware/structured_logging already
#   redacts any query key containing "token". And NOT Referer:
#   middleware/security_headers sets Referrer-Policy: no-referrer, and Referer
#   carries the ORIGINATING page's URL, never an XHR target's query string.
#
# A request with no credential now answers 403 "Not authenticated" (HTTPBearer's
# auto_error default), where it previously answered 422.

@pytest.mark.parametrize("method,path,body", OPS_REQUESTS)
def test_the_same_token_works_in_a_header_and_not_in_the_url(
    ops_client, method, path, body
):
    """A CONTRAST, deliberately, because the status alone proves nothing.

    An earlier version asserted only that the query-string request was refused —
    but 403 is also what a request with NO credential returns, so deleting the
    params left it green. Asserting on the detail does not separate them either:
    both answer "Not authenticated". Pairing the two calls is what makes the
    query half load-bearing; the primary guard against a URL credential is the
    OpenAPI allowlist below.

    The old channel is closed rather than supplemented: leaving it working "for
    compatibility" would go on writing credentials into uvicorn's access log,
    which is the entire problem, and prod shows zero calls to /api/operations
    over 30 days.
    """
    token = _token("admin")

    accepted = ops_client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, json=body
    )
    assert accepted.status_code not in (401, 403), (
        f"{method} {path} rejected a valid admin token in the header "
        f"({accepted.status_code}); the contrast below would be meaningless"
    )

    refused = ops_client.request(method, path, params={"token": token}, json=body)
    assert refused.status_code in (401, 403), (
        f"{method} {path} still accepts that same credential in the query "
        f"string ({refused.status_code})"
    )


# The business parameters these ten routes legitimately declare. An ALLOWLIST,
# because the denylist this replaced pinned one SPELLING of the defect rather
# than the property: a dependency taking the credential from `?access_token=`
# re-opened the leak on nine of ten routes with the whole suite green. Adding a
# name here is a deliberate act, and putting a credential in the URL is not one
# of the things it should let you do.
_ALLOWED_QUERY_PARAMS = {
    "days", "entity_id", "entity_type", "limit", "operation_type", "status",
}


def test_no_operations_route_declares_an_unexpected_parameter():
    """Asserted against the generated OpenAPI, because that is where the query
    binding shows up — and what a client would be told to send.

    Not parametrized: it rebuilds the app and walks the whole spec, so running
    it once per route was ten identical executions reading as per-route
    coverage it did not provide.
    """
    from fastapi import FastAPI

    import routes.operations_routes as ops

    app = FastAPI()
    app.include_router(ops.router)
    spec = app.openapi()

    for spec_path, operations in spec["paths"].items():
        for verb, operation in operations.items():
            names = {p["name"] for p in operation.get("parameters", [])}
            unexpected = names - _ALLOWED_QUERY_PARAMS
            assert not unexpected, (
                f"{verb.upper()} {spec_path} declares {sorted(unexpected)}. A "
                "credential belongs in the Authorization header; a real "
                "business parameter belongs in _ALLOWED_QUERY_PARAMS, added "
                "deliberately."
            )
            assert operation.get("security"), (
                f"{verb.upper()} {spec_path} declares no security scheme"
            )


def test_no_operations_route_depends_on_the_query_string_verifier():
    """The root cause, pinned by name.

    utils.auth.verify_jwt_token takes a bare `token: str`. Anything using it as
    a dependency puts the credential back in the URL, and the failure is silent
    — the route keeps working, it just leaks. routes/auth_routes.py defines a
    bearer-based function of the SAME NAME (now a thin adapter over
    utils.auth.get_current_user), which is easy to confuse at an import site.
    """
    # routes/auth_routes.py DEFINES the bearer-based verify_jwt_token, so
    # depending on the name there is correct by construction. It is exempted by
    # filename, not by inspecting its imports: it legitimately imports other
    # names from utils.auth, and the previous heuristic -- "is the substring
    # 'verify_jwt_token' within 300 characters after 'from utils.auth import'"
    # -- only kept clearing that file because the names it imports happened to
    # be short enough. Adding an import there would have made this guard report
    # a leak that does not exist.
    _DEFINES_ITS_OWN = {"auth_routes.py"}

    offenders = []
    for route_file in sorted((REPO_ROOT / "routes").glob("*.py")):
        if route_file.name in _DEFINES_ITS_OWN:
            continue
        src = route_file.read_text(encoding="utf-8")
        if "Depends(verify_jwt_token)" not in src:
            continue
        # Every other file can only have gotten the name by importing it, and
        # utils.auth's is the query-parameter one.
        offenders.append(route_file.name)
    assert not offenders, (
        "these bind the credential as a query parameter by depending on "
        f"utils.auth.verify_jwt_token: {offenders}"
    )
