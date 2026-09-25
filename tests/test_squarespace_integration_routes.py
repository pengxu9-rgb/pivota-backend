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
    """Only the columns each query SELECTs, so a projection bug is visible.

    The UPDATE genuinely MUTATES the row and the re-read genuinely sees it.
    That matters more than it looks: `merge_squarespace_credentials` reads,
    mutates, writes and then re-reads, and a fake whose write went nowhere
    would hand the merge back the PRE-write blob. Every assertion about what
    survived a reconnect would then be reading the fixture rather than the
    code, and the connect response's `telemetry_mode`, which is computed off
    the persisted blob, would be answered from a stale read.
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
        # Connect's existing-store lookup is the one with the domain
        # predicate; every other read (the caller's store, and the merge's
        # locking re-read) is keyed on store_id and resolves to whichever row
        # this fixture was given.
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
                    "webhook_subscription_id": "sub-OLD",
                    # The OAuth token belongs in this fixture because it is the
                    # WORST thing to leave behind, not merely another key: it is
                    # preferred over the API key on every read, so a token the
                    # old site issued keeps the sweep listing the OLD site's
                    # orders and recording them under the store that now
                    # represents the new one. A fixture without it lets that
                    # ship green.
                    "oauth_access_token": "oauth-for-site-OLD",
                    "oauth_refresh_token": "refresh-for-site-OLD",
                    "oauth_expires_at": "2026-09-01T00:30:00Z",
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
    assert blob["api_key"] == "new-key"
    assert "webhook_secret" not in blob
    assert "webhook_subscription_id" not in blob
    assert "reconciliation" not in blob
    assert "oauth_access_token" not in blob
    assert "oauth_refresh_token" not in blob
    assert "oauth_expires_at" not in blob
    # And the response must not claim webhook coverage the store no longer has.
    assert response.json()["telemetry_mode"] == "sweep_only"


def test_a_reconnect_to_a_different_site_keeps_a_NEWLY_SUPPLIED_oauth_token(
    client, monkeypatch
):
    """Dropping the old site's token must not drop the new site's.

    The counterpart to the test above: without it, "pop oauth_access_token"
    could be implemented as "never keep an OAuth token on a site change", which
    would silently downgrade a merchant who re-pointed their store WITH a fresh
    Developer-Platform token to sweep-only.
    """
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    db = _StoreDb(
        existing={
            "store_id": STORE_ID,
            "api_key": json.dumps(
                {
                    "api_key": "old-key",
                    "website_id": "site-OLD",
                    "oauth_access_token": "oauth-for-site-OLD",
                }
            ),
        }
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_website(token, **kwargs):
        return {"id": WEBSITE_ID, "title": "Other Shop"}

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={
            "merchant_id": MERCHANT_A,
            "api_key": "new-key",
            "oauth_access_token": "oauth-for-site-NEW",
            "oauth_refresh_token": "refresh-NEW",
            "oauth_expires_at": "2026-09-06T21:00:00Z",
            "domain": "shop.example",
        },
    )

    assert response.status_code == 200, response.text
    blob = json.loads(db.values[-1]["api_key"])
    assert blob["oauth_access_token"] == "oauth-for-site-NEW"
    assert blob["oauth_refresh_token"] == "refresh-NEW"
    assert blob["oauth_expires_at"] == "2026-09-06T21:00:00Z"
    assert response.json()["telemetry_mode"] == "webhook_and_sweep"


def test_a_reconnect_that_supplies_no_token_still_reports_the_stored_one(
    client, monkeypatch
):
    """`telemetry_mode` is a fact about the STORE, not about this request.

    A merchant rotating only their API key sends no OAuth token. Reading the
    mode off the request field answers `sweep_only` for a store whose webhook
    subscription is live and armed — telling them their push telemetry is off
    when it is on.
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
                    "oauth_access_token": "still-good",
                    "webhook_secret": "live-secret",
                }
            ),
        }
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_website(token, **kwargs):
        return {"id": WEBSITE_ID, "title": "My Shop"}

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "new-key", "domain": "shop.example"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["telemetry_mode"] == "webhook_and_sweep"
    blob = json.loads(db.values[-1]["api_key"])
    assert blob["oauth_access_token"] == "still-good"
    assert blob["webhook_secret"] == "live-secret"


def test_the_connect_path_uses_the_shared_merge_rather_than_its_own_write(
    client, monkeypatch
):
    """ONE critical section over the credential cell, not two.

    Connect used to hand-roll its own read-modify-write beside
    `merge_squarespace_credentials`, which meant a sweep's cursor write landing
    between connect's read and its write reverted the merchant's new
    credential. Asserting the transaction was entered is what stops that second
    copy growing back.
    """
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    db = _StoreDb(
        existing={
            "store_id": STORE_ID,
            "api_key": json.dumps({"api_key": "old-key", "website_id": WEBSITE_ID}),
        }
    )
    monkeypatch.setattr(mod, "database", db)

    async def fake_website(token, **kwargs):
        return {"id": WEBSITE_ID}

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "new-key", "domain": "shop.example"},
    )

    assert response.status_code == 200, response.text
    assert db.transactions == 1, "the reconnect write must run inside the merge's transaction"
    updates = [q for q in db.executes if q.startswith("UPDATE merchant_stores")]
    assert len(updates) == 1, updates
    # And it is the merge's own statement, carrying the connect bookkeeping.
    assert "status = 'active'" in updates[0]
    assert "connected_at = CURRENT_TIMESTAMP" in updates[0]


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


def test_reconcile_passes_the_operator_window_override_through(client, monkeypatch):
    """`modified_before` is the escape hatch over the automatic bisect.

    A store the page cap cannot read in one pass converges on its own, but
    slowly. Pinning the window's end is how an operator digs one out now, and
    the route is useless as a hatch if it drops the value on the floor.
    """
    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    db = _StoreDb(row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}))
    monkeypatch.setattr(mod, "database", db)
    calls = []

    async def fake_sweep(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "store_id": kwargs["store_id"]}

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/reconcile"
        "?modified_before=2026-02-01T00:00:00Z",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200, response.text
    assert calls[0]["modified_before"] == "2026-02-01T00:00:00Z"


def test_reconcile_defaults_the_window_override_to_none(client, monkeypatch):
    """The positive counterpart: an ordinary run must not pin a window.

    Without this, `modified_before` could be defaulted to some string and the
    test above would still pass while every unattended run silently stopped
    reading at a fixed instant.
    """
    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    db = _StoreDb(row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}))
    monkeypatch.setattr(mod, "database", db)
    calls = []

    async def fake_sweep(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "store_id": kwargs["store_id"]}

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)

    response = client.post(
        f"/integrations/squarespace/{STORE_ID}/reconcile",
        headers=_auth("merchant", MERCHANT_A),
    )

    assert response.status_code == 200, response.text
    assert calls[0]["modified_before"] is None


async def test_a_second_concurrent_reconcile_of_one_store_is_refused(monkeypatch):
    """Two sweeps of the SAME store race on one `reconciliation` cell.

    Whichever merge lands last decides the cursor, so the loser's pages are
    either re-read or — if it had advanced further — skipped outright. The
    route is reachable by an operator double-click and by any retry-on-timeout,
    so the guard is not theoretical.

    Driven against the route coroutine rather than the TestClient because the
    claim is about two requests being IN FLIGHT at once, which a synchronous
    client cannot produce.
    """
    import asyncio

    from fastapi import HTTPException

    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    db = _StoreDb(row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}))
    monkeypatch.setattr(mod, "database", db)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake_sweep(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"status": "success", "store_id": kwargs["store_id"]}

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)
    caller = {"sub": "u-1", "role": "merchant", "merchant_id": MERCHANT_A}

    first = asyncio.create_task(
        mod.run_squarespace_reconciliation(store_id=STORE_ID, current_user=caller)
    )
    await started.wait()
    try:
        with pytest.raises(HTTPException) as raised:
            await mod.run_squarespace_reconciliation(
                store_id=STORE_ID, current_user=caller
            )
        assert raised.value.status_code == 409
        assert raised.value.detail["reason"] == "sweep_already_running"
        # The refusal did not start a second sweep.
        assert len(calls) == 1
    finally:
        release.set()
        assert (await first)["status"] == "success"

    # And the guard RELEASES: a later run of the same store is not refused
    # forever. A leaked in-flight marker would take the store's only telemetry
    # path offline for the life of the process.
    release.set()
    again = await mod.run_squarespace_reconciliation(
        store_id=STORE_ID, current_user=caller
    )
    assert again["status"] == "success"
    assert len(calls) == 2


async def test_a_reconcile_of_a_DIFFERENT_store_is_not_blocked(monkeypatch):
    """The guard is per-STORE, not a global sweep lock.

    A single shared flag would pass the test above and quietly serialize every
    merchant's reconcile behind every other merchant's.
    """
    import asyncio

    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    rows = {
        STORE_ID: _store_row({"api_key": "k1", "website_id": WEBSITE_ID}),
        "store_sq_2": {
            **_store_row({"api_key": "k2", "website_id": WEBSITE_ID}),
            "store_id": "store_sq_2",
        },
    }

    class _MultiStoreDb(_StoreDb):
        async def fetch_one(self, query, values=None, *a, **kw):
            flat = " ".join(str(query).split())
            if "merchant_stores" not in flat:
                return None
            row = rows.get(str((values or {}).get("store_id") or ""))
            return self._project(row, flat) if row else None

    monkeypatch.setattr(mod, "database", _MultiStoreDb())
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_sweep(**kwargs):
        if kwargs["store_id"] == STORE_ID:
            started.set()
            await release.wait()
        return {"status": "success", "store_id": kwargs["store_id"]}

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)
    caller = {"sub": "u-1", "role": "merchant", "merchant_id": MERCHANT_A}

    first = asyncio.create_task(
        mod.run_squarespace_reconciliation(store_id=STORE_ID, current_user=caller)
    )
    await started.wait()
    try:
        other = await mod.run_squarespace_reconciliation(
            store_id="store_sq_2", current_user=caller
        )
        assert other["store_id"] == "store_sq_2"
    finally:
        release.set()
        await first


async def test_a_reconcile_that_outruns_its_timeout_is_a_504(monkeypatch):
    """A sweep is up to 200 sequential calls against somebody else's API.

    Unbounded, it holds the connection until the proxy in front gives up, and
    the caller learns nothing. The cursor is untouched either way — the sweep
    persists it only after the whole page loop completes — so a 504 costs a
    re-read, not a gap.
    """
    import asyncio

    from fastapi import HTTPException

    from routes import merchant_store_connections as mod
    from services import squarespace_order_sweep as sweep

    db = _StoreDb(row=_store_row({"api_key": "sq-key", "website_id": WEBSITE_ID}))
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "SQUARESPACE_RECONCILE_TIMEOUT_SECONDS", 0.05)

    async def fake_sweep(**kwargs):
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(sweep, "sweep_squarespace_store", fake_sweep)

    with pytest.raises(HTTPException) as raised:
        await mod.run_squarespace_reconciliation(
            store_id=STORE_ID,
            current_user={"sub": "u-1", "role": "merchant", "merchant_id": MERCHANT_A},
        )
    assert raised.value.status_code == 504

    # The in-flight marker was released by the timeout, not leaked.
    assert STORE_ID not in mod._SQUARESPACE_SWEEPS_IN_FLIGHT


# ---- the merge helper, for real --------------------------------------------
#
# Every test above stubs `merge_squarespace_credentials`, which is right for a
# route test and wrong as the ONLY coverage: with the helper always stubbed,
# replacing its whole body with `credentials = dict(updates)` — the exact
# overwrite this integration exists to avoid — survives the entire suite. These
# drive the real function against a real row.


async def _sqlite_stores(tmp_path, name: str):
    """A real `merchant_stores` table on aiosqlite.

    Raw DDL rather than a metadata table because `merchant_stores` is raw SQL in
    this repo; only the columns the merge reads and writes are here, so a merge
    that touched a column it should not would fail loudly rather than silently.
    """
    import databases

    db = databases.Database(f"sqlite+aiosqlite:///{tmp_path / name}.sqlite3")
    await db.connect()
    await db.execute(
        """
        CREATE TABLE merchant_stores (
            store_id TEXT PRIMARY KEY,
            merchant_id TEXT,
            platform TEXT,
            domain TEXT,
            name TEXT,
            api_key TEXT,
            status TEXT,
            last_sync TIMESTAMP,
            connected_at TIMESTAMP
        )
        """
    )
    return db


async def test_the_real_merge_preserves_every_key_it_was_not_asked_to_change(
    tmp_path,
):
    """The mutant this kills: `credentials = dict(updates)`.

    The blob is ONE cell holding the API key, the OAuth token, the website
    binding, the once-shown webhook secret and the reconciliation cursor. A
    sweep persists only `reconciliation`; if that write is an overwrite, the
    webhook secret is gone — and Squarespace shows it exactly once, so no
    reconnect can recover it and every delivery 401s for good.
    """
    from services.squarespace_connection import merge_squarespace_credentials

    db = await _sqlite_stores(tmp_path, "merge_preserves")
    try:
        await db.execute(
            "INSERT INTO merchant_stores (store_id, merchant_id, platform, api_key)"
            " VALUES (:store_id, :merchant_id, 'squarespace', :api_key)",
            {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_A,
                "api_key": json.dumps(
                    {
                        "api_key": "live-key",
                        "website_id": WEBSITE_ID,
                        "oauth_access_token": "live-oauth",
                        "webhook_secret": "shown-exactly-once",
                        "webhook_subscription_id": "sub-1",
                        "reconciliation": {"orders_cursor": "2026-09-01T00:00:00.000Z"},
                    }
                ),
            },
        )

        persisted = await merge_squarespace_credentials(
            store_id=STORE_ID,
            updates={"reconciliation": {"orders_cursor": "2026-09-05T00:00:00.000Z"}},
            db=db,
        )

        # The write landed...
        assert persisted["reconciliation"] == {
            "orders_cursor": "2026-09-05T00:00:00.000Z"
        }
        # ...and nothing else in the cell was touched.
        assert persisted["webhook_secret"] == "shown-exactly-once"
        assert persisted["api_key"] == "live-key"
        assert persisted["website_id"] == WEBSITE_ID
        assert persisted["oauth_access_token"] == "live-oauth"
        assert persisted["webhook_subscription_id"] == "sub-1"

        # And the return value is a genuine RE-READ of the row, not the dict the
        # caller handed in: `databases` + asyncpg reports no rowcount from an
        # UPDATE, so the re-read is the only proof the write actually landed.
        stored = json.loads(
            dict(
                await db.fetch_one(
                    "SELECT api_key FROM merchant_stores WHERE store_id = :s",
                    {"s": STORE_ID},
                )
            )["api_key"]
        )
        assert stored == persisted
    finally:
        await db.disconnect()


async def test_the_real_merge_runs_its_mutate_against_the_stored_blob(tmp_path):
    """`mutate` is how connect drops the old site's keys inside the merge's own
    critical section. It must see what is STORED, not an empty dict — a mutate
    handed a blank blob would find nothing to drop and quietly preserve the
    stale OAuth token it exists to remove."""
    from services.squarespace_connection import merge_squarespace_credentials

    db = await _sqlite_stores(tmp_path, "merge_mutate")
    try:
        await db.execute(
            "INSERT INTO merchant_stores (store_id, merchant_id, platform, api_key)"
            " VALUES (:store_id, :merchant_id, 'squarespace', :api_key)",
            {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_A,
                "api_key": json.dumps(
                    {
                        "api_key": "old-key",
                        "website_id": "site-OLD",
                        "oauth_access_token": "oauth-OLD",
                        "webhook_secret": "stale",
                    }
                ),
            },
        )
        seen = {}

        def _mutate(blob):
            seen.update(blob)
            for key in ("oauth_access_token", "webhook_secret"):
                blob.pop(key, None)
            blob["website_id"] = WEBSITE_ID
            return blob

        persisted = await merge_squarespace_credentials(
            store_id=STORE_ID,
            mutate=_mutate,
            updates={"api_key": "new-key"},
            mark_connected=True,
            db=db,
        )

        assert seen["oauth_access_token"] == "oauth-OLD", (
            "mutate must receive the STORED blob"
        )
        assert "oauth_access_token" not in persisted
        assert "webhook_secret" not in persisted
        assert persisted == {"api_key": "new-key", "website_id": WEBSITE_ID}

        # `mark_connected` is part of the same statement, so a reconnect is one
        # write rather than a merge racing a status UPDATE.
        row = dict(
            await db.fetch_one(
                "SELECT status, connected_at FROM merchant_stores WHERE store_id = :s",
                {"s": STORE_ID},
            )
        )
        assert row["status"] == "active"
        assert row["connected_at"] is not None
    finally:
        await db.disconnect()


async def test_the_real_merge_leaves_the_row_alone_when_not_marking_connected(
    tmp_path,
):
    """The negative counterpart: an ordinary cursor write must not resurrect a
    store an operator disabled, nor forge a `connected_at`."""
    from services.squarespace_connection import merge_squarespace_credentials

    db = await _sqlite_stores(tmp_path, "merge_no_mark")
    try:
        await db.execute(
            "INSERT INTO merchant_stores"
            " (store_id, merchant_id, platform, api_key, status)"
            " VALUES (:store_id, :merchant_id, 'squarespace', :api_key, 'disabled')",
            {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_A,
                "api_key": json.dumps({"api_key": "k", "website_id": WEBSITE_ID}),
            },
        )

        await merge_squarespace_credentials(
            store_id=STORE_ID, updates={"reconciliation": {}}, db=db
        )

        row = dict(
            await db.fetch_one(
                "SELECT status, connected_at FROM merchant_stores WHERE store_id = :s",
                {"s": STORE_ID},
            )
        )
        assert row["status"] == "disabled"
        assert row["connected_at"] is None
    finally:
        await db.disconnect()


# ---- the subscription lifecycle itself -------------------------------------


class _SubscriptionApi:
    """A fake Squarespace webhook_subscriptions surface, in call order."""

    def __init__(self, *, existing=(), create_fails=False, delete_fails=False):
        self.existing = list(existing)
        self.create_fails = create_fails
        self.delete_fails = delete_fails
        self.calls = []

    async def get(self, url, headers=None, **kwargs):
        self.calls.append(("list", None))
        return _SubResponse(200, {"webhookSubscriptions": self.existing})

    async def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append(("create", None))
        if self.create_fails:
            return _SubResponse(429, {})
        return _SubResponse(
            201,
            {
                "id": "sub-NEW",
                "secret": "secret-NEW",
                "endpointUrl": json["endpointUrl"],
                "topics": json["topics"],
            },
        )

    async def delete(self, url, headers=None, **kwargs):
        self.calls.append(("delete", str(url).rsplit("/", 1)[-1]))
        return _SubResponse(500 if self.delete_fails else 204, {})

    async def aclose(self):
        return None


class _SubResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


async def test_ensure_creates_the_new_subscription_BEFORE_deleting_the_old():
    """Order matters, and only in the failure case.

    Delete-then-create has a window in which the store has NO subscription at
    all, and a create that fails — a rate limit, an expired OAuth token, a
    Squarespace 5xx — leaves it there: telemetry is off until somebody notices
    and re-runs ensure. Creating first means the worst case is a brief overlap
    of two subscriptions instead of a gap.
    """
    from services.squarespace_webhook_subscriptions import (
        ensure_squarespace_subscription,
    )

    api = _SubscriptionApi(
        existing=[{"id": "sub-OLD", "endpointUrl": "https://x.example/hook"}]
    )

    result = await ensure_squarespace_subscription(
        access_token="oauth",
        callback_url="https://x.example/hook",
        topics=["order.create"],
        client=api,
    )

    assert [name for name, _ in api.calls] == ["list", "create", "delete"], api.calls
    assert result.subscription_id == "sub-NEW"
    assert result.secret == "secret-NEW"
    assert result.replaced_subscription_ids == ["sub-OLD"]


async def test_a_failed_create_leaves_the_existing_subscription_in_place():
    """The whole reason for the order above. Under delete-then-create this
    store would come out of a failed ensure with no subscription at all."""
    from services.squarespace_webhook_subscriptions import (
        SquarespaceWebhookSubscriptionError,
        ensure_squarespace_subscription,
    )

    api = _SubscriptionApi(
        existing=[{"id": "sub-OLD", "endpointUrl": "https://x.example/hook"}],
        create_fails=True,
    )

    with pytest.raises(SquarespaceWebhookSubscriptionError):
        await ensure_squarespace_subscription(
            access_token="oauth",
            callback_url="https://x.example/hook",
            topics=["order.create"],
            client=api,
        )

    assert "delete" not in [name for name, _ in api.calls], api.calls


async def test_a_failed_delete_does_not_throw_away_the_new_secret():
    """The secret is shown exactly ONCE. Failing the ensure because a leftover
    subscription could not be removed would discard the only copy Pivota will
    ever have, over a subscription whose only symptom is duplicate deliveries
    the receiver 401s."""
    from services.squarespace_webhook_subscriptions import (
        ensure_squarespace_subscription,
    )

    api = _SubscriptionApi(
        existing=[{"id": "sub-OLD", "endpointUrl": "https://x.example/hook"}],
        delete_fails=True,
    )

    result = await ensure_squarespace_subscription(
        access_token="oauth",
        callback_url="https://x.example/hook",
        topics=["order.create"],
        client=api,
    )

    assert result.secret == "secret-NEW"
    # It is reported as NOT replaced, because it was not.
    assert result.replaced_subscription_ids == []


async def test_ensure_leaves_a_subscription_for_another_endpoint_alone():
    """Only subscriptions pointing at OUR callback are ours to replace. A
    merchant's own app subscribed to the same site must survive."""
    from services.squarespace_webhook_subscriptions import (
        ensure_squarespace_subscription,
    )

    api = _SubscriptionApi(
        existing=[
            {"id": "sub-THEIRS", "endpointUrl": "https://merchant.example/their-hook"}
        ]
    )

    result = await ensure_squarespace_subscription(
        access_token="oauth",
        callback_url="https://x.example/hook",
        topics=["order.create"],
        client=api,
    )

    assert [name for name, _ in api.calls] == ["list", "create"]
    assert result.replaced_subscription_ids == []


@pytest.mark.parametrize(
    "status_code,expected",
    [(404, "upstream HTTP 404"), (401, "upstream HTTP 401"), (429, "upstream HTTP 429")],
)
def test_a_failed_connect_names_the_upstream_status(
    client, monkeypatch, status_code, expected
):
    """That `GET /1.0/authorization/website` answers a per-site API key at all
    is an ASSUMED claim (docs/SQUARESPACE_TELEMETRY.md).

    If it is wrong, every API-key connect fails — and a bare "connection
    failed" looks exactly like a mistyped key, so the assumption would be
    debugged as a support queue instead of as a bad row in a table. A 404 in
    the response detail says which it is on the first attempt.
    """
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    monkeypatch.setattr(mod, "database", _StoreDb())

    async def fake_website(token, **kwargs):
        raise conn.SquarespaceConnectionError(
            "Squarespace authorization lookup failed", status_code=status_code
        )

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "k", "domain": "shop.example"},
    )

    assert response.status_code == 400
    assert expected in response.text


def test_a_connect_failure_with_no_upstream_status_says_nothing_about_one(
    client, monkeypatch
):
    """The negative counterpart: a timeout has no status, and inventing one
    (or printing `upstream HTTP None`) would send the reader looking for a
    response that never arrived."""
    from routes import merchant_store_connections as mod
    from services import squarespace_connection as conn

    monkeypatch.setattr(mod, "database", _StoreDb())

    async def fake_website(token, **kwargs):
        raise conn.SquarespaceConnectionError(
            "Squarespace authorization lookup failed: timed out"
        )

    monkeypatch.setattr(conn, "fetch_squarespace_website", fake_website)

    response = client.post(
        "/integrations/squarespace/connect",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "api_key": "k", "domain": "shop.example"},
    )

    assert response.status_code == 400
    assert "upstream HTTP" not in response.text
    assert "timed out" in response.text
