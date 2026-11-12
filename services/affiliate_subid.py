"""Our click id in the affiliate network's sub-ID parameter, on the `/r` click-out (ADR-025 D2).

WHY. A network (Rakuten Advertising, Awin, ...) reports commissions keyed by a sub-ID the publisher
puts on the tracking link, e.g. Rakuten's `u1`. A feed's `link` is that tracking link
(services/retailer_ingest/affiliate_feed.py), but nothing put our click id in a sub-ID, so a
commission report could not be tied to a click, and through it to a product or a partner agent.
ADR-025 D2 names the carrier: "the network's tracking link, with our click id in its sub-id
parameter", and decision 2 there: the sub-ID is the click id, NEVER the agent id.

WHAT GOES IN IT. Only our opaque click id (`clk_<24 hex>`, services.commerce_attribution_service
.new_click_id), the SAME id `/r` records for the click. Never a partner, agent, merchant or buyer
value. A click id that does not have exactly the minted shape is not sent. Each network row says
the value's FORM: networks whose sub-ID is documented alphanumeric get the 24 hex characters alone
(FORM_HEX: no `clk_`, no `_` or `-`); the others get the click id as minted (FORM_CLK). A commission
import turns either back into the click id with click_id_from_subid.

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

WHICH HOSTS. A small code map, host suffix -> (parameter name, value form), matched on a label boundary (the host
IS the suffix or ends with "." + suffix; `evil-linksynergy.com` and `linksynergy.com.example` do not
match). Every entry's host is already in services.outbound_warm_handoff.AFFILIATE_HOST_SUFFIXES, and
each parameter name is from the network's public link documentation (cited per entry). Networks
whose parameter could not be confirmed are left out. AFFILIATE_SUBID_PARAMS_JSON merges over the map,
but never with a parameter on SUBID_PARAM_DENYLIST (one that carries the destination or an id).
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

#: The value forms (see WHAT GOES IN IT above).
#:  * FORM_HEX: the click id's 24 hex characters, without `clk_`: letters and digits only.
#:  * FORM_CLK: the click id as minted, `clk_<24 hex>`, for a network that allows `_`.
FORM_HEX = "hex"
FORM_CLK = "clk"
_FORMS = (FORM_HEX, FORM_CLK)

#: The prefix new_click_id puts on every click id; FORM_HEX drops it and click_id_from_subid restores it.
CLICK_ID_PREFIX = "clk_"


@dataclass(frozen=True)
class SubidRule:
    """A network's sub-ID: the query parameter, and the form our click id takes in it."""

    param: str
    form: str


#: Host suffix -> the network's sub-ID query parameter and value form. Sources (public publisher
#: docs, read only); the form follows each network's documented character rule:
#:  * linksynergy.com   u1        Rakuten Advertising publisher help: the u1 "Member ID" on
#:                                click.linksynergy.com links, up to 72 alphanumeric characters; it
#:                                says never to put an email, phone or other private data in it.
#:                                Alphanumeric: FORM_HEX.
#:  * awin1.com         clickref  Awin publisher help ("click reference"), cread.php links; the
#:                                Click Reference report shows the first 50 characters. Lowercase
#:                                letters, digits and listed specials, `_` among them: FORM_CLK.
#:  * shareasale.com    afftrack  ShareASale "Affiliate Defined Tracking": afftrack= on r.cfm
#:                                links, up to 255 alphanumeric characters: FORM_HEX.
#:  * skimresources.com xcust     Skimlinks LinkWrapper guide: xcust, <= 50 characters of letters,
#:                                digits, underscores and pipes: FORM_CLK.
#:  * go2cloud.org      aff_sub   TUNE (HasOffers) partner sub IDs aff_sub..aff_sub5 on go2cloud.org
#:                                links, URL-encoded, up to 500 characters; no character set is
#:                                stated beyond URL encoding, and `_` needs none: FORM_CLK.
#:  * sjv.io, pxf.io    subId1    impact.com partner help ("Sub ID and Shared ID parameters"):
#:                                subId1..subId3, alphanumeric, 255 characters (older docs: 64):
#:                                FORM_HEX.
#: Left out on purpose: rakuten.com and rakutenadvertising.com (not the tracking-link host; Rakuten's
#: u1 is documented on linksynergy.com links), impact.com (corporate domain, not a link host),
#: prf.hn (Partnerize carries `pubref:` as a PATH segment, not a query parameter), and every network
#: not in AFFILIATE_HOST_SUFFIXES. Two networks are left out until their docs confirm BOTH the click
#: host and the parameter:
#:  * Involve Asia: the publisher help documents the click host, but names no sub-ID URL parameter;
#:    `aff_sub` appears only as a report/API field, and the help says sub-IDs are set in the link
#:    generator. Unconfirmed: the parameter on the link, and its character rule.
#:  * CJ: a `SID` query value appears only in one developer-portal script example (the page could not
#:    be read in full); the reports now name the field shopperId; no case or character rule is
#:    published; and CJ's tracking hosts are not in AFFILIATE_HOST_SUFFIXES. Unconfirmed: the
#:    parameter's spelling and rule, and the full host list.
AFFILIATE_SUBID_PARAMS: Mapping[str, SubidRule] = MappingProxyType({
    "linksynergy.com": SubidRule("u1", FORM_HEX),
    "awin1.com": SubidRule("clickref", FORM_CLK),
    "shareasale.com": SubidRule("afftrack", FORM_HEX),
    "skimresources.com": SubidRule("xcust", FORM_CLK),
    "go2cloud.org": SubidRule("aff_sub", FORM_CLK),
    "sjv.io": SubidRule("subId1", FORM_HEX),
    "pxf.io": SubidRule("subId1", FORM_HEX),
})

#: Query parameters AFFILIATE_SUBID_PARAMS_JSON may never name (compared lowercased): writing our
#: click id into one would replace the link's destination or the publisher / merchant / offer
#: identity that decides who is paid.
#:  * the destination: murl (Rakuten deep link), url (Skimlinks, TUNE, CJ), u (impact deep link),
#:    p and ued (Awin), urllink (ShareASale), and generic spellings (dest, destination, deeplink,
#:    dl, link, redirect, redirect_url, goto, target);
#:  * an identity: id (Rakuten and Skimlinks publisher), mid (Rakuten merchant), awinmid, awinaffid,
#:    b and m (ShareASale banner, merchant; its u is the affiliate), offerid, tmpid and type (Rakuten
#:    link), offer_id, aff_id and affid (TUNE), pid and aid (CJ publisher, ad).
SUBID_PARAM_DENYLIST = frozenset({
    "murl", "url", "u", "p", "ued", "urllink",
    "dest", "destination", "deeplink", "dl", "link", "redirect", "redirect_url", "goto", "target",
    "id", "mid", "awinmid", "awinaffid", "b", "m", "offerid", "tmpid", "type",
    "offer_id", "aff_id", "affid", "pid", "aid",
})

#: The longest sub-ID value we send. The tightest documented limit among the mapped networks is 50
#: (Awin's report display, Skimlinks' xcust); a network added through the env gets the same bound.
#: Our click ids are 28 characters (`clk_` + 24 hex) and 24 in FORM_HEX, well inside it.
SUBID_VALUE_MAX_LEN = 50

#: Our click id, and nothing else: exactly what new_click_id mints, `clk_` + 24 lowercase hex.
_CLICK_ID_RE = re.compile(r"clk_[0-9a-f]{24}")
#: A FORM_HEX value as a network report returns it (any case: a report may upper-case it).
_BARE_HEX_RE = re.compile(r"[0-9a-fA-F]{24}")
_HOST_SUFFIX_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_PARAM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")

#: The ctx keys materialize_attribution_context reads the click id from, in its order. The value
#: written to the sub-ID is the one it will record.
_CLICK_ID_KEYS = (PVT_CLICK_ID, "click_id", "clickId")

CTX_PARAM = "affiliate_subid_param"
CTX_VALUE = "affiliate_subid_value"
CTX_REPLACED = "affiliate_subid_replaced"
CTX_SKIPPED = "affiliate_subid_skipped"


def _override_rule(suffix: str, value: Any) -> Optional[SubidRule]:
    """One override entry as a rule, or None (logged) when it may not be used.

    A string is the parameter, an object is {"param": ..., "form": "hex" | "clk"}. With no form, the
    code map's form for that same suffix is kept, else FORM_HEX (letters and digits: the form every
    documented network accepts).
    """
    if isinstance(value, str):
        param, form = value, None
    elif isinstance(value, dict) and set(value) <= {"param", "form"}:
        param, form = value.get("param"), value.get("form")
    else:
        logger.warning("AFFILIATE_SUBID_PARAMS_JSON: ignoring bad entry for %s", suffix)
        return None
    if not isinstance(param, str) or not _PARAM_RE.fullmatch(param):
        logger.warning("AFFILIATE_SUBID_PARAMS_JSON: ignoring bad parameter for %s", suffix)
        return None
    if param.lower() in SUBID_PARAM_DENYLIST:
        logger.warning(
            "AFFILIATE_SUBID_PARAMS_JSON: refusing parameter %r for %s: it carries the destination or an id",
            param, suffix,
        )
        return None
    if form is None:
        code = AFFILIATE_SUBID_PARAMS.get(suffix)
        form = code.form if code else FORM_HEX
    if form not in _FORMS:
        logger.warning("AFFILIATE_SUBID_PARAMS_JSON: ignoring bad form for %s", suffix)
        return None
    return SubidRule(param, form)


@lru_cache(maxsize=8)
def subid_param_map(override_json: str = "") -> Mapping[str, SubidRule]:
    """The code map with AFFILIATE_SUBID_PARAMS_JSON merged over it.

    The override is a JSON object {"<host suffix>": "<param>" | {"param", "form"} | null}: a rule
    adds or replaces a network, null removes one. A non-object or unparseable override is ignored
    whole; an entry with a bad host suffix, parameter or form, or a parameter on
    SUBID_PARAM_DENYLIST, is ignored alone (the code map's entry, if any, stays). Cached per override
    string, so each refusal is logged once. Never raises.
    """
    merged: Dict[str, SubidRule] = dict(AFFILIATE_SUBID_PARAMS)
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
        rule = _override_rule(suffix, value)
        if rule is not None:
            merged[suffix] = rule
    return MappingProxyType(merged)


def _host_matches(host: str, suffix: str) -> bool:
    """On a label boundary: the host IS the suffix, or ends with "." + suffix."""
    return host == suffix or host.endswith("." + suffix)


def subid_rule_for_url(url: str, mapping: Mapping[str, SubidRule]) -> Optional[SubidRule]:
    """The sub-ID rule for `url`'s host, or None. The longest matching suffix wins."""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    best: Optional[Tuple[str, SubidRule]] = None
    for suffix, rule in mapping.items():
        if _host_matches(host, suffix) and (best is None or len(suffix) > len(best[0])):
            best = (suffix, rule)
    return best[1] if best else None


def subid_value(click_id: str, form: str) -> str:
    """What goes in the network's parameter: the click id, without `clk_` in FORM_HEX."""
    return click_id[len(CLICK_ID_PREFIX):] if form == FORM_HEX else click_id


def click_id_from_subid(value: Any) -> Optional[str]:
    """For the commission import: the click id a network report's sub-ID value carries, or None.

    The inverse of subid_value for either form: `clk_<24 hex>` is the click id itself, and 24 bare
    hex characters (FORM_HEX) get `clk_` back. Case and surrounding space are ignored (our ids are
    lowercase). Anything else, a partner's own label included, is not ours: None.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _BARE_HEX_RE.fullmatch(text):
        return CLICK_ID_PREFIX + text.lower()
    lowered = text.lower()
    return lowered if _CLICK_ID_RE.fullmatch(lowered) else None


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
    recorded click and the sub-ID are one id) goes into the network's parameter, in the network's
    form; the ctx records the value as sent (click_id_from_subid reads it back). A click id that is
    not ours in shape, or a rewrite that would change anything but the query, sends `dest` unchanged
    and records why (`affiliate_subid_skipped`).
    """
    rule = subid_rule_for_url(dest, subid_param_map(override_json))
    if rule is None:
        return SubidDecision(dest, {})
    source = ctx if isinstance(ctx, dict) else {}
    updates: Dict[str, Any] = {CTX_PARAM: rule.param}
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
    value = subid_value(click_id, rule.form)
    stamped, replaced = set_query_param(dest, rule.param, value)
    before, after = urlsplit(dest), urlsplit(stamped)
    if (before.scheme, before.netloc, before.path, before.fragment) != (
        after.scheme, after.netloc, after.path, after.fragment
    ):
        updates.pop(PVT_CLICK_ID, None)
        updates[CTX_SKIPPED] = "url_shape"
        return SubidDecision(dest, updates)
    updates[CTX_VALUE] = value
    updates[CTX_REPLACED] = replaced
    return SubidDecision(stamped, updates)
