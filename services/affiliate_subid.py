"""Our click id in the affiliate network's sub-ID parameter, on the `/r` click-out (ADR-025 D2).

WHY. A network (Rakuten Advertising, Awin, ...) reports commissions keyed by a sub-ID the publisher
puts on the tracking link, e.g. Rakuten's `u1`. A feed's `link` is that tracking link
(services/retailer_ingest/affiliate_feed.py), but nothing put our click id in a sub-ID, so a
commission report could not be tied to a click, and through it to a product or a partner agent.
ADR-025 D2 names the carrier: "the network's tracking link, with our click id in its sub-id
parameter", and decision 2 there: the sub-ID is the click id, NEVER the agent id.

WHAT GOES IN IT. Only our opaque click id (`clk_<hex>`, services.commerce_attribution_service
.new_click_id), the SAME id `/r` records for the click. Never a partner, agent, merchant or buyer
value. A click id that does not have that shape, or is longer than SUBID_VALUE_MAX_LEN, is not sent.

WHERE. At `/r` redirect time only (routes/outbound_links.redirect_endpoint), AFTER the token's HMAC is
verified, on the URL put in the Location header. The signed `dest` is never changed:
  * the signature covers the token payload, and is checked before anything here runs;
  * the edit only touches the query string of a URL whose host the signature already vouched for,
    and a post-condition refuses any result whose scheme, host, path or fragment differ;
  * the value comes from the signed token's ctx (or a click id minted server-side), never from the
    request, so a clicker cannot steer it;
  * the URLs published at mint time (offers.resolve `cart_url` / `pdp_url`, card URLs) are compared
    against the signed `dest`, which is unchanged, so that anti-drift check holds as before.
The click's ctx records the parameter and value (`affiliate_subid_param` / `affiliate_subid_value`),
so a commission import can join on them.

WHICH HOSTS. A small code map, host suffix -> parameter name, matched on a label boundary (the host
IS the suffix or ends with "." + suffix; `evil-linksynergy.com` and `linksynergy.com.example` do not
match). Every entry's host is already in services.outbound_warm_handoff.AFFILIATE_HOST_SUFFIXES, and
each parameter name is from the network's public link documentation (cited per entry). Networks
whose parameter could not be confirmed are left out. AFFILIATE_SUBID_PARAMS_JSON merges over the map.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote, unquote_plus, urlsplit

from services.commerce_attribution_service import PVT_CLICK_ID, new_click_id

logger = logging.getLogger("affiliate_subid")

#: Host suffix -> the network's sub-ID query parameter. Sources (public publisher docs, read only):
#:  * linksynergy.com   u1        Rakuten Advertising publisher help: the u1 "Member ID" on
#:                                click.linksynergy.com links, up to 72 alphanumeric characters; it
#:                                says never to put an email, phone or other private data in it.
#:  * awin1.com         clickref  Awin publisher help ("click reference"), cread.php links; the
#:                                Click Reference report shows the first 50 characters.
#:  * shareasale.com    afftrack  ShareASale "Affiliate Defined Tracking": afftrack= on r.cfm
#:                                links, up to 255 alphanumeric characters.
#:  * skimresources.com xcust     Skimlinks LinkWrapper guide: xcust, <= 50 characters of letters,
#:                                digits, underscores and pipes.
#:  * go2cloud.org      aff_sub   TUNE (HasOffers) partner sub IDs aff_sub..aff_sub5 on go2cloud.org
#:                                links, URL-encoded, up to 500 characters.
#:  * sjv.io, pxf.io    subId1    impact.com partner help ("Sub ID and Shared ID parameters"):
#:                                subId1..subId3, alphanumeric, 255 characters (older docs: 64).
#: Left out on purpose: rakuten.com and rakutenadvertising.com (not the tracking-link host; Rakuten's
#: u1 is documented on linksynergy.com links), impact.com (corporate domain, not a link host),
#: prf.hn (Partnerize carries `pubref:` as a PATH segment, not a query parameter), and every network
#: not in AFFILIATE_HOST_SUFFIXES (Involve Asia, CJ: parameter or host not confirmed here).
AFFILIATE_SUBID_PARAMS: Mapping[str, str] = MappingProxyType({
    "linksynergy.com": "u1",
    "awin1.com": "clickref",
    "shareasale.com": "afftrack",
    "skimresources.com": "xcust",
    "go2cloud.org": "aff_sub",
    "sjv.io": "subId1",
    "pxf.io": "subId1",
})

#: The longest sub-ID value we send. The tightest documented limit among the mapped networks is 50
#: (Awin's report display, Skimlinks' xcust); a network added through the env gets the same bound.
#: Our click ids are 28 characters (`clk_` + 24 hex), well inside it.
SUBID_VALUE_MAX_LEN = 50

#: Our click id, and nothing else: `clk_` then letters, digits, `_` or `-`, at most
#: SUBID_VALUE_MAX_LEN in all. The same family woocommerce_conversion_poller accepts as ours.
_CLICK_ID_RE = re.compile(r"clk_[A-Za-z0-9_-]{6,%d}" % (SUBID_VALUE_MAX_LEN - 4))
_HOST_SUFFIX_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_PARAM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")

#: The ctx keys materialize_attribution_context reads the click id from, in its order. The value
#: written to the sub-ID is the one it will record.
_CLICK_ID_KEYS = (PVT_CLICK_ID, "click_id", "clickId")

CTX_PARAM = "affiliate_subid_param"
CTX_VALUE = "affiliate_subid_value"
CTX_REPLACED = "affiliate_subid_replaced"
CTX_SKIPPED = "affiliate_subid_skipped"


@lru_cache(maxsize=8)
def subid_param_map(override_json: str = "") -> Mapping[str, str]:
    """The code map with AFFILIATE_SUBID_PARAMS_JSON merged over it.

    The override is a JSON object {"<host suffix>": "<param>" | null}: a string adds or replaces a
    network, null removes one. A non-object or unparseable override is ignored whole; an entry with
    a bad host suffix or parameter name is ignored alone. Never raises.
    """
    merged: Dict[str, str] = dict(AFFILIATE_SUBID_PARAMS)
    raw = (override_json or "").strip()
    if not raw:
        return MappingProxyType(merged)
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("AFFILIATE_SUBID_PARAMS_JSON is not JSON; using the code map")
        return MappingProxyType(merged)
    if not isinstance(parsed, dict):
        logger.warning("AFFILIATE_SUBID_PARAMS_JSON is not an object; using the code map")
        return MappingProxyType(merged)
    for key, value in parsed.items():
        suffix = str(key or "").strip().lower()
        if not _HOST_SUFFIX_RE.fullmatch(suffix):
            logger.warning("AFFILIATE_SUBID_PARAMS_JSON: ignoring bad host suffix %r", key)
            continue
        if value is None:
            merged.pop(suffix, None)
            continue
        if not isinstance(value, str) or not _PARAM_RE.fullmatch(value):
            logger.warning("AFFILIATE_SUBID_PARAMS_JSON: ignoring bad parameter for %s", suffix)
            continue
        merged[suffix] = value
    return MappingProxyType(merged)


def _host_matches(host: str, suffix: str) -> bool:
    """On a label boundary: the host IS the suffix, or ends with "." + suffix."""
    return host == suffix or host.endswith("." + suffix)


def subid_param_for_url(url: str, mapping: Mapping[str, str]) -> Optional[str]:
    """The sub-ID parameter for `url`'s host, or None. The longest matching suffix wins."""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    best: Optional[Tuple[str, str]] = None
    for suffix, param in mapping.items():
        if _host_matches(host, suffix) and (best is None or len(suffix) > len(best[0])):
            best = (suffix, param)
    return best[1] if best else None


def set_query_param(url: str, param: str, value: str) -> Tuple[str, bool]:
    """`url` with exactly one `param=value`, and whether a value was already there.

    REPLACES, never appends a second one: the first existing `param` (name compared decoded and
    case-insensitively, since a network may read either spelling) takes the new value in place, and
    any further copies are dropped. With none, it is added at the end of the query. Every other
    query segment is kept byte-for-byte, in order, and the fragment is untouched. String surgery on
    purpose: a parse/re-encode round trip would re-encode other parameters (a deep link's `murl`).
    """
    head, hash_sep, fragment = url.partition("#")
    base, q_sep, query = head.partition("?")
    pair = f"{quote(param, safe='')}={quote(value, safe='')}"
    wanted = param.lower()
    out = []
    replaced = False
    for segment in query.split("&") if query else []:
        name = unquote_plus(segment.split("=", 1)[0])
        if name.lower() == wanted:
            if not replaced:
                out.append(pair)
            replaced = True
            continue
        out.append(segment)
    if not replaced:
        out.append(pair)
    return f"{base}?{'&'.join(out)}{hash_sep}{fragment}", replaced


@dataclass(frozen=True)
class SubidDecision:
    url: str
    ctx_updates: Dict[str, Any]


def apply_click_subid(dest: str, ctx: Optional[Dict[str, Any]], *, override_json: str = "") -> SubidDecision:
    """The URL `/r` should redirect to, and what to add to the click's ctx.

    Not a mapped network host: `dest` unchanged and nothing added. A mapped host: the click id from
    the ctx (minted, and added to the ctx under pvt_click_id, when the token carries none, so the
    recorded click and the sub-ID are one id) goes into the network's parameter. A click id that is
    not ours in shape, or a rewrite that would change anything but the query, sends `dest` unchanged
    and records why (`affiliate_subid_skipped`).
    """
    param = subid_param_for_url(dest, subid_param_map(override_json))
    if not param:
        return SubidDecision(dest, {})
    source = ctx if isinstance(ctx, dict) else {}
    updates: Dict[str, Any] = {CTX_PARAM: param}
    click_id = next(
        (str(source[k]).strip() for k in _CLICK_ID_KEYS if source.get(k) is not None and str(source[k]).strip()),
        None,
    )
    if click_id is None:
        click_id = new_click_id()
        updates[PVT_CLICK_ID] = click_id
    if not _CLICK_ID_RE.fullmatch(click_id):
        updates[CTX_SKIPPED] = "click_id_shape"
        return SubidDecision(dest, updates)
    stamped, replaced = set_query_param(dest, param, click_id)
    before, after = urlsplit(dest), urlsplit(stamped)
    if (before.scheme, before.netloc, before.path, before.fragment) != (
        after.scheme, after.netloc, after.path, after.fragment
    ):
        updates.pop(PVT_CLICK_ID, None)
        updates[CTX_SKIPPED] = "url_shape"
        return SubidDecision(dest, updates)
    updates[CTX_VALUE] = click_id
    updates[CTX_REPLACED] = replaced
    return SubidDecision(stamped, updates)
