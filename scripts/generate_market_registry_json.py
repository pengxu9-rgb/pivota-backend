"""Write the gateway's copy of the market registry (ADR-026 decision 2).

    python scripts/generate_market_registry_json.py <PIVOTA-Agent>/src/markets/market_registry.generated.json
    python scripts/generate_market_registry_json.py --check <that file>    # exit 1 if it differs

The output is services.market_registry.gateway_projection_json(): sorted keys, stable shape, so the
gateway's CI (and this --check) can compare the copy byte for byte. Never edit the generated file by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.market_registry import gateway_projection_json  # noqa: E402


def main(argv) -> int:
    check = "--check" in argv
    paths = [a for a in argv if a != "--check"]
    if len(paths) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(paths[0])
    want = gateway_projection_json()
    if check:
        have = target.read_text(encoding="utf-8") if target.exists() else ""
        if have != want:
            print(f"{target} is stale: regenerate with scripts/generate_market_registry_json.py", file=sys.stderr)
            return 1
        print(f"{target} matches the registry")
        return 0
    target.write_text(want, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
