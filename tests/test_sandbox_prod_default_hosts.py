"""Every prod host named in the backend's code is reviewed for the partner sandbox (SF1 class).

The SF1 finding was a prod URL used as a FALLBACK (`os.getenv(...) or "https://gateway.pivota.cc"`):
an env value cannot switch it off, so the guard cannot either. This test lists every prod host that
appears in ANY string in the code (outside tests, scripts, infra and docstrings): URLs, bare host
constants composed later (INDEXNOW_HOST, DEFAULT_RETURN_URL_HOSTS), pieces of a concatenation or an
f-string, emails, User-Agent texts; hosts under pivota.cc/.ai, agenpaz/agenpas.com, reap.global, and
run.app / Railway hosts. Keyed by (file, host), with what keeps it from prod in the sandbox. A NEW one
fails here until someone classifies it:

  SANDBOX_REFUSED      the code path refuses in the sandbox before any outbound call (is_sandbox())
  GUARD_REQUIRED       the default belongs to a setting the startup guard requires to be set
  GUARD_RESOLVED       the guard checks the value the resolver actually returns
  BEHIND_REFUSED_FLAG  reached only behind a flag the guard refuses ON in the sandbox
  DISPLAY              a link, origin, address or text handed to a person, client or PSP; never fetched
  LEGACY_SCRIPT        a one-off script outside the app (never imported by it)

What it cannot see: a host built from pieces that each look like nothing (split mid-label), or read
from a file or the database at runtime.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from config import sandbox_guard as G

ROOT = pathlib.Path(__file__).resolve().parents[1]
SANDBOX_REFUSED, GUARD_REQUIRED, GUARD_RESOLVED, BEHIND_REFUSED_FLAG, DISPLAY, LEGACY_SCRIPT = (
    "sandbox_refused", "guard_required", "guard_resolved", "behind_refused_flag", "display", "legacy_script")

INVENTORY = {
    ('adapters/mollie_adapter.py', 'pivota.cc'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('adapters/paypal_adapter.py', 'merchant.pivota.cc'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('adapters/psp_adapter.py', 'merchant.pivota.cc'): (DISPLAY, 'a return URL given to a PSP (the guard refuses PSP credentials)'),
    ('config/sandbox_guard.py', 'agenpas.com'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'agenpaz.com'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'api.pivota.cc'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'merchant.pivota.cc'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'pivota.cc'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'railway.app'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'railway.internal'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'reap.global'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/sandbox_guard.py', 'rlwy.net'): (DISPLAY, "the guard's own rule data (prod names it refuses, the two consent links)"),
    ('config/settings.py', 'agent.pivota.cc'): (BEHIND_REFUSED_FLAG, 'INDEXNOW_HOST default (and comments): IndexNow runs only with INDEXNOW_ENABLED, which the flag allowlist refuses ON'),
    ('config/settings.py', 'agents.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'api.pivota.cc'): (GUARD_RESOLVED, "DEFAULT_PUBLIC_API_BASE_URL (and the GSC OAuth redirect, a link); the guard checks resolve_public_api_base_url's result"),
    ('config/settings.py', 'developer.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'employee.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'gateway.pivota.cc'): (GUARD_REQUIRED, 'PIVOTA_AGENT_INTERNAL_URL and OUTBOUND_WARM_HANDOFF_RESOLVE_URL defaults; the guard requires both settings'),
    ('config/settings.py', 'merchant.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('config/settings.py', 'pivota-backend-production.up.railway.app'): (GUARD_RESOLVED, 'LEGACY_PUBLIC_API_HOSTS: the resolver skips these, the guard refuses Railway hosts'),
    ('config/settings.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('config/settings.py', 'web-production-fedb.up.railway.app'): (GUARD_RESOLVED, 'LEGACY_PUBLIC_API_HOSTS: the resolver skips these, the guard refuses Railway hosts'),
    ('jobs/enrichment_cart_variant_proof.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('jobs/scheduled_audit_job.py', 'merchant.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('middleware/cors.py', 'admin.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'agent.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'agents.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'aurora.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'creator.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'developer.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'employee.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'look-replicator.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'merchant.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('middleware/cors.py', 'www.pivota.cc'): (DISPLAY, 'a browser origin allowed by CORS, never fetched'),
    ('openapi_config.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/accounts_orders_api.py', 'gateway.pivota.cc'): (SANDBOX_REFUSED, 'Aurora exchange: 404 in the sandbox before any call (SF1)'),
    ('routes/accounts_orders_api.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('routes/admin_amazon_oauth.py', 'api.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/after_sales_cases.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/agent_account.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/agent_checkout_intents.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/agent_citation_v1.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/agent_docs.py', 'api.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/agent_docs.py', 'commerce.mcp.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/agent_payment_sdk.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/auth.py', 'developer.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/auth.py', 'employee.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/auth.py', 'merchant.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/auth.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('routes/auth.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/buyer_api.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/employee_kb_monitoring.py', 'gateway.pivota.cc'): (SANDBOX_REFUSED, 'KB monitor: disabled in the sandbox before any call (SF1)'),
    ('routes/employees_security.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('routes/gsc_oauth_routes.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/order_routes.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/payment_execution_routes.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('routes/reviews_invitation_issuer.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('run_refund_migration.py', 'metro.proxy.rlwy.net'): (LEGACY_SCRIPT, 'a one-off root script with a hard-coded legacy Railway DSN; never imported by the app (follow-up: remove it and confirm the credential is revoked)'),
    ('services/agent_center_bd_report_service.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/agent_pdp_view_assembler.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/agent_webhook_service.py', 'api.pivota.cc'): (GUARD_RESOLVED, 'used only when the resolved public URL is local, which the guard refuses'),
    ('services/audit_pivota_commitments_builder.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/brand_claim_service.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('services/brand_product_discovery.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/canonical_inci_resolver.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/catalog_sync_service.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/co_occurrence_finder.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/crawl_identity.py', 'api.pivota.cc'): (BEHIND_REFUSED_FLAG, 'a header on crawls; every crawl flag is refused ON'),
    ('services/crawl_identity.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, 'a header on crawls; every crawl flag is refused ON'),
    ('services/curated_brand_feed.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/executor_agents/gsc_url_submission.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/external_offers_service.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/external_seed_destination_liveness.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/funnel_metrics_service.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/live_offer_verification.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/merchant_ucp_checkout.py', 'agent.pivota.cc'): (BEHIND_REFUSED_FLAG, 'referring domain sent only by the merchant UCP checkout; MERCHANT_UCP_CHECKOUT_ENABLED is refused ON'),
    ('services/merchant_ucp_checkout.py', 'ucp.pivota.cc'): (BEHIND_REFUSED_FLAG, 'sent to merchants only by the merchant UCP checkout; MERCHANT_UCP_CHECKOUT_ENABLED is refused ON'),
    ('services/order_confirmation_email_service.py', 'agent.pivota.cc'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('services/order_confirmation_email_service.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('services/partner_invite_email.py', 'pivota.ai'): (DISPLAY, 'a sender/support address or link in an email; the guard refuses every email-provider key'),
    ('services/partner_invite_token_service.py', 'merchant.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/platform_onboarding_service.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/reap_agentic_client.py', 'agent.pivota.cc'): (DISPLAY, 'DEFAULT_RETURN_URL_HOSTS and comments: a redirect validation list, never fetched'),
    ('services/reap_agentic_client.py', 'api.pivota.cc'): (DISPLAY, 'DEFAULT_RETURN_URL_HOSTS: hosts a Reap buyer may be redirected BACK to (a validation list, never fetched)'),
    ('services/reap_agentic_client.py', 'pivota.cc'): (DISPLAY, 'comment text in a string, never fetched'),
    ('services/reap_agentic_client.py', 'reap.global'): (DISPLAY, "the docs URL in a comment string; calls go to REAP_API_BASE_URL, which the guard pins to Reap's sandbox"),
    ('services/report_deck_builder.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/retailer_ingest/sitemap_crawler.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/storefront_currency.py', 'pivota.cc'): (BEHIND_REFUSED_FLAG, "our crawler's User-Agent text; every crawl/live-read/fetch flag is refused ON"),
    ('services/verifiers/_product_context.py', 'agent.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('services/verifiers/frontend_agent_cite.py', 'pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
    ('utils/error_codes.py', 'api.pivota.cc'): (DISPLAY, 'a link, label or text handed to a person or a client, never fetched by the server'),
}

_ZONES = r"(?:pivota\.cc|pivota\.ai|agenpaz\.com|agenpas\.com|reap\.global|run\.app|railway\.app|railway\.internal|rlwy\.net)"
_HOST_RE = re.compile(r"(?<![a-z0-9.-])((?:[a-z0-9-]+\.)*" + _ZONES + r")(?![a-z0-9-])", re.IGNORECASE)
_SKIP = {"tests", ".venv", "venv", "scripts", "infra", "docs", "node_modules", ".git", "__pycache__", "migrations"}


def _prod_hosts(value):
    for host in _HOST_RE.findall(value):
        host = host.lower()
        if not any("sandbox" in label for label in host.split(".")):
            yield host


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
                for host in _prod_hosts(value):
                    found.add((str(rel), host))
    return found


def test_every_prod_host_literal_is_classified():
    unclassified = sorted(_scan() - set(INVENTORY))
    assert not unclassified, (
        "prod host(s) named in code but not reviewed for the sandbox; classify each in INVENTORY: %s" % unclassified)


@pytest.mark.parametrize("value,host", [
    ("agent.pivota.cc", "agent.pivota.cc"),                       # a bare host constant
    ("{scheme}://api.pivota.cc/x", "api.pivota.cc"),              # an f-string / format piece
    ("https://" + "gateway.pivota.cc", "gateway.pivota.cc"),      # concatenated at import time
    ("web-abc-uw.a.run.app", "web-abc-uw.a.run.app"), ("x.up.railway.app", "x.up.railway.app"),
    ("noreply@pivota.ai", "pivota.ai"),
])
def test_the_scanner_sees_hosts_in_every_shape(value, host):
    assert host in set(_prod_hosts(value))
    assert not list(_prod_hosts("https://sandbox.mcp.pivota.cc/x"))


def test_the_legacy_script_is_not_imported_by_the_app():
    for key, (cls, _why) in INVENTORY.items():
        if cls == LEGACY_SCRIPT:
            module = pathlib.Path(key[0]).stem
            importers = [p for p in ROOT.rglob("*.py")
                         if not any(part in _SKIP for part in p.relative_to(ROOT).parts)
                         and re.search(r"(^|\n)\s*(from|import)\s+%s\b" % re.escape(module), p.read_text(errors="ignore"))]
            assert importers == [], (key, importers)


@pytest.mark.parametrize("key", sorted(k for k, v in INVENTORY.items() if v[0] == SANDBOX_REFUSED))
def test_a_sandbox_refused_file_checks_is_sandbox(key):
    assert "is_sandbox()" in (ROOT / key[0]).read_text(), key


def test_the_guard_required_defaults_are_required_by_the_guard():
    assert set(G._PROD_DEFAULT_URL_SETTINGS) == {"PIVOTA_AGENT_INTERNAL_URL", "OUTBOUND_WARM_HANDOFF_RESOLVE_URL"}


def test_the_behind_flag_paths_have_their_flags_refused():
    assert "MERCHANT_UCP_CHECKOUT_ENABLED" in G._MORE_MONEY_DIALS
    assert "MERCHANT_UCP_CHECKOUT_ENABLED" not in G._FLAGS_ALLOWED_ON
