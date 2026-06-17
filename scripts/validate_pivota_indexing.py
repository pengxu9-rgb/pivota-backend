#!/usr/bin/env python3
"""ADR-006 Phase-1 validation tool — Pivota-owned GSC indexing.

The go/no-go gate for the active "Pivota submits it" path. Mints the Pivota
service-account token and talks to Google's Indexing API + URL Inspection
*directly* (DB-free), so you can prove the credential works and whether Google
actually honors product-URL submissions BEFORE flipping the flag or wiring
anything merchant-facing.

This is the spike instrument referenced in
docs/adr/ADR-006-pivota-owned-gsc-indexing.md (Phase 1). It does NOT write
gsc_url_submissions — the production path (services.gsc_integration
.submit_pivota_canonical_url) does that; here we only want Google's answer.

Prereqs (ADR-006 Steps B + C):
  - A Pivota service account that is a verified OWNER of the property, with the
    Indexing API + Search Console API enabled in its project.
  - Env: GSC_PIVOTA_SUBMIT_ENABLED=true
         GSC_PIVOTA_SERVICE_ACCOUNT_JSON='<full service-account key JSON>'
         GSC_PIVOTA_PROPERTY_URL='https://agent.pivota.cc/'   # exact GSC property string

Usage (from repo root, with the venv):
  .venv/bin/python scripts/validate_pivota_indexing.py preflight
  .venv/bin/python scripts/validate_pivota_indexing.py submit <url> [<url> ...]
  .venv/bin/python scripts/validate_pivota_indexing.py submit --file urls.txt
  .venv/bin/python scripts/validate_pivota_indexing.py status <url> [<url> ...]

Validation method (see ADR-006 "Validation step"): submit a TEST set of real,
currently-NOT-indexed agent.pivota.cc/products/sig_* URLs, hold back a CONTROL
set you do NOT submit, then run `status` on both daily for ~1-2 weeks. GO only
if TEST reaches indexed materially faster than CONTROL; the Indexing API is
officially scoped to JobPosting/BroadcastEvent, so product URLs may be silently
ignored (200 but no priority). On NO-GO, keep the flag off and rely on
sitemap + internal linking + freshness for the Pivota PDP.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = list(args.urls or [])
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def _preflight() -> int:
    from config.settings import settings
    from services.gsc_integration import (
        GscNotConfiguredError,
        _get_pivota_access_token,
        _require_pivota_enabled,
    )

    try:
        _require_pivota_enabled()
    except GscNotConfiguredError as exc:
        print(f"CONFIG FAIL: {exc}")
        print("  Set GSC_PIVOTA_SUBMIT_ENABLED / _SERVICE_ACCOUNT_JSON / _PROPERTY_URL.")
        return 2

    print(f"property      = {settings.gsc_pivota_property_url}")
    token = await _get_pivota_access_token()
    if not token:
        print("TOKEN FAIL: could not mint a service-account access token.")
        print("  - is GSC_PIVOTA_SERVICE_ACCOUNT_JSON the full SA key JSON?")
        print("  - are the Indexing API + Search Console API enabled in the project?")
        return 3
    print(f"TOKEN OK     : minted a service-account access token ({len(token)} chars). Credential is usable.")
    print("Next: `submit` a few not-indexed canonical URLs, hold back a control set,")
    print("      then `status` both daily for 1-2 weeks (see ADR-006 validation step).")
    return 0


async def _submit(urls: list[str]) -> int:
    from services.gsc_integration import _get_pivota_access_token, _indexing_api_publish

    if not urls:
        print("No URLs given. Pass URLs or --file.")
        return 2
    token = await _get_pivota_access_token()
    if not token:
        print("TOKEN FAIL — run `preflight` first.")
        return 3

    rc = 0
    for u in urls:
        try:
            code, body = await _indexing_api_publish(token, u)
        except Exception as exc:  # noqa: BLE001
            print(f"ERR  {u}  network: {exc}")
            rc = 1
            continue
        if code == 200:
            print(f"OK   {u}  submitted (200)")
        else:
            print(f"FAIL {u}  http_{code}: {body[:160]}")
            rc = 1
            if code == 403:
                print("       -> 403 usually means the service account is NOT an Owner of")
                print("          the property, or the Indexing API isn't enabled (Step B).")
            elif code == 429:
                print("       -> 429: Indexing API quota (~200 URLs/day/project) exhausted.")
    return rc


async def _status(urls: list[str]) -> int:
    from config.settings import settings
    from services.gsc_integration import _get_pivota_access_token, _url_inspection

    if not urls:
        print("No URLs given. Pass URLs or --file.")
        return 2
    token = await _get_pivota_access_token()
    if not token:
        print("TOKEN FAIL — run `preflight` first.")
        return 3
    site = settings.gsc_pivota_property_url

    indexed = 0
    for u in urls:
        try:
            code, body = await _url_inspection(token, u, site)
        except Exception as exc:  # noqa: BLE001
            print(f"ERR     {u}  network: {exc}")
            continue
        if code != 200 or not isinstance(body, dict):
            print(f"FAIL    {u}  http_{code}: {str(body)[:160]}")
            if code == 403:
                print("        -> siteUrl must EXACTLY match a property the SA owns")
                print(f"           (GSC_PIVOTA_PROPERTY_URL={site!r}).")
            continue
        result = (body.get("inspectionResult") or {}).get("indexStatusResult") or {}
        verdict = result.get("verdict") or "?"
        coverage = result.get("coverageState") or ""
        if verdict == "PASS":
            indexed += 1
        print(f"{verdict:8} {coverage!r:42} {u}")
    print(f"\nindexed: {indexed}/{len(urls)}  (compare TEST vs CONTROL over time)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ADR-006 Phase-1 Pivota-owned GSC indexing validation",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="check config + mint a token (proves the credential)")
    for name, helptext in (
        ("submit", "submit canonical URLs to the Indexing API"),
        ("status", "inspect index status of canonical URLs"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("urls", nargs="*", help="canonical PDP URLs")
        sp.add_argument("--file", help="newline-delimited file of URLs (# comments ok)")

    args = parser.parse_args()
    if args.cmd == "preflight":
        rc = asyncio.run(_preflight())
    elif args.cmd == "submit":
        rc = asyncio.run(_submit(_load_urls(args)))
    else:
        rc = asyncio.run(_status(_load_urls(args)))
    sys.exit(rc)


if __name__ == "__main__":
    main()
