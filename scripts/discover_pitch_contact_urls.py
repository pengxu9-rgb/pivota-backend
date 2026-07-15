#!/usr/bin/env python3
"""Wave-3 C2 — discover VERIFIED pitch/contact pages for cited-host registry
hosts that have no pitch_recipient yet, and emit them as PROPOSALS for human
review. This script NEVER writes the registry: a URL only ships to merchants
after a human approves the proposal into data/cited_host_registry.json
(pitch_recipient.submission_url) — the annotate layer renders registry-curated
paths only, guessed URLs never reach a merchant.

Usage:
  python scripts/discover_pitch_contact_urls.py [--limit N] [--timeout S]
      [--out reports/pitch_contact_proposals.json]

Verification per candidate: HTTP 200 after redirects on the SAME registrable
host + page text matches a contact/pitch heuristic. Candidates are the common
editorial paths only; no crawling beyond them.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.parse

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "data" / "cited_host_registry.json"

CANDIDATE_PATHS = (
    "/contact",
    "/contact-us",
    "/about/contact",
    "/write-for-us",
    "/contribute",
    "/tips",
    "/pitch",
    "/about",
)

# The page must actually look like a contact/pitch surface — a 200 on a
# soft-404 or a homepage redirect is not verification.
CONTENT_HINT = re.compile(
    r"(contact us|write for us|pitch|submit (a )?(tip|story|product)|"
    r"editorial (team|inquiries)|press inquir|get in touch|reach (us|out))",
    re.IGNORECASE,
)

UA = "PivotaRegistryBot/1.0 (+https://pivota.cc; partnership discovery)"


def _same_host(host: str, url: str) -> bool:
    netloc = urllib.parse.urlparse(url).netloc.lower()
    host = host.lower()
    return netloc == host or netloc == f"www.{host}" or host == netloc.removeprefix("www.")


def discover(host: str, client: httpx.Client) -> dict | None:
    for path in CANDIDATE_PATHS:
        url = f"https://{host}{path}"
        try:
            resp = client.get(url, headers={"user-agent": UA})
        except httpx.HTTPError:
            continue
        if resp.status_code != 200 or not _same_host(host, str(resp.url)):
            continue
        text = resp.text[:20000]
        match = CONTENT_HINT.search(text)
        if not match:
            continue
        return {
            "host": host,
            "candidate_submission_url": str(resp.url),
            "matched_hint": match.group(0),
            "status": "proposed",
        }
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument(
        "--out", default=str(REPO_ROOT / "reports" / "pitch_contact_proposals.json")
    )
    args = ap.parse_args()

    registry = json.loads(REGISTRY.read_text())
    hosts = registry.get("hosts") or registry
    todo = [
        h for h, meta in hosts.items()
        if isinstance(meta, dict) and not (meta.get("pitch_recipient") or {})
    ][: args.limit]
    print(f"probing {len(todo)} registry hosts without a pitch_recipient", file=sys.stderr)

    proposals = []
    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        for host in todo:
            found = discover(host, client)
            print(f"  {host}: {'FOUND ' + found['candidate_submission_url'] if found else 'none'}", file=sys.stderr)
            if found:
                proposals.append(found)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"proposals": proposals}, indent=2) + "\n")
    print(f"{len(proposals)} proposals -> {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
