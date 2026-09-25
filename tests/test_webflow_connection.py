"""Webflow credential shape, site resolution, and the reconnect drop set.

The load-bearing test in this file is the last one: every key the READ path can
prefer must be a member of the set a site change drops. That is the exact shape
of the Squarespace review's finding — a reconnect that dropped the derived state
and left the old site's credential behind kept every read reaching the old site,
and its orders were filed under the store that now represents the new one.
"""

from __future__ import annotations

import json

import pytest


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()
        self.headers = {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    async def get(self, url, headers=None, params=None):
        url = str(url)
        self.calls.append({"url": url, "headers": dict(headers or {})})
        for fragment, response in self.by_path.items():
            if fragment in url:
                return response
        return _Response({}, status_code=404)


# ---- the drop set -----------------------------------------------------------


def test_every_credential_the_read_path_prefers_is_dropped_on_a_site_change():
    """The finding this exists to not repeat.

    `webflow_read_tokens` is what every read resolves its credential through, so
    anything it can read is a credential that would keep reaching the OLD site
    if a reconnect left it behind. Comparing the two declarations rather than
    restating one of them is what makes a SECOND credential added later fail
    here instead of quietly escaping the drop.
    """
    from services.webflow_connection import (
        WEBFLOW_SITE_SCOPED_KEYS,
        WEBFLOW_TOKEN_KEYS,
        webflow_read_tokens,
    )

    assert set(WEBFLOW_TOKEN_KEYS) <= set(WEBFLOW_SITE_SCOPED_KEYS)
    # And the declaration is the one the function actually reads, not a parallel
    # list that could drift from it.
    blob = {key: f"value-{key}" for key in WEBFLOW_TOKEN_KEYS}
    assert webflow_read_tokens(blob) == [f"value-{key}" for key in WEBFLOW_TOKEN_KEYS]
    assert webflow_read_tokens({"some_other_key": "x"}) == []


def test_dropping_the_site_scoped_keys_leaves_nothing_a_read_could_use():
    from services.webflow_connection import (
        drop_site_scoped_keys,
        webflow_read_tokens,
    )

    blob = {
        "api_token": "OLD",
        "site_id": "site-OLD",
        "site_name": "Old Shop",
        "url_secret": "old-secret",
        "webhook_ids": {"ecomm_new_order": "wh-1"},
        "reconciliation": {"orders": {"cursor": "x"}},
        # Something unrelated a future feature might park here.
        "support_email_verified": True,
    }

    drop_site_scoped_keys(blob)

    assert webflow_read_tokens(blob) == []
    assert blob == {"support_email_verified": True}


# ---- site resolution --------------------------------------------------------


async def test_an_explicit_site_id_is_verified_against_the_token():
    from services.webflow_connection import resolve_webflow_site

    client = _Client({"/sites/site-1": _Response({"id": "site-1", "displayName": "A"})})

    site = await resolve_webflow_site("tok", site_id="site-1", client=client)

    assert site["id"] == "site-1"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer tok"


async def test_a_site_lookup_that_echoes_a_different_id_is_refused():
    """Never trust the echo over the request. If this fires, the URL was not
    addressing the site we asked for."""
    from services.webflow_connection import WebflowConnectionError, fetch_webflow_site

    client = _Client({"/sites/site-1": _Response({"id": "site-2"})})

    with pytest.raises(WebflowConnectionError):
        await fetch_webflow_site("tok", "site-1", client=client)


async def test_a_lone_site_is_resolved_without_the_caller_naming_it():
    from services.webflow_connection import resolve_webflow_site

    client = _Client({"/sites": _Response({"sites": [{"id": "only", "displayName": "A"}]})})

    assert (await resolve_webflow_site("tok", client=client))["id"] == "only"


async def test_several_sites_are_NOT_guessed_between():
    """Binding the wrong site files another shop's orders under this store, and
    nothing downstream can tell: the orders are well-formed."""
    from services.webflow_connection import WebflowSiteAmbiguousError, resolve_webflow_site

    client = _Client(
        {
            "/sites": _Response(
                {"sites": [{"id": "a", "displayName": "A"}, {"id": "b", "displayName": "B"}]}
            )
        }
    )

    with pytest.raises(WebflowSiteAmbiguousError) as excinfo:
        await resolve_webflow_site("tok", client=client)

    assert [site["id"] for site in excinfo.value.sites] == ["a", "b"]


async def test_a_token_reaching_no_sites_is_the_same_refusal():
    from services.webflow_connection import WebflowSiteAmbiguousError, resolve_webflow_site

    client = _Client({"/sites": _Response({"sites": []})})

    with pytest.raises(WebflowSiteAmbiguousError):
        await resolve_webflow_site("tok", client=client)


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, "WebflowUnauthorizedError"),
        (403, "WebflowUnauthorizedError"),
        (429, "WebflowConnectionError"),
        (404, "WebflowConnectionError"),
        (500, "WebflowConnectionError"),
    ],
)
async def test_the_upstream_status_survives_onto_the_error(status, expected):
    """A connect failure that says only "connection failed" is indistinguishable
    between a mistyped token, an unreachable deployment, and an endpoint that is
    not what we assumed. The status is what makes those separable on the first
    attempt."""
    import services.webflow_connection as conn

    client = _Client({"/sites": _Response({}, status_code=status)})

    with pytest.raises(getattr(conn, expected)) as excinfo:
        await conn.list_webflow_sites("tok", client=client)

    assert excinfo.value.status_code == status


# ---- id validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "value, ok",
    [
        ("5f1a0000000000000000aaaa", True),
        ("0000-0001", True),
        ("a_b-C9", True),
        ("../../token/introspect", False),
        ("site/../other", False),
        ("", False),
        ("x" * 65, False),
        ("sité", False),
        ("a b", False),
    ],
)
def test_webflow_ids_are_allowlisted_before_they_reach_a_url(value, ok):
    from services.webflow_connection import is_webflow_id

    assert is_webflow_id(value) is ok


async def test_a_path_traversal_site_id_never_reaches_the_wire():
    from services.webflow_connection import WebflowConnectionError, fetch_webflow_site

    client = _Client({})

    with pytest.raises(WebflowConnectionError):
        await fetch_webflow_site("tok", "../../token/introspect", client=client)

    assert client.calls == []


# ---- the URL secret ---------------------------------------------------------


def test_the_url_secret_is_sized_as_a_credential_not_as_an_id():
    """For a site-token installation it is the ONLY thing authenticating a
    delivery, and it travels in a URL."""
    from services.webflow_connection import mint_url_secret

    secrets = {mint_url_secret() for _ in range(50)}

    assert len(secrets) == 50
    # 32 random bytes, base64url-encoded.
    assert all(len(value) >= 40 for value in secrets)


# ---- the site list has to reach past page 1 ---------------------------------


async def test_the_site_list_walks_past_the_first_page():
    """The ambiguity check is what stops a store being bound to a site the
    merchant did not mean, and it is computed from THIS list.

    A one-page read of a token that reaches more sites than fit on a page would
    resolve against a subset — silently answering "exactly one site" (bind it!)
    or offering a truncated list of candidates.
    """
    from services.webflow_connection import _SITE_PAGE_LIMIT, list_webflow_sites

    rows = [{"id": f"site-{i}", "displayName": f"Shop {i}"} for i in range(_SITE_PAGE_LIMIT + 3)]

    class _PagedClient:
        def __init__(self):
            self.params = []

        async def get(self, url, headers=None, params=None):
            params = dict(params or {})
            self.params.append(params)
            offset = int(params.get("offset") or 0)
            limit = int(params.get("limit") or 100)
            return _Response({"sites": rows[offset : offset + limit]})

    client = _PagedClient()

    sites = await list_webflow_sites("wf-token", client=client)

    assert len(sites) == _SITE_PAGE_LIMIT + 3
    assert [call["offset"] for call in client.params] == [0, _SITE_PAGE_LIMIT]


async def test_the_site_walk_is_BOUNDED_against_an_endpoint_that_ignores_offset():
    """Whether `GET /v2/sites` pages at all is an ASSUMED claim. An endpoint
    that answers a full page to every offset must not spin."""
    from services.webflow_connection import _SITE_PAGE_LIMIT, list_webflow_sites

    rows = [{"id": f"site-{i}"} for i in range(_SITE_PAGE_LIMIT)]
    client = _Client({"/sites": _Response({"sites": rows})})

    sites = await list_webflow_sites("wf-token", client=client)

    assert len(sites) == _SITE_PAGE_LIMIT
    assert len(client.calls) == 2, "the walk did not stop when a page added nothing new"


async def test_a_short_site_list_still_costs_one_request():
    """The overwhelmingly common case: a token reaching one site."""
    client = _Client({"/sites": _Response({"sites": [{"id": "site-1"}]})})

    from services.webflow_connection import list_webflow_sites

    assert await list_webflow_sites("wf-token", client=client) == [
        {"id": "site-1", "displayName": None, "shortName": None}
    ]
    assert len(client.calls) == 1
