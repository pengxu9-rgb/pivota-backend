"""What `ensure_webflow_webhooks` will and — more importantly — will NOT delete.

This function's answer to "this webhook is stale" is DELETE, against a list that
belongs to the merchant rather than to Pivota. So the definition of stale is the
load-bearing part: it has to cover every one of OUR OWN superseded URLs for this
store, and it has to cover nothing else.
"""

from __future__ import annotations

import json

import pytest

TOKEN = "wf-token"
SITE_ID = "5f1a0000000000000000aaaa"
STORE_ID = "store_wf_1"
PREFIX = f"https://api.pivota.test/webhooks/webflow/{STORE_ID}/"
ENDPOINT = f"{PREFIX}the-current-secret"
TRIGGERS = ("ecomm_new_order", "ecomm_order_changed")


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class _Client:
    """An httpx-shaped double over the Webflow webhook endpoints."""

    def __init__(self, existing):
        self.existing = list(existing)
        self.created = []
        self.deleted = []
        self._next_id = 0

    async def get(self, url, headers=None, params=None):
        return _Response({"webhooks": self.existing})

    async def post(self, url, headers=None, json=None):
        self._next_id += 1
        row = {"id": f"wh-new-{self._next_id}", **(json or {})}
        self.created.append(row)
        return _Response(row, status_code=201)

    async def delete(self, url, headers=None):
        self.deleted.append(str(url).rsplit("/", 1)[-1])
        return _Response({}, status_code=204)

    async def aclose(self):
        return None


def _webhook(webhook_id, url, trigger="ecomm_new_order"):
    return {"id": webhook_id, "url": url, "triggerType": trigger}


async def _ensure(client):
    from services.webflow_webhook_subscriptions import ensure_webflow_webhooks

    return await ensure_webflow_webhooks(
        api_token=TOKEN,
        site_id=SITE_ID,
        callback_url=ENDPOINT,
        trigger_types=list(TRIGGERS),
        store_path_prefix=PREFIX,
        client=client,
    )


async def test_our_own_superseded_url_for_this_store_IS_removed():
    """The positive case. Without it every refusal below is satisfied by a
    function that deletes nothing at all."""
    client = _Client([_webhook("wh-old", f"{PREFIX}an-older-secret")])

    result = await _ensure(client)

    assert client.deleted == ["wh-old"]
    assert result.removed_webhook_ids == ["wh-old"]
    # And the replacements were created BEFORE the stale one was removed, so a
    # failed create can never leave the store with no webhook at all.
    assert len(client.created) == 2


@pytest.mark.parametrize(
    "case, url",
    [
        (
            # A redirector or proxy of the merchant's whose TARGET is our
            # endpoint. `prefix in url` matches it; `startswith` does not.
            "our URL embedded in somebody else's",
            f"https://hooks.zapier.example/catch/?forward={PREFIX}a-secret",
        ),
        (
            # A staging deployment that proxies to prod for the same store id.
            "our URL behind another origin's path",
            f"https://staging.example.test/proxy/{PREFIX}a-secret",
        ),
        ("another integration entirely", "https://hooks.zapier.example/catch/1/2/"),
        (
            # ANOTHER store's endpoint on our own origin. Same origin, same
            # route, different store — not ours to remove from here.
            "another store's endpoint",
            "https://api.pivota.test/webhooks/webflow/store_wf_2/its-secret",
        ),
    ],
)
async def test_a_webhook_that_is_not_ours_is_never_deleted(case, url):
    """Deleting a merchant's own Zapier hook because it CONTAINED our URL is a
    destructive answer to a provisioning request, and it is not recoverable from
    here — this function has no record of what it removed beyond an id."""
    client = _Client([_webhook("wh-theirs", url)])

    result = await _ensure(client)

    assert client.deleted == [], case
    assert result.removed_webhook_ids == [], case


async def test_a_webhook_already_at_the_exact_url_is_reused_not_recreated():
    client = _Client(
        [_webhook(f"wh-{trigger}", ENDPOINT, trigger) for trigger in TRIGGERS]
    )

    result = await _ensure(client)

    assert client.created == []
    assert client.deleted == []
    assert sorted(result.reused_trigger_types) == sorted(TRIGGERS)
    assert result.webhook_ids == {t: f"wh-{t}" for t in TRIGGERS}
