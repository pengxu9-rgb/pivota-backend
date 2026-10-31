"""READ-ONLY prod dry run of the refresh's catalogue price lane, before EXTERNAL_REFERRAL_REFRESH_CATALOGUE_ENABLED.

For the TOP_HOSTS hosts with the most active seeds whose URL their own catalogue prices, it reads each
host's catalogue with the lane's own reader (`services.external_seed_catalogue_refresh.read_host_catalogue`:
/meta.json, paged /products.json, /cart.js only when a page carries no cart_currency cookie) and plans every
seed on it with the lane's own rule (`seed_catalogue_read`). Then, per host:
  * catalogue readable? and the currency decision: base (/meta.json), the pages' cookies, /cart.js, the proof
    or the refusal;
  * seeds matched (priced off the catalogue) and why the others keep their page read;
  * price agreement with each matched seed's stored price, the latest page read's: agree, moved (within 2%,
    within 10%, more), in another currency (the refresh would refuse it: skipped_currency_mismatch), none
    stored; and whether the variant reconcile (`routes.employee_products._reconcile_seed_variants_with_read`,
    pure) would re-read every stored variant, which is what stamps the row fresh.
And the cost: origin requests made (the pacer's count) per seed matched, against the page read's ~1.3.
It writes nothing: no seed, no snapshot, no offer. The database is read in READ ONLY transactions with a
statement timeout and closed before the first fetch.

THE FETCHES LEAVE FROM THE CRAWL EGRESS ONLY, ONE HOST AT A TIME, ON ONE GLOBAL PACER: PLAN_RPS (default 0.5,
the refresh's rate, clamped like the refresh's), PLAN_BUDGET_SECONDS (default 1800) after which no request
starts, the IP breaker armed as in the refresh (a first trip pauses and halves the rate, a second ends the
run). The crawl NAT is shared by the refresh, the destination sweep and the purchasability sweep: launch it
when no refresh or sweep execution is running (gcloud run jobs executions list --job external-referral-refresh
--region us-west1 --limit 3), and it refuses 03:00-06:30Z itself.

It needs the MERGED image: the lane is imported, never restated. No at-sign anywhere in this file:
run_oneoff_job.sh picks its --args delimiter from a fixed set.

Launch (Peng; pg CPU gate first):
  SUBNET=pivota-crawl ENV_VARS=PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,CATALOGUE_PLAN_EGRESS=pivota-crawl,TOP_HOSTS=20 \\
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/plan_shopify_catalogue_refresh.py)"

Output:
  * `GATE_OPEN` once every precondition holds (production env, the egress stated, outside 03:00-06:30Z, a
    numeric statement timeout, the lane importable, a read-only transaction, the table present), else
    `GATE_CLOSED <reason>` and exit 3 before any query or fetch;
  * then only `SCR_PLAN <kind> {json}` lines: `hosts` (the plan), one `host` per host read, `summary`,
    `pacer` (requests, waits, breaker);
  * last, `SCR_PLAN DONE lines=N`, N = the SCR_PLAN lines before it. A run that stops early has none.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# `python scripts/ops/<this>.py` puts scripts/ops/ on sys.path, and `python -c` has no __file__.
# /app is the image's repo root.
try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

PREFIX = "SCR_PLAN"
CHUNK = 500
SAMPLE = 5
_lines = {"n": 0}
#: The destination sweep (03:15Z) and the nightly refresh (05:15Z) own the crawl NAT then.
QUIET_START_HOUR, QUIET_END = 3, (6, 30)

ACTIVE_DESTINATIONS_SQL = """
SELECT id, destination_url FROM external_product_seeds WHERE status = 'active' AND destination_url IS NOT NULL
"""

SEED_COLUMNS_SQL = """
SELECT id, market, destination_url, canonical_url, price_amount, price_currency, availability, last_crawled_at,
       seed_data -> 'variants' AS variants, seed_data -> 'snapshot' -> 'variants' AS snapshot_variants,
       seed_data ->> 'title' AS title
FROM external_product_seeds WHERE id = ANY($1::text[])
"""


def _closed(reason):
    print("GATE_CLOSED " + reason, flush=True)
    sys.exit(3)


def emit(kind, payload):
    print(f"{PREFIX} {kind} " + json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True), flush=True)
    _lines["n"] += 1


def _bump(counter, key, n=1):
    counter[key] = counter.get(key, 0) + n


def _now_utc():
    return datetime.now(timezone.utc)


def in_quiet_window(now):
    minutes = now.hour * 60 + now.minute
    return QUIET_START_HOUR * 60 <= minutes < QUIET_END[0] * 60 + QUIET_END[1]


def plan_hosts(rows, top_hosts):
    """{host: [seed ids]} for the `top_hosts` hosts with the most catalogue-shaped active seeds."""
    from services import crawl_politeness
    from services.external_seed_catalogue_refresh import catalogue_handle

    by_host = {}
    for row in rows:
        dest = str(row["destination_url"] or "")
        host = crawl_politeness.host_of(dest)
        if host and catalogue_handle(dest):
            by_host.setdefault(host, []).append(str(row["id"]))
    ranked = sorted(by_host.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top_hosts]
    return dict(ranked)


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def compare_seed(seed, read):
    """How the catalogue's reading of one seed compares to what it stores (the latest page read)."""
    from routes.employee_products import _reconcile_seed_variants_with_read

    snap = read.snapshot
    stored_amount = float(seed["price_amount"]) if seed.get("price_amount") is not None else None
    stored_cur = str(seed.get("price_currency") or "").strip().upper() or None
    out = {"seed_id": seed["id"], "stored": [stored_amount, stored_cur], "catalogue": [snap.price_amount, snap.price_currency]}
    if snap.price_amount is None:
        out["price"] = "catalogue_price_unread"
    elif stored_cur and stored_cur != snap.price_currency:
        out["price"] = "other_currency"
    elif stored_amount is None:
        out["price"] = "none_stored"
    else:
        diff = abs(snap.price_amount - stored_amount)
        pct = diff / stored_amount if stored_amount else None
        out["pct"] = round(pct, 4) if pct is not None else None
        if diff < 0.005:
            out["price"] = "agree"
        elif pct is not None and pct <= 0.02:
            out["price"] = "moved_within_2pct"
        elif pct is not None and pct <= 0.10:
            out["price"] = "moved_within_10pct"
        else:
            out["price"] = "moved_over_10pct"
    variants = _json(seed.get("variants"))
    if not isinstance(variants, list):
        variants = _json(seed.get("snapshot_variants"))
    stored_variants = [v for v in (variants or []) if isinstance(v, dict)]
    takes_product = out["price"] in ("agree", "none_stored") or out["price"].startswith("moved")
    _, report = _reconcile_seed_variants_with_read(
        stored_variants,
        snap.evidence["variants"],
        product_amount=snap.price_amount if takes_product else None,
        product_currency=snap.price_currency,
        product_availability=snap.availability if snap.availability != "unknown" else None,
        census=snap.evidence["variant_census"],
        product_title=seed.get("title"),
    )
    out["variants"] = "all_re_read" if not report["not_re_read_count"] else "not_all_re_read"
    out["variants_changed"] = report["changed"]
    return out


def plan_host(host, catalogue, seeds):
    """The `host` line for one host's catalogue read and its seeds."""
    from services.external_seed_catalogue_refresh import seed_catalogue_read

    reasons, prices, variants = {}, {}, {}
    sample = []
    matched = 0
    for seed in seeds:
        read, why = seed_catalogue_read(catalogue, seed["destination_url"], market=str(seed.get("market") or ""))
        _bump(reasons, why)
        if read is None:
            continue
        matched += 1
        compared = compare_seed(seed, read)
        _bump(prices, compared["price"])
        _bump(variants, compared["variants"])
        if compared["price"] not in ("agree",) and len(sample) < SAMPLE:
            sample.append(compared)
    return {**catalogue.summary(), "seeds": len(seeds), "matched": matched, "reasons": reasons,
            "price": prices, "variants": variants, "sample": sample}


async def main():
    import asyncpg

    if os.getenv("PIVOTA_ENV") != "production":
        _closed("PIVOTA_ENV is not production")
    if os.getenv("CATALOGUE_PLAN_EGRESS") != "pivota-crawl":
        _closed("CATALOGUE_PLAN_EGRESS must state pivota-crawl: launch with SUBNET=pivota-crawl, never from a laptop")
    if in_quiet_window(_now_utc()):
        _closed("03:00-06:30Z belongs to the destination sweep and the nightly refresh")
    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        _closed("DATABASE_URL missing")
    raw_timeout = (os.getenv("DB_STATEMENT_TIMEOUT_SECONDS") or "").strip()
    if not raw_timeout:
        _closed("DB_STATEMENT_TIMEOUT_SECONDS unset: refusing to run without a statement timeout")
    try:
        timeout_s = float(raw_timeout)
    except ValueError:
        _closed("DB_STATEMENT_TIMEOUT_SECONDS is not a number: " + raw_timeout[:40])
    if not timeout_s > 0:
        _closed("DB_STATEMENT_TIMEOUT_SECONDS must be positive")
    timeout_ms = max(int(timeout_s * 1000), 1000)
    try:
        top_hosts = max(1, int(os.getenv("TOP_HOSTS") or "20"))
    except ValueError:
        _closed("TOP_HOSTS is not an integer")
    try:
        from services import crawl_global_pacer, crawl_ip_throttle
        from services import external_seed_catalogue_refresh as lane
        from routes.employee_products import _reconcile_seed_variants_with_read  # noqa: F401 - the gate
    except Exception as exc:  # noqa: BLE001
        _closed("cannot import the lane: " + repr(exc)[:200])
    if not hasattr(lane, "seed_catalogue_read"):
        _closed("this image has no catalogue lane: run it on the merged image")

    conn = await asyncpg.connect(url, command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT_SECONDS") or 600))

    async def read_only():
        tr = conn.transaction(readonly=True)
        await tr.start()
        await conn.execute("SET LOCAL statement_timeout = " + str(timeout_ms))
        if await conn.fetchval("SHOW transaction_read_only") != "on":
            await tr.rollback()
            _closed("transaction is not read-only")
        return tr

    try:
        tr = await read_only()
        try:
            present = await conn.fetchval("SELECT to_regclass('external_product_seeds') IS NOT NULL")
        finally:
            await tr.rollback()
        if not present:
            _closed("table missing: external_product_seeds")
        print("GATE_OPEN", flush=True)

        tr = await read_only()
        try:
            hosts = plan_hosts([dict(r) for r in await conn.fetch(ACTIVE_DESTINATIONS_SQL)], top_hosts)
            ids = [i for seed_ids in hosts.values() for i in seed_ids]
            seeds = {}
            for start in range(0, len(ids), CHUNK):
                for r in await conn.fetch(SEED_COLUMNS_SQL, ids[start:start + CHUNK]):
                    seeds[str(r["id"])] = dict(r)
        finally:
            await tr.rollback()
    finally:
        # Closed BEFORE the first fetch: no connection is held while the run waits on the pacer.
        await conn.close()
    emit("hosts", {h: len(s) for h, s in hosts.items()})

    budget = crawl_global_pacer.resolve_budget_seconds(None, "PLAN_BUDGET_SECONDS", 1800.0)
    pacer = crawl_global_pacer.GlobalOriginPacer(
        rps=crawl_global_pacer.resolve_rps(None, "PLAN_RPS", 0.5),
        max_new_hosts_per_minute=crawl_global_pacer.resolve_new_host_cap(None, "PLAN_MAX_NEW_HOSTS_PER_MINUTE", 6),
        deadline=crawl_global_pacer._now() + budget,
    )

    def _on_trip(breaker, event):
        if event.get("action") == "cooldown":
            pacer.pause(breaker.cooldown_seconds)
            pacer.set_rate(pacer.rps / 2.0)
        else:
            pacer.stop("ip breaker tripped")

    breaker = crawl_ip_throttle.StagedIpThrottleBreaker.from_env(on_trip=_on_trip)
    totals = {"hosts_planned": len(hosts), "hosts_read": 0, "hosts_priceable": 0, "seeds": len(seeds),
              "matched": 0, "host_status": {}, "price": {}, "variants": {}, "stopped": None}
    with crawl_ip_throttle.installed(breaker), crawl_global_pacer.installed(pacer):
        for host, seed_ids in hosts.items():
            if pacer.stopped or breaker.tripped or breaker.blocks(host):
                totals["stopped"] = pacer.stop_reason or ("ip breaker" if breaker.tripped else "breaker holds " + host)
                break
            host_seeds = [seeds[i] for i in seed_ids if i in seeds]
            wanted = {lane.catalogue_handle(s["destination_url"]) for s in host_seeds} - {None}
            try:
                catalogue = await lane.read_host_catalogue(host, wanted)
            except crawl_global_pacer.GlobalPaceStopped:
                totals["stopped"] = pacer.stop_reason or "pacer stopped"
                break
            totals["hosts_read"] += 1
            line = plan_host(host, catalogue, host_seeds)
            emit("host", line)
            _bump(totals["host_status"], catalogue.status)
            totals["hosts_priceable"] += int(catalogue.status == lane.STATUS_OK)
            totals["matched"] += line["matched"]
            for key in ("price", "variants"):
                for k, n in line[key].items():
                    _bump(totals[key], k, n)
    pace = pacer.summary()
    requests = int(pace.get("paced_requests") or 0)
    totals["origin_requests"] = requests
    totals["requests_per_matched_seed"] = round(requests / totals["matched"], 3) if totals["matched"] else None
    totals["page_read_requests_for_the_same_seeds_est"] = round(1.3 * totals["matched"])
    emit("summary", totals)
    emit("pacer", {**pace, **breaker.summary()})
    print(f"{PREFIX} DONE lines={_lines['n']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
