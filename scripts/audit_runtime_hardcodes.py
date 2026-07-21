#!/usr/bin/env python3
"""Fail CI when real merchant/test-store defaults leak into runtime files."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("routes", "services", "config", "utils", "readiness", "scripts", "main.py")
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "readiness/tests",
    "readiness/fixtures",
    "scripts/fixtures",
}
FORBIDDEN_STRINGS = (
    "merch_" + "208139f7600dbf42",
    "merch_" + "6b90dc9838d5fd9c",
    "merch_" + "efbc46b4619cfbdf",
    "store_shopify_" + "chydan" + "test",
    "psp_stripe_" + "chydan" + "test",
    "chydan" + "test",
)
FORBIDDEN = tuple(re.compile(value, re.IGNORECASE) for value in FORBIDDEN_STRINGS)


def _should_skip(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in {
        "scripts/audit_runtime_hardcodes.py",
        "scripts/inventory_legacy_test_merchants.py",
        # One-off operator scripts whose PAYLOAD is a specific historical
        # merchant (backfills / dedup / retirement of the merch_efbc test
        # rig). They are not runtime code and cannot be parameterized without
        # losing their audit trail. Never add a routes/services/config file
        # here — the guard exists for runtime defaults.
        "scripts/backfill_catalog_source_domain.py",
        "scripts/stage2_backfill_attribution_edges.py",
        "scripts/step5_lane1_dedup_92sfrj.py",
        "scripts/retire_test_rig_merch_efbc.py",
    }:
        return True
    parts = rel.split("/")
    for idx in range(len(parts)):
        if "/".join(parts[: idx + 1]) in SKIP_PARTS:
            return True
    return any(part in SKIP_PARTS for part in parts)


def iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        path = REPO_ROOT / root
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        for child in path.rglob("*"):
            if child.is_file() and not _should_skip(child):
                files.append(child)
    return files


def collect_violations() -> list[str]:
    violations: list[str] = []
    for file_path in iter_scan_files():
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = file_path.relative_to(REPO_ROOT).as_posix()
        for line_no, line in enumerate(lines, start=1):
            if any(pattern.search(line) for pattern in FORBIDDEN):
                violations.append(f"{rel}:{line_no}: {line.strip()}")
    return violations


def main() -> int:
    violations = collect_violations()
    if violations:
        print("Runtime hardcode audit failed. Remove real merchant/test-store defaults:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Runtime hardcode audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
