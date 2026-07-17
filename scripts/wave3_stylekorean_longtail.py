#!/usr/bin/env python3
"""Wave 3 — StyleKorean long-tail rollout over the remaining ~395 brands.

Per brand, strictly sequential (single prod writer), state-file resumable:

  1. CRAWL + PLAN via scripts/ingest_stylekorean_brand.py (dry; crawl cached).
  2. DOMAIN DISCOVERY: candidate official domains generated from the brand's
     display name + SK slug, probed via https://{d}/products.json?limit=1
     (Shopify-enumerable check). A candidate is accepted ONLY if its vendor
     brand and the SK brand agree under match_brand (token containment either
     way) — minting an unrelated store's catalog would pollute the index, so
     no-match means NO mint lane, never a guess.
  3. MINT + ATTACH (mint FIRST — cross-lane dup lesson) via the same CLI with
     --brand-official-domain --apply-mint --apply-offers --residue-candidates.
     Unmapped brands run ATTACH-only; their residue stays for later LLM
     batches (Gemini spend is founder-gated, NOT auto-run here).
  4. JUDGE (DeepSeek, cheap) for mapped brands: judge_stylekorean_residue
     --apply-offers (the reviewed B1-safe live path; auto-apply per the
     pilot's standing directive).

Dry mode (default) runs steps 1-2 and REPORTS what step 3-4 would do —
zero DB writes, zero LLM spend. --apply enables the write lanes.

    python3 scripts/wave3_stylekorean_longtail.py \
        --state /path/wave3_state.json --workdir /path/wave3 [--apply] [--limit N]

State file: {slug: {"status": "done"|"failed"|"empty", "domain": ..., ...}}.
Re-running skips "done"/"empty" and retries "failed". Kill-safe: state is
rewritten atomically after every brand.

PENDING-REVIEW QUEUE: a brand that discovers a NEW domain during an --apply
run records it as "domain_pending_review" and finishes attach-only with
status "done" — no later run revisits it automatically. To approve such a
domain, edit its row: set "domain" to the approved value and "status" to
"dry", then re-run with --apply. (Deliberate: apply mints exactly the
human-reviewable map, nothing else.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from services.pdp_matcher.retailer_match import match_brand  # noqa: E402
from services.retailer_ingest.sitemap_crawler import fetch_text, sitemap_locs  # noqa: E402

BRANDS_SITEMAP = "https://www.stylekorean.com/sitemap-brands.xml"
# already fully processed in waves 0-2 + medicube
DONE_BRANDS = frozenset({
    "cosrx", "anua", "bb-lab", "beauty-of-joseon", "isntree", "medicube",
    "missha", "numbuzin", "round-lab", "skin1004", "torriden",
})
PY = sys.executable
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SLUG_RE = re.compile(r"/brand/([a-z0-9-]+)", re.I)
_PROBE_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
_UA = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"


def brand_slugs() -> List[str]:
    locs = sitemap_locs(fetch_text(BRANDS_SITEMAP))
    slugs: List[str] = []
    for loc in locs:
        m = _SLUG_RE.search(loc)
        if m:
            s = m.group(1).lower()
            if s not in slugs:
                slugs.append(s)
    return slugs


def candidate_domains(brand_display: str, slug: str) -> List[str]:
    """Deterministic official-domain candidates. Order matters (most likely
    first); probing stops at the first VERIFIED hit."""
    flat = re.sub(r"[^a-z0-9]", "", (brand_display or "").lower())
    # match_brand-stripped core: "Banila co" -> "banila" -> banilausa.com
    # (the legal/storefront suffix often drops out of the domain).
    stripped_flat = re.sub(r"[^a-z0-9]", "", match_brand(brand_display))
    slug_flat = slug.replace("-", "")
    cores = []
    for c in (flat, stripped_flat, slug_flat, slug):
        if c and c not in cores:
            cores.append(c)
    out: List[str] = []
    for c in cores:
        for pattern in (
            "{c}.com", "{c}.us", "{c}us.com", "{c}usa.com", "{c}-global.com", "{c}global.com",
            "the{c}.com", "{c}beauty.com", "{c}cosmetics.com", "us.{c}.com",
            "{c}.shop", "{c}.co",
        ):
            d = pattern.format(c=c)
            if d not in out:
                out.append(d)
    return out


def _tokens(brand: Optional[str]) -> frozenset:
    return frozenset(match_brand(brand).split())


def vendor_matches_brand(vendor: str, sk_brand: str) -> bool:
    """Accept a probed store only when vendor and SK brand agree under
    match_brand token containment. The vendor may EXTEND the brand freely
    ('Anua US' ⊇ 'Anua', 'Abib Cosmetics' ⊇ 'Abib'); the vendor may be a
    SUBSET of the brand only with >= 2 tokens — a single generic token
    (vendor 'Beauty' vs brand 'Beauty of Joseon') is not brand identity
    (a10fb047 review, finding 2). Empty sides never match."""
    v, b = _tokens(vendor), _tokens(sk_brand)
    if not v or not b:
        return False
    return b <= v or (v <= b and len(v) >= 2)


PROBE_SAMPLE = 10          # products sampled per candidate store
PROBE_MAJORITY = 0.8       # share of non-empty vendors that must match


def probe_official_domain(brand_display: str, slug: str) -> Tuple[Optional[str], str]:
    """First candidate whose /products.json is Shopify-enumerable AND whose
    vendors verify against the SK brand by MAJORITY (>= PROBE_MAJORITY of
    non-empty vendors across PROBE_SAMPLE products, and >= 3 matches).
    Single-product sampling both false-rejected a genuine store whose first
    product was a 'TEST' junk row (tonymoly.us) and would accept a
    multi-brand reseller whose first product happened to match (a10fb047
    review, finding 1). Returns (domain|None, note); mismatched enumerable
    stores are recorded in the note for the pre-apply human review."""
    tried = 0
    mismatches: List[str] = []
    with httpx.Client(follow_redirects=True, timeout=_PROBE_TIMEOUT,
                      headers={"User-Agent": _UA, "Accept": "application/json"}) as client:
        for domain in candidate_domains(brand_display, slug):
            tried += 1
            try:
                resp = client.get(f"https://{domain}/products.json?limit={PROBE_SAMPLE}")
                if resp.status_code != 200 or "json" not in (resp.headers.get("content-type") or ""):
                    continue
                products = (resp.json() or {}).get("products") or []
                vendors = [str(p.get("vendor") or "").strip() for p in products if isinstance(p, dict)]
                vendors = [v for v in vendors if v]
                if not vendors:
                    continue
                matches = sum(1 for v in vendors if vendor_matches_brand(v, brand_display))
                if matches >= 3 and matches / len(vendors) >= PROBE_MAJORITY:
                    return domain, (f"verified {matches}/{len(vendors)} vendors "
                                    f"(sample={vendors[0]!r})")
                mismatches.append(f"{domain}={vendors[:3]!r}({matches}/{len(vendors)})")
            except Exception:  # noqa: BLE001 — dead candidate, keep probing
                continue
    note = f"no verified Shopify official ({tried} candidates probed)"
    if mismatches:
        note += f"; enumerable-but-mismatched: {'; '.join(mismatches[:4])}"
    return None, note


def _run_cli(args: List[str], *, timeout_s: int = 2400) -> Tuple[int, str]:
    proc = subprocess.run([PY] + args, capture_output=True, text=True,
                          timeout=timeout_s, cwd=REPO_ROOT)
    return proc.returncode, (proc.stdout + proc.stderr)[-6000:]


def _plan_stats(plan_path: str) -> Dict[str, Any]:
    attach = mint = 0
    brand_display = ""
    try:
        with open(plan_path) as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not brand_display and row.get("brand"):
                    brand_display = str(row["brand"])
                if row.get("decision") == "attach":
                    attach += 1
                elif row.get("decision") == "mint":
                    mint += 1
    except FileNotFoundError:
        pass
    return {"attach": attach, "mint": mint, "brand_display": brand_display}


def process_brand(slug: str, *, workdir: str, apply: bool,
                  reviewed_domain: Optional[str] = None,
                  had_prior_probe: bool = False) -> Dict[str, Any]:
    plan_path = os.path.join(workdir, f"plan_{slug}.jsonl")
    resid_path = os.path.join(workdir, f"residue_{slug}.jsonl")
    judged_path = os.path.join(workdir, f"judged_{slug}.jsonl")

    # 1. crawl + plan (dry — also warms the crawl cache for step 3)
    rc, out = _run_cli(["scripts/ingest_stylekorean_brand.py", "--brand", slug,
                        "--out", plan_path])
    if rc != 0:
        if "0 product URLs" in out or "no product URLs" in out:
            return {"status": "empty", "note": "no SK product URLs for slug"}
        return {"status": "failed", "step": "plan", "log": out[-800:]}
    stats = _plan_stats(plan_path)
    if stats["attach"] + stats["mint"] == 0:
        return {"status": "empty", **stats}

    # 2. official-domain discovery (verified or nothing). In APPLY mode, only
    #    a domain that already survived a DRY pass (i.e. was reviewable in the
    #    state file) may mint — a freshly discovered domain is recorded for the
    #    next dry-review cycle and the brand runs ATTACH-only this round
    #    (a10fb047 review, finding 5: vendor statistics cannot distinguish a
    #    single-brand gray-market reseller; a human look at the map can).
    if apply and had_prior_probe:
        domain, note = reviewed_domain, "from reviewed dry state"
    else:
        domain, note = probe_official_domain(stats["brand_display"], slug)
    result: Dict[str, Any] = {**stats, "domain": domain, "domain_note": note}

    if not apply:
        result["status"] = "dry"
        return result
    if domain and not had_prior_probe:
        result["domain_pending_review"] = domain
        result["domain_note"] += " (NEW in apply mode — mint deferred to next review)"
        domain = None
        result["domain"] = None

    # 3. mint-first (mapped) or attach-only (unmapped)
    if domain:
        rc, out = _run_cli(["scripts/ingest_stylekorean_brand.py", "--brand", slug,
                            "--brand-official-domain", domain, "--apply-mint",
                            "--apply-offers", "--residue-candidates", resid_path,
                            "--out", plan_path])
        if rc != 0:
            return {**result, "status": "failed", "step": "mint", "log": out[-800:]}
        result["mint_log"] = out[-500:]
        # 4. judge residue (DeepSeek) + auto-attach AUTO verdicts
        if stats["mint"] > 0:
            rc, out = _run_cli(["scripts/judge_stylekorean_residue.py",
                                "--plan", plan_path, "--brand-official-domain", domain,
                                "--out", judged_path, "--apply-offers"])
            if rc != 0:
                return {**result, "status": "failed", "step": "judge", "log": out[-800:]}
            result["judge_log"] = out[-500:]
    else:
        rc, out = _run_cli(["scripts/ingest_stylekorean_brand.py", "--brand", slug,
                            "--apply-offers", "--out", plan_path])
        if rc != 0:
            return {**result, "status": "failed", "step": "attach", "log": out[-800:]}
        result["attach_log"] = out[-500:]

    result["status"] = "done"
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", required=True, help="resumable state JSON path")
    p.add_argument("--workdir", required=True, help="per-brand plan/residue dir")
    p.add_argument("--apply", action="store_true", help="enable write lanes")
    p.add_argument("--limit", type=int, default=0, help="process at most N brands")
    args = p.parse_args()
    if args.apply:
        for var in ("DATABASE_URL", "DEEPSEEK_API_KEY"):
            if not os.environ.get(var):
                print(f"[wave3] FATAL: --apply requires {var} in the environment "
                      f"(mint would run, then judge would fail wave-wide)")
                return 1
    os.makedirs(args.workdir, exist_ok=True)

    state: Dict[str, Any] = {}
    if os.path.exists(args.state):
        state = json.load(open(args.state))

    slugs = [s for s in brand_slugs() if s not in DONE_BRANDS]
    todo = [s for s in slugs
            if state.get(s, {}).get("status") not in ("done", "empty", "dry" if not args.apply else "")]
    print(f"[wave3] {len(slugs)} long-tail brands; {len(todo)} to process "
          f"(apply={args.apply}, limit={args.limit or 'all'})", flush=True)

    processed = 0
    for slug in todo:
        if args.limit and processed >= args.limit:
            break
        t0 = time.time()
        print(f"[wave3] >>> {slug}", flush=True)
        try:
            prior = state.get(slug) or {}
            result = process_brand(
                slug, workdir=args.workdir, apply=args.apply,
                reviewed_domain=prior.get("domain"),
                had_prior_probe="domain" in prior,
            )
        except Exception as exc:  # noqa: BLE001 — one brand never stops the wave
            result = {"status": "failed", "step": "exception", "log": str(exc)[:500]}
        result["elapsed_s"] = round(time.time() - t0, 1)
        state[slug] = result
        tmp = args.state + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=0)
        os.replace(tmp, args.state)  # atomic — a kill mid-write can't corrupt
        print(f"[wave3] <<< {slug}: {result.get('status')} "
              f"attach={result.get('attach')} mint={result.get('mint')} "
              f"domain={result.get('domain')} ({result['elapsed_s']}s)", flush=True)
        processed += 1

    counts: Dict[str, int] = {}
    for s in state.values():
        counts[s.get("status", "?")] = counts.get(s.get("status", "?"), 0) + 1
    print(f"[wave3] state summary: {counts}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
