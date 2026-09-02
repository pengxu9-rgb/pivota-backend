"""B1 — the verified, liveness-checked official-domain set.

Every assertion here is a rule that, if it flips, changes a headline number in
the BD report. `merchant_owned_domains` feeds `build_authority_map(
merchant_extra_hosts=...)`, which decides `first_party` on every cited host —
i.e. whether AI sent a buyer to the merchant's own store. Measured on 840
grounded AI-shopping responses (2026-09-01), the inferred-only set was wrong in
both directions:

  * UNDERSTATED — anua.com and anua.us are byte-identical storefronts; only
    anua.com was inferred, so 7 citations of anua.us scored as retailer traffic
    and the branded official share read 46% instead of 67%.
  * OVERSTATED — us.judydoll.com was scored official and has NO DNS RECORD.

So the two directions are tested as a pair, and so is the rule that makes the
liveness half safe: `unverifiable` and `unchecked` NEVER drop a domain, because
213 of 286 brand hosts in the sibling module's audit answered every request with
a Cloudflare challenge and a checker that folded "cannot verify" into "gone"
would have emptied the set on its first run.

No network and no hand-written fixture DDL: the table is built through the
accessor's own `ensure_*` backstop (the same statements migration 207 applies),
so the CHECK constraint under test is production's, not one invented here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db.merchant_official_domains as mod
import services.brand_claim_service as svc
import services.official_domain_liveness as live
from db.database import database

MERCHANT = "merch_b1"
OTHER = "merch_b1_other"


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    mod.reset_ddl_ready_for_tests()
    await mod.ensure_merchant_official_domains_table()
    for merchant in (MERCHANT, OTHER):
        await database.execute(
            "DELETE FROM merchant_official_domains WHERE merchant_id = :m",
            {"m": merchant},
        )
    yield


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this file may resolve DNS or open a socket."""

    def _explode(host):  # pragma: no cover — a failure here is the point
        raise AssertionError(f"real DNS resolution attempted for {host!r}")

    monkeypatch.setattr(live, "_default_dns_resolver", _explode)


def _inferred(monkeypatch, *hosts):
    """Pin the inferred tier. Patches the extracted inference function, so the
    onboarding/catalog queries never run."""

    async def _hosts(merchant_id):
        return set(hosts)

    monkeypatch.setattr(svc, "_inferred_merchant_hosts", _hosts)


def _now():
    return datetime.now(timezone.utc)


# =====================================================================
# The set: what is in it, and what is not
# =====================================================================


async def test_dead_inferred_domain_is_excluded_and_a_live_one_is_kept(monkeypatch):
    """The us.judydoll.com case, with its positive counterpart in the same
    assertion: a dead inferred host drops, a live inferred host stays. Without
    the second half, a `merchant_owned_domains` that returned the empty set
    would pass."""
    _inferred(monkeypatch, "us.judydoll.com", "judydoll.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="us.judydoll.com",
        source=mod.SOURCE_INFERRED,
        liveness_status=mod.LIVENESS_DEAD,
    )
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="judydoll.com",
        source=mod.SOURCE_INFERRED,
        liveness_status=mod.LIVENESS_LIVE,
    )

    hosts = await svc.merchant_owned_domains(MERCHANT)
    assert "us.judydoll.com" not in hosts
    assert "judydoll.com" in hosts


async def test_unverifiable_and_unchecked_domains_are_never_dropped(monkeypatch):
    """THE RULE. A Cloudflare challenge is not evidence a storefront is gone,
    and a domain nobody has probed yet is not evidence of anything at all."""
    _inferred(monkeypatch, "cosrx.com", "beautyofjoseon.com", "anua.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="cosrx.com",
        source=mod.SOURCE_INFERRED,
        liveness_status=mod.LIVENESS_UNVERIFIABLE,
    )
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="beautyofjoseon.com",
        source=mod.SOURCE_INFERRED,
        liveness_status=mod.LIVENESS_UNCHECKED,
    )
    # anua.com has no stored row at all — the third way to be unchecked.

    hosts = await svc.merchant_owned_domains(MERCHANT)
    assert hosts == {"cosrx.com", "beautyofjoseon.com", "anua.com"}


async def test_asserted_domain_absent_from_inference_is_included(monkeypatch):
    """The anua.us case. Inference only ever found anua.com; the merchant
    asserted anua.us, and the set must carry both."""
    _inferred(monkeypatch, "anua.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.us", source=mod.SOURCE_ASSERTED
    )

    hosts = await svc.merchant_owned_domains(MERCHANT)
    assert hosts == {"anua.com", "anua.us"}


async def test_verified_domain_absent_from_inference_is_included(monkeypatch):
    _inferred(monkeypatch, "anua.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="anua.us",
        source=mod.SOURCE_VERIFIED,
        verification_status=mod.VERIFICATION_VERIFIED,
    )
    assert "anua.us" in await svc.merchant_owned_domains(MERCHANT)


async def test_dead_asserted_domain_is_excluded(monkeypatch):
    """Asserting a domain does not exempt it from the liveness rule — a brand
    that lists a domain it let lapse must not resurrect it by assertion."""
    _inferred(monkeypatch, "anua.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="judydoll.shop",
        source=mod.SOURCE_ASSERTED,
        liveness_status=mod.LIVENESS_DEAD,
    )
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="anua.us",
        source=mod.SOURCE_ASSERTED,
        liveness_status=mod.LIVENESS_UNVERIFIABLE,
    )

    hosts = await svc.merchant_owned_domains(MERCHANT)
    assert "judydoll.shop" not in hosts
    assert "anua.us" in hosts  # positive counterpart: only `dead` excludes


async def test_a_stored_row_never_leaks_across_merchants(monkeypatch):
    _inferred(monkeypatch)
    await mod.upsert_official_domain(
        merchant_id=OTHER, domain="anua.us", source=mod.SOURCE_ASSERTED
    )
    assert await svc.merchant_owned_domains(MERCHANT) == set()
    assert await svc.merchant_owned_domains(OTHER) == {"anua.us"}


async def test_stale_inferred_row_does_not_outlive_the_inference(monkeypatch):
    """Inference is the live truth for its own tier. A row left behind by a
    catalog that has moved on must not keep granting official status."""
    _inferred(monkeypatch, "anua.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="oldshop.example.com",
        source=mod.SOURCE_INFERRED,
        liveness_status=mod.LIVENESS_LIVE,
    )
    hosts = await svc.merchant_owned_domains(MERCHANT)
    assert "oldshop.example.com" not in hosts
    assert "anua.com" in hosts


# =====================================================================
# Detail: "verified live" must be distinguishable from "inferred unchecked"
# =====================================================================


async def test_detailed_reports_source_and_liveness_per_domain(monkeypatch):
    _inferred(monkeypatch, "anua.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="anua.us",
        source=mod.SOURCE_VERIFIED,
        verification_status=mod.VERIFICATION_VERIFIED,
        liveness_status=mod.LIVENESS_LIVE,
    )

    detail = await svc.merchant_owned_domains_detailed(MERCHANT)
    assert detail["anua.us"]["source"] == mod.SOURCE_VERIFIED
    assert detail["anua.us"]["liveness_status"] == mod.LIVENESS_LIVE
    assert detail["anua.us"]["verification_status"] == mod.VERIFICATION_VERIFIED
    # The inferred host has no row at all, and reports the honest default.
    assert detail["anua.com"]["source"] == mod.SOURCE_INFERRED
    assert detail["anua.com"]["liveness_status"] == mod.LIVENESS_UNCHECKED
    assert detail["anua.com"]["verification_status"] is None


async def test_merchant_owned_domains_is_the_key_set_of_detailed(monkeypatch):
    """The two must never disagree — a caller that switches to the detailed
    form must get the same membership."""
    _inferred(monkeypatch, "anua.com", "dead.example.com")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="dead.example.com",
        source=mod.SOURCE_INFERRED,
        liveness_status=mod.LIVENESS_DEAD,
    )
    detail = await svc.merchant_owned_domains_detailed(MERCHANT)
    assert await svc.merchant_owned_domains(MERCHANT) == set(detail)
    assert set(detail) == {"anua.com"}


# =====================================================================
# Subdomain awareness (the us.judydoll.com <-> judydoll.com bind)
# =====================================================================


async def test_cited_subdomain_matches_the_official_apex(monkeypatch):
    _inferred(monkeypatch)
    await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="judydoll.com", source=mod.SOURCE_ASSERTED
    )
    known = await svc.merchant_owned_domains(MERCHANT)
    assert svc.host_matches_known("us.judydoll.com", known) is True
    assert svc.host_matches_known("https://us.judydoll.com/products/x", known) is True
    assert svc.host_matches_known("www.judydoll.com", known) is True
    # positive counterpart's negative: a look-alike is NOT the same org
    assert svc.host_matches_known("judydoll.com.evil.test", known) is False
    assert svc.host_matches_known("notjudydoll.com", known) is False


def test_host_matches_known_strips_www_and_subdomains_without_the_db():
    """Pure, so it holds regardless of what is stored."""
    assert svc.host_matches_known("us.judydoll.com", ["judydoll.com"]) is True
    assert svc.host_matches_known("www.anua.us", ["anua.us"]) is True
    assert svc.host_matches_known("anua.us", ["www.anua.us"]) is True
    assert svc.host_matches_known("anua.us", ["anua.com"]) is False


# =====================================================================
# Normalization is a DATABASE constraint, not a convention
# =====================================================================


def test_normalize_host_strips_scheme_path_port_and_www():
    assert svc.normalize_host("https://WWW.Anua.US:443/collections/all") == "anua.us"
    assert svc.normalize_host("anua.us/") == "anua.us"
    assert svc.normalize_host("www.anua.us") == "anua.us"
    assert svc.normalize_host("") == ""


@pytest.mark.parametrize(
    "bad",
    [
        "WWW.anua.us",        # not lower-cased
        "www.anua.us",        # www not stripped
        "https://anua.us",    # scheme
        "anua.us/collections",  # path
        "anua.us:443",        # port
        "anua.us.",           # trailing dot
        " anua.us",           # whitespace
        "localhost",          # no dot / not registrable
    ],
)
async def test_check_constraint_rejects_a_non_normalized_domain(bad):
    """Asserted against the DATABASE, not against our own normalizer: a caller
    that skips normalization must fail loudly rather than plant a host nothing
    downstream will ever match."""
    with pytest.raises(Exception) as err:
        await database.execute(
            mod.UPSERT_OFFICIAL_DOMAIN_SQL,
            {
                "merchant_id": MERCHANT,
                "domain": bad,
                "source": mod.SOURCE_ASSERTED,
                "verification_status": None,
                "liveness_status": mod.LIVENESS_UNCHECKED,
                "last_checked_at": None,
                "is_primary": False,
                "now": _now(),
            },
        )
    assert "ck_merchant_official_domains_domain" in str(err.value)


async def test_the_same_insert_succeeds_once_normalized():
    """The positive counterpart: the constraint is not simply rejecting
    everything."""
    ok = await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.us", source=mod.SOURCE_ASSERTED
    )
    assert ok is True
    assert [r["domain"] for r in await mod.list_official_domains(MERCHANT)] == ["anua.us"]


async def test_upsert_refuses_an_unknown_source_or_liveness():
    assert await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.us", source="guessed"
    ) is False
    assert await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.us",
        source=mod.SOURCE_ASSERTED, liveness_status="probably",
    ) is False
    assert await mod.list_official_domains(MERCHANT) == []


async def test_upsert_is_idempotent_on_merchant_and_domain():
    await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.us", source=mod.SOURCE_INFERRED
    )
    await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.us", source=mod.SOURCE_ASSERTED,
        is_primary=True,
    )
    rows = await mod.list_official_domains(MERCHANT)
    assert len(rows) == 1
    assert rows[0]["source"] == mod.SOURCE_ASSERTED
    assert bool(rows[0]["is_primary"]) is True


async def test_recording_liveness_does_not_rewrite_provenance():
    """An observation knows nothing about who asserted the domain."""
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="anua.us",
        source=mod.SOURCE_VERIFIED,
        verification_status=mod.VERIFICATION_VERIFIED,
    )
    await mod.record_liveness(
        merchant_id=MERCHANT, domain="anua.us",
        liveness_status=mod.LIVENESS_UNVERIFIABLE,
    )
    row = (await mod.list_official_domains(MERCHANT))[0]
    assert row["source"] == mod.SOURCE_VERIFIED
    assert row["verification_status"] == mod.VERIFICATION_VERIFIED
    assert row["liveness_status"] == mod.LIVENESS_UNVERIFIABLE
    assert row["last_checked_at"] is not None


# =====================================================================
# The liveness classifier
# =====================================================================


def test_no_dns_record_is_dead_and_a_resolving_host_is_not():
    dead = live.classify_host_liveness(dns_resolved=False)
    assert dead.status == live.DEAD
    assert dead.note == "no_dns_record"
    assert dead.excludes is True

    alive = live.classify_host_liveness(dns_resolved=True, status_code=200)
    assert alive.status == live.LIVE
    assert alive.excludes is False


def test_undetermined_dns_never_kills_a_domain():
    """A SERVFAIL, a timeout, or a missing resolver library is not an answer of
    'no'. With no HTTP answer either, the verdict is unverifiable."""
    obs = live.classify_host_liveness(dns_resolved=None)
    assert obs.status == live.UNVERIFIABLE
    assert obs.excludes is False


def test_hard_404_on_the_apex_is_dead():
    for code in (404, 410):
        obs = live.classify_host_liveness(dns_resolved=True, status_code=code)
        assert obs.status == live.DEAD, code


def test_cloudflare_challenge_is_unverifiable_not_dead():
    """THE RULE, at the branch that would have broken it: a challenge arrives as
    HTTP 429 with `cf-mitigated: challenge`. 213 of 286 brand hosts answered
    that way in the sibling module's audit."""
    obs = live.classify_host_liveness(
        dns_resolved=True, status_code=429, bot_challenged=True
    )
    assert obs.status == live.UNVERIFIABLE
    assert obs.note == "bot_challenge"
    assert obs.excludes is False


@pytest.mark.parametrize("code", [401, 403, 429, 500, 502, 503])
def test_a_refusing_or_failing_origin_is_unverifiable(code):
    obs = live.classify_host_liveness(dns_resolved=True, status_code=code)
    assert obs.status == live.UNVERIFIABLE


@pytest.mark.parametrize("code", [200, 204, 301, 302])
def test_a_reachable_apex_is_live(code):
    assert live.classify_host_liveness(dns_resolved=True, status_code=code).status == live.LIVE


def test_transport_error_and_missing_status_are_unverifiable():
    tls = live.classify_host_liveness(dns_resolved=True, transport_error="SSLError")
    assert tls.status == live.UNVERIFIABLE
    assert tls.note == "SSLError"
    assert live.classify_host_liveness(
        dns_resolved=True, status_code=None
    ).status == live.UNVERIFIABLE


# =====================================================================
# The probe: DNS first, one apex GET, nothing else
# =====================================================================


class _CannedClient:
    """One canned response, and a record of every URL asked for."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.urls = []

    async def get(self, url, headers=None):  # noqa: ANN001
        self.urls.append(url)
        if self._raises is not None:
            raise self._raises
        return self._response


class _Resp:
    def __init__(self, status_code, headers=None, url=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url or "https://anua.us/"


@pytest.fixture
def _no_pacing(monkeypatch):
    async def _allow(url, *, user_agent, max_wait=None):
        return None

    monkeypatch.setattr(live.crawl_politeness, "before_request", _allow)
    monkeypatch.setattr(live.crawl_politeness, "note_response", lambda *a, **k: None)


async def test_probe_skips_http_entirely_when_dns_says_no(_no_pacing):
    client = _CannedClient(_Resp(200))
    obs = await live.probe_host_liveness(
        "judydoll.shop", client=client, resolver=lambda h: False
    )
    assert obs.status == live.DEAD
    assert client.urls == []  # no request made — DNS is the cheap half


async def test_probe_fetches_only_the_apex_over_https(_no_pacing):
    client = _CannedClient(_Resp(200))
    obs = await live.probe_host_liveness(
        "https://WWW.Anua.US/collections/all", client=client, resolver=lambda h: True
    )
    assert obs.status == live.LIVE
    assert client.urls == ["https://anua.us/"]


async def test_probe_reads_the_cf_mitigated_header(_no_pacing):
    client = _CannedClient(_Resp(429, {"cf-mitigated": "challenge"}))
    obs = await live.probe_host_liveness(
        "cosrx.com", client=client, resolver=lambda h: True
    )
    assert obs.status == live.UNVERIFIABLE


async def test_probe_refuses_a_non_public_hostname_without_calling_it_dead(_no_pacing):
    """A row we refuse to probe has told us nothing. Returning `dead` here would
    let a malformed row delete itself from the official set."""
    client = _CannedClient(_Resp(200))
    for bad in ("localhost", "127.0.0.1", "internal", ""):
        obs = await live.probe_host_liveness(
            bad, client=client, resolver=lambda h: True
        )
        assert obs.status == live.UNVERIFIABLE, bad
        assert obs.note == "not_a_public_hostname"
    assert client.urls == []


async def test_probe_survives_a_transport_error(_no_pacing):
    client = _CannedClient(raises=RuntimeError("boom"))
    obs = await live.probe_host_liveness(
        "anua.us", client=client, resolver=lambda h: True
    )
    assert obs.status == live.UNVERIFIABLE
    assert obs.note == "RuntimeError"


# =====================================================================
# The sweep
# =====================================================================


@pytest.fixture
def _sweep_probe(monkeypatch):
    """Replace the probe with a per-host verdict table; record the call order."""
    calls = []
    verdicts = {}

    async def _probe(host, *, client=None, resolver=None, max_wait=0):
        calls.append(host)
        return live.HostLiveness(verdicts.get(host, live.LIVE))

    monkeypatch.setattr(live, "probe_host_liveness", _probe)
    return calls, verdicts


async def test_sweep_persists_each_verdict_and_only_dead_excludes(
    monkeypatch, _sweep_probe
):
    calls, verdicts = _sweep_probe
    _inferred(monkeypatch, "anua.com", "anua.us", "judydoll.shop")
    verdicts["judydoll.shop"] = live.DEAD
    verdicts["anua.us"] = live.UNVERIFIABLE

    summary = await live.refresh_official_domain_liveness(MERCHANT)

    assert summary["seeded"] == 3
    assert summary["checked"] == 3
    assert summary["verdicts"][live.DEAD] == 1
    assert summary["verdicts"][live.UNVERIFIABLE] == 1
    assert summary["verdicts"][live.LIVE] == 1
    assert sorted(calls) == ["anua.com", "anua.us", "judydoll.shop"]

    stored = {r["domain"]: r["liveness_status"] for r in await mod.list_official_domains(MERCHANT)}
    assert stored == {
        "anua.com": live.LIVE,
        "anua.us": live.UNVERIFIABLE,
        "judydoll.shop": live.DEAD,
    }
    # And the set the report reads now reflects it — dead out, unverifiable in.
    assert await svc.merchant_owned_domains(MERCHANT) == {"anua.com", "anua.us"}


async def test_sweep_seeds_inferred_rows_so_they_can_ever_be_checked(
    monkeypatch, _sweep_probe
):
    """Without the seed step the sweep only sees asserted domains, and the
    OVERSTATEMENT half of the defect is unreachable forever."""
    calls, _ = _sweep_probe
    _inferred(monkeypatch, "us.judydoll.com")
    assert await mod.list_official_domains(MERCHANT) == []

    await live.refresh_official_domain_liveness(MERCHANT)
    assert calls == ["us.judydoll.com"]
    rows = await mod.list_official_domains(MERCHANT)
    assert [r["source"] for r in rows] == [mod.SOURCE_INFERRED]


async def test_sweep_seeding_never_demotes_an_asserted_row(monkeypatch, _sweep_probe):
    _inferred(monkeypatch, "anua.us")
    await mod.upsert_official_domain(
        merchant_id=MERCHANT,
        domain="anua.us",
        source=mod.SOURCE_ASSERTED,
        verification_status=mod.VERIFICATION_VERIFIED,
    )
    summary = await live.refresh_official_domain_liveness(MERCHANT)
    assert summary["seeded"] == 0
    row = (await mod.list_official_domains(MERCHANT))[0]
    assert row["source"] == mod.SOURCE_ASSERTED
    assert row["verification_status"] == mod.VERIFICATION_VERIFIED


async def test_sweep_skips_rows_inside_the_ttl_and_takes_the_stale_one(
    monkeypatch, _sweep_probe
):
    calls, _ = _sweep_probe
    _inferred(monkeypatch)
    fresh = _now() - timedelta(hours=1)
    stale = _now() - timedelta(days=30)
    await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="fresh.example.com",
        source=mod.SOURCE_ASSERTED, liveness_status=mod.LIVENESS_LIVE,
        last_checked_at=fresh,
    )
    await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="stale.example.com",
        source=mod.SOURCE_ASSERTED, liveness_status=mod.LIVENESS_LIVE,
        last_checked_at=stale,
    )
    summary = await live.refresh_official_domain_liveness(
        MERCHANT, ttl=timedelta(days=7)
    )
    assert calls == ["stale.example.com"]
    assert summary["due"] == 1


async def test_sweep_stops_at_its_run_deadline_and_says_so(monkeypatch, _sweep_probe):
    """#1754: a job with no deadline can hang and take the scheduler down with
    it. A run that hits the bound stops between domains and reports it, rather
    than running past its window."""
    calls, _ = _sweep_probe
    _inferred(monkeypatch, "a.example.com", "b.example.com", "c.example.com")
    summary = await live.refresh_official_domain_liveness(
        MERCHANT, run_deadline_seconds=0.0
    )
    assert summary["deadline_hit"] is True
    assert summary["checked"] == 0
    assert calls == []

    # positive counterpart: with a real budget the same run completes.
    summary = await live.refresh_official_domain_liveness(MERCHANT)
    assert summary["deadline_hit"] is False
    assert summary["checked"] == 3


async def test_sweep_reports_an_empty_queue_distinctly_from_a_silent_one(
    monkeypatch, _sweep_probe
):
    calls, _ = _sweep_probe
    _inferred(monkeypatch)
    summary = await live.refresh_official_domain_liveness(MERCHANT)
    assert summary == {
        "due": 0, "checked": 0, "seeded": 0, "deadline_hit": False,
        "verdicts": {live.LIVE: 0, live.DEAD: 0, live.UNVERIFIABLE: 0},
    }
    assert calls == []


async def test_sweep_is_scoped_to_the_merchant_it_was_given(monkeypatch, _sweep_probe):
    calls, _ = _sweep_probe
    _inferred(monkeypatch)
    await mod.upsert_official_domain(
        merchant_id=OTHER, domain="other.example.com", source=mod.SOURCE_ASSERTED
    )
    await mod.upsert_official_domain(
        merchant_id=MERCHANT, domain="mine.example.com", source=mod.SOURCE_ASSERTED
    )
    await live.refresh_official_domain_liveness(MERCHANT)
    assert calls == ["mine.example.com"]


# =====================================================================
# Backfill: a verified claim writes a `verified` row
# =====================================================================


async def test_a_verified_dns_claim_upserts_a_verified_official_domain(monkeypatch):
    claim = {
        "claim_id": "c1",
        "merchant_id": MERCHANT,
        "claim_method": "dns",
        "brand_domain": "anua.us",
        "challenge_token": "pivota-verify=tok",
        "verification_status": "pending",
    }

    async def _get(cid):
        return claim

    async def _mark(cid, *, proof_ref=None):
        return True

    async def _set(mid):
        return True

    async def _bound(mid, dom):
        return True

    async def _promote(mid):
        return None

    monkeypatch.setattr(svc.bc, "get_brand_claim", _get)
    monkeypatch.setattr(svc.bc, "mark_claim_verified", _mark)
    monkeypatch.setattr(svc, "set_merchant_brand_direct", _set)
    monkeypatch.setattr(
        "services.claim_state.promote_merchant_skus_to_claimed", _promote
    )

    result = await svc.verify_brand_claim(
        "c1",
        txt_resolver=lambda d: ["pivota-verify=tok"],
        owned_domain_check=_bound,
    )
    assert result["status"] == "verified"

    rows = await mod.list_official_domains(MERCHANT)
    assert len(rows) == 1
    assert rows[0]["domain"] == "anua.us"
    assert rows[0]["source"] == mod.SOURCE_VERIFIED
    assert rows[0]["verification_status"] == mod.VERIFICATION_VERIFIED
    # NOT assumed live: proving TXT control is not evidence the store answers HTTP.
    assert rows[0]["liveness_status"] == mod.LIVENESS_UNCHECKED


async def test_control_proven_but_unbound_domain_is_recorded_as_asserted(monkeypatch):
    """A claim that PROVED domain control but is not bound to this merchant's
    brand identity records the domain as source='asserted' — and still does NOT
    grant brand_direct.

    This reverses the first cut of this test, which asserted no row was written.
    Three reasons:

    1. Control is the proof. A matched DNS TXT token / emailed code proves the
       claimant controls the domain. `merchant_owned_domains` answers "is this
       cited host a destination the merchant owns?", and proven control answers
       exactly that. It is strictly stronger evidence than inference, which
       already counts with no proof at all.
    2. SOURCE_ASSERTED was otherwise unreachable. The vocabulary defined it and
       `merchant_owned_domains` counted it, but nothing could ever create one.
    3. It is the measured case. Anua runs anua.com AND anua.us; `anua.us` does
       not bind to `anua.com` (different registrable domains), so the proof was
       discarded and the second storefront read as third-party — 21 points of
       error on branded official share (2026-09-01).

    The money gate is untouched: brand_direct still requires binding. This is
    the measurement gate, and the two have different risk profiles.
    """
    claim = {
        "claim_id": "c2",
        "merchant_id": MERCHANT,
        "claim_method": "dns",
        "brand_domain": "anua.us",
        "challenge_token": "pivota-verify=tok",
        "verification_status": "pending",
    }

    async def _get(cid):
        return claim

    async def _unbound(mid, dom):
        return False

    monkeypatch.setattr(svc.bc, "get_brand_claim", _get)
    result = await svc.verify_brand_claim(
        "c2",
        txt_resolver=lambda d: ["pivota-verify=tok"],
        owned_domain_check=_unbound,
    )
    assert result["status"] == "domain_verified_unbound"
    assert result["brand_direct_set"] is False, "the money gate must stay closed"

    rows = await mod.list_official_domains(MERCHANT)
    assert [(r["domain"], r["source"]) for r in rows] == [("anua.us", mod.SOURCE_ASSERTED)]
    assert "anua.us" in await svc.merchant_owned_domains(MERCHANT)


async def test_a_failed_proof_writes_no_official_row(monkeypatch):
    """The real negative: control was NOT proven, so nothing is recorded. This
    is what stops an unproven domain entering the set."""
    claim = {
        "claim_id": "c3",
        "merchant_id": MERCHANT,
        "claim_method": "dns",
        "brand_domain": "someoneelse.com",
        "challenge_token": "pivota-verify=tok",
        "verification_status": "pending",
    }

    async def _get(cid):
        return claim

    monkeypatch.setattr(svc.bc, "get_brand_claim", _get)
    result = await svc.verify_brand_claim(
        "c3",
        txt_resolver=lambda d: ["some-other-token"],
        owned_domain_check=lambda mid, dom: False,
    )
    assert result["status"] == "pending"
    assert await mod.list_official_domains(MERCHANT) == []


async def test_backfill_normalizes_before_writing():
    """record_verified_official_domain is the one writer allowed to normalize:
    a claim's brand_domain is user-supplied."""
    assert await svc.record_verified_official_domain(MERCHANT, "https://WWW.Anua.US/") is True
    assert [r["domain"] for r in await mod.list_official_domains(MERCHANT)] == ["anua.us"]
    assert await svc.record_verified_official_domain(MERCHANT, None) is False
    assert await svc.record_verified_official_domain("", "anua.us") is False


# ---------------------------------------------------------------------------
# The bypass the first cut of the assert path shipped, and the injected-stub
# test that hid it. These drive the REAL merchant_owns_domain — no
# owned_domain_check override — because the defect lives in the loop between
# what verify_brand_claim WRITES and what its own binding check READS BACK.
# ---------------------------------------------------------------------------
async def _unbound_claim(monkeypatch, domain="totally-unrelated.example"):
    claim = {
        "claim_id": "loop",
        "merchant_id": MERCHANT,
        "claim_method": "dns",
        "brand_domain": domain,
        "challenge_token": "pivota-verify=tok",
        "verification_status": "pending",
    }

    async def _get(cid):
        return claim

    async def _inferred(mid):
        return {"anua.com"}

    granted = []

    async def _grant(mid):
        granted.append(mid)
        return True

    async def _mark(cid, **kw):
        return True

    monkeypatch.setattr(svc.bc, "get_brand_claim", _get)
    monkeypatch.setattr(svc.bc, "mark_claim_verified", _mark)
    monkeypatch.setattr(svc, "_inferred_merchant_hosts", _inferred)
    monkeypatch.setattr(svc, "set_merchant_brand_direct", _grant)
    return granted


async def test_repeating_verify_never_promotes_an_unbound_domain(monkeypatch):
    """Calling /claim/verify twice must not grant brand_direct.

    The asserted row written by call 1 must not be visible to call 2's binding
    check, or the review gate becomes a one-call delay: the claim stays pending
    (no mark_claim_verified on this branch) and the route is freely repeatable.
    """
    granted = await _unbound_claim(monkeypatch)
    for _ in range(3):
        result = await svc.verify_brand_claim(
            "loop", txt_resolver=lambda d: ["pivota-verify=tok"]
        )
        assert result["status"] == "domain_verified_unbound"
        assert result["brand_direct_set"] is False
    assert granted == [], "brand_direct must never be granted on an unbound domain"


async def test_the_asserted_row_is_still_written_on_every_attempt(monkeypatch):
    """Positive counterpart: the gate holds because the BINDING view excludes
    asserted — not because the write silently failed."""
    await _unbound_claim(monkeypatch)
    await svc.verify_brand_claim("loop", txt_resolver=lambda d: ["pivota-verify=tok"])
    rows = await mod.list_official_domains(MERCHANT)
    assert [(r["domain"], r["source"]) for r in rows] == [
        ("totally-unrelated.example", mod.SOURCE_ASSERTED)
    ]
    assert "totally-unrelated.example" in await svc.merchant_owned_domains(MERCHANT)
    assert "totally-unrelated.example" not in await svc.merchant_bound_domains(MERCHANT)


async def test_a_genuinely_bound_domain_still_verifies(monkeypatch):
    """The gate must not have been closed by breaking the real path."""
    granted = await _unbound_claim(monkeypatch, domain="anua.com")
    result = await svc.verify_brand_claim(
        "loop", txt_resolver=lambda d: ["pivota-verify=tok"]
    )
    assert result["status"] == "verified"
    assert result["brand_direct_set"] is True
    assert granted == [MERCHANT]


# --- a source-only write must not blank the sweep's verdict -----------------
async def test_a_source_only_upsert_preserves_a_recorded_liveness_verdict():
    """The brand-claim backfill knows the SOURCE, not the liveness. If it blanks
    the sweep's verdict, a domain measured DEAD silently rejoins the official set
    for a whole TTL — undoing the exclusion this table exists to provide."""
    m, d = "clobber_merchant", "us.judydoll.com"
    await mod.upsert_official_domain(merchant_id=m, domain=d, source=mod.SOURCE_INFERRED)
    await mod.record_liveness(merchant_id=m, domain=d, liveness_status=mod.LIVENESS_DEAD)

    await mod.upsert_official_domain(merchant_id=m, domain=d, source=mod.SOURCE_ASSERTED)

    [row] = await mod.list_official_domains(m)
    assert row["liveness_status"] == mod.LIVENESS_DEAD
    assert row["source"] == mod.SOURCE_ASSERTED


async def test_an_explicit_liveness_verdict_still_wins():
    """Positive counterpart: COALESCE must not make the columns unwritable."""
    m, d = "explicit_merchant", "example.com"
    await mod.upsert_official_domain(merchant_id=m, domain=d, source=mod.SOURCE_INFERRED)
    await mod.record_liveness(merchant_id=m, domain=d, liveness_status=mod.LIVENESS_DEAD)
    await mod.upsert_official_domain(
        merchant_id=m, domain=d, source=mod.SOURCE_ASSERTED,
        liveness_status=mod.LIVENESS_LIVE,
    )
    [row] = await mod.list_official_domains(m)
    assert row["liveness_status"] == mod.LIVENESS_LIVE
    assert row["last_checked_at"] is not None


async def test_a_fresh_row_with_no_verdict_defaults_to_unchecked():
    m, d = "fresh_merchant", "brandnew.com"
    await mod.upsert_official_domain(merchant_id=m, domain=d, source=mod.SOURCE_ASSERTED)
    [row] = await mod.list_official_domains(m)
    assert row["liveness_status"] == mod.LIVENESS_UNCHECKED
    assert row["last_checked_at"] is None


# --- an asserted row must not SHADOW a later legitimate inference ----------
# Found in review of the fix above: merchant_owned_domains_detailed resolves a
# host to ONE source and the stored row wins, so an asserted row hid a genuine
# inferred membership for the same host. The result was an order-dependent,
# permanent, silent lockout — identical end states, opposite outcomes:
#   claim then declare  -> domain_verified_unbound forever
#   declare then claim  -> verified
async def _claim_for(monkeypatch, merchant, domain, inferred_hosts):
    claim = {
        "claim_id": f"shadow-{merchant}",
        "merchant_id": merchant,
        "claim_method": "dns",
        "brand_domain": domain,
        "challenge_token": "pivota-verify=tok",
        "verification_status": "pending",
    }
    granted = []

    async def _get(cid):
        return claim

    async def _inferred(mid):
        return set(inferred_hosts)

    async def _grant(mid):
        granted.append(mid)
        return True

    async def _mark(cid, **kw):
        return True

    monkeypatch.setattr(svc.bc, "get_brand_claim", _get)
    monkeypatch.setattr(svc.bc, "mark_claim_verified", _mark)
    monkeypatch.setattr(svc, "_inferred_merchant_hosts", _inferred)
    monkeypatch.setattr(svc, "set_merchant_brand_direct", _grant)
    return claim, granted


async def test_an_asserted_row_does_not_shadow_a_later_inference(monkeypatch):
    """Claim the domain BEFORE declaring it, then declare it. The merchant must
    still be able to verify — the asserted row must not outrank inference."""
    m, d = "shadow_a", "mybrand.example"
    hosts = set()
    claim, granted = await _claim_for(monkeypatch, m, d, hosts)

    first = await svc.verify_brand_claim(
        claim["claim_id"], txt_resolver=lambda x: ["pivota-verify=tok"]
    )
    assert first["status"] == "domain_verified_unbound"

    hosts.add(d)  # the merchant now legitimately declares it
    second = await svc.verify_brand_claim(
        claim["claim_id"], txt_resolver=lambda x: ["pivota-verify=tok"]
    )
    assert second["status"] == "verified", "an asserted row must not lock the merchant out"
    assert granted == [m]
    assert d in await svc.merchant_bound_domains(m)


async def test_the_other_order_still_verifies(monkeypatch):
    """Order independence: the same two states, declared first, already worked
    and must keep working."""
    m, d = "shadow_b", "mybrand.example"
    claim, granted = await _claim_for(monkeypatch, m, d, {d})
    result = await svc.verify_brand_claim(
        claim["claim_id"], txt_resolver=lambda x: ["pivota-verify=tok"]
    )
    assert result["status"] == "verified"
    assert granted == [m]


async def test_asserted_alone_is_still_excluded_from_binding(monkeypatch):
    """The bypass fix must survive: asserted WITHOUT inference stays unbound
    however many times /claim/verify is called."""
    m, d = "shadow_c", "totally-unrelated.example"
    claim, granted = await _claim_for(monkeypatch, m, d, {"anua.com"})
    for _ in range(3):
        result = await svc.verify_brand_claim(
            claim["claim_id"], txt_resolver=lambda x: ["pivota-verify=tok"]
        )
        assert result["status"] == "domain_verified_unbound"
    assert granted == []
    assert d in await svc.merchant_owned_domains(m)      # reporting: yes
    assert d not in await svc.merchant_bound_domains(m)  # binding: no
