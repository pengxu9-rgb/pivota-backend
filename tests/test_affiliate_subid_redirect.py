"""Our click id in the affiliate network's sub-ID, on the public `GET /r` (ADR-025 D2).

Through the real route and the real click logger (`log_outbound_click` -> `log_outbound_event`); only
the two storage calls at its bottom are captured, so what is asserted is what would be written.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

import services.affiliate_subid as subid
import services.outbound_links_service as links
from main import app
from services.commerce_attribution_service import PVT_CLICK_ID, materialize_attribution_context
from services.outbound_links_service import make_redirect_token

CLICK_ID = "clk_0123456789abcdef01234567"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
#: The click row's context for a token whose ctx is just the click id (the logger adds eventType).
PLAIN_CLICK_CTX = {PVT_CLICK_ID: CLICK_ID, "eventType": "click"}
RAKUTEN = "https://click.linksynergy.com/deeplink?id=AbC123&mid=99999&murl=https%3A%2F%2Fshop-a.example.com%2Fp%3Fx%3D1%26y%3D2"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OUTBOUND_LINKS_SIGNING_SECRET", "test-signing-secret-not-real")
    monkeypatch.setenv("AFFILIATE_SUBID_CLICK_ID_ENABLED", "true")
    monkeypatch.delenv("AFFILIATE_SUBID_PARAMS_JSON", raising=False)
    monkeypatch.delenv("OUTBOUND_WARM_HANDOFF_ENABLED", raising=False)
    subid.subid_param_map.cache_clear()
    yield
    subid.subid_param_map.cache_clear()


@pytest.fixture
def written(monkeypatch) -> Dict[str, List[Dict[str, Any]]]:
    """The outbound_click_events row and the surface_click_events input the real logger produces."""
    out: Dict[str, List[Dict[str, Any]]] = {"click_rows": [], "surface": []}

    async def _execute(_stmt, values=None):
        out["click_rows"].append(dict(values or {}))

    async def _surface(**kwargs):
        out["surface"].append(kwargs)
        return {}

    monkeypatch.setattr(links.database, "execute", _execute)
    monkeypatch.setattr(links, "record_surface_event", _surface)
    return out


def _token(dest: str, ctx: Optional[Dict[str, Any]] = None, **extra) -> str:
    return make_redirect_token(
        {"market": "US", "tool": "*", "dest": dest, "ctx": {PVT_CLICK_ID: CLICK_ID} if ctx is None else ctx, **extra}
    )


def _hit(token: str) -> str:
    res = TestClient(app).get(f"/r?token={token}", headers={"user-agent": UA}, follow_redirects=False)
    assert res.status_code == 302
    return res.headers["location"]


def _query(url: str) -> List[tuple]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


# --- the stamp --------------------------------------------------------------------------------------

def test_rakuten_destination_gets_u1_click_id_and_the_click_records_it(written) -> None:
    loc = _hit(_token(RAKUTEN))
    assert loc == RAKUTEN + "&u1=" + CLICK_ID
    ctx = written["click_rows"][0]["context"]
    assert ctx[subid.CTX_PARAM] == "u1" and ctx[subid.CTX_VALUE] == CLICK_ID
    assert ctx[subid.CTX_REPLACED] is False
    # The row keeps the SIGNED destination; the sub-ID is on the Location only.
    assert written["click_rows"][0]["destination_url"] == RAKUTEN
    # The id the surface row will record (the real materialiser, on what /r handed it) is the u1 value.
    surface_ctx = written["surface"][0]["token_payload"]["ctx"]
    assert materialize_attribution_context(surface_ctx)[PVT_CLICK_ID] == CLICK_ID


def test_existing_u1_is_replaced_not_appended(written) -> None:
    dest = "https://click.linksynergy.com/deeplink?id=AbC123&u1=partner-a-label&mid=99999&U1=dup&%751=enc#top"
    loc = _hit(_token(dest))
    assert loc == f"https://click.linksynergy.com/deeplink?id=AbC123&u1={CLICK_ID}&mid=99999#top"
    assert [v for k, v in _query(loc) if k.lower() == "u1"] == [CLICK_ID]
    assert written["click_rows"][0]["context"][subid.CTX_REPLACED] is True


def test_other_params_and_fragment_are_preserved_byte_for_byte(written) -> None:
    dest = "https://www.awin1.com/cread.php?awinmid=1&awinaffid=2&p=https%3A%2F%2Fshop-a.example.com%2F%3Fa%3D1&&e=%20x#frag?not=query"
    loc = _hit(_token(dest))
    head, _, frag = dest.partition("#")
    assert loc == f"{head}&clickref={CLICK_ID}#{frag}"


def test_destination_without_a_query_gets_one(written) -> None:
    dest = "https://brand-x.sjv.io/c/1/2/3#f"
    assert _hit(_token(dest)) == f"https://brand-x.sjv.io/c/1/2/3?subId1={CLICK_ID}#f"


@pytest.mark.parametrize(
    "dest",
    [
        "https://linksynergy.com/deeplink?id=AbC123",
        "https://click.linksynergy.com:443/deeplink?id=AbC123",
        "https://CLICK.LinkSynergy.com/deeplink?id=AbC123",
    ],
)
def test_the_bare_host_a_port_and_any_case_still_match(written, dest) -> None:
    assert _hit(_token(dest)) == f"{dest}&u1={CLICK_ID}"


def test_the_longest_matching_suffix_wins(monkeypatch, written) -> None:
    monkeypatch.setenv("AFFILIATE_SUBID_PARAMS_JSON", '{"click.linksynergy.com": "u9"}')
    assert _hit(_token(RAKUTEN)) == f"{RAKUTEN}&u9={CLICK_ID}"
    other = "https://other.linksynergy.com/x?a=1"
    assert _hit(_token(other)) == f"{other}&u1={CLICK_ID}"


@pytest.mark.parametrize(
    "dest",
    [
        "https://rakuten.com.example/deeplink?u1=keep",
        "https://evil-linksynergy.com/deeplink?u1=keep",
        "https://linksynergy.com.example/x",
        "https://click.linksynergy.com.evil.example/x",
        "https://linksynergy.com@shop-a.example.com/x",
        "https://notawin1.com/cread.php?clickref=keep",
    ],
)
def test_look_alike_hosts_are_untouched(written, dest) -> None:
    assert _hit(_token(dest)) == dest
    assert subid.CTX_PARAM not in (written["click_rows"][0]["context"] or {})


@pytest.mark.parametrize(
    "dest",
    ["https://shop-a.example.com/products/item-1?variant=1&u1=x#d", "http://brand-x.example.com/"],
)
def test_non_affiliate_destination_is_untouched(written, dest) -> None:
    assert _hit(_token(dest)) == dest
    assert written["click_rows"][0]["context"] == PLAIN_CLICK_CTX


def test_a_token_with_no_click_id_gets_one_minted_and_recorded_as_the_same_id(written) -> None:
    loc = _hit(_token(RAKUTEN, ctx={"area": "card"}))
    sent = dict(_query(loc))["u1"]
    assert subid._CLICK_ID_RE.fullmatch(sent)
    surface_ctx = written["surface"][0]["token_payload"]["ctx"]
    assert materialize_attribution_context(surface_ctx)[PVT_CLICK_ID] == sent
    assert written["click_rows"][0]["context"][subid.CTX_VALUE] == sent


@pytest.mark.parametrize("bad", ["partner-a", "clk_x", "buyer@example.com", "clk_" + "a" * 47])
def test_a_click_id_that_is_not_ours_in_shape_is_never_sent(written, bad) -> None:
    loc = _hit(_token(RAKUTEN, ctx={PVT_CLICK_ID: bad}))
    assert loc == RAKUTEN
    ctx = written["click_rows"][0]["context"]
    assert ctx[subid.CTX_SKIPPED] == "click_id_shape" and subid.CTX_VALUE not in ctx


def test_expired_and_synthetic_tokens_keep_the_signed_dest(written) -> None:
    expired = make_redirect_token(
        {"market": "US", "tool": "*", "dest": RAKUTEN, "ctx": {PVT_CLICK_ID: CLICK_ID}}, ttl_seconds=-10
    )
    assert _hit(expired) == RAKUTEN
    assert _hit(_token(RAKUTEN, syn=1)) == RAKUTEN
    assert written["click_rows"] == []


# --- the env override ---------------------------------------------------------------------------------

def test_env_override_adds_replaces_and_removes_networks(monkeypatch, written) -> None:
    monkeypatch.setenv(
        "AFFILIATE_SUBID_PARAMS_JSON",
        '{"network-b.example": "sub1", "linksynergy.com": "u2", "awin1.com": null, "bad host": "x", "ok.example": "1bad"}',
    )
    assert _hit(_token("https://t.network-b.example/c?o=1")) == f"https://t.network-b.example/c?o=1&sub1={CLICK_ID}"
    assert _hit(_token(RAKUTEN)) == f"{RAKUTEN}&u2={CLICK_ID}"
    awin = "https://www.awin1.com/cread.php?awinmid=1"
    assert _hit(_token(awin)) == awin
    assert _hit(_token("https://ok.example/x")) == "https://ok.example/x"


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"u1"'])
def test_a_malformed_override_falls_back_to_the_code_map(monkeypatch, written, raw) -> None:
    monkeypatch.setenv("AFFILIATE_SUBID_PARAMS_JSON", raw)
    assert _hit(_token(RAKUTEN)) == f"{RAKUTEN}&u1={CLICK_ID}"


# --- flag off -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "false", "1", "yes"])
def test_flag_off_leaves_the_redirect_and_the_click_ctx_unchanged(monkeypatch, written, flag) -> None:
    if flag is None:
        monkeypatch.delenv("AFFILIATE_SUBID_CLICK_ID_ENABLED")
    else:
        monkeypatch.setenv("AFFILIATE_SUBID_CLICK_ID_ENABLED", flag)
    for dest in (RAKUTEN, "https://click.linksynergy.com/x?u1=old#f", "https://shop-a.example.com/p"):
        assert _hit(_token(dest)) == dest
    assert [row["context"] for row in written["click_rows"]] == [PLAIN_CLICK_CTX] * 3


# --- the map itself -----------------------------------------------------------------------------------

def test_every_mapped_host_is_on_the_warm_handoff_affiliate_denylist() -> None:
    from services.outbound_warm_handoff import AFFILIATE_HOST_SUFFIXES

    assert set(subid.AFFILIATE_SUBID_PARAMS) <= set(AFFILIATE_HOST_SUFFIXES)


def test_a_real_click_id_fits_the_value_bound() -> None:
    from services.commerce_attribution_service import new_click_id

    cid = new_click_id()
    assert len(cid) <= subid.SUBID_VALUE_MAX_LEN and subid._CLICK_ID_RE.fullmatch(cid)
