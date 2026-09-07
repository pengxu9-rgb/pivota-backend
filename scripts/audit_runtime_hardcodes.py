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

# A line may carry a forbidden id and NOT be a leaked runtime default:
#
#   * a full-line comment. Prose that names a rig ("the 763 rows under merch_...
#     are held out of the sitemap only by that merchant's indexable bit") is the
#     explanation an operator needs at the one place they are already looking;
#     forbidding it pushes the reason out of the code. The id in prose is inert.
#   * a DENYLIST entry. services/test_merchant_policy.py lists the rig ids so
#     serving lanes EXCLUDE them -- the inverse of a leaked default. Such a line
#     says so with the marker below, and the marker must name the reason: a bare
#     "noqa" would let a real default hide behind a comment. The marker is
#     honoured ONLY in the files named in DENYLIST_FILES: an unscoped marker is
#     a self-service opt-out, and a default in routes/ or services/ followed by
#     the marker would otherwise pass (measured in review).
#
# Inline comments are NOT stripped: `x = "merch_..."  # a comment` is a default
# with a comment, and the guard must still see it.
DENYLIST_MARKER = "runtime-hardcode-guard: denylist"
DENYLIST_FILES = frozenset({"services/test_merchant_policy.py"})


def line_is_violation(line: str, rel: str = "") -> bool:
    """True when `line` (in file `rel`) carries a forbidden id in a way that can
    reach runtime."""
    stripped = line.strip()
    if not any(pattern.search(line) for pattern in FORBIDDEN):
        return False
    if stripped.startswith("#"):
        return False
    if DENYLIST_MARKER in line and "#" in line and rel in DENYLIST_FILES:
        # The marker must sit in the trailing comment, after the code.
        code, _, comment = line.partition("#")
        return DENYLIST_MARKER not in comment
    return True


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
            if line_is_violation(line, rel):
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
