"""Literal-true flags for code that moves Pivota-held money (FIX_PLAN O-05, W3 review F1/F5).

Each flag arms its path only when it is literally 'true' (case and surrounding spaces ignored);
unset, empty, '1', 'yes' or 'on' leave it OFF. Read at call time, so arming or disarming needs no
deploy. A leaf module: services may import it without importing the scheduler.
"""
from __future__ import annotations

import os

#: The day-10 Stripe Connect transfer of settlement files, AND every direct caller of
#: services.settlement_file_service.transfer (the staff retry route included).
SETTLEMENT_FILE_TRANSFER_FLAG = "SETTLEMENT_FILE_TRANSFER_ENABLED"
#: T8 partner settlement (snapshots + legacy pending payout rows).
PARTNER_SETTLEMENT_MONTHLY_FLAG = "PARTNER_SETTLEMENT_MONTHLY_ENABLED"
#: T7 merchant GMV invoicing.
INVOICE_GENERATION_MONTHLY_FLAG = "INVOICE_GENERATION_MONTHLY_ENABLED"
#: The legacy agent_payouts Stripe transfer (partner_settlement_service's approve and execute
#: payout functions). No route or job calls it today; the flag keeps a future caller from
#: bypassing O-05 silently.
PARTNER_PAYOUT_EXECUTION_FLAG = "PARTNER_PAYOUT_EXECUTION_ENABLED"


def money_job_enabled(flag: str) -> bool:
    """True only when `flag` is literally 'true'. Unset, empty or anything else is OFF."""
    return (os.getenv(flag) or "").strip().lower() == "true"
