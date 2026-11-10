"""Our click id in the affiliate network's sub-ID, on the public `GET /r` (ADR-025 D2).

Through the real route and the real click logger (`log_outbound_click` -> `log_outbound_event`); only
the two storage calls at its bottom are captured, so what is asserted is what would be written.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

import logging

import pytest
from fastapi.testclient import TestClient

import services.affiliate_subid as subid
import services.outbound_links_service as links
from main import app
from services.commerce_attribution_service import PVT_CLICK_ID, materialize_attribution_context
from services.outbound_links_service import make_redirect_token

CLICK_ID = "clk_0123456789abcdef01234567"
#: The same id in FORM_HEX: what an alphanumeric-only network (Rakuten u1, ...) is sent.
HEX = CLICK_ID[len("clk_"):]
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
    assert loc == RAKUTEN + "&u1=" + HEX
    ctx = written["click_rows"][0]["context"]
    assert ctx[subid.CTX_PARAM] == "u1" and ctx[subid.CTX_VALUE] == HEX
    assert ctx[subid.CTX_REPLACED] is False
    # The import re-prefixes the bare form back to the click id the click row records.
    assert subid.click_id_from_subid(ctx[subid.CTX_VALUE]) == ctx[PVT_CLICK_ID] == CLICK_ID
    # The row keeps the SIGNED destination; the sub-ID is on the Location only.
    assert written["click_rows"][0]["destination_url"] == RAKUTEN
    # The id the surface row will record (the real materialiser, on what /r handed it) is the u1 value.
    surface_ctx = written["surface"][0]["token_payload"]["ctx"]
    assert materialize_attribution_context(surface_ctx)[PVT_CLICK_ID] == CLICK_ID


def test_existing_u1_is_replaced_not_appended(written) -> None:
    dest = "https://click.linksynergy.com/deeplink?id=AbC123&u1=partner-a-label&mid=99999&U1=dup&%751=enc#top"
    loc = _hit(_token(dest))
    assert loc == f"https://click.linksynergy.com/deeplink?id=AbC123&u1={HEX}&mid=99999#top"
    assert [v for k, v in _query(loc) if k.lower() == "u1"] == [HEX]
    assert written["click_rows"][0]["context"][subid.CTX_REPLACED] is True


def test_other_params_and_fragment_are_preserved_byte_for_byte(written) -> None:
    dest = "https://www.awin1.com/cread.php?awinmid=1&awinaffid=2&p=https%3A%2F%2Fshop-a.example.com%2F%3Fa%3D1&&e=%20x#frag?not=query"
    loc = _hit(_token(dest))
    head, _, frag = dest.partition("#")
    assert loc == f"{head}&clickref={CLICK_ID}#{frag}"


def test_destination_without_a_query_gets_one(written) -> None:
    dest = "https://brand-x.sjv.io/c/1/2/3#f"
    assert _hit(_token(dest)) == f"https://brand-x.sjv.io/c/1/2/3?subId1={HEX}#f"


@pytest.mark.parametrize(
    "dest",
    [
        "https://linksynergy.com/deeplink?id=AbC123",
        "https://click.linksynergy.com:443/deeplink?id=AbC123",
        "https://CLICK.LinkSynergy.com/deeplink?id=AbC123",
    ],
)
def test_the_bare_host_a_port_and_any_case_still_match(written, dest) -> None:
    assert _hit(_token(dest)) == f"{dest}&u1={HEX}"


def test_the_longest_matching_suffix_wins(monkeypatch, written) -> None:
    monkeypatch.setenv("AFFILIATE_SUBID_PARAMS_JSON", '{"click.linksynergy.com": "u9"}')
    assert _hit(_token(RAKUTEN)) == f"{RAKUTEN}&u9={HEX}"  # a suffix not in the code map: FORM_HEX
    other = "https://other.linksynergy.com/x?a=1"
    assert _hit(_token(other)) == f"{other}&u1={HEX}"


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
    assert sent.isalnum() and len(sent) == 24
    surface_ctx = written["surface"][0]["token_payload"]["ctx"]
    assert materialize_attribution_context(surface_ctx)[PVT_CLICK_ID] == subid.click_id_from_subid(sent)
    assert written["click_rows"][0]["context"][subid.CTX_VALUE] == sent


@pytest.mark.parametrize(
    "bad",
    [
        "partner-a",
        "clk_x",
        "buyer@example.com",
        "clk_" + "a" * 47,
        "clk_" + "a" * 23,  # one hex short
        "clk_" + "a" * 25,  # one hex long
        "clk_0123456789ABCDEF01234567",  # upper case: new_click_id never mints it
        "clk_0123456789abcdef0123456_",  # an underscore in the body
        "clk_0123456789abcdef0123-567",  # a hyphen in the body
        "clk_mpx_0123456789abcdef",  # a probe job's throwaway id, not a buyer click
        HEX,  # the bare form is what we SEND, never a click id we accept
    ],
)
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
    # A network the code map does not know gets FORM_HEX; a code-map network keeps its own form.
    assert _hit(_token("https://t.network-b.example/c?o=1")) == f"https://t.network-b.example/c?o=1&sub1={HEX}"
    assert _hit(_token(RAKUTEN)) == f"{RAKUTEN}&u2={HEX}"
    awin = "https://www.awin1.com/cread.php?awinmid=1"
    assert _hit(_token(awin)) == awin
    assert _hit(_token("https://ok.example/x")) == "https://ok.example/x"


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"u1"'])
def test_a_malformed_override_falls_back_to_the_code_map(monkeypatch, written, raw) -> None:
    monkeypatch.setenv("AFFILIATE_SUBID_PARAMS_JSON", raw)
    assert _hit(_token(RAKUTEN)) == f"{RAKUTEN}&u1={HEX}"


def test_env_override_object_sets_the_form_and_a_bad_form_is_ignored(monkeypatch, written) -> None:
    monkeypatch.setenv(
        "AFFILIATE_SUBID_PARAMS_JSON",
        '{"network-b.example": {"param": "sub1", "form": "clk"}, "network-c.example": {"param": "s2"},'
        ' "skimresources.com": {"param": "xcust", "form": "hex"}, "awin1.com": "clickref2",'
        ' "ok.example": {"param": "s", "form": "b64"}, "ok2.example": {"param": "s", "extra": 1}}',
    )
    assert _hit(_token("https://t.network-b.example/c")) == f"https://t.network-b.example/c?sub1={CLICK_ID}"
    assert _hit(_token("https://t.network-c.example/c")) == f"https://t.network-c.example/c?s2={HEX}"
    skim = "https://go.skimresources.com/?id=1X2&url=https%3A%2F%2Fshop-a.example.com%2F"
    assert _hit(_token(skim)) == f"{skim}&xcust={HEX}"
    awin = "https://www.awin1.com/cread.php?awinmid=1"
    assert _hit(_token(awin)) == f"{awin}&clickref2={CLICK_ID}"  # a string keeps the code map's form
    for ignored in ("https://ok.example/x", "https://ok2.example/x"):
        assert _hit(_token(ignored)) == ignored


DENIED = sorted(subid.SUBID_PARAM_DENYLIST)


@pytest.mark.parametrize("name", DENIED + [n.upper() for n in DENIED] + ["Murl", "AwinMid", "URL"])
def test_env_override_never_names_a_destination_or_id_parameter(monkeypatch, written, caplog, name) -> None:
    monkeypatch.setenv(
        "AFFILIATE_SUBID_PARAMS_JSON",
        '{"linksynergy.com": "%s", "network-b.example": {"param": "%s", "form": "clk"}}' % (name, name),
    )
    caplog.set_level(logging.WARNING, logger="affiliate_subid")
    # A code-map network keeps its own entry; the destination (murl) and the ids are byte-identical.
    assert _hit(_token(RAKUTEN)) == f"{RAKUTEN}&u1={HEX}"
    other = "https://t.network-b.example/c?u=https%3A%2F%2Fshop-a.example.com%2F&id=AbC123&p=1"
    assert _hit(_token(other)) == other
    assert subid.CTX_PARAM not in written["click_rows"][1]["context"]
    # Two more clicks: the refusal is logged once per entry, not per click.
    _hit(_token(RAKUTEN))
    _hit(_token(other))
    refusals = [r for r in caplog.records if r.name == "affiliate_subid" and "refusing" in r.getMessage()]
    assert len(refusals) == 2
    assert {r.getMessage().rsplit(" for ", 1)[1].split(":")[0] for r in refusals} == {
        "linksynergy.com", "network-b.example",
    }


def test_no_code_map_parameter_is_on_the_denylist() -> None:
    assert not {rule.param.lower() for rule in subid.AFFILIATE_SUBID_PARAMS.values()} & subid.SUBID_PARAM_DENYLIST


# --- every network row ----------------------------------------------------------------------------------

#: One tracking link per code-map row, with the parameter and form it must get. A row added to the
#: map without one here fails test_every_code_map_row_is_tested.
NETWORK_ROWS = {
    "linksynergy.com": (RAKUTEN, "u1", "hex"),
    "awin1.com": ("https://www.awin1.com/cread.php?awinmid=1&awinaffid=2&p=https%3A%2F%2Fshop-a.example.com%2F", "clickref", "clk"),
    "shareasale.com": ("https://shareasale.com/r.cfm?b=1&u=2&m=3&urllink=shop-a.example.com%2Fp", "afftrack", "hex"),
    "skimresources.com": ("https://go.skimresources.com/?id=1X2&url=https%3A%2F%2Fshop-a.example.com%2F", "xcust", "clk"),
    "go2cloud.org": ("https://network-b.go2cloud.org/aff_c?offer_id=1&aff_id=2", "aff_sub", "clk"),
    "sjv.io": ("https://brand-x.sjv.io/c/1/2/3?u=https%3A%2F%2Fshop-a.example.com%2F", "subId1", "hex"),
    "pxf.io": ("https://brand-x.pxf.io/c/1/2/3", "subId1", "hex"),
}


def test_every_code_map_row_is_tested() -> None:
    assert set(NETWORK_ROWS) == set(subid.AFFILIATE_SUBID_PARAMS)
    for host, (_dest, param, form) in NETWORK_ROWS.items():
        assert subid.AFFILIATE_SUBID_PARAMS[host] == subid.SubidRule(param, form)


@pytest.mark.parametrize("host", sorted(NETWORK_ROWS))
def test_each_network_gets_its_parameter_in_its_form_and_the_import_reads_it_back(written, host) -> None:
    dest, param, form = NETWORK_ROWS[host]
    loc = _hit(_token(dest))
    want = HEX if form == "hex" else CLICK_ID
    sep = "&" if "?" in dest else "?"
    assert loc == f"{dest}{sep}{param}={want}"
    if form == "hex":
        assert want.isalnum() and "clk" not in want  # letters and digits only
    ctx = written["click_rows"][0]["context"]
    assert (ctx[subid.CTX_PARAM], ctx[subid.CTX_VALUE]) == (param, want)
    surface_ctx = written["surface"][0]["token_payload"]["ctx"]
    assert subid.click_id_from_subid(dict(_query(loc))[param]) == materialize_attribution_context(surface_ctx)[PVT_CLICK_ID]


@pytest.mark.parametrize(
    "value, want",
    [
        (HEX, CLICK_ID),
        (HEX.upper(), CLICK_ID),  # a report may upper-case it
        (f"  {HEX} ", CLICK_ID),
        (CLICK_ID, CLICK_ID),
        (CLICK_ID.upper(), CLICK_ID),
        ("partner-a-label", None),
        (HEX[:-1], None),
        (HEX + "0", None),
        ("clk_" + HEX[:-1], None),
        ("clk-" + HEX, None),
        ("", None),
        (None, None),
        (123, None),
    ],
)
def test_click_id_from_subid_re_prefixes_the_bare_form(value, want) -> None:
    assert subid.click_id_from_subid(value) == want


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
