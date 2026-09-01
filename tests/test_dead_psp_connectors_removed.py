"""The whole `psp` package is DELETED, and the live money paths are not.

psp/connectors.py carried StripeConnector, AdyenConnector, PayPalConnector and a
PSPManager registry. None of it could ever run: `PSPManager.__init__` started
with an empty `self.connectors` and `register_connector` had ZERO call sites
anywhere in the repo, so `select_psp` could only raise "No PSP connectors
available".

orchestrator/payment_orchestrator.py and five `/api/payments/*` endpoints sat on
top of that empty registry -- mounted in main.py, reachable over HTTP, and
unable to reach a PSP on any input. What they DID do on every call was write an
Order and a Payment into the in-memory `dashboard_core` maps, which
`GET /api/payments/orders/{order_id}` served back and routes/dashboard_api.py
counted. Nothing in the repo called any of the five, and
docs/monetization/T1_stripe_codebase_audit.md had already recorded them as
legacy rather than the Stripe flow.

psp/production_connectors.py went with them. It had zero importers outside the
package, and it was the WORSE copy of the same defect: an identical hardcoded
test-card body, but posted to checkout-live.adyen.com with real credentials read
from psp/production_config.py. Its module-level `ProductionPSPManager()` only
looked harmless because `_initialize_production_connectors` swallowed the
missing-env RuntimeError in a broad `except`.

Hardcoding a fabricated payload behind a surface that reads as live is the
defect class tests/test_platform_connectors_prefix.py memorializes. That file's
precedent is followed here -- assert DELETION (ModuleNotFoundError, 404, absent
paths), never merely "not reachable", so a future re-mount fails loudly.

Every negative below is paired with a POSITIVE assertion that a surviving live
path still answers. A suite that only proves things are gone cannot tell a
deletion from a broken import, and this change removed imports from modules that
other live code still loads.
"""
import importlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every module of the deleted package.
DELETED_PSP_MODULES = [
    "psp",
    "psp.connectors",
    "psp.production_connectors",
    "psp.production_config",
]

# (method, path) for each endpoint that stood on the empty registry.
REMOVED_ROUTES = [
    ("post", "/api/payments/process"),
    ("post", "/api/payments/retry"),
    ("get", "/api/payments/status"),
    ("get", "/api/payments/psps"),
    ("get", "/api/payments/orders/order_1"),
]

# The path strings as FastAPI registers them (path params included).
REMOVED_ROUTE_PATHS = [
    "/api/payments/process",
    "/api/payments/retry",
    "/api/payments/status",
    "/api/payments/psps",
    "/api/payments/orders/{order_id}",
]

LIVE_WEBHOOK_PATH = "/api/payments/webhooks/checkout"


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def _payment_route_paths():
    from main import app

    return {
        r.path
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/payments")
    }


# ---------------------------------------------------------------------------
# The package
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", DELETED_PSP_MODULES)
def test_the_psp_package_is_deleted_not_merely_unused(module):
    """An unused module can be imported again by a one-line mistake -- the
    precedent tests/test_platform_connectors_prefix.py sets for this repo.

    `psp` itself is listed: leaving the package __init__ behind would keep
    `import psp` working and give the deleted modules somewhere to come back to.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


THIS_FILE = "tests/test_dead_psp_connectors_removed.py"

# git grep -E is POSIX ERE, which has no `\s`. Written with the POSIX class
# instead -- see test_the_import_regex_is_not_vacuous below for why that detail
# gets its own test rather than a comment.
_IMPORT_RE = r"^[[:space:]]*(from|import)[[:space:]]+{}(\.|[[:space:]]|$)"


def _grep(pattern):
    return subprocess.run(
        ["git", "grep", "-nE", pattern],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def test_the_import_regex_is_not_vacuous():
    """Guards the test below, and it has already earned its place: the first
    version used `\s`, which git grep -E matches NOTHING against. It passed
    with `from psp.connectors import PaymentRequest` sitting in
    routes/psp_routes.py -- a green probe that ran nothing at all.

    Running the same SHAPE against a module every file imports is what catches
    that; asserting the psp pattern finds nothing cannot.
    """
    assert _grep(_IMPORT_RE.format("os")), (
        "the import regex matches nothing even for `os` -- it could not detect "
        "a re-added psp import either"
    )


def test_no_module_imports_the_psp_package():
    """The import statements, not just the runtime. A module that imports `psp`
    inside a function body would leave this suite green until that line ran."""
    offenders = [h for h in _grep(_IMPORT_RE.format("psp")) if not h.startswith(THIS_FILE)]
    assert not offenders, "the psp package is imported again:\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# The orchestrator that stood on the registry
# ---------------------------------------------------------------------------

def test_the_orchestrator_module_is_deleted_not_merely_unmounted():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("orchestrator.payment_orchestrator")


def test_the_live_orchestrator_sibling_still_imports():
    """POSITIVE counterpart. orchestrator/ is NOT gone: routes/psp_routes.py
    imports handle_psp_webhook from orchestrator.callback_handler on the live
    PSP webhook path. Deleting the package would have taken that with it."""
    mod = importlib.import_module("orchestrator.callback_handler")
    assert callable(mod.handle_psp_webhook)


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", REMOVED_ROUTE_PATHS)
def test_the_registry_backed_routes_left_the_route_table(path):
    assert path not in _payment_route_paths(), f"{path} is registered again"


@pytest.mark.parametrize("method,path", REMOVED_ROUTES)
def test_the_registry_backed_routes_answer_404(client, method, path):
    """404, not "not 200": these routes were behind Depends(get_current_user),
    so an unauthenticated call used to answer 401/403. Only 404 distinguishes
    "the route is gone" from "the route is there and refused me"."""
    kwargs = {"json": {}} if method == "post" else {}
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 404, f"{method.upper()} {path} -> {resp.status_code}"


def test_the_checkout_webhook_is_still_mounted(client):
    """POSITIVE counterpart, and the reason routes/payment_routes.py survives
    at all: this is a live Checkout.com finalizer, covered by
    tests/test_checkout_webhook_contract.py."""
    assert LIVE_WEBHOOK_PATH in _payment_route_paths()
    # Registered for POST specifically -- a GET-only re-mount would satisfy the
    # path check above while breaking every real delivery.
    resp = client.get(LIVE_WEBHOOK_PATH)
    assert resp.status_code == 405, (
        f"expected method-not-allowed on GET, got {resp.status_code}"
    )


def test_the_guard_hoisted_out_of_the_webhook_survives():
    """POSITIVE counterpart. tests/test_platform_guard_parity.py imports this
    helper by name; trimming the module must not take it."""
    from routes.payment_routes import _unsigned_webhook_is_fatal

    assert callable(_unsigned_webhook_is_fatal)


# ---------------------------------------------------------------------------
# The real card rails, which this change must not have touched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module,attr",
    [
        ("adapters.psp_adapter", None),
        ("services.acp_offsession_capture", None),
        ("routes.psp_routes", "router"),
    ],
)
def test_the_live_money_paths_still_import(module, attr):
    """POSITIVE counterpart. The deleted package was named `psp`, and these are
    the modules that actually move money. If the deletion had caught one of
    them, every negative assertion above would still have passed."""
    mod = importlib.import_module(module)
    if attr:
        assert getattr(mod, attr) is not None


# ---------------------------------------------------------------------------
# The fabricated payload
# ---------------------------------------------------------------------------

# Distinctive fragments of the hardcoded Adyen test-card body. The bare card
# digits are NOT used as a needle: they legitimately appear in
# docs/runbooks/adyen_acp_capture_canary.md and tests/test_merchant_events_ingest.py.
FABRICATED_PAYLOAD_NEEDLES = ["test_4111", "encryptedCardNumber", "your-company.com"]

# This file names them so the regression stays legible; nothing else may.
KNOWN_PAYLOAD_FILES = {"tests/test_dead_psp_connectors_removed.py"}


@pytest.mark.parametrize("needle", FABRICATED_PAYLOAD_NEEDLES)
def test_the_fabricated_card_payload_exists_nowhere_in_the_repo(needle):
    hits = set(
        subprocess.run(
            ["git", "grep", "-l", needle],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    assert hits <= KNOWN_PAYLOAD_FILES, (
        f"{needle!r} is present in: {sorted(hits - KNOWN_PAYLOAD_FILES)}"
    )


def test_the_needles_still_match_something():
    """Guards the test above: a needle that matches nothing passes vacuously,
    which is how a renamed field would slip a fresh copy back in. Each needle
    must still find THIS file, which is the only place they now live."""
    for needle in FABRICATED_PAYLOAD_NEEDLES:
        hits = subprocess.run(
            ["git", "grep", "-l", needle],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert hits == ["tests/test_dead_psp_connectors_removed.py"], (
            f"{needle!r} matched {hits}; the grep is not running as assumed"
        )
