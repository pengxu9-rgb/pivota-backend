"""Fix 3 — merchant-grade narrative for the per-SKU AI-readiness report.

Turns the structured per-SKU audit (scores, the Fix 1 resolved cited hosts, and
the Fix 2 findability-vs-endorsement split) into the decision-grade story a
merchant can act on. The seven sections mirror the target report drafts:

  1. one-sentence honest story (branded vs category)
  2. what's working (branded/navigational + distribution + a real excerpt)
  3. where you're losing (independent category recommendation) + who AI cites
  4. per-SKU scorecard (plain-language status)
  5. DeepSeek verify summary in plain language (reach vs accuracy)
  6. prioritized actions mapped to merchant-growth phases
  7. honest limits (provider coverage pending, data gaps named)

Cross-cutting guardrails: **no fabrication** (never invent competitors/hosts —
state the limit) and **no inflation** (own-listing surfacing is never reported
as endorsement). Every field here is derived from data already computed upstream;
this module adds no probes and reads no DB.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services.cited_host_classifier import (
    ROLE_COMPETITOR,
    classify_host,
    is_findability_role,
)
from services.competitor_brand_filter import is_ingredient_or_category_type
from services.vertical_profiles import BEAUTY_PROFILE, VerticalProfile

# Probe axes that name the SKU/brand (branded/navigational) vs the non-branded
# category/discovery axis. Kept local so this module has no import cycle with
# agent_center_bd_report_service (which imports it).
_CATEGORY_DISCOVERY_AXES = frozenset({"category"})

# Primary-gap -> merchant-growth phase. The two phases are the spec's
# "create & distribute" (publish identity / earn independent citations) and
# "evidence intake" (substantiate content / make it transactable).
_GROWTH_PHASE_BY_GAP = {
    "identity": "create_and_distribute",
    "citation": "create_and_distribute",
    "content": "evidence_intake",
    "content_richness": "evidence_intake",
    "routability": "evidence_intake",
}
_GROWTH_PHASE_LABEL = {
    "create_and_distribute": "Create & distribute",
    "evidence_intake": "Evidence intake",
}

_EXCERPT_CAP = 320


def _excerpt(text: Optional[str]) -> Optional[str]:
    s = str(text or "").strip()
    if not s:
        return None
    return (s[: _EXCERPT_CAP - 1] + "…") if len(s) > _EXCERPT_CAP else s


def _axis_of(run: Dict[str, Any]) -> str:
    meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
    return str(meta.get("axis") or "").strip().lower()


def _is_branded_axis(axis: str) -> bool:
    return bool(axis) and axis not in _CATEGORY_DISCOVERY_AXES


def _summary(authority_map: Dict[str, Any]) -> Dict[str, Any]:
    s = authority_map.get("host_attribution_summary") if isinstance(authority_map, dict) else None
    return s if isinstance(s, dict) else {}


def _first_branded_excerpt(per_sku_reports: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A real verbatim grounded excerpt from a branded/navigational query — the
    honest 'what's working' proof. Returns None when none exists (no fabrication)."""
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        for ev in report.get("verbatim_grounding_evidence") or []:
            if not isinstance(ev, dict):
                continue
            excerpt = _excerpt(ev.get("evidence_excerpt"))
            sources = ev.get("grounding_sources") or []
            if not excerpt or not sources:
                continue
            if not _is_branded_axis(_axis_of(ev)):
                continue
            # Only use the excerpt as "what's working" proof when the SKU was
            # actually found in that answer — never frame a "couldn't find it"
            # excerpt as success (no inflation). Older runs that predate this
            # signal degrade to None rather than guessing.
            if not ev.get("product_visible"):
                continue
            source_labels = [
                (s.get("title") or s.get("uri") or "").strip()
                for s in sources
                if isinstance(s, dict) and (s.get("title") or s.get("uri"))
            ]
            return {
                "sku_title": report.get("sku_title"),
                "query": ev.get("query"),
                "excerpt": excerpt,
                "source_labels": source_labels[:4],
            }
    return None


def _who_ai_cites_instead(
    authority_map: Dict[str, Any],
    *,
    vertical_profile: VerticalProfile = BEAUTY_PROFILE,
) -> Dict[str, Any]:
    """The real hosts + competitors AI cites that are NOT the merchant. Strictly
    from grounded data — if nothing independent surfaced, says so explicitly."""
    hosts = authority_map.get("hosts") if isinstance(authority_map, dict) else None
    hosts = hosts if isinstance(hosts, list) else []
    cited_hosts: List[Dict[str, Any]] = []
    for row in hosts:
        if not isinstance(row, dict):
            continue
        # Exclude the merchant's OWN findability — own site (first_party) and the
        # merchant's own product listed on a marketplace (marketplace_self_listing).
        # Those are the merchant's distribution; surfacing them under "who AI cites
        # instead" would read own-listing indexing as a competitor citation (the
        # no-inflation guardrail).
        if row.get("first_party") or is_findability_role(row.get("citation_role")):
            continue
        # A competitor's own storefront the engine cited is competitive intel, not
        # an outreach target — you can't "get cited on" a rival's store. Drop it
        # here so it never becomes a "get cited on" outreach move; it still shows
        # up as a named competitor below and under store_as_destination.
        if row.get("is_competitor") or row.get("citation_role") == ROLE_COMPETITOR:
            continue
        host = row.get("host")
        if host:
            cited_hosts.append({
                "host": host,
                "citation_role": row.get("citation_role"),
                "recommendation_class": row.get("recommendation_class"),
                "prompts_cited_count": row.get("prompts_cited_count") or 0,
                "cited_on_category_query": bool(row.get("cited_on_category_query")),
            })

    # Competitor brand names live on the per-SKU authority hosts (the brand-level
    # rollup doesn't carry them). Aggregate from there — real grounded names only.
    # `times_named` is the honest count: distinct SKUs whose grounded answers named
    # the competitor — NOT the per-cited-host tally (the host rollup fans
    # `competitors_named` onto every host in a run, which would multiply it by the
    # number of cited hosts). `_prominence` (that same host-fanned frequency) is
    # kept only as an ordering proxy so the most-cited competitors rank first
    # (otherwise single-SKU runs tie at 1 and sort alphabetically, burying the
    # relevant names). It is not surfaced.
    competitor_skus: Dict[str, set] = {}
    competitor_prominence: Dict[str, int] = {}
    # Per-host grounded competitor names (same brand-only filter). The brand-level
    # host rollup doesn't carry competitors_named, so this is what lets the
    # outreach layer say "this host grounded an answer that named a rival" as a
    # fact about THAT host — recommendation_class alone can't (it's a host-TYPE
    # property, silent on who got recommended).
    host_competitors: Dict[str, List[str]] = {}
    for sku in (authority_map.get("skus") if isinstance(authority_map, dict) else None) or []:
        sku_key = sku.get("sku_key") if isinstance(sku, dict) else None
        for row in (sku.get("authority_hosts") if isinstance(sku, dict) else None) or []:
            row_host = str(row.get("host") or "").strip().lower() if isinstance(row, dict) else ""
            for comp in (row.get("competitors_named") if isinstance(row, dict) else None) or []:
                name = str(comp or "").strip()
                # For CATEGORY questions ("best collagen", "best magnesium") the
                # grounded answer names ingredient/supplement TYPES, which the
                # extractor captures verbatim as competitors. Drop those generic
                # types — only real competitor brands belong here. No fabrication:
                # this only ever removes a name, never invents one, and the list
                # degrades to empty (the caller emits an honest note) if nothing
                # brand-like remains.
                if name and not is_ingredient_or_category_type(
                    name,
                    ingredient_tokens=vertical_profile.competitor_ingredient_tokens,
                    form_tokens=vertical_profile.competitor_form_tokens,
                ):
                    competitor_skus.setdefault(name, set()).add(sku_key)
                    competitor_prominence[name] = competitor_prominence.get(name, 0) + 1
                    if row_host:
                        bucket = host_competitors.setdefault(row_host, [])
                        if name not in bucket:
                            bucket.append(name)
    for h in cited_hosts:
        h["competitors_named"] = host_competitors.get(
            str(h["host"]).strip().lower(), []
        )[:8]
    # Rank BEFORE the [:8] cap, classification-aware. The old
    # (-prompts_cited_count, host) key broke 1-citation ties alphabetically, so
    # a recommends-class editorial cited on the category query (the host the
    # win plan says to win) could be truncated out while unclassified 1-cite
    # blogs survived — making this panel contradict the win-plan panel. Count
    # still dominates; ties break by endorsement weight: recommends-class,
    # then category-query citation, then any known citation role, then a
    # grounded rival on the host, then host. The competitors_named term sits
    # LAST before the alphabetical fallback so it only splits otherwise-true
    # ties: among equal-frequency, equal-classification peers, the host that
    # grounded a named rival is the more actionable "you're losing this
    # endorsement" move, so it should survive the [:8] cap ahead of a peer
    # that named none. It never overrides frequency or the higher-signal terms.
    def _cited_host_rank(h: Dict[str, Any]) -> Tuple:
        recommends = str(h.get("recommendation_class") or "").strip().lower() == "recommends"
        classified = str(h.get("citation_role") or "").strip().lower() not in {"", "unclassified"}
        return (
            -(h["prompts_cited_count"] or 0),
            0 if recommends else 1,
            0 if h.get("cited_on_category_query") else 1,
            0 if classified else 1,
            0 if h.get("competitors_named") else 1,
            h["host"],
        )
    cited_hosts.sort(key=_cited_host_rank)
    competitors = [
        {"name": name, "times_named": len(competitor_skus[name])}
        for name in sorted(
            competitor_skus,
            key=lambda n: (-competitor_prominence.get(n, 0), n),
        )
    ]
    available = bool(cited_hosts or competitors)
    note = None
    if not available:
        note = (
            "Competitor/host landscape not available: this run's grounded "
            "sources named no third-party hosts or competitors for these queries."
        )
    elif not competitors:
        note = (
            "AI cites the hosts above for these queries; no competitor brand was "
            "named in the grounded answers."
        )
    return {
        "available": available,
        "cited_hosts": cited_hosts[:8],
        "competitors": competitors[:8],
        "note": note,
    }


def _headline_story(
    merchant_name: str,
    summary: Dict[str, Any],
    per_sku_reports: Optional[List[Dict[str, Any]]] = None,
) -> str:
    name = merchant_name or "This brand"
    findable = bool(summary.get("findability_hosts"))
    endorsed_category = bool(summary.get("independently_recommended_for_category"))
    endorsed = bool(summary.get("has_independent_endorsement"))
    cited_via_hosts = bool(summary.get("cited_via_hosts"))
    if not findable and not endorsed:
        if cited_via_hosts:
            # Cited via retailers/marketplaces on branded searches, but no
            # recognized own-site and no category endorsement. NOT invisible —
            # the citation data shows reach; the gap is own-site + category.
            return (
                f"AI does cite {name} — but via retailers and marketplaces on "
                "branded searches, not through your own site, and not yet for the "
                "category questions new shoppers ask. That's distribution, not "
                "endorsement."
            )
        return (
            f"{name} is invisible to AI shopping agents today — neither your own "
            "listings nor any independent source surface when shoppers ask."
        )
    if endorsed_category:
        headline = (
            f"{name} is independently recommended for the category, not just "
            "found through your own listings — the channel is working for you."
        )
        # BRIDGE (operator-review fix): "channel working" beside a scorecard
        # where every audited SKU is blocked read as the report disagreeing
        # with itself (live Mojawa run 7420c2b5). When the brand earns the
        # category recommendation but ALL audited SKUs sit in the blocked
        # band, say both truths in one sentence.
        reports = [r for r in (per_sku_reports or []) if isinstance(r, dict)]
        bands = [str(r.get("band") or "").strip().lower() for r in reports]
        if bands and all(band == "blocked" for band in bands):
            headline += (
                " The audited products themselves aren't agent-ready yet, "
                "though — the brand's recognition isn't attached to these "
                "exact SKUs; the actions below close that gap."
            )
        return headline
    if endorsed:
        return (
            f"Independent sources cite {name}, but not yet on the category "
            "questions new shoppers ask — recommendation is branded, not category-wide."
        )
    return (
        f"Shoppers who already know {name} can find you — your listings are "
        "indexed — but AI does not yet recommend you to new shoppers asking the "
        "category question."
    )


def _whats_working(
    merchant_name: str,
    summary: Dict[str, Any],
    per_sku_reports: List[Dict[str, Any]],
) -> Dict[str, Any]:
    findability_hosts = [str(h) for h in (summary.get("findability_hosts") or []) if h]
    branded = 0
    category = 0
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        cov = report.get("query_class_coverage")
        cov = cov if isinstance(cov, dict) else {}
        branded += int(cov.get("branded_navigational") or 0)
        category += int(cov.get("category_discovery") or 0)
    if findability_hosts:
        listed = ", ".join(findability_hosts[:5])
        text = (
            f"{merchant_name or 'The brand'} is findable where shoppers who "
            f"already know it look: listed across {listed}."
        )
    else:
        text = (
            "No own-site or marketplace listings surfaced in grounded answers "
            "yet — there is no findability base to build on."
        )
    return {
        "summary": text,
        "findability_hosts": findability_hosts,
        "branded_navigational_probes": branded,
        "category_discovery_probes": category,
        "evidence_excerpt": _first_branded_excerpt(per_sku_reports),
    }


def _win_plan_summary(win_plan: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Brand-level rollup of the per-SKU win-plan (Fix 4), surfaced inside
    "where you're losing" so the narrative names the *path to winning* the
    category recommendation, not just the loss. None when there is no plan
    (no losing category queries) — never fabricates a path."""
    if not isinstance(win_plan, dict) or not win_plan.get("available"):
        return None
    roll = win_plan.get("rollup") if isinstance(win_plan.get("rollup"), dict) else {}
    losing = int(roll.get("losing_category_queries") or 0)
    if not losing:
        return None
    hosts = [h for h in (roll.get("independent_hosts_to_win") or []) if h]
    # "Ready-to-send" means a one-click pitch actually rendered (email recipient
    # + matching template), not merely an emailable host — the honest count.
    pitch_ready = [h for h in (roll.get("pitch_ready_hosts") or []) if h]
    q_word = "query" if losing == 1 else "queries"
    if hosts:
        h_word = "host" if len(hosts) == 1 else "hosts"
        it = "it" if losing == 1 else "them"
        tail = (
            f" — {len(pitch_ready)} with a ready-to-send pitch."
            if pitch_ready
            else " — none with a ready-to-send pitch yet (target named, recipient pending)."
        )
        text = (
            f"You're losing {losing} category {q_word}; AI grounds {it} in "
            f"{len(hosts)} independent {h_word} you're not cited in" + tail
        )
    else:
        text = (
            f"You're losing {losing} category {q_word}, but the grounded answers "
            "named no independent publisher to target — the play is to earn a "
            "first independent category citation."
        )
    return {
        "summary": text,
        "losing_category_queries": losing,
        "independent_hosts_to_win": hosts,
        "pitch_ready_hosts": pitch_ready,
    }


# Off-platform outreach: route a cited host (one that cites a competitor, not
# you) to the right action verb + playbook lever by its classified type. These
# are the "do this even without a connected store" moves — pitch the editorial
# sites AI grounds in, get carried by the retailers it trusts, engage the
# communities it cites, partner with creators.
_OUTREACH_BY_TYPE: Dict[str, Tuple[str, str]] = {
    "editorial": ("Pitch", "editorial_outreach"),
    "review_aggregator": ("Get reviewed by", "editorial_outreach"),
    "retailer": ("Get carried by", "wholesale_onboarding"),
    "marketplace": ("List on", "marketplace_listing"),
    "community": ("Engage", "research"),
    "forum": ("Engage", "research"),
    "reddit": ("Engage", "research"),
    "social": ("Partner with creators on", "creator_partnership"),
    "video": ("Partner with creators on", "creator_partnership"),
}
_FIRST_MOVE_BY_LEVER: Dict[str, str] = {
    "editorial_outreach": (
        "Send a concise product pitch (with samples or proof points) to their "
        "editorial/tips contact — these sites shape what AI recommends."
    ),
    "wholesale_onboarding": (
        "Apply to be carried, and make sure your listing's title and specs "
        "match your own product page exactly."
    ),
    "marketplace_listing": (
        "Create or claim your listing and align the title, images, and specs "
        "with your own product page."
    ),
    "research": (
        "Join the threads where buyers ask about this category and answer with "
        "honest, useful detail — don't spam."
    ),
    "creator_partnership": (
        "Reach out to creators in your niche for an honest review or collab."
    ),
}

# Major mainstream publishers: AI grounds in them, but they rarely cover an
# emerging / medium-tail brand on a cold pitch — so "Pitch Vogue" is not a real
# move for most merchants. We keep the citation insight but reframe the action
# to the reachable path (reviews + community first) and flag realism so the UI
# can prioritise doable moves.
_MAJOR_PUBLISHER_HOSTS = frozenset({
    "vogue.com", "forbes.com", "marieclaire.com", "allure.com", "elle.com",
    "cosmopolitan.com", "goodhousekeeping.com", "harpersbazaar.com", "glamour.com",
    "instyle.com", "refinery29.com", "womenshealthmag.com", "nytimes.com", "wsj.com",
    # Consumer Reports buys every tested product anonymously at retail and
    # accepts no pitches or samples — the crowd-review first_move its
    # review_site subtype would otherwise get is dishonest for CR.
    "consumerreports.org",
})
_REVIEW_BUILD_FIRST_MOVE = (
    "Earn authentic reviews and keep your product listing accurate in their "
    "crowd-reviewed database — that's how AI (and shoppers) pick you up here, "
    "not a paid pitch."
)
_MAJOR_PUBLISHER_FIRST_MOVE = (
    "Major publishers rarely cover an emerging brand on a cold pitch. Build "
    "review-site listings and authentic community/Reddit presence first — "
    "editorial coverage follows the signals they pick up."
)
_ALREADY_ENDORSES_FIRST_MOVE = (
    "Keep the coverage that earned the endorsement accurate and current, then "
    "pitch the specific category queries this host grounds where you're not "
    "yet the recommendation — extending won coverage beats cold outreach."
)


def _move_realism(host: str, htype: str, subtype: str) -> str:
    """How realistically a merchant can act on this outreach move now —
    so 'engage Reddit' / 'earn reviews' outrank 'pitch Vogue' for a long-tail
    brand. reachable = earn it directly; diy = self-serve (community/reviews);
    onboarding = list/get carried; hard = needs scale/relationships first."""
    if htype in {"retailer", "marketplace"}:
        return "onboarding"
    if htype in {"community", "forum", "reddit", "social", "video"}:
        return "diy"
    if htype == "editorial":
        # Major publisher check FIRST — a big name classified as review_site
        # (e.g. forbes.com) is still hard to land for an emerging brand.
        if subtype == "magazine" or host in _MAJOR_PUBLISHER_HOSTS:
            return "hard"
        if subtype in {"review_aggregator", "review_site", "community"}:
            return "reachable"
        return "reachable"
    return "investigate"


def _outreach_moves(
    who: Dict[str, Any],
    endorsement_hosts: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Turn the 'who AI cites instead' hosts into concrete off-platform moves —
    no connected store required. Each host is classified (editorial / retailer /
    marketplace / community / creator) and routed to the right verb + playbook
    lever; hosts that aren't outreach-actionable (cdn, bare competitor domains,
    unclassified) are skipped. Ranked: grounds-a-rival-recommendation first,
    then category-query citations, then citation frequency. Hosts that already
    independently endorse the merchant (endorsement_hosts) are never framed as
    recommending a rival — their move is to extend the won coverage."""
    endorsed = {
        str(e or "").strip().lower()
        for e in (endorsement_hosts or [])
        if str(e or "").strip()
    }
    moves: List[Dict[str, Any]] = []
    for h in (who.get("cited_hosts") if isinstance(who, dict) else None) or []:
        host = str((h or {}).get("host") or "").strip()
        if not host:
            continue
        cls = classify_host(host) or {}
        htype = str(cls.get("type") or "").strip().lower()
        subtype = str(cls.get("subtype") or "").strip().lower()
        routed = _OUTREACH_BY_TYPE.get(htype) or _OUTREACH_BY_TYPE.get(subtype)
        if not routed:
            # cdn = page assets (not a destination); brand = a competitor's own
            # domain (you don't pitch a rival's site). Skip both. Anything else
            # the registry doesn't recognize is still a real source AI trusts —
            # surface it honestly as one to investigate rather than drop it.
            if htype in {"cdn", "brand"}:
                continue
            routed = ("Get cited on", "research")
            cls = {**cls, "outreach_hint": (
                "AI grounds answers in this source — look at what it covers and "
                "how to get your product featured, listed, or reviewed there."
            )}
        verb, lever = routed
        realism = _move_realism(host, htype, subtype)
        # recommendation_class is a HOST-TYPE property (editorial/video sources
        # recommend rather than list) — it says the host's citations carry
        # endorsement weight, NOT that a rival was endorsed. We additionally
        # require a competitor named in this host's grounded answers (and that
        # the host isn't one that already independently recommends THIS
        # merchant) before flagging it. NOTE: competitors_named is fanned onto
        # every co-cited host of a run upstream (build_authority_map appends the
        # run's competitors to every grounding source's row), so it proves this
        # host was co-cited in a run whose answer named a rival — NOT that this
        # host's own content named it (that per-(host, competitor) provenance
        # isn't retained). The rendered copy is therefore co-citation framing —
        # the host "grounds answers that recommend competitors" — and never
        # asserts this specific host personally "recommends a competitor over you".
        recommends_class = str(h.get("recommendation_class") or "").strip().lower() == "recommends"
        already_endorses = host.lower() in endorsed
        recommends_rival = (
            recommends_class
            and bool(h.get("competitors_named"))
            and not already_endorses
        )
        cited_n = int(h.get("prompts_cited_count") or 0)
        category = bool(h.get("cited_on_category_query"))
        if already_endorses:
            why = (
                f"{host} already recommends you independently — the move isn't "
                "to win it over but to extend that coverage to the category "
                "queries it grounds."
            )
        else:
            why = f"AI cites {host}"
            if recommends_rival:
                why += " and it grounds answers that recommend competitors over you"
            elif cited_n:
                why += f" in {cited_n} of your tested prompts"
            if category:
                why += " on category questions"
            why += " — getting in front of it is how you get cited there too."
        moves.append({
            "host": host,
            "host_type": htype or "unclassified",
            "host_subtype": cls.get("subtype"),
            "action_verb": "Build on" if already_endorses else verb,
            "lever": lever,
            "recommendation_class": h.get("recommendation_class"),
            "prompts_cited_count": cited_n,
            "cited_on_category_query": category,
            "already_endorses_you": already_endorses,
            "headline": f"{'Build on' if already_endorses else verb} {host}",
            "why": why,
            # Reframe the action to what's actually doable: extend-the-win for
            # hosts that already endorse you, review-building for review
            # aggregators, the indirect path for major publishers (don't tell a
            # long-tail brand to "pitch Vogue").
            "first_move": (
                _ALREADY_ENDORSES_FIRST_MOVE if already_endorses
                else _MAJOR_PUBLISHER_FIRST_MOVE if realism == "hard"
                else _REVIEW_BUILD_FIRST_MOVE if subtype in {"review_aggregator", "review_site"}
                else cls.get("outreach_hint") or _FIRST_MOVE_BY_LEVER.get(lever)
            ),
            "realism": realism,
            "pitch_recipient": cls.get("pitch_recipient"),
            # A host that grounds a RIVAL's recommendation is the sharpest move
            # (you're losing the endorsement, not just absent) — weight it well
            # above raw citation frequency. But de-prioritise "hard" (major-
            # publisher) moves a long-tail brand can't realistically land, so
            # reachable/DIY moves (reviews, Reddit/community) surface first.
            # The hard penalty is about COLD-pitch odds — extending coverage a
            # host already gave you isn't a cold pitch, so endorsed hosts keep
            # their standing even at major publishers.
            "_priority": (5 if recommends_rival else 0) + (1 if category else 0) + cited_n
            - (4 if realism == "hard" and not already_endorses else 0),
        })
    moves.sort(key=lambda m: -m["_priority"])
    for m in moves:
        m.pop("_priority", None)
    return moves[:6]


def _vertical_pitch_targets(
    vertical_profile: VerticalProfile,
    who: Dict[str, Any],
    endorsement_hosts: List[str],
) -> List[Dict[str, Any]]:
    """Phase-4 T5: the profile's STANDING authority-host pitch list for this
    vertical, annotated with what this audit actually observed. _outreach_moves
    only surfaces hosts the engines happened to cite in this run's sample — so
    an electronics partner would see youtube + one editorial and never learn
    that Rtings/SoundGuys/What Hi-Fi are the category's pitch targets. This
    list is the vertical knowledge itself (empty for profiles without
    authority_hosts, e.g. beauty today), status-stamped so the operator knows
    which targets already ground answers in their category.

    Copy mirrors _outreach_moves' editorial routing (review-build vs major-
    publisher realism) so the panel reads as one voice."""
    hosts = getattr(vertical_profile, "authority_hosts", ()) or ()
    if not hosts:
        return []
    endorsed = {str(h or "").strip().lower() for h in (endorsement_hosts or [])}
    cited = {
        str((h or {}).get("host") or "").strip().lower()
        for h in ((who.get("cited_hosts") if isinstance(who, dict) else None) or [])
    }
    targets: List[Dict[str, Any]] = []
    for raw in hosts:
        host = str(raw or "").strip().lower()
        if not host:
            continue
        cls = classify_host(host) or {}
        subtype = str(cls.get("subtype") or "").strip().lower()
        realism = _move_realism(host, "editorial", subtype)
        if host in endorsed:
            status = "already_endorses_you"
            why = f"{host} already recommends you — keep the listing accurate and build on it."
        elif host in cited:
            status = "cited_in_your_category"
            why = (
                f"AI grounded answers in {host} during this audit — it shapes "
                "your category's recommendations today."
            )
        else:
            status = "not_yet_observed"
            why = (
                f"{host} is a standing authority for this category — AI engines "
                "ground category answers in it even when this audit's sample "
                "didn't surface it."
            )
        targets.append({
            "host": host,
            "status": status,
            "why": why,
            "realism": realism,
            "tier": cls.get("tier"),
            "ai_grounding_weight": cls.get("ai_grounding_weight"),
            "expected_outreach_cycle_weeks": cls.get("expected_outreach_cycle_weeks"),
            "pitch_recipient": cls.get("pitch_recipient"),
            "first_move": (
                _MAJOR_PUBLISHER_FIRST_MOVE if realism == "hard"
                else _REVIEW_BUILD_FIRST_MOVE if subtype in {"review_aggregator", "review_site"}
                else cls.get("outreach_hint") or _FIRST_MOVE_BY_LEVER.get("editorial_outreach")
            ),
        })
    # already-cited targets first (proven to shape this category's answers),
    # then reachable before hard; profile order breaks ties.
    order = {"already_endorses_you": 0, "cited_in_your_category": 1, "not_yet_observed": 2}
    targets.sort(key=lambda t: (order.get(t["status"], 3), t["realism"] == "hard"))
    return targets


def _where_youre_losing(
    merchant_name: str,
    authority_map: Dict[str, Any],
    summary: Dict[str, Any],
    win_plan: Optional[Dict[str, Any]] = None,
    vertical_profile: VerticalProfile = BEAUTY_PROFILE,
) -> Dict[str, Any]:
    endorsed_category = bool(summary.get("independently_recommended_for_category"))
    endorsement_hosts = list(summary.get("endorsement_category_hosts") or summary.get("endorsement_hosts") or [])
    findable = bool(summary.get("findability_hosts"))
    if endorsed_category:
        text = (
            f"{merchant_name or 'The brand'} earns independent category "
            f"recommendation from {', '.join(endorsement_hosts[:4])}."
        )
    elif findable:
        text = (
            "When shoppers ask the category question, no independent source "
            f"recommends {merchant_name or 'the brand'} — only your own/retail "
            "listings appear, which is distribution, not endorsement."
        )
    elif summary.get("cited_via_hosts"):
        # Cited via retailers/marketplaces (no recognized own-site, no category
        # endorsement). Don't claim "doesn't surface at all" — the citation data
        # contradicts it; the gap is category recommendation, not presence.
        text = (
            f"{merchant_name or 'The brand'} surfaces via retailers and "
            "marketplaces on branded searches, but no independent source "
            "recommends it for the category — that's distribution, not endorsement."
        )
    else:
        # Genuinely nothing surfaced (invisible): don't claim "only your own
        # listings appear" when even those didn't — that would contradict the
        # invisible headline.
        text = (
            "When shoppers ask the category question, "
            f"{merchant_name or 'the brand'} doesn't surface at all — neither "
            "your own listings nor any independent source appears."
        )
    who = _who_ai_cites_instead(authority_map, vertical_profile=vertical_profile)
    return {
        "summary": text,
        "independently_recommended_for_category": endorsed_category,
        "endorsement_hosts": endorsement_hosts,
        "who_ai_cites_instead": who,
        # Off-platform outreach moves derived from those cited hosts — pitch the
        # editorial sites / get carried by the retailers / engage the
        # communities AI grounds in. No connected store required.
        # endorsement_hosts keeps a host that already independently recommends
        # the merchant from being framed as "recommends a competitor over you".
        "outreach_moves": _outreach_moves(who, endorsement_hosts=endorsement_hosts),
        # Phase-4 T5 — the vertical's standing pitch-target list (profile
        # authority_hosts), status-stamped against this audit. Empty list for
        # verticals without a curated list (beauty today) — the panel hides it.
        "pitch_targets": _vertical_pitch_targets(
            vertical_profile, who, endorsement_hosts
        ),
        # Fix 4 — the path to winning the category recommendation back, rolled
        # up from the per-SKU win-plan. None when there's no plan to show.
        "win_plan_summary": _win_plan_summary(win_plan),
    }


def _per_sku_scorecard(
    per_sku_reports: List[Dict[str, Any]],
    authority_map: Dict[str, Any],
) -> List[Dict[str, Any]]:
    signals_by_sku: Dict[str, Dict[str, Any]] = {}
    for sku in (authority_map.get("skus") if isinstance(authority_map, dict) else None) or []:
        if isinstance(sku, dict) and sku.get("sku_key"):
            signals_by_sku[sku["sku_key"]] = sku.get("citation_signals") or {}
    out: List[Dict[str, Any]] = []
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        band_display = report.get("band_display")
        band_display = band_display if isinstance(band_display, dict) else {}
        primary_gaps = report.get("primary_gaps")
        primary_gaps = primary_gaps if isinstance(primary_gaps, list) else []
        top_gap = primary_gaps[0] if primary_gaps else {}
        sig = signals_by_sku.get(report.get("sku_key"), {})
        out.append({
            "sku_key": report.get("sku_key"),
            "sku_title": report.get("sku_title"),
            "band": report.get("band"),
            "status": band_display.get("label") or report.get("band"),
            "what_it_means": (
                band_display.get("meaning")
                or (top_gap.get("meaning") if isinstance(top_gap, dict) else None)
                or (top_gap.get("dimension") if isinstance(top_gap, dict) else None)
            ),
            "surfaced_only_via_own_listing": bool(sig.get("surfaced_only_via_own_listing")),
            "independently_recommended_for_category": bool(
                sig.get("independently_recommended_for_category")
            ),
        })
    return out


# Human-readable explanation for each answer-quality verify skip reason, so the
# merchant report says WHY verify didn't run rather than a bare reason code (or
# nothing at all). Distinguishes config reasons (no verifier configured/available
# — on us to fix) from data reasons (no answer cited you — expected until
# citations land). Keep keys in sync with `_verify_skipped_summary` reasons.
_VERIFY_SKIP_PHRASE = {
    "no_verify_providers_resolved": "no answer-quality verifier was configured for this run",
    "deepseek_not_resolved_for_verify": "the configured verifier wasn't DeepSeek",
    "missing_deepseek_api_key": "the DeepSeek verifier wasn't available",
    "no_citation_positive_probes": "no AI answer cited you yet, so there was nothing to fact-check",
    "verify_sample_cap_zero": "the verify sample resolved to zero citations",
    "no_skus_audited": "no SKUs were audited",
}


def _verify_skip_phrase(reason: Optional[str]) -> str:
    key = (reason or "").strip()
    if key in _VERIFY_SKIP_PHRASE:
        return _VERIFY_SKIP_PHRASE[key]
    return f"reason: {key}" if key else "reason unavailable"


def _verify_plain(verify_summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    vs = verify_summary if isinstance(verify_summary, dict) else {}
    status = vs.get("status") or "not_run"
    verified = int(vs.get("verified") or 0)
    flagged = int(vs.get("flagged") or 0)
    candidates = int(vs.get("citation_positive_candidates") or 0)
    # `verified` is the number of citations actually checked; `flagged` is a
    # SUBSET of those (a checked citation DeepSeek flagged for accuracy). Do not
    # add them — that double-counts and can report "checked" > candidates.
    checked = verified
    # Reason(s) the verify pass skipped. Per-SKU summaries carry `reason`
    # (singular); the brand ROLLUP aggregates them into `reasons` (plural list).
    # Read both — else the brand-level narrative shows a blank "reason unavailable"
    # while the real cause sits in `reasons`.
    reason_codes = (
        [str(vs["reason"])] if vs.get("reason")
        else [str(r) for r in (vs.get("reasons") or []) if r]
    )
    reason_phrase = "; ".join(
        dict.fromkeys(_verify_skip_phrase(c) for c in reason_codes)
    ) or _verify_skip_phrase(None)
    if status in {"completed", "partial"} and checked:
        held = max(0, verified - flagged)
        accuracy = (
            "every checked citation held up"
            if flagged == 0
            else f"{flagged} flagged for accuracy"
        )
        text = (
            f"DeepSeek answer-quality verify checked {checked} of {candidates} "
            f"cited answers: {held} held up, {accuracy} — reach is real, "
            "accuracy is the watch item."
        )
    elif status in {"completed", "partial"}:
        text = (
            "DeepSeek verify ran but had no positive citations to second-guess "
            "yet — nothing to confirm on accuracy."
        )
    else:
        text = (
            "Answer-quality verify did not run for this audit — "
            f"{reason_phrase}; accuracy is unconfirmed."
        )
    # The actual flagged citations: which query AI answered and WHY DeepSeek
    # flagged it (misstates facts / doesn't support recommending you, + its note).
    # Surfaced so "N flagged" is actionable, not just a count. Merchant-safe subset
    # of verify_summary.flagged_probes (already aggregated + capped upstream).
    flagged_examples = [
        {
            "query": q,
            "misstates_facts": bool(p.get("misstates_facts")),
            "unsupported_recommendation": p.get("supports_recommendation") is False,
            "note": str(p.get("note") or "").strip()[:300],
        }
        for p in (vs.get("flagged_probes") or [])
        if isinstance(p, dict) and (q := str(p.get("query") or "").strip())
    ][:8]
    return {
        "status": status,
        "reason": reason_codes[0] if reason_codes else None,
        "reasons": reason_codes,
        "reason_phrase": reason_phrase,
        "verified": verified,
        "flagged": flagged,
        "checked": checked,
        "candidates": candidates,
        "flagged_examples": flagged_examples,
        "text": text,
    }


def _prioritized_actions(per_sku_reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    actions: List[Dict[str, Any]] = []
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        nba = report.get("next_best_action")
        nba = nba if isinstance(nba, dict) else {}
        headline = (nba.get("headline") or "").strip()
        if not headline:
            continue
        gap = (nba.get("primary_gap") or "").strip().lower()
        key = (gap, headline.lower())
        if key in seen:
            continue
        seen.add(key)
        phase = _GROWTH_PHASE_BY_GAP.get(gap, "create_and_distribute")
        actions.append({
            "sku_title": report.get("sku_title"),
            "primary_gap": gap or None,
            "headline": headline,
            "first_move": nba.get("first_move"),
            "why_this_first": nba.get("why_this_first"),
            "growth_phase": phase,
            "growth_phase_label": _GROWTH_PHASE_LABEL.get(phase, phase),
        })
    # Create & distribute first (earning citations unblocks the rest), then
    # evidence intake; stable within phase by SKU order.
    phase_order = {"create_and_distribute": 0, "evidence_intake": 1}
    actions.sort(key=lambda a: phase_order.get(a["growth_phase"], 9))
    return actions[:5]


def _honest_limits(
    summary: Dict[str, Any],
    who_cites: Dict[str, Any],
    verify_plain: Dict[str, Any],
    providers: Optional[List[str]],
    verify_providers: Optional[List[str]],
    pending_engine_support: Optional[List[str]],
    coverage_profile: Optional[str],
) -> List[str]:
    limits: List[str] = []
    provs = ", ".join(providers or []) or "none"
    vers = ", ".join(verify_providers or []) or "none"
    cov = f" (coverage profile: {coverage_profile})" if coverage_profile else ""
    limits.append(
        f"Provider coverage: grounded on {provs}; answer-quality verify on "
        f"{vers}{cov}."
    )
    pending = [p for p in (pending_engine_support or []) if p]
    if pending:
        limits.append(
            "Coverage pending: " + ", ".join(pending) +
            " not yet included — citation/host counts will rise as they are added."
        )
    if not who_cites.get("available"):
        limits.append(
            "Competitor/host landscape not available from this run's grounded "
            "sources — not shown rather than guessed."
        )
    elif not who_cites.get("competitors"):
        limits.append(
            "No competitor brand was named in the grounded answers; only cited "
            "hosts are shown."
        )
    # Answer-quality verify coverage + scope. When it RAN, the report shows
    # "checked X of Y cited answers" elsewhere, but never disclosed that (a) only
    # a SAMPLE was checked and (b) the scope is citation-positive ("branded")
    # answers only — verify never touches the discovery/category queries where
    # the merchant isn't cited yet. Disclosing both keeps the headline honest
    # (a strong-citation / low-accuracy state can't hide behind a partial,
    # branded-only check). When it did NOT run, say so (unchanged).
    verify_status = verify_plain.get("status")
    if verify_status in {"completed", "partial"}:
        checked = int(verify_plain.get("checked") or 0)
        candidates = int(verify_plain.get("candidates") or 0)
        if checked > 0:
            limits.append(
                "Answer-quality verify covers a sample of citation-positive "
                f"answers only: it fact-checked {checked} of {candidates} answers "
                "where an AI already cited you, and does not check the "
                "discovery/category queries where you aren't cited yet — accuracy "
                "outside that checked sample is unmeasured."
            )
        else:
            limits.append(
                "Answer-quality verify ran but found no cited answers to "
                "fact-check — accuracy is unmeasured this run."
            )
    else:
        limits.append(
            "Answer-quality (DeepSeek) verification did not run for this audit — "
            f"{verify_plain.get('reason_phrase') or _verify_skip_phrase(None)}."
        )
    return limits


def build_merchant_narrative(
    *,
    merchant_name: Optional[str],
    per_sku_reports: List[Dict[str, Any]],
    brand_rollup: Optional[Dict[str, Any]] = None,
    authority_map: Optional[Dict[str, Any]] = None,
    verify_summary: Optional[Dict[str, Any]] = None,
    providers: Optional[List[str]] = None,
    verify_providers: Optional[List[str]] = None,
    pending_engine_support: Optional[List[str]] = None,
    coverage_profile: Optional[str] = None,
    win_plan: Optional[Dict[str, Any]] = None,
    vertical_profile: VerticalProfile = BEAUTY_PROFILE,
) -> Dict[str, Any]:
    """Assemble the seven merchant-facing narrative sections from already-built
    report data. Pure + defensive: every section degrades to an honest "not
    available" rather than fabricating competitors, hosts, or endorsement."""
    name = (merchant_name or "").strip()
    authority_map = authority_map if isinstance(authority_map, dict) else {}
    brand_rollup = brand_rollup if isinstance(brand_rollup, dict) else {}
    summary = _summary(authority_map)
    # Whether the brand is cited ANYWHERE (any product scored a citation or was
    # cited by any model). `findable` only counts FIRST-PARTY (own-site) hosts,
    # so a brand cited heavily via retailers/marketplaces on branded searches —
    # but with no recognized own-site — would wrongly read as "invisible /
    # doesn't surface at all". This flag lets the headline + where-losing copy
    # say "cited via retailers, not own-site/category" instead of claiming an
    # invisibility the citation data contradicts.
    summary["cited_via_hosts"] = any(
        (((r or {}).get("scores") or {}).get("citation") or {}).get("score")
        or ((r or {}).get("models_cited") or {}).get("cited")
        for r in (per_sku_reports or []) if isinstance(r, dict)
    )

    where = _where_youre_losing(
        name, authority_map, summary, win_plan, vertical_profile=vertical_profile
    )
    verify_plain = _verify_plain(verify_summary)
    honest_limits = _honest_limits(
        summary,
        where["who_ai_cites_instead"],
        verify_plain,
        providers,
        verify_providers,
        pending_engine_support,
        coverage_profile,
    )
    # Issue #902 — when audited SKUs sit on freshly-minted Pivota canonical PDPs
    # still inside Google's indexing window, surface that caveat here so a
    # merchant reads zero category citations as indexing latency, not a content
    # gap. Read from the brand indexing-arc rollup; absent -> nothing added.
    arc = brand_rollup.get("indexing_arc") if isinstance(brand_rollup, dict) else None
    arc_caveat = arc.get("caveat") if isinstance(arc, dict) else None
    if arc_caveat:
        honest_limits = list(honest_limits) + [arc_caveat]
    return {
        "headline_story": _headline_story(name, summary, per_sku_reports),
        "whats_working": _whats_working(name, summary, per_sku_reports),
        "where_youre_losing": where,
        "per_sku_scorecard": _per_sku_scorecard(per_sku_reports, authority_map),
        "verify_summary_plain": verify_plain,
        "prioritized_actions": _prioritized_actions(per_sku_reports),
        "honest_limits": honest_limits,
        "verdict_label": brand_rollup.get("brand_verdict_label"),
        "verdict_explanation": brand_rollup.get("brand_verdict_explanation"),
    }
