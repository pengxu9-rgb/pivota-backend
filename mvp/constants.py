from __future__ import annotations

from typing import Final, Literal


SchemaVersion = Literal["0.1"]
SCHEMA_VERSION: Final[SchemaVersion] = "0.1"


RiskTier = Literal["unknown", "low", "medium", "high"]
SURFACE_BACKEND: Final[str] = "backend"
SURFACE_GATEWAY: Final[str] = "gateway"


EVENT_OFFER_GENERATED: Final[str] = "offer_generated"
EVENT_OFFER_SELECTED: Final[str] = "offer_selected"
EVENT_OFFER_CITED: Final[str] = "offer_cited"
EVENT_CHECKOUT_ATTEMPTED: Final[str] = "checkout_attempted"
EVENT_CHECKOUT_SUCCEEDED: Final[str] = "checkout_succeeded"
EVENT_CHECKOUT_FAILED: Final[str] = "checkout_failed"

EVENT_QUOTE_DRIFT_DETECTED: Final[str] = "quote_drift_detected"
EVENT_QUOTE_REQUIRED_BLOCKED: Final[str] = "quote_required_blocked"
EVENT_QUOTE_CONSUMED: Final[str] = "quote_consumed"

EVENT_REFUND_REQUESTED: Final[str] = "refund_requested"
EVENT_DISPUTE_OPENED: Final[str] = "dispute_opened"
EVENT_DISPUTE_RESOLVED: Final[str] = "dispute_resolved"

EVENT_AUDIT: Final[str] = "audit_event"
EVENT_LEDGER: Final[str] = "ledger_event"
