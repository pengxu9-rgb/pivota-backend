"""B3 — the primary commerce destination of one grounded AI answer.

Three layers are covered here, because the signal only exists if all three
hold and each one has a different way of silently dying:

1. THE RULE (services/primary_destination.py) — which cited host, if any, is
   the place the answer sent the buyer.
2. THE CAPTURE (build_authority_map) — citation ORDER exists in exactly one
   loop in the pipeline; every structure downstream is host-keyed, so if the
   ordinal is not threaded out of that loop the rule has nothing to rank.
3. THE DEPOSIT (extract_citation_observations) — the two columns that reach
   citation_observations, and the invariant that AT MOST ONE row per response
   claims primary.

Every negative assertion below is paired with its positive counterpart in the
same test: "the editorial host did not win" is worthless on its own, because a
selector that returned None for everything would pass it.
"""

from __future__ import annotations

import pytest

from services.audit_evidence_builder import extract_citation_observations
from services.primary_destination import (
    COMMERCE_HOST_TYPES,
    NON_DESTINATION_HOST_TYPES,
    PRIMARY_DESTINATION_VERSION,
    DestinationCandidate,
    commerce_candidates,
    is_commerce_destination,
    select_primary_destination,
)


def _c(host, ordinal, host_type=None, first_party=False):
    return {
        "host": host,
        "ordinal": ordinal,
        "host_type": host_type,
        "first_party": first_party,
    }


# =====================================================================
# 1. The rule
# =====================================================================


def test_version_is_pinned_and_the_two_vocabularies_do_not_overlap():
    """The version is what a before/after diff consults (db/audit_basis.py), so
    it must exist and be an int. And a host type can never be both a
    destination and a source — an overlap would make admission depend on which
    set was consulted first."""
    assert isinstance(PRIMARY_DESTINATION_VERSION, int)
    assert PRIMARY_DESTINATION_VERSION >= 1
    assert COMMERCE_HOST_TYPES & NON_DESTINATION_HOST_TYPES == frozenset()
    assert COMMERCE_HOST_TYPES  # non-empty: an empty set admits nothing, ever


def test_lowest_ordinal_among_commerce_hosts_wins():
    """The ordering IS the signal. Deliberately asserted with the winner on
    BOTH sides of the alphabet: `ulta` beats `sephora` when ulta is cited
    first, and `sephora` beats `ulta` when sephora is. A selector that dropped
    the ordinal and fell through to the lexicographic tie-break would pass the
    second assertion and fail the first — which is the mutant this pairing
    exists to kill."""
    first_is_later_alphabetically = select_primary_destination([
        _c("ulta.com", 1, "retailer"),
        _c("sephora.com", 3, "retailer"),
    ])
    assert first_is_later_alphabetically is not None
    assert first_is_later_alphabetically.host == "ulta.com"
    assert first_is_later_alphabetically.ordinal == 1

    first_is_earlier_alphabetically = select_primary_destination([
        _c("sephora.com", 1, "retailer"),
        _c("ulta.com", 3, "retailer"),
    ])
    assert first_is_earlier_alphabetically is not None
    assert first_is_earlier_alphabetically.host == "sephora.com"


def test_an_editorial_host_cited_first_does_not_beat_a_retailer_cited_later():
    """The whole point of the admission step: a citation list is not a
    destination list. Forbes at position 0 is the SOURCE the model read; the
    shopper cannot buy there. Positive counterpart in the same assertion — the
    retailer at position 2 IS selected, so a selector that simply returned None
    cannot pass."""
    winner = select_primary_destination([
        _c("forbes.com", 0, "editorial"),
        _c("reddit.com", 1, "reddit"),
        _c("sephora.com", 2, "retailer"),
    ])
    assert winner is not None
    assert winner.host == "sephora.com"


def test_no_commerce_host_yields_no_primary_and_is_not_an_error():
    """The "AI answered and gave the shopper nowhere to buy" case. Paired with
    the positive: adding ONE retailer to the same list produces a primary, so
    this None is about the data and not about a broken selector."""
    sources_only = [
        _c("forbes.com", 0, "editorial"),
        _c("reddit.com", 1, "reddit"),
        _c("youtube.com", 2, "creator"),
        _c("someblog.example", 3, "unclassified"),
    ]
    assert select_primary_destination(sources_only) is None
    assert commerce_candidates(sources_only) == []

    with_store = sources_only + [_c("ulta.com", 4, "retailer")]
    winner = select_primary_destination(with_store)
    assert winner is not None and winner.host == "ulta.com"


def test_merchants_own_domain_is_a_destination_even_when_unclassified():
    """A small brand's own domain is almost never in the cited-host registry, so
    admission cannot depend on the registry alone. Negative counterpart: the
    SAME unclassified host with first_party False is not admitted."""
    assert is_commerce_destination("unclassified", first_party=True) is True
    assert is_commerce_destination("unclassified", first_party=False) is False

    winner = select_primary_destination([
        _c("forbes.com", 0, "editorial"),
        _c("merchant.test", 1, "unclassified", first_party=True),
    ])
    assert winner is not None and winner.host == "merchant.test"


def test_tie_on_ordinal_prefers_first_party_then_lexicographic_host():
    """Documented tie-break, asserted in both directions so a change to it is
    loud. Same ordinal: the merchant's own store wins over a retailer; with no
    first party in the tie, the lexicographically smaller host wins."""
    winner = select_primary_destination([
        _c("sephora.com", 2, "retailer"),
        _c("merchant.test", 2, "unclassified", first_party=True),
    ])
    assert winner is not None and winner.host == "merchant.test"

    winner2 = select_primary_destination([
        _c("ulta.com", 2, "retailer"),
        _c("sephora.com", 2, "retailer"),
    ])
    assert winner2 is not None and winner2.host == "sephora.com"


def test_selection_is_deterministic_across_input_order():
    """A rule whose answer depends on dict/list ordering is not a measurement.
    Positive: both orderings pick the same host, and it is the right one."""
    a = [_c("forbes.com", 0, "editorial"), _c("sephora.com", 1, "retailer"),
         _c("ulta.com", 2, "retailer")]
    b = list(reversed(a))
    assert select_primary_destination(a).host == "sephora.com"
    assert select_primary_destination(b).host == "sephora.com"


def test_dataclass_candidates_and_malformed_entries():
    """Accepts the frozen dataclass as well as mappings; drops entries with no
    host or no ordinal rather than ranking them at position 0. Positive
    counterpart: the well-formed candidate in the same list still wins."""
    winner = select_primary_destination([
        {"host": "", "ordinal": 0, "host_type": "retailer"},
        {"host": "noordinal.com", "ordinal": None, "host_type": "retailer"},
        {"host": "negative.com", "ordinal": -1, "host_type": "retailer"},
        DestinationCandidate(host="sephora.com", ordinal=5, host_type="retailer"),
    ])
    assert winner is not None and winner.host == "sephora.com"


def test_empty_input_has_no_primary():
    assert select_primary_destination([]) is None
    assert select_primary_destination(None) is None
    # Positive counterpart so this file cannot pass with a selector that always
    # returns None.
    assert select_primary_destination([_c("sephora.com", 0, "retailer")]) is not None


# =====================================================================
# 2. The capture — build_authority_map threads the ordinal out
# =====================================================================


def _run(query, sources, axis="category", provider_runs=True):
    return {
        "query": query,
        "parsed": {},
        "axis_metadata": {"axis": axis, "source": "auto_generated"},
        "grounding_sources": sources,
        "url_match": {},
    }


def _probe(provider, runs):
    return {"provider": provider, "model": "m", "raw_runs": runs}


def _authority(runs, **kwargs):
    from services.agent_center_bd_report_service import build_authority_map

    return build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1", "content_key": "ck_abc"}],
        {"sku-1": runs},
        **kwargs,
    )


def _obs_by_host(authority_map):
    out = {}
    for host in authority_map["skus"][0]["authority_hosts"]:
        out[host["host"]] = host["query_observations"]
    return out


def test_build_authority_map_records_citation_position_and_one_primary():
    """The ordinal survives the host-keyed fold, and exactly one host per
    response is primary. Positive AND negative in one shape: forbes is first in
    the answer (rank 0) and is NOT the destination; sephora is second (rank 1)
    and IS. amazon.com is a second commerce host placed LATER but
    alphabetically EARLIER, so a pipeline that lost the ordinal on the way
    through the fold would name amazon and fail here."""
    runs = [_probe("gemini", [_run("best vitamin c serum", [
        {"uri": "https://forbes.com/best-serums", "title": "Best serums"},
        {"uri": "https://sephora.com/p/serum", "title": "Sephora"},
        {"uri": "https://reddit.com/r/x/comments/1", "title": "reddit"},
        {"uri": "https://amazon.com/dp/1", "title": "Amazon"},
    ])])]
    obs = _obs_by_host(_authority(runs))

    assert obs["forbes.com"][0]["destination_rank"] == 0
    assert obs["forbes.com"][0]["is_primary_destination"] is False
    assert obs["sephora.com"][0]["destination_rank"] == 1
    assert obs["sephora.com"][0]["is_primary_destination"] is True
    assert obs["reddit.com"][0]["destination_rank"] == 2
    assert obs["reddit.com"][0]["is_primary_destination"] is False
    assert obs["amazon.com"][0]["destination_rank"] == 3
    assert obs["amazon.com"][0]["is_primary_destination"] is False

    primaries = [
        o for rows in obs.values() for o in rows if o["is_primary_destination"]
    ]
    assert len(primaries) == 1


def test_a_dropped_citation_does_not_renumber_the_ones_after_it():
    """The ordinal is the position in the ANSWER's citation list, not among the
    citations we managed to resolve. An unresolvable Vertex redirector (no
    title) is dropped from the rollup; the host after it must still report
    position 2. Positive counterpart: the resolvable host before it reports 0."""
    runs = [_probe("gemini", [_run("best serum", [
        {"uri": "https://forbes.com/best-serums", "title": "Best serums"},
        {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x",
         "title": ""},
        {"uri": "https://sephora.com/p/serum", "title": "Sephora"},
    ])])]
    obs = _obs_by_host(_authority(runs))
    assert "vertexaisearch.cloud.google.com" not in obs
    assert obs["forbes.com"][0]["destination_rank"] == 0
    assert obs["sephora.com"][0]["destination_rank"] == 2


def test_repeated_host_in_one_response_keeps_its_best_position():
    """The _observations fold is still ONE entry per (query, provider, host) —
    citation_observations has no ordinal in its identity, so a second entry
    would collide on idempotency_key and vanish. The kept position is the
    LOWEST: a host cited at 1 and again at 3 was reached at 1."""
    runs = [_probe("gemini", [_run("best serum", [
        {"uri": "https://forbes.com/a", "title": "a"},
        {"uri": "https://sephora.com/p/serum", "title": "Sephora"},
        {"uri": "https://forbes.com/b", "title": "b"},
        {"uri": "https://sephora.com/p/other", "title": "Sephora"},
    ])])]
    obs = _obs_by_host(_authority(runs))
    assert len(obs["sephora.com"]) == 1
    assert obs["sephora.com"][0]["destination_rank"] == 1
    assert len(obs["forbes.com"]) == 1
    assert obs["forbes.com"][0]["destination_rank"] == 0


def test_each_response_gets_its_own_primary_independently():
    """Two providers answering two different questions are two responses, so two
    primaries — one each — not one for the pair. Negative counterpart: neither
    editorial host is primary in its own response."""
    runs = [
        _probe("gemini", [_run("best serum", [
            {"uri": "https://forbes.com/x", "title": "f"},
            {"uri": "https://sephora.com/p", "title": "s"},
        ])]),
        _probe("chatgpt", [_run("where to buy serum", [
            {"uri": "https://ulta.com/p", "title": "u"},
            {"uri": "https://allure.com/x", "title": "a"},
        ], axis="intent")]),
    ]
    obs = _obs_by_host(_authority(runs))
    flat = [(h, o) for h, rows in obs.items() for o in rows]
    primary = {o["provider"]: h for h, o in flat if o["is_primary_destination"]}
    assert primary == {"gemini": "sephora.com", "chatgpt": "ulta.com"}
    assert not any(
        o["is_primary_destination"] for h, o in flat
        if h in ("forbes.com", "allure.com")
    )


def test_a_response_citing_only_sources_has_no_primary_at_all():
    """Zero primaries is a real outcome. Paired with the positive case above by
    asserting the observations still exist and are ranked — the response was
    measured, it simply had no destination."""
    runs = [_probe("gemini", [_run("is retinol safe", [
        {"uri": "https://forbes.com/x", "title": "f"},
        {"uri": "https://reddit.com/r/y/comments/2", "title": "r"},
    ])])]
    obs = _obs_by_host(_authority(runs))
    flat = [o for rows in obs.values() for o in rows]
    assert len(flat) == 2
    assert [o["destination_rank"] for o in sorted(flat, key=lambda x: x["destination_rank"])] == [0, 1]
    assert not any(o["is_primary_destination"] for o in flat)


# =====================================================================
# 3. The deposit
# =====================================================================


def _report(hosts):
    return {"authority_map": {"skus": [{
        "product_key": "p1",
        "content_key": "ck_abc",
        "authority_hosts": hosts,
    }]}}


def _host(host, obs, host_type="retailer"):
    return {"host": host, "host_type": host_type, "evidence_urls": [],
            "query_observations": obs}


def test_extract_carries_rank_and_primary_through_to_the_deposit_rows():
    rows = extract_citation_observations(_report([
        _host("forbes.com", [{"query": "q", "provider": "gemini",
                              "destination_rank": 0,
                              "is_primary_destination": False}], "editorial"),
        _host("sephora.com", [{"query": "q", "provider": "gemini",
                               "destination_rank": 1,
                               "is_primary_destination": True}]),
    ]))
    by_host = {r["cited_host"]: r for r in rows}
    assert by_host["sephora.com"]["destination_rank"] == 1
    assert by_host["sephora.com"]["is_primary_destination"] is True
    assert by_host["forbes.com"]["destination_rank"] == 0
    assert by_host["forbes.com"]["is_primary_destination"] is False


def test_a_second_primary_claim_on_one_response_is_demoted():
    """Enforced, not assumed. build_authority_map cannot produce this, but this
    function also runs over authority maps loaded from stored report_jsonb.
    Positive counterpart: the FIRST claim survives, so this is a demotion and
    not a blanket clear."""
    rows = extract_citation_observations(_report([
        _host("sephora.com", [{"query": "q", "provider": "gemini",
                               "destination_rank": 1,
                               "is_primary_destination": True}]),
        _host("ulta.com", [{"query": "q", "provider": "gemini",
                            "destination_rank": 2,
                            "is_primary_destination": True}]),
    ]))
    primaries = [r["cited_host"] for r in rows if r["is_primary_destination"]]
    assert primaries == ["sephora.com"]
    assert len(rows) == 2


def test_two_different_responses_may_each_have_a_primary():
    """The demotion above must be scoped to ONE response. Without this
    counterpart, a global "only one primary ever" bug would pass."""
    rows = extract_citation_observations(_report([
        _host("sephora.com", [
            {"query": "q1", "provider": "gemini", "destination_rank": 0,
             "is_primary_destination": True},
            {"query": "q2", "provider": "chatgpt", "destination_rank": 0,
             "is_primary_destination": True},
        ]),
    ]))
    assert sorted(r["query"] for r in rows if r["is_primary_destination"]) == ["q1", "q2"]


def test_a_pre_b3_observation_deposits_a_null_rank_not_zero():
    """A stored report written before B3 carries no position. NULL says "we do
    not know"; 0 would claim "the answer's first citation". Positive
    counterpart in the same call: a B3-shaped observation still records its 0."""
    rows = extract_citation_observations(_report([
        _host("sephora.com", [{"query": "old", "provider": "gemini"}]),
        _host("ulta.com", [{"query": "new", "provider": "gemini",
                            "destination_rank": 0,
                            "is_primary_destination": True}]),
    ]))
    by_host = {r["cited_host"]: r for r in rows}
    assert by_host["sephora.com"]["destination_rank"] is None
    assert by_host["sephora.com"]["is_primary_destination"] is False
    assert by_host["ulta.com"]["destination_rank"] == 0
    assert by_host["ulta.com"]["is_primary_destination"] is True


@pytest.mark.parametrize("bad", ["3", 2.9, True, -1, None, object()])
def test_a_non_integer_rank_is_stored_as_null(bad):
    """A rank must be a real non-negative int or absent. `True` is called out
    because bool is an int subclass and would otherwise deposit rank 1."""
    rows = extract_citation_observations(_report([
        _host("sephora.com", [{"query": "q", "provider": "gemini",
                               "destination_rank": bad}]),
    ]))
    assert rows[0]["destination_rank"] is None


# ---------------------------------------------------------------------------
# The DELIVERY PATH. Review found three mutants alive here: forcing
# is_primary_destination=False or destination_rank=None at the persist boundary
# (services/audit_evidence_builder.py:1246-1247), or forcing False inside the DB
# writer, all shipped with every test green. The whole point of B3 is those two
# values reaching the row, and nothing asserted that they do.
# ---------------------------------------------------------------------------
import db.audit_evidence as ae  # noqa: E402


async def _one_observation(monkeypatch, rank, primary):
    """Capture the bound parameters of the real insert.

    The writer builds a SQLAlchemy Insert (`citation_observations.insert()
    .values(...)`), so the values ride on the STATEMENT, not on a second
    argument to `database.execute` — reading them off the statement is what
    makes this test exercise the delivering line rather than a stub's shape.
    """
    captured = {}

    async def _fake_execute(stmt, values=None):
        try:
            captured.update(stmt.compile().params)
        except Exception:  # pragma: no cover - a shape change should fail loud
            captured["__unparsed__"] = repr(stmt)
        return None

    async def _noop():
        return None

    monkeypatch.setattr(ae.database, "execute", _fake_execute)
    monkeypatch.setattr(ae, "ensure_audit_evidence_tables", _noop)
    await ae.insert_citation_observation(
        audit_run_id="run-1", merchant_id="m1", content_key="ck1",
        product_key="pk1", provider="gemini", query="best cream blush",
        axis="category", query_class="category_discovery",
        cited_host="ulta.com", host_type="retailer", citation_role=None,
        first_party=False, is_competitor=False,
        evidence_url="https://ulta.com/p", content_key_basis="resolved",
        idempotency_key="idem-1",
        destination_rank=rank, is_primary_destination=primary,
    )
    assert "__unparsed__" not in captured, captured.get("__unparsed__")
    return captured


async def test_the_rank_and_primary_flag_reach_the_insert(monkeypatch):
    """M3/M4/M5: assert the values actually arrive as bound parameters."""
    values = await _one_observation(monkeypatch, rank=0, primary=True)
    assert values.get("destination_rank") == 0
    assert values.get("is_primary_destination") is True


async def test_a_non_primary_observation_persists_its_rank_and_false(monkeypatch):
    """Positive counterpart: the writer is not hardcoded the other way either."""
    values = await _one_observation(monkeypatch, rank=3, primary=False)
    assert values.get("destination_rank") == 3
    assert values.get("is_primary_destination") is False


async def test_an_absent_rank_persists_as_null_not_zero(monkeypatch):
    """Rank 0 is the BEST position; None means 'no ordinal known'. Collapsing
    the two would make an unranked row look like the top destination."""
    values = await _one_observation(monkeypatch, rank=None, primary=False)
    assert values.get("destination_rank") is None


# --- the extract -> insert BOUNDARY (audit_evidence_builder.py:1246-1247) ---
# The two tests above pin the DB writer. They do NOT pin the hand-off into it:
# mutants forcing `is_primary_destination=False` / `destination_rank=None` at
# the call site survived both. This drives the real deposit loop and asserts the
# kwargs it actually passes.
async def test_the_deposit_loop_forwards_rank_and_primary(monkeypatch):
    import services.audit_evidence_builder as eb

    seen = []

    async def _capture(**kwargs):
        seen.append(kwargs)
        return "obs-id"

    def _one_obs(brand_report, content_key_map=None):
        return [{
            "content_key": "ck1", "product_key": "pk1", "provider": "gemini",
            "query": "best cream blush", "axis": "category",
            "query_class": "category_discovery", "cited_host": "ulta.com",
            "host_type": "retailer", "citation_role": None,
            "first_party": False, "is_competitor": False,
            "evidence_url": "https://ulta.com/p", "content_key_basis": "resolved",
            "destination_rank": 0, "is_primary_destination": True,
        }]

    # The helper imports it locally from db.audit_evidence, so patch it there.
    monkeypatch.setattr(ae, "insert_citation_observation", _capture)
    monkeypatch.setattr(eb, "extract_citation_observations", _one_obs)
    await eb._deposit_citation_observations(
        brand_report={}, content_key_map=None,
        audit_run_id="run-1", merchant_id="m1",
        summary={"citation_observations_inserted": 0,
                 "citation_observations_skipped": 0},
    )
    assert len(seen) == 1, seen
    assert seen[0]["destination_rank"] == 0
    assert seen[0]["is_primary_destination"] is True


async def test_the_deposit_loop_forwards_a_non_primary_unchanged(monkeypatch):
    """Positive counterpart: the hand-off is not hardcoded the other way."""
    import services.audit_evidence_builder as eb

    seen = []

    async def _capture(**kwargs):
        seen.append(kwargs)
        return "obs-id"

    def _one_obs(brand_report, content_key_map=None):
        return [{
            "content_key": "ck1", "provider": "gemini", "query": "q",
            "cited_host": "editorial.example", "content_key_basis": "resolved",
            "destination_rank": 4, "is_primary_destination": False,
        }]

    # The helper imports it locally from db.audit_evidence, so patch it there.
    monkeypatch.setattr(ae, "insert_citation_observation", _capture)
    monkeypatch.setattr(eb, "extract_citation_observations", _one_obs)
    await eb._deposit_citation_observations(
        brand_report={}, content_key_map=None,
        audit_run_id="run-1", merchant_id="m1",
        summary={"citation_observations_inserted": 0,
                 "citation_observations_skipped": 0},
    )
    assert seen[0]["destination_rank"] == 4
    assert seen[0]["is_primary_destination"] is False
