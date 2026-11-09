"""One audit line for every seed a tool puts back to (or keeps re-asserting as) `active` OUTSIDE the draft-approval rule.

The staff doors that publish a seed (the audit APPROVE, `PATCH status=active`, a CSV `status=active`) run
`routes.employee_products._publish_seed`: a staff role, never one of the seed's authors, the conflict check. These
tools do not (owner decision 2026-11-08: they stay ops-only, run by hand with production DB access, or are the
machine ingest itself):

  * the operator reverts, each putting back only seeds its own run deactivated:
    scripts/withdraw_catalog_rows.py --revert, scripts/remediate_unpublished_crawl_rows.py --revert,
    scripts/repair_same_title_listings.py (its revert), the lane-4 mirror-seed deactivation script (scripts/deactivate_lane4_*_mirror_seeds.py) revert,
    scripts/retire_superseded_brand_keys.py revert, services/identity_resolution.revert_run / revert_proposal;
  * the re-ingest upserts, which set `active` on a seed someone took down when they write it again:
    scripts/onboard_external_brand_from_crawl.py (ON CONFLICT ... status='active') and
    services/catalog_enrichment_agent/apply.py (status = EXCLUDED.status).

Each logs `external_seed_reactivated_outside_approval` here, one line per seed: the seed id, the tool and the status
it had before. No person's identity. See docs/runbooks/seed_reactivation_outside_approval.md.

THE LINE IS ONE BARE JSON OBJECT ON STDOUT, with a `severity`, so Cloud Logging files it as jsonPayload at WARNING
(a `[ts] LEVEL - {...}` text line lands in textPayload at DEFAULT, which a severity or jsonPayload filter misses).
The same shape as services/market_telemetry's event lines.

This must never change what the tool does: every failure here is swallowed.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, Iterable, Mapping, Optional

EVENT = "external_seed_reactivated_outside_approval"
_LOGGER_NAME = "external_seed_reactivation_audit"


class _StdoutJsonLineHandler(logging.Handler):
    """Writes each record's message (already a JSON object) as one line to the CURRENT `sys.stdout`."""

    _seed_reactivation_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(record.getMessage() + "\n")
            sys.stdout.flush()
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)


def _make_logger() -> logging.Logger:
    log = logging.getLogger(_LOGGER_NAME)
    log.setLevel(logging.INFO)
    log.propagate = False
    if not any(getattr(h, "_seed_reactivation_handler", False) for h in log.handlers):
        log.addHandler(_StdoutJsonLineHandler())
    return log


_logger = _make_logger()


def log_seed_reactivated_outside_approval(
    seed_ids: Optional[Iterable[Any]],
    *,
    tool: str,
    previous_status: str = "inactive",
    row_confirmed: bool = True,
) -> None:
    """Log one line per reactivated seed. Never raises.

    `row_confirmed` is False where the tool's UPDATE has no RETURNING: the seed changed only if the statement's own
    guard still held.
    """
    try:
        for seed_id in list(seed_ids or []):
            _logger.warning(json.dumps({
                "severity": "WARNING",
                "message": EVENT,
                "event": EVENT,
                "seed_id": str(seed_id),
                "tool": str(tool),
                "previous_status": str(previous_status),
                "row_confirmed": bool(row_confirmed),
            }, sort_keys=True))
    except Exception:  # noqa: BLE001 -- an audit line must never change what the tool does
        pass


async def read_seed_statuses(db: Any, seed_ids: Iterable[Any]) -> Dict[str, str]:
    """The stored status of each of these seeds, read before an upsert that may revive one. Never raises.

    Read inside its own transaction (a savepoint when the caller is in one), so a failed read cannot abort the
    caller's transaction; on any failure the answer is {} and nothing is logged.
    """
    try:
        ids = sorted({str(s) for s in seed_ids or [] if s})
        if not ids:
            return {}
        names = [f"seed_id_{i}" for i in range(len(ids))]
        async with db.transaction():
            rows = await db.fetch_all(
                f"SELECT id, status FROM external_product_seeds WHERE id IN ({', '.join(':' + n for n in names)})",
                dict(zip(names, ids)),
            )
        return {str(dict(r)["id"]): str(dict(r).get("status") or "") for r in rows or []}
    except Exception:  # noqa: BLE001 -- an audit read must never change what the tool does
        return {}


def log_upsert_revivals(prior_statuses: Mapping[str, str], rows: Iterable[Mapping[str, Any]], *, tool: str) -> None:
    """After an upsert: log each row that is written `active` over a stored seed that was not. Never raises."""
    try:
        for row in rows or []:
            seed_id = str(row.get("id") or "")
            before = str(prior_statuses.get(seed_id) or "").strip().lower()
            after = str(row.get("status") or "active").strip().lower()
            if seed_id and before and before != "active" and after == "active":
                log_seed_reactivated_outside_approval([seed_id], tool=tool, previous_status=before, row_confirmed=False)
    except Exception:  # noqa: BLE001
        pass
