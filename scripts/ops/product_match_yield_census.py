"""How many products would the deterministic cross-merchant tiers join? A read-only census.

WHY. Census v2 (2026-10-16): every one of 12,477 US / 3,572 SG products with retailer offers has
exactly ONE retailer host -- each retailer URL mints its own listing and content_key, and only an
exact key ever joins an existing product. Exact brand+title already finds 1,270 split groups and
GTIN 620. This census sizes the phase-1 deterministic tiers with the shared normalizer
(services/product_match_normalizer) BEFORE the cross_merchant_same_size strategy is built.

TIERS (per market, US and SG; a product counts in each market it has a live offer in):
  T0a  valid, non-restricted, non-polluted GTIN equal + canonical brand equal + parsed size equal
       (a GTIN is POLLUTED when the market's listings carry it on >1 size or >1 shade).
  T0b  canonical brand + catalog_identity.normalize_title equal (exact title, digits kept).
  T0c  match_key_t0c: canonical brand + title-core token set + (size, unit, pack count) equal.
Every tier then applies the hard vetoes: a key group whose members disagree on conflict tokens
(EDT/EDP, SPF/PA, version, format, refill/tester/mini/set/multipack ...) or shade is VETOED and
split by (conflicts, shade). A group JOINS products when it spans >= 2 content_keys (a product
without one counts as its own). A joining group is DEFERRED (phase 2) when a member product's
variants (catalog_skus titles) or a member content_key's listings carry >1 size: offers.resolve,
agent_pdp_view and the gateway sibling path have no size awareness yet.

Not modelled: the same-size price-ratio veto (no prices are read), star topology (groups are
counted as found, not anchored), and agent tiers.

SAFETY. ONE transaction, READ ONLY, `SET LOCAL statement_timeout = '60s'`, three SELECTs (the
third, variant titles, runs in a savepoint and is optional). Everything else is computed in
Python, off the database. pivota-pg has 2 vCPU and serves live search: start it only while CPU
is under 35%, never 05:15-06:15Z, 10:30-11:20Z, or during a drain or index build.

RUN IT (after this file and services/product_match_normalizer.py are in the deployed image):

    bash scripts/ops/run_oneoff_job.sh scripts/ops/product_match_yield_census.py

OUTPUT. JSON lines only, each `{"census": "product_match_yield", "section": ..., ...}`. Cloud
Run logs a bare JSON line as jsonPayload, so run_oneoff_job.sh's text fetch shows those lines
BLANK. Read them back with:

    gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="<job>"
      AND jsonPayload.census="product_match_yield"' --format=json --project=pivota-prod

The program text is kept free of the at-sign (run_oneoff_job.sh's --args delimiter choice).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, Hashable, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:  # python -c has no __file__; /app is the image's repo root
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

from services.product_match_normalizer import (  # noqa: E402
    canonical_brand,
    conflict_family,
    exact_title_key,
    gtin_canonical,
    gtin_restriction,
    parse_size,
    size_key,
    title_core,
)

CENSUS = "product_match_yield"
MARKETS: Tuple[str, ...] = ("US", "SG")
TOP_BRANDS = 30
SAMPLE_GROUPS = 15
TITLE_CHARS = 140

# A product the agent can serve: signed, live, not suppressed.
_PRODUCT_FILTER = """
    p.pivota_signature_id IS NOT NULL AND p.suppressed_at IS NULL AND p.sync_status = 'live'
"""
PRODUCTS_SQL = f"""
SELECT p.product_key, p.brand, p.title, p.gtin, p.content_key, p.pivota_signature_id, p.source_system
FROM catalog_products p
WHERE {_PRODUCT_FILTER}
  AND EXISTS (SELECT 1 FROM catalog_offers o
              WHERE o.product_key = p.product_key AND o.suppressed_at IS NULL
                AND o.market = ANY($1::text[]))
"""
# One row per (product, market, host). The host is the offer's source_domain, else the host of
# its destination URL.
HOSTS_SQL = f"""
SELECT o.product_key, o.market,
       lower(coalesce(nullif(btrim(o.source_domain), ''),
                      substring(o.offer_payload->>'destination_url' from '^[A-Za-z][A-Za-z0-9+.-]*://([^/:?#]+)'))) AS host,
       bool_or(o.offer_type = 'brand_direct') AS brand_direct
FROM catalog_offers o
JOIN catalog_products p ON p.product_key = o.product_key
WHERE {_PRODUCT_FILTER} AND o.suppressed_at IS NULL AND o.market = ANY($1::text[])
GROUP BY 1, 2, 3
"""
SKU_TITLES_SQL = f"""
SELECT s.product_key, array_agg(DISTINCT s.title) AS titles
FROM catalog_skus s
JOIN catalog_products p ON p.product_key = s.product_key
WHERE {_PRODUCT_FILTER} AND s.suppressed_at IS NULL
GROUP BY 1
"""


def _line(section: str, **fields: Any) -> Dict[str, Any]:
    return {"census": CENSUS, "section": section, **fields}


def fold_host(host: Optional[str]) -> str:
    """Lowercase, leading "www." dropped (www/apex are one store)."""
    h = (host or "").strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


class _Listing:
    """One product as seen in one market, with every normalizer signal precomputed."""

    __slots__ = (
        "product_key", "brand", "title", "content_key", "ck", "hosts", "brand_direct", "cb", "tc",
        "size_key", "gtin", "gtin_restriction", "exact_key", "t0c_key", "signature", "variant_sizes",
    )

    def __init__(self, row: Mapping[str, Any], hosts: Set[str], brand_direct: bool,
                 sku_titles: Sequence[str]) -> None:
        self.product_key = str(row["product_key"])
        self.brand = row.get("brand")
        self.title = row.get("title")
        self.content_key = row.get("content_key")
        self.ck = self.content_key or f"pk:{self.product_key}"
        self.hosts = hosts
        self.brand_direct = brand_direct
        self.cb = canonical_brand(self.brand)
        self.tc = title_core(self.brand, self.title)
        self.size_key = size_key(self.tc.size)
        self.gtin = gtin_canonical(row.get("gtin"))
        self.gtin_restriction = gtin_restriction(self.gtin) if self.gtin else None
        self.exact_key = exact_title_key(self.brand, self.title)
        self.t0c_key = (self.cb, self.tc.tokens, self.size_key) if (
            self.cb and self.tc.tokens and self.size_key is not None) else None
        self.signature = (self.tc.conflicts, self.tc.shade_tokens)
        sizes = {self.size_key} if self.size_key else set()
        for t in sku_titles or ():
            if not t or str(t).strip().lower() == "default title":
                continue
            sk = size_key(parse_size(t))
            if sk:
                sizes.add(sk)
        self.variant_sizes = sizes


def _veto_tokens(signatures: Iterable[Tuple[frozenset, frozenset]]) -> Tuple[Set[str], Set[str]]:
    """Tokens that differ across a group's signatures, split into one-sided (some member lacks
    the token's whole family) and two-sided (members state different values of one family)."""
    sigs = list(signatures)
    tokens: Set[str] = set()
    union = set().union(*(c for c, _s in sigs))
    for tok in union:
        if not all(tok in c for c, _s in sigs):
            tokens.add(tok)
    if len({s for _c, s in sigs}) > 1:
        tokens.add("shade")
    one, two = set(), set()
    for tok in tokens:
        fam = "shade" if tok == "shade" else conflict_family(tok)
        if tok == "shade":
            stated = [s for _c, s in sigs if s]
            (two if len(set(stated)) > 1 else one).add(tok)
            continue
        fam_values = [frozenset(t for t in c if conflict_family(t) == fam) for c, _s in sigs]
        (one if any(not v for v in fam_values) else two).add(tok)
    return one, two


def _sample(group: Sequence[_Listing]) -> List[Dict[str, Any]]:
    return [
        {"pk": g.product_key[:60], "ck": (g.content_key or "")[:12], "hosts": sorted(g.hosts)[:3],
         "brand": (g.brand or "")[:40], "title": (g.title or "")[:TITLE_CHARS],
         "size": list(g.size_key) if g.size_key else None, "conflicts": sorted(g.tc.conflicts),
         "shade": g.tc.shade}
        for g in sorted(group, key=lambda x: x.product_key)[:6]
    ]


def evaluate_tier(name: str, listings: Sequence[_Listing], key_fn: Callable[[_Listing], Optional[Hashable]],
                  ck_sizes: Mapping[str, Set[Any]], *, samples: int = SAMPLE_GROUPS) -> Dict[str, Any]:
    """Group `listings` by `key_fn`, apply the signature veto, count joins. Pure."""
    by_key: Dict[Hashable, List[_Listing]] = defaultdict(list)
    for li in listings:
        k = key_fn(li)
        if k is not None:
            by_key[k].append(li)
    out = Counter()
    veto_by_token: Counter = Counter()
    veto_one_sided: Counter = Counter()
    veto_two_sided: Counter = Counter()
    joinable: List[List[_Listing]] = []
    vetoed_samples: List[Dict[str, Any]] = []
    deferred_samples: List[Dict[str, Any]] = []
    joined_products: Set[str] = set()
    for _key, members in by_key.items():
        if len(members) < 2:
            continue
        out["key_groups"] += 1
        if len({m.ck for m in members}) < 2:
            out["key_groups_already_one_ck"] += 1
            continue
        out["split_key_groups"] += 1
        parts: Dict[Any, List[_Listing]] = defaultdict(list)
        for m in members:
            parts[m.signature].append(m)
        if len(parts) > 1:
            out["vetoed_key_groups"] += 1
            one, two = _veto_tokens(parts.keys())
            for tok in one | two:
                veto_by_token[tok] += 1
            for tok in one:
                veto_one_sided[tok] += 1
            for tok in two:
                veto_two_sided[tok] += 1
            if len(vetoed_samples) < samples:
                vetoed_samples.append({"tokens": sorted(one | two), "members": _sample(members)})
        for sub in parts.values():
            if len({m.ck for m in sub}) < 2:
                continue
            out["accepted_groups"] += 1
            hosts = set().union(*(m.hosts for m in sub))
            multi_host = len(hosts) >= 2
            out["accepted_multi_host_groups"] += int(multi_host)
            out["accepted_products"] += len(sub)
            deferred = any(len(m.variant_sizes) > 1 for m in sub) or any(len(ck_sizes.get(m.ck, ())) > 1 for m in sub)
            if deferred:
                out["deferred_multi_size_groups"] += 1
                out["deferred_multi_size_multi_host_groups"] += int(multi_host)
                if len(deferred_samples) < samples:
                    deferred_samples.append({"members": _sample(sub)})
                continue
            joinable.append(sub)
            out["joinable_groups"] += 1
            out["joinable_products"] += len(sub)
            out["joinable_multi_host_groups"] += int(multi_host)
            out["joinable_with_brand_direct"] += int(any(m.brand_direct for m in sub))
            joined_products.update(m.product_key for m in sub)
    accepted_samples = [{"members": _sample(g)} for g in sorted(joinable, key=lambda g: g[0].product_key)[:samples]]
    return {
        "tier": name,
        "counts": dict(out),
        "joined_products_distinct": len(joined_products),
        "veto_groups_by_token": dict(veto_by_token.most_common()),
        "veto_one_sided_by_token": dict(veto_one_sided.most_common()),
        "veto_two_sided_by_token": dict(veto_two_sided.most_common()),
        "_joinable": joinable,
        "samples": {"joinable": accepted_samples, "vetoed": vetoed_samples, "deferred": deferred_samples},
    }


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_listings(products: Sequence[Mapping[str, Any]], hosts: Sequence[Mapping[str, Any]],
                   sku_titles: Mapping[str, Sequence[str]], market: str) -> List[_Listing]:
    by_pk: Dict[str, Set[str]] = defaultdict(set)
    bd: Dict[str, bool] = defaultdict(bool)
    for h in hosts:
        if h.get("market") != market:
            continue
        pk = str(h["product_key"])
        host = fold_host(h.get("host"))
        if host:
            by_pk[pk].add(host)
        else:
            by_pk.setdefault(pk, set())
        bd[pk] = bd[pk] or bool(h.get("brand_direct"))
    return [
        _Listing(p, by_pk[str(p["product_key"])], bd[str(p["product_key"])],
                 sku_titles.get(str(p["product_key"]), ()))
        for p in products if str(p["product_key"]) in by_pk
    ]


def census_market(market: str, listings: Sequence[_Listing], *, samples: int = SAMPLE_GROUPS) -> List[Dict[str, Any]]:
    """Every JSON line for one market. Pure: no I/O."""
    lines: List[Dict[str, Any]] = []
    host_hist = Counter(min(len(li.hosts), 3) for li in listings)
    ck_hosts: Dict[str, Set[str]] = defaultdict(set)
    ck_sizes: Dict[str, Set[Any]] = defaultdict(set)
    for li in listings:
        ck_hosts[li.ck].update(li.hosts)
        ck_sizes[li.ck].update(li.variant_sizes)  # title size + every variant's size
    # GTIN pollution: one GTIN on >1 size or >1 stated shade within the market.
    gtin_sizes: Dict[str, Set[Any]] = defaultdict(set)
    gtin_shades: Dict[str, Set[Any]] = defaultdict(set)
    gtin_brands: Dict[str, Set[str]] = defaultdict(set)
    for li in listings:
        if li.gtin and not li.gtin_restriction:
            if li.size_key:
                gtin_sizes[li.gtin].add(li.size_key)
            if li.tc.shade_tokens:
                gtin_shades[li.gtin].add(li.tc.shade_tokens)
            gtin_brands[li.gtin].add(li.cb)
    polluted = {g for g in gtin_brands if len(gtin_sizes[g]) > 1 or len(gtin_shades[g]) > 1}
    restriction_hist = Counter(li.gtin_restriction for li in listings if li.gtin and li.gtin_restriction)
    lines.append(_line(
        "inputs", market=market,
        products=len(listings),
        products_by_host_count={"1": host_hist.get(1, 0), "2": host_hist.get(2, 0), "3+": host_hist.get(3, 0),
                                "0": host_hist.get(0, 0)},
        products_with_brand_direct_offer=sum(1 for li in listings if li.brand_direct),
        products_with_primary_size=sum(1 for li in listings if li.size_key),
        products_with_multi_size_title=sum(1 for li in listings if li.size_key is None and len(li.tc.size.sizes) > 1),
        products_with_multi_size_variants=sum(1 for li in listings if len(li.variant_sizes) > 1),
        products_with_t0c_key=sum(1 for li in listings if li.t0c_key),
        products_with_conflicts=sum(1 for li in listings if li.tc.conflicts),
        products_with_shade=sum(1 for li in listings if li.tc.shade_tokens),
        products_with_valid_gtin=sum(1 for li in listings if li.gtin),
        gtin_restricted_by_reason=dict(restriction_hist.most_common()),
        gtins_polluted=len(polluted),
        gtins_on_two_brands=sum(1 for b in gtin_brands.values() if len(b) > 1),
        content_keys=len(ck_hosts),
        content_keys_multi_host_today=sum(1 for h in ck_hosts.values() if len(h) >= 2),
        conflict_tokens_top=dict(Counter(t for li in listings for t in li.tc.conflicts).most_common(40)),
    ))

    def t0a(li: _Listing) -> Optional[Hashable]:
        if not li.gtin or li.gtin_restriction or li.gtin in polluted or not li.cb or li.size_key is None:
            return None
        return (li.gtin, li.cb, li.size_key)

    def t0a_size_missing(li: _Listing) -> Optional[Hashable]:
        if not li.gtin or li.gtin_restriction or li.gtin in polluted or not li.cb:
            return None
        return (li.gtin, li.cb)

    tiers = [
        evaluate_tier("T0a", listings, t0a, ck_sizes, samples=samples),
        evaluate_tier("T0b", listings, lambda li: li.exact_key, ck_sizes, samples=samples),
        evaluate_tier("T0c", listings, lambda li: li.t0c_key, ck_sizes, samples=samples),
    ]
    # Informational: GTIN + brand without the size requirement (how much T0a's strict size costs).
    gtin_any = evaluate_tier("T0a_size_not_required", listings, t0a_size_missing, ck_sizes, samples=0)

    uf = _UnionFind()
    for tier in tiers:
        for group in tier["_joinable"]:
            for m in group[1:]:
                uf.union(group[0].product_key, m.product_key)
    by_pk = {li.product_key: li for li in listings}
    components: Dict[str, List[_Listing]] = defaultdict(list)
    for pk in list(uf.parent):
        components[uf.find(pk)].append(by_pk[pk])
    any_groups = [c for c in components.values() if len({m.ck for m in c}) >= 2]
    any_multi_host = [c for c in any_groups if len(set().union(*(m.hosts for m in c))) >= 2]

    for tier in tiers + [gtin_any]:
        tier = dict(tier)
        tier.pop("_joinable")
        sample_block = tier.pop("samples")
        lines.append(_line("tier", market=market, **tier))
        if any(sample_block.values()):
            lines.append(_line("tier_samples", market=market, tier=tier["tier"], **sample_block))
    lines.append(_line(
        "any_tier", market=market,
        groups=len(any_groups),
        products=sum(len(c) for c in any_groups),
        multi_host_groups=len(any_multi_host),
        note="union of joinable T0a/T0b/T0c groups (transitive: an upper bound on star-anchored joins)",
    ))
    brand_rows: Dict[str, Counter] = defaultdict(Counter)
    for c in any_multi_host:
        brand_rows[c[0].cb]["any_multi_host_groups"] += 1
        brand_rows[c[0].cb]["any_products"] += len(c)
    for tier in tiers:
        for group in tier["_joinable"]:
            if len(set().union(*(m.hosts for m in group))) >= 2:
                brand_rows[group[0].cb][f"{tier['tier']}_multi_host_groups"] += 1
    top = sorted(brand_rows.items(), key=lambda kv: (-kv[1]["any_multi_host_groups"], kv[0]))[:TOP_BRANDS]
    lines.append(_line("top_brands", market=market,
                       brands=[{"brand": b, **dict(c)} for b, c in top]))
    return lines


def compute_census(products: Sequence[Mapping[str, Any]], hosts: Sequence[Mapping[str, Any]],
                   sku_titles: Mapping[str, Sequence[str]], markets: Sequence[str] = MARKETS,
                   *, samples: int = SAMPLE_GROUPS) -> List[Dict[str, Any]]:
    """All census lines for `markets` from already-loaded rows. Pure."""
    lines: List[Dict[str, Any]] = [_line(
        "loaded", products=len(products), host_rows=len(hosts), products_with_sku_titles=len(sku_titles),
    )]
    for market in markets:
        lines.extend(census_market(market, build_listings(products, hosts, sku_titles, market), samples=samples))
    return lines


async def load(conn: Any, markets: Sequence[str] = MARKETS) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, List[str]], Optional[str]]:
    """The only database access: one READ ONLY transaction, three SELECTs."""
    sku_titles: Dict[str, List[str]] = {}
    sku_error: Optional[str] = None
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        products = [dict(r) for r in await conn.fetch(PRODUCTS_SQL, list(markets))]
        hosts = [dict(r) for r in await conn.fetch(HOSTS_SQL, list(markets))]
        try:
            async with conn.transaction():  # savepoint: variant titles are optional
                for r in await conn.fetch(SKU_TITLES_SQL):
                    sku_titles[str(r["product_key"])] = [t for t in (r["titles"] or []) if t]
        except Exception as exc:  # noqa: BLE001 - deferral falls back to title sizes only
            sku_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return products, hosts, sku_titles, sku_error


async def _main() -> int:
    import asyncpg

    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        products, hosts, sku_titles, sku_error = await load(conn)
    finally:
        await conn.close()
    if sku_error:
        print(json.dumps(_line("warning", sku_titles_skipped=sku_error)), flush=True)
    for line in compute_census(products, hosts, sku_titles):
        print(json.dumps(line, default=str, separators=(",", ":"), ensure_ascii=False), flush=True)
    print(json.dumps(_line("done")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
