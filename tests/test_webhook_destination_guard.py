"""services/webhook_destination_guard.py and its two callers: webhooks go to public addresses only.

The guard has two layers: a DNS-free check when a URL is saved, and a transport that resolves,
refuses any non-public answer, and pins the connection to the address it checked. The delivery
services must use the transport for every owner-registered URL and record a refusal as terminal.
The network is never touched: the resolver is injected and the real HTTP transport's send is
replaced with a recorder.
"""

from __future__ import annotations

import importlib
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import webhook_destination_guard as guard  # noqa: E402

MODULES = ["services.agent_webhook_service", "services.merchant_webhook_service"]
PUBLIC_V4 = "93.184.216.34"


# ── layer 1: the URL an owner saves ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://hooks.example.com/pivota",
    "https://hooks.example.com:8443/pivota",
    "https://8.8.8.8/hook",
    "https://[2001:4860:4860::8888]/hook",
    "https://hooks.example.com./x",
])
def test_a_public_https_url_is_accepted_unchanged(url):
    assert guard.check_destination_url(f"  {url} ") == url


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",          # plaintext
    "ftp://hooks.example.com/x",
    "hooks.example.com/x",
    "https://user:pw@hooks.example.com/x",  # credentials in the URL
    "https://@hooks.example.com/x",
    "https://hooks.example.com:99999/x",    # invalid port
    "https:///x",                           # no host
    "https://localhost/x",
    "https://metadata/computeMetadata",     # single label
    "https://metadata.google.internal/x",
    "https://printer.local/x",
    "https://api.localhost/x",
    "https://10.0.0.5/x",
    "https://127.0.0.1/x",
    "https://169.254.169.254/x",            # link-local metadata
    "https://100.64.0.1/x",                 # carrier-grade NAT
    "https://0.0.0.0/x",
    "https://224.0.0.1/x",                  # multicast
    "https://[::1]/x",
    "https://[fd00::1]/x",
    "https://[::ffff:127.0.0.1]/x",         # v4-mapped loopback
    "https://[64:ff9b::a00:5]/x",           # NAT64 of 10.0.0.5
    "https://[2002:a00:5::]/x",             # 6to4 of 10.0.0.5
    "",
])
def test_an_internal_or_insecure_url_is_rejected_with_a_value_error(url):
    with pytest.raises(guard.DestinationRejected) as info:
        guard.check_destination_url(url)
    assert isinstance(info.value, ValueError)  # the routes turn ValueError into a 400


# ── layer 2: every send ─────────────────────────────────────────────────────────────────────


class _Sent:
    """Replaces the real transport's send: records the pinned request, answers 200."""

    def __init__(self, body=b"ok"):
        self.requests = []
        self.body = body

    def as_method(self):
        async def handle_async_request(transport, request):
            self.requests.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(self.body))
        return handle_async_request


def _resolver(answers, calls=None):
    async def resolve(host, port):
        if calls is not None:
            calls.append((host, port))
        result = answers[host] if isinstance(answers, dict) else answers
        if isinstance(result, Exception):
            raise result
        return list(result)
    return resolve


async def _post(url, resolver, sent, monkeypatch, **kw):
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", sent.as_method())
    transport = guard.PublicDestinationTransport(resolver=resolver, **kw)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as client:
        response = await client.post(url, content=b"{}")
        await response.aread()
        return response


async def test_the_connection_is_pinned_to_the_checked_address_with_host_and_sni_kept(monkeypatch):
    sent = _Sent()
    await _post("https://hooks.example.com/pivota?x=1", _resolver([PUBLIC_V4]), sent, monkeypatch)
    [req] = sent.requests
    assert req.url.host == PUBLIC_V4 and req.url.path == "/pivota" and req.url.query == b"x=1"
    assert req.headers["host"] == "hooks.example.com"
    assert req.extensions["sni_hostname"] == "hooks.example.com"


async def test_a_custom_port_and_plain_http_are_pinned_too(monkeypatch):
    # Already-stored URLs may still be http or off-port; the send-time guard is about addresses.
    sent = _Sent()
    await _post("http://hooks.example.com:8080/x", _resolver([PUBLIC_V4]), sent, monkeypatch)
    [req] = sent.requests
    assert (req.url.host, req.url.port) == (PUBLIC_V4, 8080)
    assert req.headers["host"] == "hooks.example.com:8080"
    assert "sni_hostname" not in req.extensions


@pytest.mark.parametrize("answers", [
    ["10.0.0.7"],
    [PUBLIC_V4, "10.0.0.7"],         # one private answer is enough to refuse
    ["169.254.169.254"],
    ["::ffff:127.0.0.1"],
    ["fe80::1%eth0"],
    ["not-an-ip"],
])
async def test_a_name_that_resolves_to_any_non_public_address_is_blocked_before_sending(monkeypatch, answers):
    sent = _Sent()
    with pytest.raises(guard.DestinationBlocked):
        await _post("https://rebind.example.com/x", _resolver(answers), sent, monkeypatch)
    assert sent.requests == []


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/x", "http://10.1.2.3:8080/x", "https://[::1]/x",
    "https://localhost/x", "https://metadata.google.internal/x", "http://redis/x",
])
async def test_an_internal_literal_or_name_is_blocked_without_a_lookup(monkeypatch, url):
    sent, calls = _Sent(), []
    with pytest.raises(guard.DestinationBlocked):
        await _post(url, _resolver([PUBLIC_V4], calls), sent, monkeypatch)
    assert sent.requests == [] and calls == []


async def test_a_public_ip_literal_is_sent_without_a_lookup(monkeypatch):
    sent, calls = _Sent(), []
    await _post("https://8.8.8.8/x", _resolver([], calls), sent, monkeypatch)
    assert calls == [] and sent.requests[0].url.host == "8.8.8.8"


async def test_a_name_that_does_not_resolve_is_a_transient_connect_error(monkeypatch):
    sent = _Sent()
    with pytest.raises(httpx.ConnectError):
        await _post("https://gone.example.com/x", _resolver(OSError("nx")), sent, monkeypatch)
    with pytest.raises(httpx.ConnectError):
        await _post("https://gone.example.com/x", _resolver([]), sent, monkeypatch)
    assert sent.requests == []


async def test_the_answer_is_read_with_a_size_cap(monkeypatch):
    sent = _Sent(body=b"x" * 2048)
    with pytest.raises(httpx.ReadError):
        await _post("https://hooks.example.com/x", _resolver([PUBLIC_V4]), sent, monkeypatch,
                    max_response_bytes=1024)


# ── the delivery services ───────────────────────────────────────────────────────────────────


def _owner(mod):
    return {"agent_id": "agent_1"} if "agent" in mod.__name__ else {"merchant_id": "merch_1"}


def _arm(mod, monkeypatch, answers):
    monkeypatch.setattr(mod.webhook_delivery_gate, "delivery_enabled", lambda *a, **k: True)
    monkeypatch.setattr(guard, "_system_resolver", _resolver(answers))
    sent = _Sent()
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", sent.as_method())
    persisted = []

    async def persist(**kwargs):
        persisted.append(kwargs)

    monkeypatch.setattr(mod, "_persist_delivery_attempt", persist)
    return sent, persisted


async def _deliver(mod, url):
    return await mod._attempt_delivery(
        **_owner(mod), event_type="order.created", payload={"order_id": "o1"},
        destination_url=url, signing_secret="whsec_test", request_id=None,
    )


@pytest.mark.parametrize("modname", MODULES)
async def test_a_blocked_destination_is_a_terminal_failure_and_nothing_is_sent(monkeypatch, modname):
    mod = importlib.import_module(modname)
    sent, persisted = _arm(mod, monkeypatch, ["10.0.0.7"])
    result = await _deliver(mod, "https://rebind.example.com/hook")
    assert sent.requests == []
    assert result["status"] == "failed" and result["next_retry_at"] is None
    assert result["last_error"].startswith("Webhook destination blocked")
    assert persisted[0]["status"] == "failed" and persisted[0]["response_body"] is None


@pytest.mark.parametrize("modname", MODULES)
async def test_a_public_destination_is_delivered_through_the_pinned_transport(monkeypatch, modname):
    mod = importlib.import_module(modname)
    sent, _ = _arm(mod, monkeypatch, [PUBLIC_V4])
    result = await _deliver(mod, "https://hooks.example.com/hook")
    assert result["status"] == "delivered"
    [req] = sent.requests
    assert req.url.host == PUBLIC_V4 and req.headers["host"] == "hooks.example.com"
    assert req.headers["x-pivota-signature"].startswith("v1=")


def test_only_the_agents_own_managed_inbox_skips_the_transport():
    mod = importlib.import_module("services.agent_webhook_service")
    managed = mod.get_managed_receiver_url("agent_1")
    assert mod._delivery_client_options("agent_1", managed) == {}
    # another agent's managed inbox is just a URL, guarded like any other
    other = mod._delivery_client_options("agent_1", mod.get_managed_receiver_url("agent_2"))
    assert isinstance(other["transport"], guard.PublicDestinationTransport)
    assert other["trust_env"] is False  # an env proxy would route around the pinned transport


# ── saving a URL ────────────────────────────────────────────────────────────────────────────


def test_merchant_save_rejects_a_new_internal_url_and_keeps_an_unchanged_stored_one():
    mod = importlib.import_module("services.merchant_webhook_service")
    with pytest.raises(ValueError):
        mod._validate_destination_url("http://10.0.0.5/x", stored_url=None)
    with pytest.raises(ValueError):
        mod._validate_destination_url("http://legacy.example.com/new", stored_url="http://legacy.example.com/x")
    assert mod._validate_destination_url("http://legacy.example.com/x", "http://legacy.example.com/x") == (
        "http://legacy.example.com/x"
    )
    assert mod._validate_destination_url("  ", None) is None


def _agent_saving(monkeypatch, stored_url):
    mod = importlib.import_module("services.agent_webhook_service")
    written = []

    async def raw_config(agent_id):
        return {"agent_id": agent_id, "destination_url": stored_url, "created_at": "x", "signing_secret": "s"}

    async def execute(query, params=None):
        written.append(params)

    async def noop(*a, **k):
        return None

    async def config(agent_id):
        return {"destination_url": written[-1]["destination_url"] if written else None}

    monkeypatch.setattr(mod, "_get_or_create_raw_config", raw_config)
    monkeypatch.setattr(mod.database, "execute", execute)
    monkeypatch.setattr(mod, "_sync_legacy_agent_webhook_url", noop)
    monkeypatch.setattr(mod, "get_webhook_config", config)
    return mod, written


async def test_agent_save_rejects_a_new_internal_url_before_writing(monkeypatch):
    mod, written = _agent_saving(monkeypatch, stored_url=None)
    with pytest.raises(guard.DestinationRejected):
        await mod.update_webhook_config("agent_1", enabled=True, destination_url="https://169.254.169.254/x",
                                        subscribed_events=None)
    assert written == []


async def test_agent_save_keeps_an_unchanged_stored_url_and_the_managed_inbox(monkeypatch):
    mod, written = _agent_saving(monkeypatch, stored_url="http://legacy.example.com/x")
    out = await mod.update_webhook_config("agent_1", enabled=False, destination_url="http://legacy.example.com/x",
                                          subscribed_events=None)
    assert out["destination_url"] == "http://legacy.example.com/x"
    managed = mod.get_managed_receiver_url("agent_1")
    out = await mod.update_webhook_config("agent_1", enabled=True, destination_url=managed, subscribed_events=None)
    assert out["destination_url"] == managed


async def test_the_agent_route_answers_400_for_a_rejected_url(monkeypatch):
    from fastapi import HTTPException

    import routes.agent_webhooks as route

    async def reject(*a, **k):
        raise guard.DestinationRejected("Webhook URL must point to a public address")

    monkeypatch.setattr(route, "update_webhook_config", reject)
    monkeypatch.setattr(route, "_authorize_agent_scope", lambda *a, **k: None)
    request = route.UpdateWebhookConfigRequest(enabled=True, destination_url="https://10.0.0.1/x")
    with pytest.raises(HTTPException) as info:
        await route.put_agent_webhook_config("agent_1", request, current_user={"role": "agent"})
    assert info.value.status_code == 400
