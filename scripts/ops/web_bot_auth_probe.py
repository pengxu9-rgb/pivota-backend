#!/usr/bin/env python3
"""READ-ONLY measurement: does a Web Bot Auth signature change what a storefront answers us?

Run from the CRAWL subnet (docs/runbooks/crawl_identity.md step 6), with the key mounted and the
flag on, against stores that answer `429 local_rate_limited` today:

    python scripts/ops/web_bot_auth_probe.py flowerknows.co dermalogica.com

Per store: ONE unsigned and ONE signed GET /products.json?limit=1, 3 s apart, same User-Agent
(the external-offer lane's). At most 5 stores. Prints `E3 <host> <unsigned|signed> <status>
<retry-after> <first 40 bytes>` plus the signing status; exits 2 when signing is not active (a
comparison of two unsigned requests proves nothing). Never retries; writes nothing.
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services import crawl_identity  # noqa: E402
from services.external_offers_service import DEFAULT_UA  # noqa: E402

GAP_S = 3.0


async def _one(host: str, label: str, kwargs) -> None:
    async with httpx.AsyncClient(timeout=15.0, **kwargs) as client:
        try:
            r = await client.get(f"https://{host}/products.json?limit=1", headers={"User-Agent": DEFAULT_UA})
            snippet = r.text[:40].replace("\n", " ")
            print("E3", host, label, r.status_code, r.headers.get("retry-after") or "-", repr(snippet))
        except Exception as exc:
            print("E3", host, label, "ERROR", type(exc).__name__)


async def main(hosts) -> int:
    status = crawl_identity.status()
    print("E3 signing", status)
    if status != "signed":
        return 2
    for host in hosts[:5]:
        await _one(host, "unsigned", {})
        await asyncio.sleep(GAP_S)
        await _one(host, "signed", crawl_identity.transport_kwargs())
        await asyncio.sleep(GAP_S)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main([h.strip().lower() for h in sys.argv[1:] if h.strip()])))
