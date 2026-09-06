"""Connect, ensure, and reconcile for Squarespace.

`tests/test_merchant_store_connection_ownership_parity.py` covers the ownership
gates for these routes as part of its table. What is pinned HERE is what those
gates protect:

* connect validates the credential against `authorization/website` and BINDS the
  website id, and a RECONNECT preserves the webhook secret and the cursor
  instead of overwriting the cell (the PrestaShop P1);
* `webhooks/ensure` refuses an API-key-only store with `oauth_required` rather
  than reporting a provisioning that cannot exist, and never returns the secret;
* `reconcile` runs the sweep for the store the caller owns and no other.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

MERCHANT_A = "merchant_A"
MERCHANT_B = "merchant_B"
STORE_ID = "store_sq_1"
WEBSITE_ID = "site-aaaa"


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _auth(role: str, merchant_id=None):
    from utils.auth import create_access_token

    claims = {"sub": f"u-{merchant_id or role}", "email": "x@example.com", "role": role}
    if merchant_id:
        claims["merchant_id"] = merchant_id
    return {"Authorization": f"Bearer {create_access_token(claims)}"}


class _StoreDb:
    """Only the columns each query SELECTs, so a projection bug is visible."""

    def __init__(self, *, row=None, existing=None):
        self.row = row
        self.existing = existing
        self.executes = []
        self.values = []

    @staticmethod
    def _project(row, query):
        _, _, rest = query.partition("SELECT ")
        columns, _, _ = rest.partition(" FROM ")
        wanted = [c.strip() for c in columns.strip().split(",")]
        return {k: v for k, v in row.items() if k in wanted}

    async def fetch_one(self, query, values=None, *a, **kw):
        flat = " ".join(str(query).split())
        if "merchant_stores" not in flat:
            return None
        source = self.existing if "AND domain =" in flat else self.row
        return self._project(source, flat) if source else None

    async def fetch_all(self, *a, **kw):
        return []

    async def execute(self, query, values=None, *a, **kw):
        self.executes.append(" ".join(str(query).split()))
        self.values.append(dict(values or {}))
        return None


def _store_row(credentials, merchant_id=MERCHANT_A):
    return {
        "store_id": STORE_ID,
        "merchant_id": merchant_id,
        "domain": "shop.example",
        "name": "Shop",
        "api_key": json.dumps(credentials),
    }


# ---- connect ---------------------------------------------------------------


def test_connect_validates_the_key_and_binds_the_website_id(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    db = _StoreDb()
    monkeypatch.setattr(mod, "database", db)
    seen = {}

    async def fake_website(token, **kwargs):
        seen["token"] = token
        return {"id": WEBSITE_ID, "title": "My Shop", "identifier": "my-shop"}

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "sq-key"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["website_id"] == WEBSITE_ID
    # Without an OAuth token the store is sweep-only and the response says so,
    # rather than advertising a webhook path that can never be armed.
    assert body["telemetry_mode"] == "sweep_only"
    assert seen["token"] == "sq-key"
    blob = json.loads(db.values[-1]["api_key"])
    assert blob == {"api_key": "sq-key", "website_id": WEBSITE_ID}


def test_connect_refuses_a_credential_the_platform_rejects(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    db = _StoreDb()
    monkeypatch.setattr(mod, "database", db)

    async def fake_website(token, **kwargs):
        raise conn.SquarespaceConnectionError("Squarespace refused the credential")

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "bad-key"},
    )

    assert response.status_code == 400
    # Nothing was written for a credential that does not work.
    assert not db.executes


def test_a_reconnect_preserves_the_webhook_secret_and_the_cursor(client, monkeypatch):
    """The PrestaShop P1, in the one cell that holds every Squarespace secret.

    A merchant re-entering their API key is routine. An overwrite here would
    destroy the subscription secret — after which the receiver 401s every
    delivery — and the reconciliation cursor, which would silently re-read (or,
    worse, skip) history.
    """
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    db = _StoreDb(
        existing={
            "store_id": STORE_ID,
            "api_key": json.dumps(
                {
                    "api_key": "old-key",
                    "website_id": WEBSITE_ID,
                    "webhook_secret": "live-secret",
                    "webhook_subscription_id": "sub-1",
                    "reconciliation": {"orders_cursor": "2026-09-01T00:00:00.000Z"},
                }
            ),
        }
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_website(token, **kwargs):
        return {"id": WEBSITE_ID, "title": "My Shop", "identifier": "my-shop"}

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "new-key", "domain": "shop.example"},
    )

    assert response.status_code == 200, response.text
    blob = json.loads(db.values[-1]["api_key"])
    assert blob["api_key"] == "new-key"
    assert blob["webhook_secret"] == "live-secret"
    assert blob["webhook_subscription_id"] == "sub-1"
    assert blob["reconciliation"] == {"orders_cursor": "2026-09-01T00:00:00.000Z"}


def test_a_reconnect_to_a_different_site_drops_the_stale_secret_and_cursor(
    client, monkeypatch
):
    """The one case where preserving is WRONG: the credential now belongs to
    another Squarespace site. Its subscription secret would authenticate
    deliveries the `websiteId` bind then rejects, and its cursor is a
    high-water mark over a different site's orders."""
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    db = _StoreDb(
        existing={
            "store_id": STORE_ID,
            "api_key": json.dumps(
                {
                    "api_key": "old-key",
                    "website_id": "site-OLD",
                    "webhook_secret": "stale-secret",
                    "reconciliation": {"orders_cursor": "2026-09-01T00:00:00.000Z"},
                }
            ),
        }
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_website(token, **kwargs):
        return {"id": WEBSITE_ID, "title": "Other Shop", "identifier": "other"}

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "new-key", "domain": "shop.example"},
    )

    assert response.status_code == 200, response.text
    blob = json.loads(db.values[-1]["api_key"])
    assert blob["website_id"] == WEBSITE_ID
    assert "webhook_secret" not in blob
    assert "reconciliation" not in blob


def test_an_oauth_token_is_persisted_and_reported_as_webhook_capable(
    client, monkeypatch
):
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    db = _StoreDb()
    monkeypatch.setattr(mod, "database", db)
    seen = {}

    async def fake_website(token, **kwargs):
        seen["token"] = token
        return {"id": WEBSITE_ID, "title": "My Shop"}

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={
            "merchant_id": MERCHANT_A,
            "api_key": "sq-key",
            "oauth_access_token": "sq-oauth",
        },
    )

    assert response.json()["telemetry_mode"] == "webhook_and_sweep"
    # Validated with the credential that also carries webhook subscriptions.
    assert seen["token"] == "sq-oauth"
    assert json.loads(db.values[-1]["api_key"])["oauth_access_token"] == "sq-oauth"


# ---- webhooks/ensure -------------------------------------------------------


def test_ensure_refuses_an_api_key_only_store_with_oauth_required(client, monkeypatch):
    """Honest refusal, not a fake provisioning.

    A per-site Developer API key cannot create a webhook subscription. Pretending
    otherwise would leave a store that reports telemetry as armed and receives
    nothing at all.
    """
    from routes import merchant_store_connections as mod

    db = _StoreDb(row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}))
    monkeypatch.setattr(mod, "database", db)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "oauth_required"
    assert detail["reconcile_path"].endswith(f"/{STORE_ID}/reconcile")
    assert not db.executes


def test_ensure_refuses_a_store_with_no_website_binding(client, monkeypatch):
    from routes import merchant_store_connections as mod

    db = _StoreDb(row=_store_row({"api_key": "k", "oauth_access_token": "sq-oauth"}))
    monkeypatch.setattr(mod, "database", db)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 409
    assert "website_id" in response.json()["detail"]


def test_ensure_persists_the_platform_secret_and_never_returns_it(
    client, monkeypatch
):
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn
    from services import squarespace_webhook_subscriptions as subs

    monkeypatch.setenv("SQUARESPACE_WEBHOOK_BASE_URL", "https://api.example.com")
    db = _StoreDb(
        row=_store_row(
            {
                "api_key": "sq-key",
                "oauth_access_token": "sq-oauth",
                "website_id": WEBSITE_ID,
            }
        )
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_ensure(*, access_token, callback_url, topics, **kwargs):
        return subs.SquarespaceSubscriptionResult(
            subscription_id="sub-9",
            secret="platform-issued-secret",
            topics=list(topics),
            endpoint_url=callback_url,
            replaced_subscription_ids=["sub-old"],
        )

    persisted = {}

    async def fake_merge(*, store_id, updates):
        persisted.update(updates)
        return {"webhook_secret": updates["webhook_secret"], **updates}

    monkeypatch.setattr(subs, "ensure_squarespace_subscription", fake_ensure)
    monkeypatch.setattr(conn, "merge_squarespace_credentials", fake_merge)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["secret_provisioned"] is True
    assert body["subscription_id"] == "sub-9"
    assert body["endpoint"] == f"https://api.example.com/webhooks/squarespace/{STORE_ID}"
    assert "order.create" in body["topics"] and "order.update" in body["topics"]
    # The value itself never leaves: Pivota installs the subscription, so no
    # human needs to see it.
    assert "platform-issued-secret" not in response.text
    assert persisted["webhook_secret"] == "platform-issued-secret"
    assert persisted["webhook_subscription_id"] == "sub-9"


def test_ensure_discards_its_subscription_when_a_concurrent_call_won(
    client, monkeypatch
):
    """Two ensures race; the last write wins the row. The receiver holds THAT
    secret, so this request's subscription can never authenticate — leaving it
    installed would deliver notifications we answer 401 to, for good."""
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn
    from services import squarespace_webhook_subscriptions as subs

    monkeypatch.setenv("SQUARESPACE_WEBHOOK_BASE_URL", "https://api.example.com")
    db = _StoreDb(
        row=_store_row(
            {
                "api_key": "sq-key",
                "oauth_access_token": "sq-oauth",
                "website_id": WEBSITE_ID,
            }
        )
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_ensure(*, access_token, callback_url, topics, **kwargs):
        return subs.SquarespaceSubscriptionResult(
            subscription_id="sub-mine",
            secret="mine",
            topics=list(topics),
            endpoint_url=callback_url,
            replaced_subscription_ids=[],
        )

    async def fake_merge(*, store_id, updates):
        # The other call's value is what actually persisted.
        return {"webhook_secret": "theirs", "webhook_subscription_id": "sub-theirs"}

    deleted = []

    async def fake_delete(*, access_token, subscription_id, **kwargs):
        deleted.append(subscription_id)

    monkeypatch.setattr(subs, "ensure_squarespace_subscription", fake_ensure)
    monkeypatch.setattr(subs, "delete_squarespace_subscription", fake_delete)
    monkeypatch.setattr(conn, "merge_squarespace_credentials", fake_merge)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200, response.text
    assert response.json()["subscription_id"] == "sub-theirs"
    assert deleted == ["sub-mine"]


def test_ensure_undoes_the_create_when_the_secret_could_not_be_persisted(
    client, monkeypatch
):
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn
    from services import squarespace_webhook_subscriptions as subs

    monkeypatch.setenv("SQUARESPACE_WEBHOOK_BASE_URL", "https://api.example.com")
    db = _StoreDb(
        row=_store_row(
            {
                "api_key": "sq-key",
                "oauth_access_token": "sq-oauth",
                "website_id": WEBSITE_ID,
            }
        )
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_ensure(*, access_token, callback_url, topics, **kwargs):
        return subs.SquarespaceSubscriptionResult(
            subscription_id="sub-orphan",
            secret="mine",
            topics=list(topics),
            endpoint_url=callback_url,
            replaced_subscription_ids=[],
        )

    async def fake_merge(*, store_id, updates):
        return {}

    deleted = []

    async def fake_delete(*, access_token, subscription_id, **kwargs):
        deleted.append(subscription_id)

    monkeypatch.setattr(subs, "ensure_squarespace_subscription", fake_ensure)
    monkeypatch.setattr(subs, "delete_squarespace_subscription", fake_delete)
    monkeypatch.setattr(conn, "merge_squarespace_credentials", fake_merge)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 503
    assert deleted == ["sub-orphan"]


def test_ensure_needs_an_https_callback_origin(client, monkeypatch):
    from routes import merchant_store_connections as mod

    monkeypatch.setenv("SQUARESPACE_WEBHOOK_BASE_URL", "http://insecure.example.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("PIVOTA_BACKEND_BASE_URL", "")
    db = _StoreDb(
        row=_store_row(
            {
                "api_key": "sq-key",
                "oauth_access_token": "sq-oauth",
                "website_id": WEBSITE_ID,
            }
        )
    )
    monkeypatch.setattr(mod, "database", db)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/webhooks/ensure",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 503
    assert "HTTPS" in response.json()["detail"]


# ---- reconcile -------------------------------------------------------------


def test_reconcile_runs_the_sweep_for_the_callers_own_store(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    db = _StoreDb(row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}))
    monkeypatch.setattr(mod, "database", db)
    calls = []

    async def fake_sweep(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "store_id": kwargs["store_id"], "accepted": 3}

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/reconcile?max_pages=5",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 3
    assert calls[0]["store_id"] == STORE_ID
    assert calls[0]["max_pages"] == 5
    assert calls[0]["apply"] is True


def test_reconcile_refuses_another_merchants_store(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    db = _StoreDb(
        row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}, MERCHANT_B)
    )
    monkeypatch.setattr(mod, "database", db)
    calls = []

    async def fake_sweep(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 403
    assert "Can only manage your own store" in response.text
    # The refusal happened BEFORE the sweep, not after it read another
    # merchant's orders.
    assert calls == []


def test_reconcile_is_404_for_a_store_that_is_not_squarespace(client, monkeypatch):
    from routes import merchant_store_connections as mod

    db = _StoreDb(row=None)
    monkeypatch.setattr(mod, "database", db)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 404


def test_reconcile_reports_a_sweep_failure_as_502(client, monkeypatch):
    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    db = _StoreDb(row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}))
    monkeypatch.setattr(mod, "database", db)

    async def fake_sweep(**kwargs):
        raise sweep.SquarespaceSweepError("Squarespace rate-limited the order list read")

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 502
