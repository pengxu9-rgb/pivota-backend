"""scripts/ops/reap_local_e2e.py — the local end-to-end harness for the Reap agentic rail.

WHAT IS UNDER TEST

  * the two safety guards (local DATABASE_URL, sandbox-only REAP_API_BASE_URL), as pure functions
    and through the CLI;
  * the allowlisted environment, the call-log redaction and the sandbox-only egress transport;
  * `seed` on a FRESH SQLite file, read back through the ROUTE'S OWN SQL (imported, not copied);
  * `run --dry-run` driven to 'completed' against the harness's fake Reap, as a subprocess, with
    and without a seeded ACTIVE enrollment. No network, no key: the fake sits under the real
    Reap client, and a placeholder key is the only key that exists.

The harness module is loaded from its path; it imports only the standard library at import time,
so loading it here has no side effect on this process's database binding.

Mutants killed (see the PR body for the table): guard inverted; guard is `startswith("sqlite")`;
sandbox check by substring; each individual refusal arm of both guards; the egress host check;
the log's secret scrub; the env allowlist.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ops" / "reap_local_e2e.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("reap_local_e2e_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


H = _load_harness()


# ── the DATABASE_URL guard ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:////tmp/x/local.db",
    "sqlite:///relative.db",
    "sqlite+aiosqlite:///./pivota.db",
    "postgresql://localhost/pivota_local",
    "postgresql://LOCALHOST:5432/pivota_local",
    "postgresql://127.0.0.1/pivota_local",
    "postgresql://[::1]:5432/pivota_local",
    "postgres://me:pw@localhost/pivota_local",
    "postgresql://me@127.0.0.1:5433/pivota_local?sslmode=disable",
])
def test_the_database_guard_accepts_a_sqlite_file_and_loopback_postgres(url):
    assert H.check_local_database_url(url) == url


@pytest.mark.parametrize("url, why", [
    ("postgresql://10.25.0.2/pivota", "private ip"),
    ("postgresql://10.25.0.2:5432/pivota", "private ip with port"),
    ("postgresql://db.internal/pivota", "a dotted hostname"),
    ("postgresql://localhost.localdomain/pivota", "a dotted hostname that starts with localhost"),
    ("postgresql://127.0.0.1.nip.io/pivota", "a dotted hostname that starts with 127.0.0.1"),
    ("postgresql://pivota:pw@34.120.1.9:5432/pivota", "prod-looking cloud sql ip"),
    ("postgresql://pivota:secret@prod-db.pivota.cc/pivota", "prod-looking hostname"),
    ("postgresql://localhost@10.25.0.2/pivota", "userinfo that says localhost, remote host"),
    ("postgresql://localhost:pw@10.25.0.2/pivota", "userinfo:password, remote host"),
    ("postgresql://localhost:5432,10.25.0.2/pivota", "multi-host list led by localhost"),
    ("postgresql://localhost,10.25.0.2/pivota", "multi-host list without ports"),
    ("postgresql://localhost/pivota?host=10.25.0.2", "a ?host= override"),
    ("postgresql://localhost/pivota?hostaddr=10.25.0.2", "a ?hostaddr= override"),
    ("postgresql:///pivota", "no host: libpq would read PGHOST"),
    ("postgresql://localhost:notaport/pivota", "malformed port"),
    ("sqlite://evilhost/x.db", "sqlite with an authority"),
    ("sqlite+aiosqlite:///:memory:", "in-memory sqlite"),
    ("sqlite:///", "sqlite with no file"),
    ("mysql://localhost/pivota", "unsupported scheme"),
    ("", "empty"),
])
def test_the_database_guard_refuses_everything_that_is_not_local(url, why):
    with pytest.raises(H.HarnessRefused):
        H.check_local_database_url(url)


# ── the sandbox base-URL guard ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://sandbox.api.reap.global",
    "https://sandbox.api.reap.global/",
    "https://SANDBOX.api.reap.global",
    "https://sandbox.api.reap.global:443",
])
def test_the_base_guard_accepts_the_sandbox_origin(url):
    assert H.check_sandbox_base_url(url) == "https://sandbox.api.reap.global"


@pytest.mark.parametrize("url, why", [
    ("https://api.reap.global", "production"),
    ("https://prod.api.reap.global", "a reap.global host that is not the sandbox"),
    ("https://sandbox.api.reap.global.evil.com", "sandbox host as a prefix"),
    ("https://evil-sandbox.api.reap.global", "sandbox as a substring of the host"),
    ("https://x.sandbox.api.reap.global", "a subdomain of the sandbox"),
    ("https://api.reap.global/sandbox", "sandbox in the path"),
    ("https://api.reap.global/?env=sandbox", "sandbox in the query"),
    ("https://sandbox.api.reap.global@api.reap.global", "sandbox as userinfo"),
    ("https://u:p@sandbox.api.reap.global", "userinfo on the sandbox"),
    ("http://sandbox.api.reap.global", "plain http"),
    ("https://sandbox.api.reap.global:8443", "a non-default port"),
    ("https://sandbox.api.reap.global/agentic", "a path"),
    ("", "empty"),
])
def test_the_base_guard_refuses_everything_but_the_sandbox(url, why):
    with pytest.raises(H.HarnessRefused):
        H.check_sandbox_base_url(url)


def test_the_sandbox_hosts_are_ones_the_client_sends_the_simulate_header_to():
    """The run only completes because the checkout create carries `X-Simulate-Checkout`, and the
    client emits it only for its own exact sandbox hosts."""
    import services.reap_agentic_client as rc

    assert H.SANDBOX_HOSTS <= rc.SIMULATE_CHECKOUT_SANDBOX_HOSTS
    assert H.DEFAULT_REAP_BASE_URL == H.check_sandbox_base_url(H.DEFAULT_REAP_BASE_URL)


# ── the CLI refuses before touching anything ─────────────────────────────────────────────────


def test_seed_refuses_a_remote_database_before_building_anything(tmp_path, capsys):
    code = H.main(["seed", "--state-dir", str(tmp_path),
                   "--database-url", "postgresql://10.25.0.2/pivota"])
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out
    assert not (tmp_path / "state.json").exists()


def test_a_shell_exporting_a_production_base_is_refused_not_ignored(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("REAP_API_BASE_URL", "https://api.reap.global")
    code = H.main(["seed", "--state-dir", str(tmp_path)])
    assert code == 2
    assert "not the Reap sandbox" in capsys.readouterr().out
    assert not (tmp_path / "state.json").exists()


def test_fast_is_refused_outside_a_dry_run(tmp_path, capsys):
    assert H.main(["poll", "--fast", "--state-dir", str(tmp_path)]) == 2
    assert "dry-run option" in capsys.readouterr().out


# ── environment, redaction, egress ───────────────────────────────────────────────────────────


def test_the_harness_environment_is_an_allowlist_not_the_shell(tmp_path):
    shell = {
        "PATH": "/usr/bin",
        "HOME": "/home/op",
        "DATABASE_URL": "postgresql://pivota:pw@10.25.0.2/pivota",
        "REDIS_URL": "redis://10.0.0.3:6379",
        "SENTRY_DSN": "https://key@sentry.example/1",
        "K_SERVICE": "pivota-backend",
        "MERCHANT_PURCHASABILITY_ENFORCE": "1",
        "BUYER_IDENTITY_LINK_SECRET": "s3cret",
        "REAP_API_KEY": "sk_from_the_shell",
    }
    env = H._harness_env(database_url="sqlite:///x.db", reap_base_url=H.DEFAULT_REAP_BASE_URL,
                         reap_api_key=None, state_dir=tmp_path, shell=shell)
    assert env["DATABASE_URL"] == "sqlite:///x.db"
    assert env["PATH"] == "/usr/bin"
    for leaked in ("REDIS_URL", "SENTRY_DSN", "K_SERVICE", "MERCHANT_PURCHASABILITY_ENFORCE",
                   "BUYER_IDENTITY_LINK_SECRET", "REAP_API_KEY"):
        assert leaked not in env, leaked
    assert env["AUDIT_WORKER_ENABLED"] == "false"
    assert env["REAP_AGENTIC_SIMULATE_CHECKOUT"] == "COMPLETED"
    assert env["PIVOTA_ENV"] == "development"
    assert "sandbox.api.reap.global" in env["NO_PROXY"].split(",")


def test_the_key_passed_in_is_the_only_key_in_the_environment(tmp_path):
    env = H._harness_env(database_url="sqlite:///x.db", reap_base_url=H.DEFAULT_REAP_BASE_URL,
                         reap_api_key="sk_loaded", state_dir=tmp_path, shell={})
    assert env["REAP_API_KEY"] == "sk_loaded"
    shown = "\n".join(H._serve_env_keys_for_display(env))
    assert "sk_loaded" not in shown
    assert "REAP_API_KEY=<loaded, never printed>" in shown


def test_the_call_log_redacts_authorization_and_scrubs_the_key_anywhere(tmp_path):
    log = H.CallLog(tmp_path / "calls.json", "sk_live_secret_123")
    log.add({
        "request_headers": {"Authorization": "Bearer sk_live_secret_123", "Reap-Version": "v"},
        "request_body": {"note": "echo sk_live_secret_123", "api_key": "x"},
    })
    text = (tmp_path / "calls.json").read_text()
    assert "sk_live_secret_123" not in text
    entry = json.loads(text)["calls"][0]
    assert entry["request_headers"]["Authorization"] == "<redacted>"
    assert entry["request_headers"]["Reap-Version"] == "v"
    assert entry["request_body"]["api_key"] == "<redacted>"
    assert oct((tmp_path / "calls.json").stat().st_mode & 0o777) == "0o600"


async def test_the_recording_transport_refuses_every_host_but_the_sandbox(tmp_path):
    import httpx

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    log = H.CallLog(None, None)
    transport_cls = H._make_recording_transport(lambda: httpx.MockTransport(handler), log)
    real = httpx.AsyncClient
    async with real(transport=transport_cls()) as client:
        with pytest.raises(H.HarnessRefused):
            await client.get("https://api.reap.global/agentic/checkouts/x")
        with pytest.raises(H.HarnessRefused):
            await client.get("https://sandbox.api.reap.global.evil.com/agentic/x")
        ok = await client.get("https://sandbox.api.reap.global/agentic/checkouts/x",
                              headers={"Authorization": "Bearer k"})
    assert ok.status_code == 200
    assert seen == ["https://sandbox.api.reap.global/agentic/checkouts/x"]
    assert log.entries[0]["request_headers"]["authorization"] == "<redacted>"


async def _checkout_statuses(simulate: bool):
    """Drive the fake's checkout the way the poller does: create, then GET until terminal."""
    import httpx

    fake = H.FakeReapSandbox({"merchant_domain": "fashionnova.com",
                              "product_title": "Maven Lipstick - Snatched",
                              "variant_title": "OS", "price": "1.98", "currency": "USD"})
    headers = {"Authorization": "Bearer k"}
    if simulate:
        headers["X-Simulate-Checkout"] = "COMPLETED"
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler),
                                 base_url="https://sandbox.api.reap.global") as client:
        quote = (await client.post("/agentic/quotes", headers=headers,
                                   json={"items": [{"variantId": "v", "quantity": 1}]})).json()
        assert quote["amountBreakdown"]["finalAmount"]["amount"] == 8.97
        created = (await client.post("/agentic/checkouts", headers=headers, json={
            "quoteId": quote["id"], "enrollmentId": "5a3637e1-0000-4000-8000-000000000001",
        })).json()
        statuses = []
        for _ in range(4):
            body = (await client.get(f"/agentic/checkouts/{created['id']}",
                                     headers={"Authorization": "Bearer k"})).json()
            statuses.append(body["status"])
            if body["status"] in ("COMPLETED", "FAILED"):
                return statuses, body
    return statuses, body


async def test_the_fake_completes_only_a_checkout_created_with_the_simulate_header():
    """Mirrors the sandbox measured 2026-09-25: approval + simulate header -> PROCESSING ->
    COMPLETED with an orderId; approval without it -> FAILED. So a dry run that completes is
    evidence the header reached the checkout create."""
    statuses, body = await _checkout_statuses(simulate=True)
    assert statuses == ["REQUIRES_ACTION", "PROCESSING", "COMPLETED"]
    assert body["orderId"].startswith("ord_dry_")
    assert body["finalAmount"] == {"amount": 8.97, "currency": "USD"}

    statuses, body = await _checkout_statuses(simulate=False)
    assert statuses == ["REQUIRES_ACTION", "FAILED"]
    assert "orderId" not in body


# ── seed on a fresh SQLite, read through the route's own SQL ─────────────────────────────────


def _harness(*args, timeout=180):
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if os.environ.get(k)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=str(REPO), env=env,
        capture_output=True, text=True, timeout=timeout,
    )


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    state_dir = tmp_path_factory.mktemp("reap_local_e2e_seed")
    proc = _harness("seed", "--state-dir", str(state_dir))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    state = json.loads((state_dir / "state.json").read_text())
    return state_dir, state, proc


def test_seed_writes_its_state_privately_and_prints_the_local_credentials(seeded):
    state_dir, state, proc = seeded
    assert oct((state_dir / "state.json").stat().st_mode & 0o777) == "0o600"
    assert oct((state_dir / "signing_key.pem").stat().st_mode & 0o777) == "0o600"
    assert state["agent_api_key"] in proc.stdout
    assert "X-Agent-User-JWT: ey" in proc.stdout
    assert state["database_url"].startswith("sqlite+aiosqlite:///")
    assert state["seeded_enrollment"] is None


def test_the_seeded_row_is_resolved_by_the_routes_own_catalog_sql(seeded):
    """The route's `_PRODUCT_SQL` folds `source_domain` canonically; `_SKU_BY_KEY_SQL`,
    `_OFFER_SQL` and `_ELIGIBILITY_SQL` are the rest of what the POST reads. All four are
    imported from the route and run verbatim against the file `seed` wrote."""
    import routes.agent_commerce_reap as routes_reap

    state_dir, state, _ = seeded
    domain_key = routes_reap._merchant_domain_key(state["merchant_domain"])
    conn = sqlite3.connect(str(state_dir / "local.db"))
    conn.row_factory = sqlite3.Row
    try:
        product = conn.execute(
            routes_reap._PRODUCT_SQL,
            {"product_key": state["product_key"], "merchant_domain": domain_key},
        ).fetchall()
        assert len(product) == 1
        assert product[0]["platform"] == "shopify"
        assert product[0]["product_title"] == "Maven Lipstick - Snatched"

        sku = conn.execute(
            routes_reap._SKU_BY_KEY_SQL,
            {"variant_key": state["variant_key"], "product_key": state["product_key"]},
        ).fetchall()
        assert [r["variant_title"] for r in sku] == ["OS"]

        offer = conn.execute(
            routes_reap._OFFER_SQL,
            {"sku_key": state["variant_key"], "product_key": state["product_key"],
             "merchant_id": product[0]["merchant_id"]},
        ).fetchall()
        assert [(r["currency"], r["price"]) for r in offer] == [("USD", "1.98")]

        eligibility = conn.execute(
            routes_reap._ELIGIBILITY_SQL,
            {"merchant_domain": domain_key, "market_country": "US",
             "merchant_row": "", "product_key": state["product_key"]},
        ).fetchall()
        assert len(eligibility) == 1 and eligibility[0]["enabled"]
    finally:
        conn.close()


# ── the dry run, end to end ──────────────────────────────────────────────────────────────────


def _dry_run(state_dir: Path):
    result_path = state_dir / "result.json"
    log_path = state_dir / "calls.json"
    proc = _harness("run", "--dry-run", "--fast", "--interval", "0.3", "--timeout", "60",
                    "--state-dir", str(state_dir), "--reap-log", str(log_path),
                    "--result-json", str(result_path))
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    return json.loads(result_path.read_text()), log_path.read_text(), proc


def test_a_dry_run_walks_the_whole_machine_to_completed(seeded):
    state_dir, _, _ = seeded
    result, log_text, proc = _dry_run(state_dir)

    states = [t["state"] for t in result["transitions"]]
    assert states == ["needs_enrollment", "quoting", "awaiting_approval", "processing",
                      "completed"], states
    view = result["view"]
    assert view["quoted_total_minor"] == 897 and view["final_total_minor"] == 897
    assert view["shipping_minor"] == 699 and view["our_price_minor"] == 198
    assert result["order_reference"].startswith("ord_dry_")
    assert "buyer_email" not in view and "shipping_address" not in view
    assert "CARD ENTRY" in proc.stdout and "https://pay.prava.space/enroll/" in proc.stdout
    assert "APPROVE BEFORE" in proc.stdout
    # SQLite cannot run the Postgres-only edge INSERT; the harness says so instead of pretending.
    assert result["attribution_edges"]["rows"] == []
    assert "Postgres-only" in proc.stdout

    calls = json.loads(log_text)["calls"]
    assert H.DRY_RUN_PLACEHOLDER_KEY not in log_text
    assert all(c["request_headers"].get("authorization") == "<redacted>" for c in calls)
    creates = [c for c in calls if c["method"] == "POST" and c["url"].endswith("/agentic/checkouts")]
    assert len(creates) == 1
    assert creates[0]["request_headers"].get("x-simulate-checkout") == "COMPLETED"
    assert all(c["url"].startswith("https://sandbox.api.reap.global/") for c in calls)


def test_a_seeded_active_enrollment_skips_card_entry(tmp_path):
    reap_enrollment = "5a3637e1-0000-4000-8000-00000000abcd"
    proc = _harness("seed", "--state-dir", str(tmp_path), "--seed-enrollment", reap_enrollment)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["seeded_enrollment"]["reap_enrollment_id"] == reap_enrollment
    assert state["seeded_enrollment"]["buyer_ref"] == "pivota-probe-buyer-001"

    result, log_text, proc = _dry_run(tmp_path)
    states = [t["state"] for t in result["transitions"]]
    assert states == ["quoting", "awaiting_approval", "processing", "completed"], states
    assert "CARD ENTRY" not in proc.stdout
    calls = json.loads(log_text)["calls"]
    assert not any("/agentic/enrollments" in c["url"] for c in calls)
    create = next(c for c in calls
                  if c["method"] == "POST" and c["url"].endswith("/agentic/checkouts"))
    assert create["request_body"]["enrollmentId"] == reap_enrollment


def test_seed_refuses_an_enrollment_id_that_is_not_a_uuid(tmp_path):
    proc = _harness("seed", "--state-dir", str(tmp_path), "--seed-enrollment", "5a3637e1")
    assert proc.returncode == 2
    assert not (tmp_path / "state.json").exists()
