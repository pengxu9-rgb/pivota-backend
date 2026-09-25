import asyncio
import contextlib
import io
import json
import re
import sys
import time
import types

import pytest

from scripts import affiliate_coverage_census as census


# --- host keys -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("us.oliveyoung.com", "oliveyoung.com"),
        ("https://global.oliveyoung.com/product/detail?x=1", "oliveyoung.com"),
        ("WWW.Sephora.com", "sephora.com"),
        ("sg.althea.kr", "althea.kr"),
        ("www.stylekorean.co.kr", "stylekorean.co.kr"),
        ("shop.brand.co.kr", "brand.co.kr"),
        ("lookfantastic.com.sg", "lookfantastic.com.sg"),
        # Every subdomain of a hosting platform is a different merchant: never collapse.
        ("brand-a.myshopify.com", "brand-a.myshopify.com"),
        # An unlisted two-part country suffix is never itself a key (review of #2269).
        ("themedicube.us.com", "themedicube.us.com"),
        ("shop.reddane.co.za", "reddane.co.za"),
        ("reddane.co.za", "reddane.co.za"),
        ("", ""),
        (None, ""),
    ],
)
def test_site_key(value, expected):
    assert census.site_key(value) == expected


def test_two_stores_on_one_platform_do_not_share_a_key():
    assert census.site_key("a.myshopify.com") != census.site_key("b.myshopify.com")


# --- prod program and the log channel ------------------------------------------------------

def test_prod_program_is_valid_python_without_the_runner_delimiter():
    prog = census.build_prod_program()
    compile(prog, "<prod_program>", "exec")
    # run_oneoff_job.sh picks its --args delimiter from characters absent in the payload.
    assert "@" not in prog


def test_prod_queries_are_read_only():
    writes = re.compile(r"\b(insert|update|delete|alter|drop|truncate|create|grant|copy)\b", re.I)
    for name, sql in census.PROD_QUERIES.items():
        assert sql.lstrip().upper().startswith("SELECT"), name
        assert not writes.search(sql), name


def _run_program_against(rows_by_query, monkeypatch):
    """Execute the exact prod program text against a fake `db.database`, capture its log."""
    calls = []
    session = []

    class FakeDB:
        async def connect(self):
            pass

        async def disconnect(self):
            pass

        def connection(self):
            db = self

            class _Conn:
                async def __aenter__(self):
                    return db

                async def __aexit__(self, *exc):
                    return False

            return _Conn()

        async def execute(self, sql):
            session.append(sql)

        async def fetch_all(self, sql):
            # The session must already be read-only when the first query runs.
            assert session == ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"]
            calls.append(sql)
            for name, q in census.PROD_QUERIES.items():
                if q == sql:
                    rows = rows_by_query.get(name)
                    if isinstance(rows, Exception):
                        raise rows
                    return rows or []
            raise AssertionError("program ran a query that is not in PROD_QUERIES")

    db_pkg = types.ModuleType("db")
    db_mod = types.ModuleType("db.database")
    db_mod.database = FakeDB()
    monkeypatch.setitem(sys.modules, "db", db_pkg)
    monkeypatch.setitem(sys.modules, "db.database", db_mod)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(census.build_prod_program(), {"__name__": "__main__"})
    return out.getvalue(), calls


def test_program_output_round_trips_through_a_shuffled_duplicated_log(monkeypatch):
    rows = {
        "offer_pairs": [{"host": f"h{i}.com", "nb": "brand", "mk": "US", "n": i, "se": i} for i in range(400)],
        "totals": [{"products": 1, "offers": 2, "seeds": 3}],
    }
    log, calls = _run_program_against(rows, monkeypatch)
    assert len(calls) == len(census.PROD_QUERIES)
    lines = log.splitlines()
    chunk_lines = [l for l in lines if l.startswith("CJ0")]
    assert len(chunk_lines) > 3  # the payload really was split
    # Cloud Logging returns newest-first and can repeat entries.
    scrambled = "\n".join(list(reversed(lines)) + chunk_lines[:2])
    decoded = census.decode_chunks(scrambled)
    assert decoded["offer_pairs"] == rows["offer_pairs"]
    assert decoded["totals"] == rows["totals"]


def test_a_failed_prod_query_is_carried_as_an_error_not_an_empty_list(monkeypatch):
    log, _ = _run_program_against({"seeds": RuntimeError("statement timeout")}, monkeypatch)
    decoded = census.decode_chunks(log)
    assert isinstance(decoded["seeds"], str) and decoded["seeds"].startswith("ERR RuntimeError")
    with pytest.raises(ValueError, match="failed in prod"):
        census.build_report(decoded, None, None)


def test_decode_refuses_a_log_with_a_missing_chunk(monkeypatch):
    rows = {"offer_pairs": [{"host": f"h{i}.com", "nb": "b", "mk": "US", "n": 1, "se": 1} for i in range(400)]}
    log, _ = _run_program_against(rows, monkeypatch)
    gapped = "\n".join(l for l in log.splitlines() if not l.startswith("CJ0001|"))
    with pytest.raises(ValueError, match="chunks missing"):
        census.decode_chunks(gapped)


def test_decode_refuses_two_runs_in_one_log():
    with pytest.raises(ValueError, match="more than one run"):
        census.decode_chunks("CJHEAD 2 100\nCJHEAD 3 150\n")


def test_decode_refuses_a_log_without_output():
    with pytest.raises(ValueError, match="no CJHEAD"):
        census.decode_chunks("QDONE offer_pairs 1.0 12\n")


# --- Rakuten -------------------------------------------------------------------------------

def _adv(mid, url, name="Adv", deep_links=True):
    return {"id": mid, "name": name, "url": url, "features": {"deep_links": deep_links}}


def _part(mid, status, network=1, name="Adv"):
    return {"advertiser": {"id": mid, "name": name, "network": network, "status": "active"}, "status": status}


@pytest.mark.parametrize(
    "status, bucket",
    [
        ("active", "approved"),
        ("pending", "pending"),
        ("extended", "pending"),
        ("temp-decline", "declined_temporary"),
        ("permanent-decline", "declined_permanent"),
        ("self-removed", "self_removed"),
        (None, "not_applied"),
        ("brand-new-status", "unknown:brand-new-status"),
    ],
)
def test_partnership_bucket(status, bucket):
    assert census.partnership_bucket(status) == bucket


def test_best_status_wins_then_the_market_network():
    idx = census.index_rakuten(
        [_adv(1, "https://www.oliveyoung.com"), _adv(2, "https://global.oliveyoung.com"),
         _adv(3, "https://us.oliveyoung.com")],
        [_part(1, "pending", network=3), _part(2, "active", network=3), _part(3, "active", network=1)],
    )
    progs = idx["oliveyoung.com"]
    assert len(progs) == 3
    assert census.pick_program(progs, "US")["mid"] == 3
    assert census.pick_program(progs, "GB")["mid"] == 2


def test_an_unpartnered_advertiser_is_the_apply_queue():
    idx = census.index_rakuten([_adv(9, "http://brand.com")], [])
    prog = census.pick_program(idx["brand.com"], "US")
    assert prog["bucket"] == "not_applied"
    assert census.next_action("not_applied", prog, []) == "APPLY on Rakuten (MID 9)"


def test_a_partnership_no_advertiser_url_can_place_is_reported():
    lost = census.unkeyed_partnerships([_adv(1, "https://a.com"), _adv(2, "")], [_part(1, "active"), _part(2, "active"), _part(3, "pending")])
    assert [x.split(":")[0] for x in lost] == ["2", "3"]


def test_next_page_follows_either_envelope_spelling():
    p = {"page": 1, "limit": 200}
    assert census.next_page_params({"_metadata": {"_links": {"next": "/v2/advertisers?page=2&limit=200"}}}, p, 200)["page"] == "2"
    assert census.next_page_params({"metadata": {"links": {"next": "/v1/partnerships?page=2&limit=200"}}}, p, 200)["page"] == "2"
    # A `next` that points at the current page would loop forever.
    assert census.next_page_params({"_metadata": {"_links": {"next": "/x?page=1&limit=200"}}}, p, 200) is None


def test_next_page_without_links_uses_total_then_page_size():
    p = {"page": 2, "limit": 200}
    assert census.next_page_params({"_metadata": {"total": 450}}, p, 200)["page"] == 3
    assert census.next_page_params({"_metadata": {"total": 400}}, p, 200) is None
    assert census.next_page_params({}, p, 200)["page"] == 3
    assert census.next_page_params({}, p, 17) is None
    assert census.next_page_params({}, p, 0) is None


def test_mint_token_sends_the_documented_request():
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok", "expires_in": 3600}

    class Client:
        async def post(self, url, headers, data):
            seen.update(url=url, headers=headers, data=data)
            return Resp()

    tok = asyncio.run(census.RakutenClient.mint_token(Client(), "cid", "sec", "12345"))
    assert tok == "tok"
    assert seen["url"] == "https://api.linksynergy.com/token"
    import base64
    assert seen["headers"]["Authorization"] == "Bearer " + base64.b64encode(b"cid:sec").decode()
    assert seen["data"] == {"scope": "12345"}


def test_mint_token_failure_does_not_echo_credentials():
    class Resp:
        status_code = 401
        text = "invalid_client"

    class Client:
        async def post(self, url, headers, data):
            return Resp()

    with pytest.raises(RuntimeError) as e:
        asyncio.run(census.RakutenClient.mint_token(Client(), "cid-SECRETISH", "sec-SECRETISH", "1"))
    assert "SECRETISH" not in str(e.value)


# --- signals -------------------------------------------------------------------------------

def test_detect_networks():
    html = '<script src="https://utt.impactcdn.com/A1.js"></script><img src="//www.dwin1.com/123.js">'
    assert census.detect_networks(html) == ["awin", "impact"]
    assert census.detect_networks("<html>nothing</html>") == []


def test_no_signal_is_not_a_finding():
    assert census.next_action("not_on_rakuten", None, []) == "no program found -> aggregator or direct deal"
    assert census.next_action("not_on_rakuten", None, ["impact"]) == "check program on impact"


# --- report --------------------------------------------------------------------------------

def _inventory():
    return {
        "offer_pairs": [
            # A retailer carrying two brands.
            {"host": "global.oliveyoung.com", "nb": "cosrx", "mk": "US", "n": 10, "se": 8},
            {"host": "global.oliveyoung.com", "nb": "round lab", "mk": "US", "n": 5, "se": 5},
            # A brand store Rakuten lists under another URL.
            {"host": "cosrx.com", "nb": "cosrx", "mk": "US", "n": 4, "se": 3},
            # A brand store with an unapplied program.
            {"host": "www.byterry.com", "nb": "by terry", "mk": "US", "n": 6, "se": 6},
        ],
        "product_only_pairs": [{"host": "tiny.myshopify.com", "nb": "tiny", "n": 2, "se": 0}],
        "offer_modes": [{"host": "global.oliveyoung.com", "om": "external", "ot": "referral", "ch": "", "n": 15}],
        "seeds": [{"host": "cosrx.com", "mk": "US", "pt": "affiliate", "n": 3, "attached": 1}],
        "totals": [{"products": 27, "offers": 25, "seeds": 3}],
    }


def _rakuten():
    return {
        "fetched_at": "2026-09-23T00:00:00Z",
        "advertisers": [
            _adv(100, "https://us.oliveyoung.com", name="OLIVE YOUNG"),
            _adv(200, "https://www.byterry.com", name="By Terry", deep_links=False),
            _adv(300, "https://cosrx-official.example", name="COSRX Official"),
        ],
        "partnerships": [_part(100, "active", name="OLIVE YOUNG")],
    }


def test_report_buckets_hosts_and_rolls_brands_up_through_the_checkout_host():
    rep = census.build_report(_inventory(), _rakuten(), None)
    by_host = {r["host"]: r for r in rep["hosts"]}

    oy = by_host["global.oliveyoung.com"]
    assert (oy["rakuten_bucket"], oy["rakuten_mid"], oy["kind"]) == ("approved", 100, "retailer")
    assert oy["next_action"] == "approved -> wrap outbound links"

    bt = by_host["byterry.com"]
    assert bt["rakuten_bucket"] == "not_applied"
    assert bt["deep_links"] == "false"
    # Matched by domain, so its name match is not offered as a second, weaker candidate.
    assert bt["rakuten_name_candidates"] == ""

    cx = by_host["cosrx.com"]
    assert cx["rakuten_bucket"] == "not_on_rakuten"
    assert cx["kind"] == "brand_store"
    assert "MID 300" in cx["rakuten_name_candidates"]  # offered, never counted
    assert cx["seed_partner_types"] == "affiliate:3"

    assert by_host["tiny.myshopify.com"]["rakuten_bucket"] == "not_on_rakuten"

    # COSRX is covered only through Olive Young: 8 of its 11 serving rows.
    brands = {b["brand"]: b for b in rep["brands"]}
    assert brands["cosrx"]["serving"] == 11
    assert brands["cosrx"]["serving_approved"] == 8
    assert brands["cosrx"]["approved_hosts"] == "global.oliveyoung.com"
    assert brands["by terry"]["serving_applyable"] == 6

    assert rep["by_bucket"]["approved"]["serving"] == 13
    assert rep["serving_total"] == 22
    assert "byterry.com" in census.render_summary(rep)


def test_report_without_rakuten_says_so_instead_of_claiming_no_program():
    rep = census.build_report(_inventory(), None, None)
    assert {r["rakuten_bucket"] for r in rep["hosts"]} == {"rakuten_not_fetched"}
    assert "NOT FETCHED" in census.render_summary(rep)


def test_cli_report_writes_every_file(tmp_path):
    (tmp_path / "inventory.json").write_text(json.dumps(_inventory()))
    (tmp_path / "rakuten.json").write_text(json.dumps(_rakuten()))
    with contextlib.redirect_stdout(io.StringIO()):
        assert census.main(["report", "--out-dir", str(tmp_path)]) == 0
    for f in ("coverage.csv", "brands.csv", "coverage.json", "summary.md"):
        assert (tmp_path / f).stat().st_size > 0


def test_brand_spelling_variants_do_not_turn_a_brand_store_into_a_retailer():
    inv = {
        "offer_pairs": [
            {"host": "tartecosmetics.com", "nb": "tarte", "mk": "US", "n": 176, "se": 170},
            {"host": "tartecosmetics.com", "nb": "tarte cosmetics", "mk": "US", "n": 73, "se": 60},
        ],
        "product_only_pairs": [], "offer_modes": [], "seeds": [], "totals": [{}],
    }
    rep = census.build_report(inv, None, None)
    assert rep["hosts"][0]["kind"] == "brand_store"


def test_two_sites_under_an_unlisted_country_suffix_do_not_share_a_key():
    assert census.site_key("a.co.za") != census.site_key("b.co.za")
    assert census.site_key("x.us.com") != census.site_key("y.us.com")


def test_per_host_counts_are_distinct_products_not_market_sums():
    inv = {
        "offer_pairs": [
            {"host": "arencia.jp", "nb": "arencia", "mk": "JP", "n": 10, "se": 8},
            {"host": "arencia.jp", "nb": "arencia", "mk": "US", "n": 10, "se": 8},
        ],
        "host_products": [{"host": "arencia.jp", "n": 10, "se": 8}],
        "product_only_pairs": [{"host": "arencia.jp", "nb": "arencia", "n": 2, "se": 0}],
        "offer_modes": [], "seeds": [], "totals": [{}],
    }
    row = census.build_report(inv, None, None)["hosts"][0]
    assert (row["products"], row["serving"]) == (12, 8)
    # An older inventory without host_products falls back to the sums.
    inv.pop("host_products")
    row = census.build_report(inv, None, None)["hosts"][0]
    assert (row["products"], row["serving"]) == (22, 16)


@pytest.mark.parametrize(
    "host, public",
    [("brand.com", True), ("shop.brand.co.kr", True), ("localhost", False), ("x.localhost", False),
     ("10.0.0.5", False), ("169.254.169.254", False), ("[::1]", False), ("metadata.google.internal", False),
     ("", False), ("nodots", False)],
)
def test_only_public_hostnames_are_probed(host, public):
    assert census.is_public_hostname(host) is public


class _Resp:
    def __init__(self, url, status=200, location=None, text="<html></html>"):
        self.url, self.status_code, self.text = url, status, text
        self.headers = {"location": location} if location else {}


class _Client:
    def __init__(self, chain):
        self.chain = dict(chain)
        self.seen = []

    async def get(self, url, headers=None):
        self.seen.append(url)
        return self.chain[url]


def test_redirects_are_followed_only_to_public_https():
    ok = _Client({
        "https://brand.com/": _Resp("https://brand.com/", 301, "https://www.brand.com/"),
        "https://www.brand.com/": _Resp("https://www.brand.com/", 200),
    })
    r = asyncio.run(census.fetch_public_https(ok, "https://brand.com/", {}))
    assert r.status_code == 200 and ok.seen == ["https://brand.com/", "https://www.brand.com/"]

    for bad in ("http://brand.com/", "https://169.254.169.254/latest", "https://localhost/"):
        c = _Client({"https://brand.com/": _Resp("https://brand.com/", 302, bad)})
        with pytest.raises(ValueError):
            asyncio.run(census.fetch_public_https(c, "https://brand.com/", {}))
        assert c.seen == ["https://brand.com/"]  # the unsafe hop is never requested


def test_a_redirect_loop_stops():
    loop = _Client({"https://a.com/": _Resp("https://a.com/", 302, "https://a.com/")})
    with pytest.raises(ValueError, match="too_many_redirects"):
        asyncio.run(census.fetch_public_https(loop, "https://a.com/", {}))
