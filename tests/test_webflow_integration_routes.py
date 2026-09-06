"""Connect, ensure, and reconcile for Webflow.

`tests/test_merchant_store_connection_ownership_parity.py` covers the ownership
gates for these routes as part of its table. What is pinned HERE is what those
gates protect:

* connect resolves and BINDS the site, refuses to guess between several, and a
  reconnect preserves the provisioning instead of overwriting the cell — while a
  reconnect to a DIFFERENT site drops every credential and every piece of
  site-derived state;
* `webhooks/ensure` persists the URL secret BEFORE it registers the URL that
  carries it, reuses an existing secret so it is safe to re-run, and never
  returns or logs the secret;
* `reconcile` runs the sweep for the store the caller owns and no other, refuses
  a concurrent run, and is audit-logged with the actor.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

MERCHANT_A = "merchant_A"
MERCHANT_B = "merchant_B"
STORE_ID = "store_wf_1"
SITE_ID = "5f1a0000000000000000aaaa"
OTHER_SITE_ID = "5f1a0000000000000000bbbb"


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _https_base(monkeypatch):
    monkeypatch.setenv("WEBFLOW_WEBHOOK_BASE_URL", "https://api.pivota.test")


def _auth(role: str, merchant_id=None):
    from utils.auth import create_access_token

    claims = {"sub": f"u-{merchant_id or role}", "email": "x@example.com", "role": role}
    if merchant_id:
        claims["merchant_id"] = merchant_id
    return {"Authorization": f"Bearer {create_access_token(claims)}"}


class _StoreDb:
    """Only the columns each query SELECTs, so a projection bug is visible.

    The UPDATE genuinely MUTATES the row and the re-read genuinely sees it. That
    matters more than it looks: `merge_webflow_credentials` reads, mutates,
    writes and then re-reads, and a fake whose write went nowhere would hand the
    merge back the PRE-write blob. Every assertion about what survived a
    reconnect would then be reading the fixture rather than the code.
    """

    def __init__(self, *, row=None, existing=None):
        self.row = row
        self.existing = existing
        self.executes = []
        self.values = []
        self.transactions = 0

    @staticmethod
    def _project(row, query):
        _, _, rest = query.partition("SELECT ")
        columns, _, _ = rest.partition(" FROM ")
        wanted = [c.strip() for c in columns.strip().split(",")]
        return {k: v for k, v in row.items() if k in wanted}

    def transaction(self):
        db = self

        class _Txn:
            async def __aenter__(self_inner):
                db.transactions += 1
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Txn()

    async def fetch_one(self, query, values=None, *a, **kw):
        flat = " ".join(str(query).split())
        if "merchant_stores" not in flat:
            return None
        source = self.existing if "AND domain =" in flat else self._target()
        return self._project(source, flat) if source else None

    def _target(self):
        return self.row if self.row is not None else self.existing

    async def fetch_all(self, *a, **kw):
        return []

    async def execute(self, query, values=None, *a, **kw):
        flat = " ".join(str(query).split())
        self.executes.append(flat)
        params = dict(values or {})
        self.values.append(params)
        if flat.startswith("UPDATE merchant_stores") and "api_key" in params:
            target = self._target()
            if target is not None:
                target["api_key"] = params["api_key"]
        return None


def _store_row(credentials, merchant_id=MERCHANT_A):
    return {
        "store_id": STORE_ID,
        "merchant_id": merchant_id,
        "domain": "shop.webflow.io",
        "name": "Shop",
        "api_key": json.dumps(credentials),
    }


def _stored(db):
    return json.loads(db._target()["api_key"])


# ---- connect ---------------------------------------------------------------


def test_connect_resolves_the_lone_site_and_binds_it(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import webflow_connection as conn

    db = _StoreDb()
    monkeypatch.setattr(mod, "database", db)
    seen = {}

    async def fake_resolve(token, *, site_id=None, **kwargs):
        seen.update({"token": token, "site_id": site_id})
        return {"id": SITE_ID, "displayName": "My Shop", "shortName": "my-shop"}

    monkeypatch.setattr(conn, "resolve_webflow_site", fake_resolve)

    response = client.post(
        "/integrations/webflow/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_token": "wf-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["site_id"] == SITE_ID
    assert body["site_name"] == "My Shop"
    assert body["domain"] == "my-shop.webflow.io"
    # No provisioning has run yet, so the store's only telemetry is the sweep —
    # and the response says so rather than advertising an armed webhook.
    assert body["telemetry_mode"] == "sweep_only_until_provisioned"
    assert seen == {"token": "wf-token", "site_id": None}
    inserted = json.loads(db.values[-1]["api_key"])
    assert inserted == {
        "api_token": "wf-token",
        "site_id": SITE_ID,
        "site_name": "My Shop",
    }


def test_connect_refuses_to_guess_between_several_sites(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import webflow_connection as conn
    from services.webflow_connection import WebflowSiteAmbiguousError

    db = _StoreDb()
    monkeypatch.setattr(mod, "database", db)

    async def fake_resolve(token, *, site_id=None, **kwargs):
        raise WebflowSiteAmbiguousError(
            "this Webflow token reaches 2 sites; name one as site_id",
            sites=[
                {"id": "a", "displayName": "Shop A"},
                {"id": "b", "displayName": "Shop B"},
            ],
        )

    monkeypatch.setattr(conn, "resolve_webflow_site", fake_resolve)

    response = client.post(
        "/integrations/webflow/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_token": "wf-token"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "site_selection_required"
    assert [site["id"] for site in detail["sites"]] == ["a", "b"]
    # Nothing was written: a store bound to a guessed site would file another
    # shop's orders under this merchant.
    assert db.executes == []


def test_connect_names_the_upstream_status_on_a_failure(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import webflow_connection as conn
    from services.webflow_connection import WebflowConnectionError

    monkeypatch.setattr(mod, "database", _StoreDb())

    async def fake_resolve(token, *, site_id=None, **kwargs):
        raise WebflowConnectionError("Webflow site lookup failed", status_code=404)

    monkeypatch.setattr(conn, "resolve_webflow_site", fake_resolve)

    response = client.post(
        "/integrations/webflow/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_token": "wf-token"},
    )

    assert response.status_code == 400
    assert "upstream HTTP 404" in response.json()["detail"]


def test_a_reconnect_to_the_SAME_site_preserves_the_provisioning(client, monkeypatch):
    """The PrestaShop P1, in Webflow's clothing.

    The URL secret is baked into the webhook registered AT WEBFLOW. An overwrite
    here leaves Webflow delivering to a path the receiver can only 401, and no
    reconnect recovers it — only a re-provision, which nobody knows to run.
    """
    from routes import merchant_store_connections as mod
    from services import webflow_connection as conn

    existing = _store_row(
        {
            "api_token": "old-token",
            "site_id": SITE_ID,
            "site_name": "My Shop",
            "url_secret": "the-only-copy",
            "webhook_ids": {"ecomm_new_order": "wh-1"},
            "reconciliation": {"orders": {"cursor": "2026-09-01T00:00:00.000Z"}},
        }
    )
    db = _StoreDb(existing=existing)
    monkeypatch.setattr(mod, "database", db)

    async def fake_resolve(token, *, site_id=None, **kwargs):
        return {"id": SITE_ID, "displayName": "My Shop", "shortName": "shop"}

    monkeypatch.setattr(conn, "resolve_webflow_site", fake_resolve)

    response = client.post(
        "/integrations/webflow/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={
            "merchant_id": MERCHANT_A,
            "api_token": "new-token",
            "domain": "shop.webflow.io",
        },
    )

    assert response.status_code == 200, response.text
    blob = _stored(db)
    assert blob["api_token"] == "new-token"
    assert blob["url_secret"] == "the-only-copy"
    assert blob["webhook_ids"] == {"ecomm_new_order": "wh-1"}
    assert blob["reconciliation"] == {"orders": {"cursor": "2026-09-01T00:00:00.000Z"}}
    # And `telemetry_mode` is read off the blob THAT PERSISTED: this reconnect
    # supplied no secret, and the store is still armed.
    assert response.json()["telemetry_mode"] == "webhook_and_sweep"


def test_a_reconnect_to_a_DIFFERENT_site_drops_the_old_credential_too(
    client, monkeypatch
):
    """The Squarespace review's finding, not repeated.

    Dropping the derived state and keeping the old token would leave every read
    reaching the OLD site — and its orders would be recorded under the store that
    now represents the new one. Well-formed rows belonging to somebody else's
    shop, with no signal anywhere.
    """
    from routes import merchant_store_connections as mod
    from services import webflow_connection as conn

    existing = _store_row(
        {
            "api_token": "OLD-site-token",
            "site_id": OTHER_SITE_ID,
            "site_name": "Old Shop",
            "url_secret": "old-secret",
            "webhook_ids": {"ecomm_new_order": "wh-old"},
            "reconciliation": {"orders": {"cursor": "2026-09-01T00:00:00.000Z"}},
            "support_email_verified": True,
        }
    )
    db = _StoreDb(existing=existing)
    monkeypatch.setattr(mod, "database", db)

    async def fake_resolve(token, *, site_id=None, **kwargs):
        return {"id": SITE_ID, "displayName": "New Shop", "shortName": "new"}

    monkeypatch.setattr(conn, "resolve_webflow_site", fake_resolve)

    response = client.post(
        "/integrations/webflow/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={
            "merchant_id": MERCHANT_A,
            "api_token": "NEW-site-token",
            "site_id": SITE_ID,
            "domain": "shop.webflow.io",
        },
    )

    assert response.status_code == 200, response.text
    blob = _stored(db)
    assert blob["api_token"] == "NEW-site-token"
    assert blob["site_id"] == SITE_ID
    # Everything the old site issued or derived is gone...
    assert "url_secret" not in blob
    assert "webhook_ids" not in blob
    assert "reconciliation" not in blob
    # ...and nothing unrelated was collateral damage.
    assert blob["support_email_verified"] is True
    assert response.json()["telemetry_mode"] == "sweep_only_until_provisioned"


def test_the_reconnect_runs_inside_the_merges_own_transaction(client, monkeypatch):
    """One critical section over the cell, not two.

    Connect expresses its drop as a `mutate` INSIDE the shared merge. A second,
    hand-rolled read-modify-write beside it would mean a sweep's cursor write
    landing between connect's read and its write reverts the merchant's new
    token.
    """
    from routes import merchant_store_connections as mod
    from services import webflow_connection as conn

    db = _StoreDb(existing=_store_row({"api_token": "old", "site_id": SITE_ID}))
    monkeypatch.setattr(mod, "database", db)

    async def fake_resolve(token, *, site_id=None, **kwargs):
        return {"id": SITE_ID, "displayName": "Shop", "shortName": "shop"}

    monkeypatch.setattr(conn, "resolve_webflow_site", fake_resolve)

    response = client.post(
        "/integrations/webflow/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={
            "merchant_id": MERCHANT_A,
            "api_token": "new",
            "domain": "shop.webflow.io",
        },
    )

    assert response.status_code == 200
    assert db.transactions == 1
    assert sum(1 for q in db.executes if q.startswith("UPDATE merchant_stores")) == 1


def test_the_token_is_never_logged(client, monkeypatch, caplog):
    from routes import merchant_store_connections as mod
    from services import webflow_connection as conn

    monkeypatch.setattr(mod, "database", _StoreDb())

    async def fake_resolve(token, *, site_id=None, **kwargs):
        return {"id": SITE_ID, "displayName": "Shop", "shortName": "shop"}

    monkeypatch.setattr(conn, "resolve_webflow_site", fake_resolve)

    with caplog.at_level("INFO"):
        client.post(
            "/integrations/webflow/connect",
            headers=_auth("merchant", MERCHANT_A),
            json={"merchant_id": MERCHANT_A, "api_token": "super-secret-token"},
        )

    assert "super-secret-token" not in caplog.text
    assert "webflow_connect" in caplog.text


# ---- webhooks/ensure -------------------------------------------------------


class _WebhookApi:
    """A fake `ensure_webflow_webhooks`, recording the URL it was handed."""

    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    async def __call__(self, **kwargs):
        from services.webflow_webhook_subscriptions import WebflowWebhookResult

        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return WebflowWebhookResult(
            webhook_ids={t: f"wh-{t}" for t in kwargs["trigger_types"]},
            endpoint_url=kwargs["callback_url"],
            created_trigger_types=list(kwargs["trigger_types"]),
        )


def _ensure_app(monkeypatch, credentials, *, api=None, merchant_id=MERCHANT_A):
    from routes import merchant_store_connections as mod
    from services import webflow_webhook_subscriptions as subs

    db = _StoreDb(row=_store_row(credentials, merchant_id=merchant_id))
    monkeypatch.setattr(mod, "database", db)
    webhook_api = api or _WebhookApi()
    monkeypatch.setattr(subs, "ensure_webflow_webhooks", webhook_api)
    mod._WEBFLOW_ENSURES_IN_FLIGHT.clear()
    return db, webhook_api


def test_ensure_persists_the_secret_BEFORE_it_registers_the_url(client, monkeypatch):
    """The order of the two writes is the whole design.

    A crash after the first write leaves a stored secret and no webhook —
    harmless, and fixed by re-running. The opposite order would leave Webflow
    delivering to a URL whose secret was never stored, and the receiver would
    answer 401 to every delivery forever.
    """
    db, api = _ensure_app(monkeypatch, {"api_token": "wf-token", "site_id": SITE_ID})

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200, response.text
    secret = _stored(db)["url_secret"]
    assert secret
    # The registered URL carries exactly the secret that was persisted first.
    assert api.calls[0]["callback_url"] == (
        f"https://api.pivota.test/webhooks/webflow/{STORE_ID}/{secret}"
    )
    assert api.calls[0]["store_path_prefix"] == (
        f"https://api.pivota.test/webhooks/webflow/{STORE_ID}/"
    )
    assert api.calls[0]["trigger_types"] == ["ecomm_new_order", "ecomm_order_changed"]
    # Two merges: the secret, then the ids.
    assert db.transactions == 2
    assert _stored(db)["webhook_ids"] == {
        "ecomm_new_order": "wh-ecomm_new_order",
        "ecomm_order_changed": "wh-ecomm_order_changed",
    }


def test_ensure_never_returns_or_logs_the_secret(client, monkeypatch, caplog):
    db, _api = _ensure_app(monkeypatch, {"api_token": "wf-token", "site_id": SITE_ID})

    with caplog.at_level("INFO"):
        response = client.post(
            f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
            headers=_auth("merchant", MERCHANT_A),
        )

    secret = _stored(db)["url_secret"]
    assert response.status_code == 200
    assert response.json()["secret_provisioned"] is True
    assert secret not in response.text
    assert secret not in caplog.text
    # But the audit line names WHO provisioned it.
    assert "webflow_webhooks_ensure action=provisioned" in caplog.text
    assert f"store_id={STORE_ID}" in caplog.text
    assert "actor_role=merchant" in caplog.text
    assert f"actor_user_id=u-{MERCHANT_A}" in caplog.text


def test_ensure_REUSES_an_existing_secret_so_it_is_safe_to_re_run(client, monkeypatch):
    """Idempotence is what makes it safe to re-run after a partial failure.

    Rotating on every call would 401 every in-flight delivery for no reason.
    """
    db, api = _ensure_app(
        monkeypatch,
        {"api_token": "wf-token", "site_id": SITE_ID, "url_secret": "already-live"},
    )

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200
    assert response.json()["secret_rotated"] is False
    assert _stored(db)["url_secret"] == "already-live"
    assert api.calls[0]["callback_url"].endswith("/already-live")


def test_ensure_with_rotate_mints_a_new_secret(client, monkeypatch):
    db, api = _ensure_app(
        monkeypatch,
        {"api_token": "wf-token", "site_id": SITE_ID, "url_secret": "the-old-one"},
    )

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/webhooks/ensure?rotate=true",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200
    assert response.json()["secret_rotated"] is True
    assert _stored(db)["url_secret"] != "the-old-one"
    assert not api.calls[0]["callback_url"].endswith("the-old-one")


def test_a_token_without_the_webhook_scope_is_409_naming_the_scope(client, monkeypatch):
    """A 502 would tell the merchant to retry. What they actually need is to
    re-issue the token with `webhooks:write`."""
    from services.webflow_webhook_subscriptions import WebflowWebhookScopeError

    _db, _api = _ensure_app(
        monkeypatch,
        {"api_token": "wf-token", "site_id": SITE_ID},
        api=_WebhookApi(error=WebflowWebhookScopeError("cannot manage webhooks")),
    )

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "scope_required"
    assert "webhooks:write" in detail["required_scopes"]
    # And the store is pointed at the telemetry it still has.
    assert detail["reconcile_path"] == f"/integrations/webflow/{STORE_ID}/reconcile"


def test_a_store_with_no_site_binding_cannot_be_provisioned(client, monkeypatch):
    _db, api = _ensure_app(monkeypatch, {"api_token": "wf-token"})

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 409
    assert "site_id" in response.json()["detail"]
    assert api.calls == []


def test_a_non_https_callback_origin_is_refused(client, monkeypatch):
    monkeypatch.setenv("WEBFLOW_WEBHOOK_BASE_URL", "http://insecure.test")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PIVOTA_BACKEND_BASE_URL", raising=False)
    _db, api = _ensure_app(monkeypatch, {"api_token": "wf-token", "site_id": SITE_ID})

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 503
    assert api.calls == []


def test_ensure_refuses_a_concurrent_run_for_the_same_store(client, monkeypatch):
    """Two ensures racing would register two different URLs, of which only the
    last-persisted secret authenticates."""
    from routes import merchant_store_connections as mod

    _db, api = _ensure_app(monkeypatch, {"api_token": "wf-token", "site_id": SITE_ID})
    mod._WEBFLOW_ENSURES_IN_FLIGHT.add(STORE_ID)
    try:
        response = client.post(
            f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
            headers=_auth("merchant", MERCHANT_A),
        )
    finally:
        mod._WEBFLOW_ENSURES_IN_FLIGHT.discard(STORE_ID)

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "ensure_already_running"
    assert api.calls == []


def test_ensure_refuses_another_merchants_store(client, monkeypatch):
    _db, api = _ensure_app(
        monkeypatch,
        {"api_token": "wf-token", "site_id": SITE_ID},
        merchant_id=MERCHANT_B,
    )

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 403
    assert api.calls == []


# ---- reconcile -------------------------------------------------------------


def _reconcile_app(monkeypatch, *, merchant_id=MERCHANT_A, error=None):
    from routes import merchant_store_connections as mod
    from services import webflow_order_sweep as sweep

    db = _StoreDb(row=_store_row({"api_token": "t", "site_id": SITE_ID}, merchant_id))
    monkeypatch.setattr(mod, "database", db)
    calls = []

    async def fake_sweep(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return {"status": "success", "platform": "webflow", "accepted": 3}

    monkeypatch.setattr(sweep, "sweep_webflow_store", fake_sweep)
    mod._WEBFLOW_SWEEPS_IN_FLIGHT.clear()
    return calls


def test_reconcile_runs_the_sweep_for_the_callers_own_store(client, monkeypatch):
    calls = _reconcile_app(monkeypatch)

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/reconcile?apply=true&max_pages=3",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 3
    assert calls[0]["store_id"] == STORE_ID
    assert calls[0]["apply"] is True
    assert calls[0]["max_pages"] == 3


def test_reconcile_passes_a_lane_selection_through(client, monkeypatch):
    calls = _reconcile_app(monkeypatch)

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/reconcile?lane=refunded&lane=dispute_lost",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200
    assert calls[0]["lanes"] == ["refunded", "dispute_lost"]


def test_reconcile_refuses_another_merchants_store(client, monkeypatch):
    calls = _reconcile_app(monkeypatch, merchant_id=MERCHANT_B)

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 403
    assert calls == []


def test_reconcile_refuses_a_concurrent_sweep_of_the_same_store(client, monkeypatch):
    """Two sweeps race on one `reconciliation` cell, and the loser's offsets are
    re-read or skipped depending on which merge landed last."""
    from routes import merchant_store_connections as mod

    calls = _reconcile_app(monkeypatch)
    mod._WEBFLOW_SWEEPS_IN_FLIGHT.add(STORE_ID)
    try:
        response = client.post(
            f"/integrations/webflow/{STORE_ID}/reconcile",
            headers=_auth("merchant", MERCHANT_A),
        )
    finally:
        mod._WEBFLOW_SWEEPS_IN_FLIGHT.discard(STORE_ID)

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "sweep_already_running"
    assert calls == []


def test_the_in_flight_guard_is_released_after_a_failure(client, monkeypatch):
    """A guard that leaked on the error path would 409 the store forever."""
    from routes import merchant_store_connections as mod
    from services.webflow_order_sweep import WebflowSweepError

    _reconcile_app(monkeypatch, error=WebflowSweepError("site verification failed"))

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 502
    assert STORE_ID not in mod._WEBFLOW_SWEEPS_IN_FLIGHT


def test_a_sweep_timeout_is_504_and_leaves_the_cursors_alone(client, monkeypatch):
    """The sweep persists its state only after the whole lane loop completes, so
    a timeout re-reads rather than skips."""
    _reconcile_app(monkeypatch, error=TimeoutError())

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 504


def test_an_unknown_store_is_404_not_403(client, monkeypatch):
    from routes import merchant_store_connections as mod

    monkeypatch.setattr(mod, "database", _StoreDb())

    response = client.post(
        f"/integrations/webflow/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 404
