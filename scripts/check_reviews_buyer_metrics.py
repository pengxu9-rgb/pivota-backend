#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx


FAMILIES = (
    "reviews_buyer_exchange_total",
    "reviews_buyer_create_total",
    "reviews_buyer_media_upload_total",
)


@dataclass(frozen=True)
class Sample:
    process_start: Optional[float]
    totals: Dict[str, float]


def _parse_metrics(text: str) -> Sample:
    totals = {k: 0.0 for k in FAMILIES}
    process_start: Optional[float] = None
    for line in text.splitlines():
        if line.startswith("process_start_time_seconds "):
            try:
                process_start = float(line.split()[-1])
            except Exception:
                process_start = None
            continue
        for fam in FAMILIES:
            if not (line.startswith(fam + "{") or line.startswith(fam + " ")):
                continue
            m = re.search(r"\s([0-9]+(?:\.[0-9]+)?)$", line)
            if not m:
                continue
            try:
                totals[fam] += float(m.group(1))
            except Exception:
                pass
    return Sample(process_start=process_start, totals=totals)


def main() -> int:
    base_url = (os.getenv("REVIEWS_BASE_URL") or os.getenv("BASE_URL") or "").strip().rstrip("/")
    token = (os.getenv("METRICS_BEARER_TOKEN") or "").strip()
    attempts = int(os.getenv("METRICS_ATTEMPTS") or "10")
    delay_s = float(os.getenv("METRICS_DELAY_SECONDS") or "0.2")

    if not base_url:
        print("ERROR: missing REVIEWS_BASE_URL", file=sys.stderr)
        return 2
    if not token:
        print("ERROR: missing METRICS_BEARER_TOKEN", file=sys.stderr)
        return 2

    url = f"{base_url}/metrics"
    headers = {"Authorization": f"Bearer {token}"}

    samples: Dict[str, Sample] = {}
    unique_process = set()

    with httpx.Client(timeout=5.0) as client:
        for _ in range(max(1, attempts)):
            try:
                resp = client.get(url, headers=headers)
                if resp.status_code != 200:
                    time.sleep(delay_s)
                    continue
                if "# HELP " not in resp.text:
                    time.sleep(delay_s)
                    continue
                sample = _parse_metrics(resp.text)
                key = str(sample.process_start or "unknown")
                samples[key] = sample
                if sample.process_start is not None:
                    unique_process.add(sample.process_start)
            except Exception:
                time.sleep(delay_s)
                continue
            time.sleep(delay_s)

    print(f"samples={len(samples)} unique_processes={len(unique_process)}")
    if not samples:
        print("metrics_ok=0")
        return 1

    max_totals = {k: 0.0 for k in FAMILIES}
    for s in samples.values():
        for fam in FAMILIES:
            max_totals[fam] = max(max_totals[fam], float(s.totals.get(fam, 0.0)))

    for fam in FAMILIES:
        print(f"{fam}_max={max_totals[fam]}")

    # Best-effort: in multi-replica or multi-worker setups, the scrape endpoint may not hit the same
    # process that served the earlier requests. We accept "non-zero observed anywhere" as proof.
    any_nonzero = any(v > 0.0 for v in max_totals.values())
    print(f"metrics_nonzero={1 if any_nonzero else 0}")
    return 0 if any_nonzero else 1


if __name__ == "__main__":
    raise SystemExit(main())

