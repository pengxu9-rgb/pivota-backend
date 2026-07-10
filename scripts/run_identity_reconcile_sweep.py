#!/usr/bin/env python3
"""Manual runner for the ADR-010 D-2 Phase-B reconcile sweep.

Propose-only by default: runs gauges + classification + strategy proposals +
review-task enqueue, applies NOTHING. --apply additionally auto-approves and
applies the mechanical allowlist (same_url_dup, junk_url) through the engine
and its guard set — identical to what the weekly scheduler tick does.

Bypasses the ENABLE_IDENTITY_RECONCILE_SWEEP env flag (force=True): running
this by hand IS the deliberate act the flag exists to gate.

  Propose-only:  python3 scripts/run_identity_reconcile_sweep.py
  Full sweep:    python3 scripts/run_identity_reconcile_sweep.py --apply

Access notes as in scripts/step5_working_set.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.identity_reconcile_sweep import (  # noqa: E402
    run_identity_reconcile_sweep_tick,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Also auto-approve + apply the mechanical allowlist")
    args = parser.parse_args()
    summary = asyncio.run(
        run_identity_reconcile_sweep_tick(apply_allowlist=args.apply, force=True)
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
