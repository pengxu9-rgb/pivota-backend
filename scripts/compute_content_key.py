#!/usr/bin/env python3
"""Compute catalog_products.content_key for a JSON input object.

Usage:
  echo '{"brand":"The Ordinary","title":"Niacinamide 10% + Zinc 1%"}' |
    python3 scripts/compute_content_key.py

  python3 scripts/compute_content_key.py '{"brand":"Brand","title":"Title","gtin":null}'
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.catalog_identity import make_content_key  # noqa: E402


def _read_payload(argv: list[str]) -> Dict[str, Any]:
    raw = argv[1] if len(argv) > 1 else sys.stdin.read()
    if not raw.strip():
        raise SystemExit("expected JSON object on argv[1] or stdin")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit("expected a JSON object")
    return payload


def main(argv: list[str]) -> int:
    payload = _read_payload(argv)
    content_key = make_content_key(
        payload.get("brand"),
        payload.get("title"),
        payload.get("gtin"),
    )
    print("null" if content_key is None else content_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
