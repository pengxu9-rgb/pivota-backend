"""One audit line for every seed an operator tool puts back to `active` OUTSIDE the draft-approval rule.

The staff doors that publish a seed (the audit APPROVE, `PATCH status=active`, a CSV `status=active`) run
`routes.employee_products._publish_seed`: a staff role, never the seed's own creator, the conflict check. A few
operator tools reactivate seeds they themselves deactivated, as part of undoing their own run, and do NOT go through
that rule (owner decision 2026-11-08: they stay ops-only, run by hand with production DB access). They are:

  * scripts/withdraw_catalog_rows.py --revert
  * scripts/remediate_unpublished_crawl_rows.py --revert
  * scripts/repair_same_title_listings.py (its revert)
  * services/identity_resolution.revert_run / revert_proposal

Each logs `external_seed_reactivated_outside_approval` here, one line per seed: the seed id, the tool and the status
it had before. No person's identity is logged. See docs/runbooks/seed_reactivation_outside_approval.md.

This must never change what the tool does: a logging failure is swallowed.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from utils.logger import logger as _pivota_logger

EVENT = "external_seed_reactivated_outside_approval"

#: A child of the `pivota` logger, the one that reaches Cloud Logging (a plain module logger's lines may not).
_logger = _pivota_logger.getChild("external_seed_reactivation")


def log_seed_reactivated_outside_approval(
    seed_ids: Optional[Iterable[Any]],
    *,
    tool: str,
    previous_status: str = "inactive",
    row_confirmed: bool = True,
) -> None:
    """Log one WARNING line per reactivated seed. Never raises.

    `row_confirmed` is False where the tool's UPDATE has no RETURNING: the statement is guarded on
    `status = <previous_status>`, so the seed changed only if it still had that status.
    """
    try:
        for seed_id in list(seed_ids or []):
            _logger.warning(json.dumps({
                "event": EVENT,
                "seed_id": str(seed_id),
                "tool": str(tool),
                "previous_status": str(previous_status),
                "row_confirmed": bool(row_confirmed),
            }, sort_keys=True))
    except Exception:  # noqa: BLE001 -- an audit line must never change what the tool does
        pass
