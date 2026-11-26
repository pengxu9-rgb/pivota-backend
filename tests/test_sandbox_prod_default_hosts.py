"""Every prod-host URL literal in the backend's code is reviewed for the partner sandbox (SF1 class).

The SF1 finding was a prod URL used as a FALLBACK (`os.getenv(...) or "https://gateway.pivota.cc"`):
an env value cannot switch it off, so the guard cannot either. This test lists every such literal
outside tests, scripts, infra and docstrings, by (file, URL), with what keeps it from reaching prod in
the sandbox. A NEW literal fails here until someone classifies it:

  SANDBOX_REFUSED      the code path refuses in the sandbox before any outbound call (is_sandbox())
  GUARD_REQUIRED       the default belongs to a setting the startup guard requires to be set
  GUARD_RESOLVED       the guard checks the value the resolver actually returns
  BEHIND_REFUSED_FLAG  reached only behind a flag the guard refuses ON in the sandbox
  DISPLAY              a link or origin handed to a person, client or PSP; the server never fetches it
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from config import sandbox_guard as G

ROOT = pathlib.Path(__file__).resolve().parents[1]
SANDBOX_REFUSED, GUARD_REQUIRED, GUARD_RESOLVED, BEHIND_REFUSED_FLAG, DISPLAY = (
    "sandbox_refused", "guard_required", "guard_resolved", "behind_refused_flag", "display")

INVENTORY = {
    ('adapters/mollie_adapter.py', 'https://pivota.cc/order/'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('adapters/paypal_adapter.py', 'https://merchant.pivota.cc/payment/cancel'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('adapters/paypal_adapter.py', 'https://merchant.pivota.cc/payment/success'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('adapters/psp_adapter.py', 'https://merchant.pivota.cc/payment/cancel'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('adapters/psp_adapter.py', 'https://merchant.pivota.cc/payment/success'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('adapters/psp_adapter.py', 'https://merchant.pivota.cc/payment/success?session_id={CHECKOUT_SESSION_ID}'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('config/sandbox_guard.py', 'https://merchant.pivota.cc/privacy'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/sandbox_guard.py', 'https://pivota.cc/terms'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://agents.pivota.cc/checkout/acp'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://agents.pivota.cc/checkout/cancel'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://agents.pivota.cc/checkout/success'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://api.pivota.cc'): (GUARD_RESOLVED, "DEFAULT_PUBLIC_API_BASE_URL; the guard checks resolve_public_api_base_url's result (SF2)"),
    ('config/settings.py', 'https://api.pivota.cc/api/gsc/oauth/callback'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://developer.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://employee.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://gateway.pivota.cc'): (GUARD_REQUIRED, 'PIVOTA_AGENT_INTERNAL_URL default; the guard requires the setting'),
    ('config/settings.py', 'https://gateway.pivota.cc/internal/ucp/warm-handoff/resolve'): (GUARD_REQUIRED, 'OUTBOUND_WARM_HANDOFF_RESOLVE_URL default; the guard requires the setting'),
    ('config/settings.py', 'https://merchant.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'https://merchant.pivota.cc/signup'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('middleware/cors.py', 'https://admin.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://agent.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://agents.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://aurora.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://creator.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://developer.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://employee.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://look-replicator.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://merchant.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'https://www.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('routes/accounts_orders_api.py', 'https://gateway.pivota.cc'): (SANDBOX_REFUSED, 'Aurora exchange: 404 in the sandbox before any call (SF1)'),
    ('routes/admin_amazon_oauth.py', 'https://api.pivota.cc/admin/amazon/callback'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/after_sales_cases.py', 'https://pivota.cc/return-labels/'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/agent_checkout_intents.py', 'https://agent.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/agent_payment_sdk.py', 'https://agent.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/auth.py', 'https://developer.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/auth.py', 'https://employee.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/auth.py', 'https://merchant.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/buyer_api.py', 'https://agent.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('routes/employee_kb_monitoring.py', 'https://gateway.pivota.cc'): (SANDBOX_REFUSED, 'KB monitor: disabled in the sandbox before any call (SF1)'),
    ('routes/order_routes.py', 'https://agent.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('services/agent_pdp_view_assembler.py', 'https://agent.pivota.cc/'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('services/agent_pdp_view_assembler.py', 'https://agent.pivota.cc/products/'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('services/agent_webhook_service.py', 'https://api.pivota.cc'): (GUARD_RESOLVED, 'used only when the resolved public URL is local, which the guard refuses'),
    ('services/catalog_sync_service.py', 'https://agent.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('services/crawl_identity.py', 'https://api.pivota.cc'): (BEHIND_REFUSED_FLAG, 'a header on crawls; every crawl flag is refused ON'),
    ('services/merchant_ucp_checkout.py', 'https://ucp.pivota.cc/.well-known/ucp-agent'): (BEHIND_REFUSED_FLAG, 'sent to merchants only by the merchant UCP checkout; MERCHANT_UCP_CHECKOUT_ENABLED is refused ON'),
    ('services/order_confirmation_email_service.py', 'https://agent.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('services/partner_invite_token_service.py', 'https://merchant.pivota.cc/signup'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('services/verifiers/_product_context.py', 'https://agent.pivota.cc'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
    ('utils/error_codes.py', 'https://api.pivota.cc/agent/docs/overview'): (DISPLAY, 'a link handed to a person or a client, never fetched by the server'),
}

_HOST = re.compile(r"^https?://([a-z0-9.-]+)", re.I)
_ZONES = ("pivota.cc", "pivota.ai", "agenpaz.com", "agenpas.com", "reap.global")
_SKIP = {"tests", ".venv", "venv", "scripts", "infra", "docs", "node_modules", ".git", "__pycache__", "migrations"}


def _prod_url(value):
    match = _HOST.match(value.strip())
    if not match:
        return None
    host = match.group(1).lower().rstrip(".")
    if not any(host == z or host.endswith("." + z) for z in _ZONES):
        return None
    if any("sandbox" in label for label in host.split(".")):
        return None
    return value.strip()


def _docstrings(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _scan():
    found = set()
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in _SKIP for part in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                values = [node.value]
            elif isinstance(node, ast.JoinedStr):
                values = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            for value in values:
                url = _prod_url(value)
                if url:
                    found.add((str(rel), url))
    return found


def test_every_prod_host_literal_is_classified():
    unclassified = sorted(_scan() - set(INVENTORY))
    assert not unclassified, (
        "prod-host URL literal(s) not reviewed for the sandbox; classify each in INVENTORY: %s" % unclassified)


@pytest.mark.parametrize("key", sorted(k for k, v in INVENTORY.items() if v[0] == SANDBOX_REFUSED))
def test_a_sandbox_refused_file_checks_is_sandbox(key):
    assert "is_sandbox()" in (ROOT / key[0]).read_text(), key


def test_the_guard_required_defaults_are_required_by_the_guard():
    assert set(G._PROD_DEFAULT_URL_SETTINGS) == {"PIVOTA_AGENT_INTERNAL_URL", "OUTBOUND_WARM_HANDOFF_RESOLVE_URL"}


def test_the_behind_flag_paths_have_their_flags_refused():
    assert "MERCHANT_UCP_CHECKOUT_ENABLED" in G._MORE_MONEY_DIALS
    assert "MERCHANT_UCP_CHECKOUT_ENABLED" not in G._FLAGS_ALLOWED_ON
