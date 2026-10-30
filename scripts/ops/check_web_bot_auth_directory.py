#!/usr/bin/env python3
"""READ-ONLY: fetch our Web Bot Auth key directory and verify it the way a verifier would.

    python scripts/ops/check_web_bot_auth_directory.py [https://api.pivota.cc]

Exit 0 and `OK` when the directory is served with the right media type, holds only public Ed25519
keys, and its signature over ("@authority";req) verifies and is unexpired. Run it before filing
Shopify's higher-tier form and after every key rotation. Sends one GET; prints no key material
beyond the public keyids. services/crawl_identity.py has the format.
"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services import crawl_identity  # noqa: E402


def main() -> int:
    origin = (sys.argv[1] if len(sys.argv) > 1 else crawl_identity.DEFAULT_SIGNATURE_AGENT).rstrip("/")
    url = origin + crawl_identity.DIRECTORY_PATH
    resp = httpx.get(url, headers={"Accept": crawl_identity.DIRECTORY_MEDIA_TYPE}, timeout=15.0)
    if resp.status_code != 200:
        print(json.dumps({"ok": False, "url": url, "status": resp.status_code}))
        return 1
    result = crawl_identity.verify_directory(resp.content, dict(resp.headers),
                                             authority=crawl_identity.authority_of(httpx.URL(url)))
    print(json.dumps({"url": url, **result}))
    print("OK" if result["ok"] else "FAILED")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
