#!/usr/bin/env python3
"""Affiliate coverage census: for every merchant host we send buyers to, which affiliate
program (if any) could credit Pivota for the sale, and what our status is there.

The unit is the HOST THE BUYER CHECKS OUT ON, not the brand. An affiliate network credits the
store whose confirmation page fires its pixel, so a brand we carry only through a retailer is
covered by the RETAILER's program (Olive Young, say), never by the brand's own. The brand
rollup in the report is derived from host coverage for exactly that reason.

This census covers only the referral lanes (affiliate_outbound, referral PDP links, cart
permalinks). A UCP or Reap agentic checkout fires no pixel, so no affiliate network can
credit it, whatever this report says about the host. See merchant_ucp_checkout.build_attribution.

Four stages, each writing one file into --out-dir, so any stage can be re-run alone:

  1. inventory  (prod, read-only)  -- which hosts, how many products / serving rows / brands.
       python scripts/affiliate_coverage_census.py prod-program > "$OUT/prod_program.py"
       bash scripts/ops/run_oneoff_job.sh -c "$(cat "$OUT/prod_program.py")" > "$OUT/prod_job.log"
       python scripts/affiliate_coverage_census.py decode --log "$OUT/prod_job.log" --out-dir "$OUT"
     The prod DB is on a private VPC address, so the query runs as a one-off Cloud Run job and
     its result comes back through the job log as numbered chunks. `decode` refuses a partial
     log rather than reporting a census over the chunks that happened to arrive.

  2. rakuten    (Rakuten Advertising publisher API) -- every advertiser + our partnership status.
       RAKUTEN_ACCESS_TOKEN=...  python scripts/affiliate_coverage_census.py rakuten --out-dir "$OUT"
     or RAKUTEN_CLIENT_ID / RAKUTEN_CLIENT_SECRET / RAKUTEN_ACCOUNT_ID (the publisher site id)
     to mint a token. Tokens are never printed or written.

  3. signals    (optional, public storefront HTML) -- tags of OTHER affiliate networks on the
     hosts Rakuten does not cover. A tag is a hint that a program exists, not proof that it is
     open to publishers; a missing tag proves nothing (most load through a tag manager).
       python scripts/affiliate_coverage_census.py signals --out-dir "$OUT"

  4. report     (local) -- the join: coverage.csv, brands.csv, summary.md, coverage.json.
       python scripts/affiliate_coverage_census.py report --out-dir "$OUT"

Nothing here writes to any database or applies to any program. Applying stays a human click in
the Rakuten publisher dashboard; this produces the ranked list of what to click.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import re
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

RAKUTEN_API = "https://api.linksynergy.com"
# 100 calls/min on every endpoint we use (developer portal, 2026-09). 0.7s keeps us under it
# with room for the token call.
RAKUTEN_MIN_INTERVAL_S = 0.7
RAKUTEN_PAGE_LIMIT = 200
RAKUTEN_MAX_PAGES = 500

# Rakuten network ids (Partnerships API docs). Used to prefer the program whose currency matches
# the market we serve the host in.
RAKUTEN_NETWORKS = {1: "US", 3: "UK", 5: "CA", 7: "FR", 8: "BR", 9: "DE", 41: "AU"}
MARKET_TO_NETWORK = {"US": 1, "GB": 3, "UK": 3, "CA": 5, "FR": 7, "BR": 8, "DE": 9, "AU": 41}

# partner_status values the Partnerships API documents, mapped onto what we would do next.
PARTNER_STATUS_BUCKET = {
    "active": "approved",
    "pending": "pending",
    "extended": "pending",
    "temp-decline": "declined_temporary",
    "temp-remove": "declined_temporary",
    "permanent-decline": "declined_permanent",
    "permanent-remove": "declined_permanent",
    "self-removed": "self_removed",
}
BUCKET_ORDER = [
    "approved",
    "pending",
    "not_applied",
    "declined_temporary",
    "self_removed",
    "declined_permanent",
    "not_on_rakuten",
]

# Second-level suffixes where the registrable domain is three labels, not two. The list is not a
# public-suffix list; `site_key` also treats any second label in GENERIC_SECOND_LABELS as part of
# the suffix (co.za, us.com, gov.uk, ...). That rule is what keeps a suffix from ever becoming a key
# and merging unrelated sites (review of #2269: us.com and co.za did before it).
MULTI_LABEL_SUFFIXES = frozenset(
    {
        "co.kr", "or.kr", "ne.kr", "com.sg", "edu.sg", "co.uk", "org.uk", "com.au", "net.au",
        "co.jp", "ne.jp", "or.jp", "com.my", "com.hk", "com.tw", "co.nz", "com.br", "com.cn",
        "com.ph", "co.id", "co.th", "com.vn", "com.mx", "co.in", "com.tr",
    }
)
GENERIC_SECOND_LABELS = frozenset(
    {"com", "co", "net", "org", "ac", "go", "or", "ne", "gov", "edu", "us", "gen", "ltd", "plc", "nom", "biz"}
)
# Hosting platforms where every subdomain is a DIFFERENT merchant. Collapsing these to the
# platform domain would credit one store's program to every store on the platform.
SHARED_PLATFORM_DOMAINS = frozenset(
    {"myshopify.com", "wixsite.com", "square.site", "bigcartel.com", "mybigcommerce.com",
     "squarespace.com", "company.site", "ecwid.com", "shoplineapp.com"}
)

# Storefront HTML markers for affiliate / partner networks other than Rakuten, and for the
# Shopify-native affiliate apps. Lowercased substring match on the raw HTML.
NETWORK_MARKERS: Dict[str, Tuple[str, ...]] = {
    "rakuten": ("tag.rmp.rakuten.com", "linksynergy.com"),
    "impact": ("utt.impactcdn.com", "impactradius-event.com", "impactradius.com", "impact-ad.jp"),
    "awin": ("dwin1.com", "awin1.com", "zenaps.com"),
    "shareasale": ("shareasale.com", "shareasale-analytics.com"),
    "cj": ("emjcd.com", "mczbf.com", "cj.dotomi.com"),
    "partnerize": ("prf.hn",),
    "refersion": ("refersion.com",),
    "uppromote": ("uppromote.com", "secomapp.com/affiliate"),
    "goaffpro": ("goaffpro.com",),
    "involve_asia": ("invol.co", "involve.asia"),
    "skimlinks_merchant": ("skimresources.com",),
    "optimise": ("optimise.net", "clk.omgt"),
}

CHUNK_HEAD = "CJHEAD"
CHUNK_PREFIX = "CJ"
CHUNK_BYTES = 700


# ------------------------------------------------------------------------------------------
# Host normalisation and matching (pure)
# ------------------------------------------------------------------------------------------

def normalize_host(value: Optional[str]) -> str:
    """Bare lowercase hostname from a host or URL; '' when there is none."""
    s = (value or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s
    host = urlparse(s).hostname or ""
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def site_key(value: Optional[str]) -> str:
    """The registrable domain we match programs on: us.oliveyoung.com -> oliveyoung.com."""
    host = normalize_host(value)
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last2 = ".".join(labels[-2:])
    if last2 in SHARED_PLATFORM_DOMAINS:
        return ".".join(labels[-3:])
    if last2 in MULTI_LABEL_SUFFIXES or labels[-2] in GENERIC_SECOND_LABELS:
        return ".".join(labels[-3:])
    return last2


_NAME_NOISE = re.compile(
    r"\b(inc|llc|ltd|limited|co|corp|official|store|shop|online|us|usa|uk|ca|au|global|"
    r"beauty|cosmetics|com|the)\b"
)


def normalize_name(value: Optional[str]) -> str:
    """Brand/advertiser name for a CANDIDATE match only. Drops generic words, so it is lossy by
    design and is never counted as coverage -- a human confirms every name match."""
    s = (value or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = _NAME_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def partnership_bucket(status: Optional[str]) -> str:
    s = (status or "").strip().lower()
    return PARTNER_STATUS_BUCKET.get(s, f"unknown:{s}" if s else "not_applied")


def _bucket_rank(bucket: str) -> int:
    return BUCKET_ORDER.index(bucket) if bucket in BUCKET_ORDER else len(BUCKET_ORDER)


def index_rakuten(advertisers: List[dict], partnerships: List[dict]) -> Dict[str, List[dict]]:
    """site_key -> programs on that site, each carrying our partnership bucket.

    A partnership row carries no URL, so it reaches a site only through its advertiser id.
    `run_rakuten` fetches /v2/advertisers/{id} for every partnered advertiser the list omitted;
    one still missing after that is reported by `unkeyed_partnerships`, never dropped silently.
    """
    by_id: Dict[int, dict] = {}
    for a in advertisers:
        try:
            aid = int(a.get("id"))
        except (TypeError, ValueError):
            continue
        by_id[aid] = a
    part_by_id: Dict[int, dict] = {}
    for p in partnerships:
        adv = p.get("advertiser") or {}
        try:
            aid = int(adv.get("id"))
        except (TypeError, ValueError):
            continue
        part_by_id[aid] = p

    out: Dict[str, List[dict]] = defaultdict(list)
    for aid, a in by_id.items():
        key = site_key(a.get("url"))
        if not key:
            continue
        p = part_by_id.get(aid)
        features = a.get("features") or {}
        ships_to = ((a.get("policies") or {}).get("international_capabilities") or {}).get("ships_to") or []
        out[key].append(
            {
                "mid": aid,
                "name": a.get("name"),
                "url": a.get("url"),
                "network": (p or {}).get("advertiser", {}).get("network") if p else a.get("network"),
                "advertiser_status": (p or {}).get("advertiser", {}).get("status") if p else None,
                "partnership_status": (p or {}).get("status"),
                "bucket": partnership_bucket((p or {}).get("status")),
                "deep_links": features.get("deep_links"),
                "ships_to": ships_to,
            }
        )
    return out


def unkeyed_partnerships(advertisers: List[dict], partnerships: List[dict]) -> List[str]:
    """Partnerships that no advertiser URL can place on a site -- invisible to the host join."""
    keyed = {str(a.get("id")) for a in advertisers if site_key(a.get("url"))}
    out = []
    for p in partnerships:
        adv = p.get("advertiser") or {}
        if str(adv.get("id")) not in keyed:
            out.append(f"{adv.get('id')}:{adv.get('name')}:{p.get('status')}")
    return out


def pick_program(programs: List[dict], market: Optional[str]) -> Optional[dict]:
    """The one program that decides a host's bucket: best partnership status first, then the
    network matching the market we serve that host in."""
    if not programs:
        return None
    want = MARKET_TO_NETWORK.get((market or "").upper())

    def rank(p: dict) -> Tuple[int, int, int]:
        net = p.get("network")
        try:
            net = int(net) if net is not None else None
        except (TypeError, ValueError):
            net = None
        return (_bucket_rank(p["bucket"]), 0 if (want and net == want) else 1, int(p["mid"]))

    return sorted(programs, key=rank)[0]


def detect_networks(html: str) -> List[str]:
    low = (html or "").lower()
    return sorted(n for n, marks in NETWORK_MARKERS.items() if any(m in low for m in marks))


def next_action(bucket: str, program: Optional[dict], signals: List[str]) -> str:
    if bucket == "approved":
        if program and program.get("deep_links") is False:
            return "approved; deep links NOT allowed -> homepage/text links only"
        return "approved -> wrap outbound links"
    if bucket == "pending":
        return "applied; waiting on advertiser"
    if bucket == "not_applied":
        return f"APPLY on Rakuten (MID {program['mid']})" if program else "APPLY on Rakuten"
    if bucket == "declined_temporary":
        return "re-apply later on Rakuten"
    if bucket in ("declined_permanent", "self_removed"):
        return "Rakuten closed -> aggregator or direct deal"
    others = [s for s in signals if s != "rakuten"]
    if others:
        return "check program on " + "/".join(others)
    return "no program found -> aggregator or direct deal"


# ------------------------------------------------------------------------------------------
# Stage 1: prod inventory program + log decoder
# ------------------------------------------------------------------------------------------

_HOST_P = (
    "regexp_replace(lower(coalesce(nullif(p.source_domain,''), "
    "split_part(split_part(p.canonical_url,'://',2),'/',1))),'^www[.]','')"
)
_HOST_O = (
    "regexp_replace(lower(coalesce(nullif(o.source_domain,''), nullif(p.source_domain,''), "
    "split_part(split_part(p.canonical_url,'://',2),'/',1))),'^www[.]','')"
)
_HOST_S = (
    "regexp_replace(lower(coalesce(nullif(s.domain,''), "
    "split_part(split_part(s.destination_url,'://',2),'/',1))),'^www[.]','')"
)
# The same brand normalisation the Meitu census used, so brand keys line up across reports.
_NB = (
    "btrim(regexp_replace(lower(replace(replace(coalesce(p.brand,''),'\u00b7',''),'&',' and ')),"
    "'[^a-z0-9]+',' ','g'))"
)
_LIVE_P = "p.suppressed_at IS NULL AND p.suppression_reason IS NULL"

PROD_QUERIES: Dict[str, str] = {
    # One row per (checkout host, brand, market): products and serving products. The host is
    # the OFFER's seller when there is one -- that is where the buyer checks out.
    "offer_pairs": (
        "SELECT " + _HOST_O + " host, " + _NB + " nb, o.market mk, "
        "count(DISTINCT p.product_key) n, "
        "count(DISTINCT p.product_key) FILTER (WHERE ips.serving_eligible) se "
        "FROM catalog_offers o JOIN catalog_products p ON p.product_key = o.product_key "
        "LEFT JOIN index_pipeline_state ips ON ips.content_key = p.content_key "
        "WHERE o.suppressed_at IS NULL AND " + _LIVE_P + " GROUP BY 1, 2, 3"
    ),
    # Distinct products per checkout host. offer_pairs is also split by market, so summing it
    # counts a product offered in two markets on one host twice (review of #2269).
    "host_products": (
        "SELECT " + _HOST_O + " host, count(DISTINCT p.product_key) n, "
        "count(DISTINCT p.product_key) FILTER (WHERE ips.serving_eligible) se "
        "FROM catalog_offers o JOIN catalog_products p ON p.product_key = o.product_key "
        "LEFT JOIN index_pipeline_state ips ON ips.content_key = p.content_key "
        "WHERE o.suppressed_at IS NULL AND " + _LIVE_P + " GROUP BY 1"
    ),
    # Products with no live offer still name a store; counted on the product's own host.
    "product_only_pairs": (
        "SELECT " + _HOST_P + " host, " + _NB + " nb, count(*) n, "
        "count(*) FILTER (WHERE ips.serving_eligible) se "
        "FROM catalog_products p LEFT JOIN index_pipeline_state ips ON ips.content_key = p.content_key "
        "WHERE " + _LIVE_P + " AND NOT EXISTS (SELECT 1 FROM catalog_offers o "
        "WHERE o.product_key = p.product_key AND o.suppressed_at IS NULL) GROUP BY 1, 2"
    ),
    # How each host's offers are sold -- a host we only reach through UCP checkout earns no
    # affiliate commission whatever its program status.
    "offer_modes": (
        "SELECT " + _HOST_O + " host, coalesce(o.offer_mode,'') om, coalesce(o.offer_type,'') ot, "
        "coalesce(o.channel,'') ch, count(*) n "
        "FROM catalog_offers o JOIN catalog_products p ON p.product_key = o.product_key "
        "WHERE o.suppressed_at IS NULL AND " + _LIVE_P + " GROUP BY 1, 2, 3, 4"
    ),
    # External referral seeds: their own host, and the partner_type we already stamp on them.
    "seeds": (
        "SELECT " + _HOST_S + " host, s.market mk, coalesce(s.partner_type,'') pt, count(*) n, "
        "count(*) FILTER (WHERE s.attached_product_key IS NOT NULL) attached "
        "FROM external_product_seeds s WHERE s.status = 'active' GROUP BY 1, 2, 3"
    ),
    "totals": (
        "SELECT (SELECT count(*) FROM catalog_products p WHERE " + _LIVE_P + ") products, "
        "(SELECT count(*) FROM catalog_offers o WHERE o.suppressed_at IS NULL) offers, "
        "(SELECT count(*) FROM external_product_seeds s WHERE s.status = 'active') seeds"
    ),
}


def build_prod_program() -> str:
    """The inline program for scripts/ops/run_oneoff_job.sh -c. Read-only: SELECTs only.

    It must not contain '@' (run_oneoff_job's --args delimiter choice) -- a test pins that.
    Output is zlib+base64 JSON cut into numbered log lines, because a job's only channel back
    is its log and Cloud Logging does not preserve order.
    """
    queries = json.dumps(PROD_QUERIES)
    return (
        "import asyncio, json, zlib, base64, time\n"
        "from db.database import database\n"
        "Q = json.loads(" + repr(queries) + ")\n"
        "async def main():\n"
        "    await database.connect()\n"
        "    o = {}\n"
        # One connection, made read-only at the database, so the guarantee does not rest on the
        # queries alone. Autocommit statements, so one failing query cannot abort the others.
        "    async with database.connection() as conn:\n"
        "        await conn.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY')\n"
        "        for k, sql in Q.items():\n"
        "            t = time.time()\n"
        "            try:\n"
        "                o[k] = [dict(r) for r in await conn.fetch_all(sql)]\n"
        "            except Exception as e:\n"
        "                o[k] = 'ERR ' + type(e).__name__ + ' ' + str(e)[:300]\n"
        "            print('QDONE', k, round(time.time() - t, 1), len(o[k]) if isinstance(o[k], list) else o[k][:200], flush=True)\n"
        "    blob = json.dumps(o, default=str, separators=(',', ':'))\n"
        "    z = base64.b64encode(zlib.compress(blob.encode(), 9)).decode()\n"
        "    parts = [z[i:i+" + str(CHUNK_BYTES) + "] for i in range(0, len(z), " + str(CHUNK_BYTES) + ")]\n"
        "    print('" + CHUNK_HEAD + " ' + str(len(parts)) + ' ' + str(len(blob)), flush=True)\n"
        "    for i, ch in enumerate(parts):\n"
        "        print('" + CHUNK_PREFIX + "' + str(i).zfill(4) + '|' + ch, flush=True)\n"
        "    time.sleep(25)\n"
        "    await database.disconnect()\n"
        "asyncio.run(main())\n"
    )


_CHUNK_RE = re.compile(r"CJ(\d{4})\|([A-Za-z0-9+/=]+)")
_HEAD_RE = re.compile(r"CJHEAD (\d+) (\d+)")


def decode_chunks(text: str) -> dict:
    """Reassemble the job's numbered chunks, in any order, duplicates allowed. Raises on a gap
    rather than decoding a prefix: a census over the chunks that happened to arrive would
    under-count silently."""
    heads = _HEAD_RE.findall(text)
    if not heads:
        raise ValueError("no CJHEAD line in the log -- the job did not reach its output stage")
    counts = {(int(n), int(size)) for n, size in heads}
    if len(counts) != 1:
        raise ValueError(f"log mixes output from more than one run: {sorted(counts)}")
    (total, size), = counts
    parts: Dict[int, str] = {}
    for idx, chunk in _CHUNK_RE.findall(text):
        i = int(idx)
        if i in parts and parts[i] != chunk:
            raise ValueError(f"chunk {i} appears twice with different content")
        parts[i] = chunk
    missing = [i for i in range(total) if i not in parts]
    if missing:
        raise ValueError(
            f"{len(missing)}/{total} chunks missing (first: {missing[:10]}) -- Cloud Logging lag; "
            "re-read the job's log with `gcloud logging read` and decode again"
        )
    blob = zlib.decompress(base64.b64decode("".join(parts[i] for i in range(total)))).decode()
    if len(blob) != size:
        raise ValueError(f"decoded {len(blob)} bytes, job reported {size}")
    return json.loads(blob)


# ------------------------------------------------------------------------------------------
# Stage 2: Rakuten
# ------------------------------------------------------------------------------------------

class RakutenClient:
    def __init__(self, token: str, client: Any):
        self._token = token
        self._client = client
        self._last = 0.0

    @staticmethod
    async def mint_token(client: Any, client_id: str, client_secret: str, account_id: str) -> str:
        """POST /token exactly as the developer portal's access-token guide shows: the base64 of
        client_id:client_secret as a Bearer "token-key", and the publisher site id as scope."""
        key = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = await client.post(
            RAKUTEN_API + "/token",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"scope": account_id},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Rakuten /token returned {r.status_code}: {r.text[:300]}")
        tok = (r.json() or {}).get("access_token")
        if not tok:
            raise RuntimeError("Rakuten /token response had no access_token")
        return tok

    async def get(self, path: str, params: Dict[str, Any]) -> dict:
        for attempt in range(5):
            wait = RAKUTEN_MIN_INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            r = await self._client.get(
                RAKUTEN_API + path, params=params, headers={"Authorization": f"Bearer {self._token}"}
            )
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(min(60, 5 * (attempt + 1)))
                continue
            if r.status_code == 401:
                raise RuntimeError(f"Rakuten {path}: 401 -- token expired or wrong scope (portal tokens last 60 min)")
            if r.status_code != 200:
                raise RuntimeError(f"Rakuten {path} returned {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"Rakuten {path}: still throttled after 5 attempts")

    async def paged(self, path: str, items_key: str, extra: Optional[Dict[str, Any]] = None) -> List[dict]:
        rows: List[dict] = []
        params: Dict[str, Any] = {"page": 1, "limit": RAKUTEN_PAGE_LIMIT, **(extra or {})}
        for _ in range(RAKUTEN_MAX_PAGES):
            body = await self.get(path, params)
            page = body.get(items_key) or []
            if isinstance(page, dict):
                page = [page]
            rows.extend(page)
            nxt = next_page_params(body, params, len(page))
            if nxt is None:
                return rows
            params = nxt
        raise RuntimeError(f"Rakuten {path}: more than {RAKUTEN_MAX_PAGES} pages; raise the cap deliberately")


def next_page_params(body: dict, params: Dict[str, Any], got: int) -> Optional[Dict[str, Any]]:
    """Follow the `next` link when there is one. The two endpoints spell the envelope
    differently in the docs (`_metadata._links` vs `metadata.links`), so both are read; with
    neither, fall back to page counting against `total`, then to "a short page is the last"."""
    meta = body.get("_metadata") or body.get("metadata") or {}
    links = meta.get("_links") or meta.get("links") or {}
    nxt = links.get("next")
    if nxt:
        q = parse_qs(urlparse(nxt).query)
        new = {k: v[0] for k, v in q.items()}
        if str(new.get("page")) == str(params.get("page")):
            return None
        return {**params, **new}
    if got == 0:
        return None
    total = meta.get("total")
    page, limit = int(params.get("page", 1)), int(params.get("limit", RAKUTEN_PAGE_LIMIT))
    if isinstance(total, int):
        return {**params, "page": page + 1} if page * limit < total else None
    return {**params, "page": page + 1} if got >= limit else None


async def run_rakuten(out_dir: Path) -> None:
    import httpx

    token = os.environ.get("RAKUTEN_ACCESS_TOKEN", "").strip()
    async with httpx.AsyncClient(timeout=60) as client:
        if not token:
            cid = os.environ.get("RAKUTEN_CLIENT_ID", "").strip()
            sec = os.environ.get("RAKUTEN_CLIENT_SECRET", "").strip()
            acct = os.environ.get("RAKUTEN_ACCOUNT_ID", "").strip()
            if not (cid and sec and acct):
                sys.exit(
                    "set RAKUTEN_ACCESS_TOKEN, or RAKUTEN_CLIENT_ID + RAKUTEN_CLIENT_SECRET + "
                    "RAKUTEN_ACCOUNT_ID (publisher site id)"
                )
            token = await RakutenClient.mint_token(client, cid, sec, acct)
        rk = RakutenClient(token, client)
        advertisers = await rk.paged("/v2/advertisers", "advertisers")
        partnerships = await rk.paged("/v1/partnerships", "partnerships")
        # A partnership only reaches a site through its advertiser's URL. Fetch the detail for
        # every partnered advertiser the list did not return (e.g. one now inactive).
        listed = {str(a.get("id")) for a in advertisers}
        for aid in sorted({str((p.get("advertiser") or {}).get("id")) for p in partnerships} - listed):
            if aid in ("", "None"):
                continue
            try:
                body = await rk.get(f"/v2/advertisers/{aid}", {})
            except RuntimeError as e:
                print(f"rakuten: advertiser {aid} detail unavailable: {e}")
                continue
            if body.get("advertiser"):
                advertisers.append(body["advertiser"])
    out = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "advertisers": advertisers,
        "partnerships": partnerships,
    }
    (out_dir / "rakuten.json").write_text(json.dumps(out, indent=1, default=str))
    by_status: Dict[str, int] = defaultdict(int)
    for p in partnerships:
        by_status[str(p.get("status"))] += 1
    print(f"rakuten: {len(advertisers)} advertisers, {len(partnerships)} partnerships {dict(by_status)}")
    no_url = sum(1 for a in advertisers if not site_key(a.get("url")))
    if no_url:
        print(f"rakuten: {no_url} advertisers carry no usable url and can only match by name")
    lost = unkeyed_partnerships(advertisers, partnerships)
    if lost:
        print(f"rakuten: {len(lost)} partnerships cannot be joined to a site: {lost[:20]}")


# ------------------------------------------------------------------------------------------
# Stage 3: storefront signals
# ------------------------------------------------------------------------------------------

MAX_REDIRECTS = 5


def is_public_hostname(host: str) -> bool:
    """A DNS name that is not localhost and not an IP literal. Hosts come from our own catalog, but
    a redirect chain is the site's to choose, so every hop is held to this."""
    import ipaddress

    h = (host or "").strip().lower().rstrip(".")
    if not h or "." not in h or h == "localhost" or h.endswith(".localhost") or h.endswith(".internal"):
        return False
    try:
        ipaddress.ip_address(h.strip("[]"))
        return False
    except ValueError:
        return True


async def fetch_public_https(client: Any, url: str, headers: Dict[str, str]) -> Any:
    """GET following redirects by hand: https only, public hostnames only, at most MAX_REDIRECTS."""
    from urllib.parse import urljoin

    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not is_public_hostname(parsed.hostname or ""):
            raise ValueError("redirect_to_non_public_or_non_https")
        r = await client.get(url, headers=headers)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            url = urljoin(str(r.url), r.headers["location"])
            continue
        return r
    raise ValueError("too_many_redirects")


async def run_signals(out_dir: Path, hosts: List[str], concurrency: int) -> None:
    import httpx

    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, dict] = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    async def probe(client: Any, host: str) -> None:
        async with sem:
            if not is_public_hostname(host):
                results[host] = {"status": None, "networks": [], "verdict": "skipped_not_public"}
                return
            try:
                r = await fetch_public_https(client, f"https://{host}/", headers)
                html = r.text[:2_000_000]
                final_host = normalize_host(str(r.url))
                # A tag found after a redirect to ANOTHER site is that site's, not this host's.
                same_site = site_key(final_host) == site_key(host)
                results[host] = {
                    "status": r.status_code,
                    "final_host": final_host,
                    "networks": detect_networks(html) if r.status_code < 400 and same_site else [],
                    # A 403/429/503 here is a bot wall, not a finding about the program.
                    "verdict": ("fetched" if same_site else "redirected_offsite")
                    if r.status_code < 400 else "unverifiable",
                }
            except Exception as e:  # transport failure = unverifiable, never "no program"
                results[host] = {"status": None, "networks": [], "verdict": "unverifiable",
                                 "error": type(e).__name__}

    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        await asyncio.gather(*(probe(client, h) for h in hosts))
    (out_dir / "signals.json").write_text(json.dumps(results, indent=1))
    found = sum(1 for v in results.values() if v["networks"])
    unv = sum(1 for v in results.values() if v["verdict"] == "unverifiable")
    print(f"signals: {len(results)} hosts probed, {found} with a network tag, {unv} unverifiable")


# ------------------------------------------------------------------------------------------
# Stage 4: report
# ------------------------------------------------------------------------------------------

def _rows(inv: dict, key: str) -> List[dict]:
    v = inv.get(key)
    if isinstance(v, str):
        raise ValueError(f"inventory query {key!r} failed in prod: {v}")
    return v or []


def build_host_table(inv: dict) -> Dict[str, dict]:
    """host -> products / serving / brands / markets / lanes, from the prod inventory."""
    hosts: Dict[str, dict] = defaultdict(
        lambda: {"products": 0, "serving": 0, "brands": defaultdict(lambda: [0, 0]),
                 "markets": defaultdict(int), "modes": defaultdict(int), "seeds": 0,
                 "seed_partner_types": defaultdict(int)}
    )
    for r in _rows(inv, "offer_pairs"):
        h = normalize_host(r["host"])
        hosts[h]["products"] += int(r["n"])
        hosts[h]["serving"] += int(r["se"])
        b = hosts[h]["brands"][r["nb"] or ""]
        b[0] += int(r["n"])
        b[1] += int(r["se"])
        hosts[h]["markets"][r.get("mk") or ""] += int(r["n"])
    for r in _rows(inv, "product_only_pairs"):
        h = normalize_host(r["host"])
        hosts[h]["products"] += int(r["n"])
        hosts[h]["serving"] += int(r["se"])
        b = hosts[h]["brands"][r["nb"] or ""]
        b[0] += int(r["n"])
        b[1] += int(r["se"])
    for r in _rows(inv, "offer_modes"):
        h = normalize_host(r["host"])
        hosts[h]["modes"]["/".join(x for x in (r["om"], r["ot"], r["ch"]) if x) or "?"] += int(r["n"])
    for r in _rows(inv, "seeds"):
        h = normalize_host(r["host"])
        hosts[h]["seeds"] += int(r["n"])
        hosts[h]["markets"][r.get("mk") or ""] += 0
        hosts[h]["seed_partner_types"][r["pt"] or "none"] += int(r["n"])
    distinct = inv.get("host_products")
    if isinstance(distinct, list):
        # Exact per-host counts replace the market-split sums; product-only rows are added back.
        exact: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for r in distinct:
            e = exact[normalize_host(r["host"])]
            e[0] += int(r["n"])
            e[1] += int(r["se"])
        for r in _rows(inv, "product_only_pairs"):
            e = exact[normalize_host(r["host"])]
            e[0] += int(r["n"])
            e[1] += int(r["se"])
        for h, (n, se) in exact.items():
            hosts[h]["products"], hosts[h]["serving"] = n, se
    hosts.pop("", None)
    # Per-host products/serving are distinct counts when the inventory carries host_products (an
    # older inventory falls back to sums that count a product once per market). Brand counts are
    # per (host, brand, market) and a brand's total across hosts can exceed its distinct products.
    return hosts


def build_report(inv: dict, rakuten: Optional[dict], signals: Optional[dict]) -> dict:
    hosts = build_host_table(inv)
    idx = index_rakuten(rakuten["advertisers"], rakuten["partnerships"]) if rakuten else {}
    names: Dict[str, List[dict]] = defaultdict(list)
    for progs in idx.values():
        for p in progs:
            names[normalize_name(p["name"])].append(p)

    host_rows: List[dict] = []
    for host, h in hosts.items():
        key = site_key(host)
        market = max(h["markets"], key=h["markets"].get) if h["markets"] else ""
        prog = pick_program(idx.get(key, []), market)
        bucket = prog["bucket"] if prog else ("not_on_rakuten" if rakuten else "rakuten_not_fetched")
        sig = (signals or {}).get(host) or {}
        brands = sorted(h["brands"].items(), key=lambda kv: -kv[1][0])
        total = sum(v[0] for _, v in brands) or 1
        # Share is taken over normalised names so "tarte" and "tarte cosmetics" on
        # tartecosmetics.com count as one brand, not a 71% retailer.
        by_norm: Dict[str, int] = defaultdict(int)
        for b, v in brands:
            by_norm[normalize_name(b) or b] += v[0]
        top_share = max(by_norm.values()) / total if brands else 0
        kind = "brand_store" if brands and top_share >= 0.8 else ("retailer" if brands else "seed_only")
        # A name candidate is only offered for a brand store Rakuten did not match by domain:
        # the advertiser may list a different URL (regional site, old domain).
        candidates = []
        if not prog and kind == "brand_store" and brands[0][0]:
            candidates = [f"{p['name']} (MID {p['mid']}, {p['bucket']})"
                          for p in names.get(normalize_name(brands[0][0]), [])]
        host_rows.append(
            {
                "host": host,
                "site_key": key,
                "kind": kind,
                "market": market,
                "products": h["products"],
                "serving": h["serving"],
                "active_seeds": h["seeds"],
                "brand_count": len([b for b in h["brands"] if b]),
                "top_brands": "; ".join(f"{b or '?'}:{v[0]}" for b, v in brands[:5]),
                "lanes": "; ".join(f"{k}:{v}" for k, v in sorted(h["modes"].items(), key=lambda kv: -kv[1])),
                "seed_partner_types": "; ".join(f"{k}:{v}" for k, v in h["seed_partner_types"].items()),
                "rakuten_bucket": bucket,
                "rakuten_mid": prog["mid"] if prog else "",
                "rakuten_advertiser": prog["name"] if prog else "",
                "rakuten_network": RAKUTEN_NETWORKS.get(prog.get("network"), prog.get("network")) if prog else "",
                "rakuten_status": (prog or {}).get("partnership_status") or "",
                "deep_links": "" if not prog or prog.get("deep_links") is None else str(prog["deep_links"]).lower(),
                "other_programs_on_site": len(idx.get(key, [])) - (1 if prog else 0),
                "rakuten_name_candidates": " | ".join(candidates),
                "signal_networks": ",".join(sig.get("networks") or []),
                "signal_verdict": sig.get("verdict", "not_probed"),
                "next_action": next_action(bucket, prog, sig.get("networks") or [])
                if rakuten else "fetch Rakuten first",
            }
        )
    host_rows.sort(key=lambda r: (-r["serving"], -r["products"], r["host"]))

    bucket_by_host = {r["host"]: r["rakuten_bucket"] for r in host_rows}
    brand_rows: Dict[str, dict] = {}
    for host, h in hosts.items():
        for b, (n, se) in h["brands"].items():
            if not b:
                continue
            row = brand_rows.setdefault(b, {"brand": b, "products": 0, "serving": 0,
                                            "serving_approved": 0, "serving_applyable": 0,
                                            "hosts": 0, "approved_hosts": []})
            row["products"] += n
            row["serving"] += se
            row["hosts"] += 1
            if bucket_by_host.get(host) == "approved":
                row["serving_approved"] += se
                row["approved_hosts"].append(host)
            elif bucket_by_host.get(host) in ("pending", "not_applied", "declined_temporary"):
                row["serving_applyable"] += se
    brands_out = sorted(brand_rows.values(), key=lambda r: (-r["serving"], r["brand"]))
    for r in brands_out:
        r["approved_hosts"] = ",".join(sorted(r["approved_hosts"]))

    tot_serving = sum(r["serving"] for r in host_rows)
    by_bucket: Dict[str, Dict[str, int]] = defaultdict(lambda: {"hosts": 0, "serving": 0, "products": 0})
    for r in host_rows:
        b = by_bucket[r["rakuten_bucket"]]
        b["hosts"] += 1
        b["serving"] += r["serving"]
        b["products"] += r["products"]
    return {
        "totals": (_rows(inv, "totals") or [{}])[0],
        "serving_total": tot_serving,
        "by_bucket": dict(by_bucket),
        "hosts": host_rows,
        "brands": brands_out,
        "rakuten_fetched_at": (rakuten or {}).get("fetched_at"),
    }


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def render_summary(rep: dict) -> str:
    tot = rep["serving_total"]
    lines = [
        "# Affiliate coverage census",
        "",
        f"Rakuten data fetched: {rep['rakuten_fetched_at'] or 'NOT FETCHED'}",
        f"Prod totals: {json.dumps(rep['totals'], default=str)}",
        "",
        "Unit = the host the buyer checks out on. Referral lanes only: a UCP/Reap agentic",
        "checkout fires no pixel, so no affiliate network credits it whatever the bucket says.",
        "",
        "## Serving products by Rakuten status of their checkout host",
        "",
        "| bucket | hosts | serving products | share of serving |",
        "|---|---:|---:|---:|",
    ]
    order = BUCKET_ORDER + sorted(k for k in rep["by_bucket"] if k not in BUCKET_ORDER)
    for k in order:
        v = rep["by_bucket"].get(k)
        if v:
            lines.append(f"| {k} | {v['hosts']} | {v['serving']} | {_pct(v['serving'], tot)} |")
    queue = [r for r in rep["hosts"] if r["rakuten_bucket"] in ("not_applied", "declined_temporary")]
    lines += ["", f"## Apply queue ({len(queue)} hosts, by serving products)", "",
              "| host | serving | MID | advertiser | network | deep links | top brands |",
              "|---|---:|---:|---|---|---|---|"]
    for r in queue[:50]:
        lines.append(f"| {r['host']} | {r['serving']} | {r['rakuten_mid']} | {r['rakuten_advertiser']} | "
                     f"{r['rakuten_network']} | {r['deep_links']} | {r['top_brands']} |")
    uncovered = [r for r in rep["hosts"] if r["rakuten_bucket"] == "not_on_rakuten" and r["serving"]]
    lines += ["", f"## Not on Rakuten, serving ({len(uncovered)} hosts)", "",
              "| host | kind | serving | other-network signal | name candidates | top brands |",
              "|---|---|---:|---|---|---|"]
    for r in uncovered[:50]:
        sig = r["signal_networks"] or ("(unverifiable)" if r["signal_verdict"] == "unverifiable" else "")
        lines.append(f"| {r['host']} | {r['kind']} | {r['serving']} | {sig} | "
                     f"{r['rakuten_name_candidates']} | {r['top_brands']} |")
    lines += ["", "Name candidates are unconfirmed (lossy name match) and are NOT counted as coverage.",
              "A missing network signal proves nothing: most tags load through a tag manager.", ""]
    return "\n".join(lines)


def _write_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _load(path: Path) -> Optional[dict]:
    return json.loads(path.read_text()) if path.exists() else None


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prod-program", help="print the read-only prod inventory program")
    d = sub.add_parser("decode", help="decode the prod job log into inventory.json")
    d.add_argument("--log", required=True, type=Path)
    for name in ("decode", "rakuten", "signals", "report"):
        p = d if name == "decode" else sub.add_parser(name)
        p.add_argument("--out-dir", required=True, type=Path)
        if name == "signals":
            p.add_argument("--concurrency", type=int, default=6)
            p.add_argument("--include-covered", action="store_true",
                           help="also probe hosts Rakuten already covers")
            p.add_argument("--min-serving", type=int, default=1)
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "prod-program":
        sys.stdout.write(build_prod_program())
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.cmd == "decode":
        inv = decode_chunks(args.log.read_text(errors="replace"))
        (args.out_dir / "inventory.json").write_text(json.dumps(inv, indent=1, default=str))
        errs = {k: v for k, v in inv.items() if isinstance(v, str)}
        print(f"inventory: {', '.join(f'{k}={len(v)}' for k, v in inv.items() if isinstance(v, list))}")
        if errs:
            print(f"FAILED QUERIES: {errs}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "rakuten":
        asyncio.run(run_rakuten(args.out_dir))
        return 0
    inv = _load(args.out_dir / "inventory.json")
    if inv is None:
        sys.exit("inventory.json missing -- run the prod program and `decode` first")
    if args.cmd == "signals":
        rakuten = _load(args.out_dir / "rakuten.json")
        rep = build_report(inv, rakuten, None)
        hosts = [r["host"] for r in rep["hosts"] if r["serving"] >= args.min_serving
                 and (args.include_covered or r["rakuten_bucket"] in ("not_on_rakuten", "rakuten_not_fetched"))]
        asyncio.run(run_signals(args.out_dir, hosts, args.concurrency))
        return 0
    rep = build_report(inv, _load(args.out_dir / "rakuten.json"), _load(args.out_dir / "signals.json"))
    _write_csv(args.out_dir / "coverage.csv", rep["hosts"])
    _write_csv(args.out_dir / "brands.csv", rep["brands"])
    (args.out_dir / "coverage.json").write_text(json.dumps(rep, indent=1, default=str))
    summary = render_summary(rep)
    (args.out_dir / "summary.md").write_text(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
