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
    "https://my-receiver.example.app/hook",
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
    "https://[64:ff9b:1::a00:5]/x",         # local-use NAT64
    "https://[::a00:5]/x",                  # IPv4-compatible
    "https://[fec0::1]/x",                  # site-local
    "https://web-123456789012.us-west1.run.app/x",  # our own Cloud Run: internal under VPC egress
    "https://gateway-abc123-uw.a.run.app/x",
    "https://0177.0.0.1/x",                 # httpx refuses it at send time
    "https://hooks.example.com:0/x",
    "https://a.b\u3002localhost/x",          # ideographic full stop: httpx reads a.b.localhost
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


async def test_a_long_answer_is_truncated_not_failed(monkeypatch):
    sent = _Sent(body=b"x" * 2048)
    response = await _post("https://hooks.example.com/x", _resolver([PUBLIC_V4]), sent, monkeypatch,
                           max_response_bytes=1024)
    assert response.status_code == 200 and response.content == b"x" * 1024


async def test_run_app_names_are_blocked_at_send_whatever_they_resolve_to(monkeypatch):
    sent, calls = _Sent(), []
    with pytest.raises(guard.DestinationBlocked):
        await _post("https://web-123456789012.us-west1.run.app/x", _resolver([PUBLIC_V4], calls), sent, monkeypatch)
    assert sent.requests == [] and calls == []


def test_extra_blocked_suffixes_are_configurable(monkeypatch):
    monkeypatch.setenv("WEBHOOK_BLOCKED_HOST_SUFFIXES", "internal.example, .corp.example")
    for url in ("https://svc.internal.example/x", "https://a.corp.example/x", "https://svc.run.app/x"):
        with pytest.raises(guard.DestinationRejected):
            guard.check_destination_url(url)  # extra suffixes ADD to the default, never replace it
    assert guard.check_destination_url("https://hooks.example.com/x") == "https://hooks.example.com/x"


async def test_a_resolver_that_hangs_is_a_connect_timeout(monkeypatch):
    import asyncio

    async def hang(host, port):
        await asyncio.sleep(30)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _Sent().as_method())
    transport = guard.PublicDestinationTransport(resolver=hang)
    async with httpx.AsyncClient(transport=transport, timeout=0.05, trust_env=False) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.post("https://hooks.example.com/x", content=b"{}")


async def test_a_dead_address_falls_through_to_the_next(monkeypatch):
    tried = []

    async def handle(transport, request):
        tried.append(request.url.host)
        if len(tried) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    transport = guard.PublicDestinationTransport(resolver=_resolver(["93.184.216.35", PUBLIC_V4]))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.post("https://hooks.example.com/x", content=b'{"a":1}')
    assert response.status_code == 200 and tried == [PUBLIC_V4, "93.184.216.35"]


def test_the_interpreter_knows_the_current_special_purpose_registry():
    import ipaddress

    # 64:ff9b:1::/48 entered the stdlib registry in 3.11.10 / 3.12.4; an older interpreter would
    # call it global, so fail loudly here rather than trust it.
    assert not guard.is_public_address(ipaddress.ip_address("64:ff9b:1::1"))
    assert ipaddress.ip_address("64:ff9b:1::1").is_global is False


# ── through the real httpcore pool, with a recording network backend (no sockets) ──────────


def _recording_backend(rec):
    from httpcore._backends import mock

    class Stream(mock.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            rec.setdefault("written", b"")
            rec["written"] += bytes(buffer)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            rec["sni"] = server_hostname
            rec["check_hostname"] = ssl_context.check_hostname
            return self

        def get_extra_info(self, info):
            return None if info == "ssl_object" else super().get_extra_info(info)

    class Backend(mock.AsyncMockBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            rec["connect"] = (host, port)
            return Stream(list(self._buffer))

    return Backend([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"])


@pytest.mark.parametrize("url,answers,connect,host_header", [
    ("https://hooks.example.com/p", [PUBLIC_V4], (PUBLIC_V4, 443), "hooks.example.com"),
    ("https://hooks.example.com:8443/p", [PUBLIC_V4], (PUBLIC_V4, 8443), "hooks.example.com:8443"),
    ("https://hooks.example.com/p", ["2001:4860:4860::8888"], ("2001:4860:4860::8888", 443), "hooks.example.com"),
])
async def test_real_httpcore_connects_to_the_checked_address_and_verifies_the_registered_name(
        url, answers, connect, host_header):
    rec = {}
    transport = guard.PublicDestinationTransport(resolver=_resolver(answers))
    transport._pool._network_backend = _recording_backend(rec)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.post(url, content=b"{}")
    assert response.status_code == 200
    assert rec["connect"] == connect
    assert rec["sni"] == "hooks.example.com" and rec["check_hostname"] is True
    head = rec["written"].split(b"\r\n\r\n", 1)[0].decode("latin1").split("\r\n")
    assert f"Host: {host_header}" in head


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


@pytest.mark.parametrize("modname", MODULES)
async def test_a_2xx_with_a_long_html_body_is_delivered_not_retried(monkeypatch, modname):
    mod = importlib.import_module(modname)
    sent, persisted = _arm(mod, monkeypatch, [PUBLIC_V4])
    sent.body = b"<html>" + b"x" * 200_000 + b"</html>"
    result = await _deliver(mod, "https://hooks.example.com/hook")
    assert result["status"] == "delivered" and result["http_status"] == 200
    assert len(persisted[0]["response_body"]) == 4000


async def test_the_transport_itself_refuses_a_url_with_credentials(monkeypatch):
    sent = _Sent()
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", sent.as_method())
    transport = guard.PublicDestinationTransport(resolver=_resolver([PUBLIC_V4]))
    with pytest.raises(guard.DestinationBlocked):
        await transport.handle_async_request(httpx.Request("POST", "https://u:p@hooks.example.com/x"))
    assert sent.requests == []



@pytest.mark.parametrize("raw", ["", " ", ",", " , .", "corp.example"])
def test_the_run_app_block_survives_any_suffix_setting(monkeypatch, raw):
    monkeypatch.setenv("WEBHOOK_BLOCKED_HOST_SUFFIXES", raw)
    with pytest.raises(guard.DestinationRejected):
        guard.check_destination_url("https://x.run.app/x")


@pytest.mark.parametrize("url", ["https://web-1.run%2Eapp/x", "https://a%20b.example/x", "https://a\\.b/x",
                                 "https://web-1.run.app%00.evil.example/x"])
def test_a_host_no_resolver_can_look_up_is_rejected_at_save(url):
    with pytest.raises(guard.DestinationRejected):
        guard.check_destination_url(url)


async def test_dns_tls_and_host_all_judge_the_punycode_name(monkeypatch):
    rec = {}
    looked_up = []

    async def resolve(host, port):
        looked_up.append(host)
        return [PUBLIC_V4]

    transport = guard.PublicDestinationTransport(resolver=resolve)
    transport._pool._network_backend = _recording_backend(rec)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        await client.post("https://fa\u00df.de/p", content=b"{}")
    head = rec["written"].split(b"\r\n\r\n", 1)[0].decode("latin1").split("\r\n")
    assert looked_up == ["xn--fa-hia.de"] and rec["sni"] == "xn--fa-hia.de"
    assert "Host: xn--fa-hia.de" in head


async def test_a_trailing_dot_is_not_sent_as_the_tls_name():
    rec = {}
    transport = guard.PublicDestinationTransport(resolver=_resolver([PUBLIC_V4]))
    transport._pool._network_backend = _recording_backend(rec)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        await client.post("https://hooks.example.com./p", content=b"{}")
    assert rec["sni"] == "hooks.example.com"


async def test_real_httpcore_falls_through_a_refused_address_without_sending_twice():
    from httpcore._backends import mock

    connects, rec = [], {}

    class Stream(mock.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            rec.setdefault("written", b"")
            rec["written"] += bytes(buffer)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            return self

        def get_extra_info(self, info):
            return None if info == "ssl_object" else super().get_extra_info(info)

    class Backend(mock.AsyncMockBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            import httpcore

            connects.append(host)
            if len(connects) == 1:
                raise httpcore.ConnectError("refused")
            return Stream(list(self._buffer))

    transport = guard.PublicDestinationTransport(resolver=_resolver(["93.184.216.35", PUBLIC_V4]))
    transport._pool._network_backend = Backend([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"])
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.post("https://hooks.example.com/p", content=b'{"event":1}')
    assert response.status_code == 200 and connects == [PUBLIC_V4, "93.184.216.35"]
    assert rec["written"].count(b'{"event":1}') == 1
