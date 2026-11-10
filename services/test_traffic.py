"""The ONE test-traffic predicate for money reporting (FIX_PLAN O-07 / decision D-11).

An order, attribution edge or click is TEST traffic when any server-controlled signal says so:

  synthetic_call   the request carries our own verified eval-traffic assertion
                   (services.synthetic_call.is_synthetic_call). Note: that verdict is honoured only
                   on the read tools' routes, so an order-create request is never synthetic today;
                   the leg covers what those routes write (issued clicks) and any future scope.
  synthetic_link   a click minted inside synthetic traffic (outbound token `syn`=1).
  ops_canary       the payment canary's own order (metadata.ops_canary, a server-only order key:
                   routes.order_routes strips it from caller metadata).
  test_merchant    the merchant is a rig: services.test_merchant_policy.static_test_merchant_ids
                   (the baked-in rig list plus PIVOTA_TEST_MERCHANT_IDS).
  test_psp_probe   the order the server GRANTED the test-processor bypass at create
                   (routes.order_routes._apply_server_granted_test_psp_stamp: ALLOW_TEST_PSP_PROBE on,
                   merchant in TEST_PSP_PROBE_MERCHANTS, AND every active processor of the merchant
                   normalises to TEST). Such an order can only be paid on a test processor. The grant
                   writes `metadata.test_psp_probe_granted`, a server-only order key the create route
                   strips from caller metadata. Merchant membership in the probe list is NOT a signal
                   on its own: a probe merchant's real (live-processor) orders stay real (W3 F2).
  shopify_test     a Shopify order payload with `test: true` (Shopify's own test-order flag; read only
                   from a Shopify-signed webhook or our own poll, never from a self-report).

Caller-supplied values never count. `metadata.synthetic`, a surface name, or an agent id a caller
chose are NOT signals: a caller who could mark its own traffic "test" could take its orders out of
billing. For the same reason `strip_caller_markers` removes the stamp keys from any caller metadata
before a writer stamps.

STAMP. With TEST_TRAFFIC_STAMP_ENABLED literally 'true', writers stamp a test row's JSON metadata
(orders.metadata, commerce_attribution_edges.metadata, surface_click_events.context,
outbound_click_events.context) with `test_traffic: true` and `test_traffic_reasons: [...]`. A real
row is never touched. Off (the default), nothing is stamped.

EXCLUDE. Money reads (GMV roll-up, monthly statements, outcome signal, settlement scope) drop rows
whose metadata has `test_traffic` true (NULL-safe, like the `inferred` gate of #1481).
With the stamp flag off no row carries the key, so every read answers exactly as before.
Agent share accrual and partner settlement read billing rows built from the GMV roll-up, so they
inherit the exclusion and need no stamp of their own.

Rows written before stamping was enabled are NOT stamped; a backfill needs its own go.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

STAMP_FLAG = "TEST_TRAFFIC_STAMP_ENABLED"

#: The stamp. A writer adds these keys to a test row's metadata/context; nothing else does.
STAMP_KEY = "test_traffic"
REASONS_KEY = "test_traffic_reasons"
_STAMP_KEYS = (STAMP_KEY, REASONS_KEY)

REASON_SYNTHETIC_CALL = "synthetic_call"
REASON_SYNTHETIC_LINK = "synthetic_link"
REASON_OPS_CANARY = "ops_canary"
REASON_TEST_MERCHANT = "test_merchant"
REASON_TEST_PSP_PROBE = "test_psp_probe"
REASON_SHOPIFY_TEST = "shopify_test"

#: Written by routes.order_routes._apply_server_granted_test_psp_stamp on a granted order (only
#: while stamping is on), and stripped from caller order metadata as a server-only key.
TEST_PSP_GRANT_KEY = "test_psp_probe_granted"


def stamp_enabled() -> bool:
    """True only when TEST_TRAFFIC_STAMP_ENABLED is literally 'true'. Read at call time."""
    return (os.getenv(STAMP_FLAG) or "").strip().lower() == "true"


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _test_merchant_ids() -> set:
    from services.test_merchant_policy import static_test_merchant_ids

    return {m.strip().lower() for m in static_test_merchant_ids() if str(m or "").strip()}


def _current_call_is_synthetic() -> bool:
    try:
        from services.synthetic_call import is_synthetic_call

        return bool(is_synthetic_call())
    except Exception:  # noqa: BLE001 — a predicate must never break a writer
        return False


def classify_test_traffic(
    *,
    merchant_ids: Iterable[Optional[str]] = (),
    server_metadata: Optional[Mapping[str, Any]] = None,
    shopify_order: Optional[Mapping[str, Any]] = None,
    synthetic_link: bool = False,
    synthetic_call: Optional[bool] = None,
) -> List[str]:
    """Every reason this row is test traffic, sorted; [] for real traffic.

    merchant_ids     the row's merchant(s) (an edge's subject and converting merchant, say).
    server_metadata  metadata whose `ops_canary` / `test_psp_probe_granted` only the server can
                     set (order metadata after the caller's server-only keys were stripped:
                     routes.order_routes on create, routes.ap2_routes on the AP2 edge).
    shopify_order    a Shopify-SIGNED order payload (webhook or our own poll) — never a self-report.
    synthetic_link   the click's outbound token was minted inside synthetic traffic.
    synthetic_call   override for the request verdict; None reads the current request's.
    """
    reasons = set()
    if synthetic_call is None:
        synthetic_call = _current_call_is_synthetic()
    if synthetic_call:
        reasons.add(REASON_SYNTHETIC_CALL)
    if synthetic_link:
        reasons.add(REASON_SYNTHETIC_LINK)
    if isinstance(server_metadata, Mapping) and _truthy(server_metadata.get("ops_canary")):
        reasons.add(REASON_OPS_CANARY)
    if isinstance(server_metadata, Mapping) and server_metadata.get(TEST_PSP_GRANT_KEY) is True:
        reasons.add(REASON_TEST_PSP_PROBE)
    ids = {str(m or "").strip().lower() for m in merchant_ids if str(m or "").strip()}
    if ids:
        if ids & _test_merchant_ids():
            reasons.add(REASON_TEST_MERCHANT)
    if isinstance(shopify_order, Mapping) and shopify_order.get("test") is True:
        reasons.add(REASON_SHOPIFY_TEST)
    return sorted(reasons)


def is_test_traffic(**kwargs: Any) -> bool:
    """The predicate: True when `classify_test_traffic(**kwargs)` names any reason."""
    return bool(classify_test_traffic(**kwargs))


def is_stamped_test(metadata: Any) -> bool:
    """Whether a stored row's metadata/context carries the stamp (the read-side twin of the SQL)."""
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except Exception:  # noqa: BLE001
            return False
    return isinstance(metadata, Mapping) and metadata.get(STAMP_KEY) is True


def strip_caller_markers(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Remove the stamp keys from caller-supplied metadata IN PLACE (and return it).

    Unconditional, whatever the flag: the money reads exclude a row carrying the stamp, so a
    caller-supplied stamp would take a real order out of billing.
    """
    if isinstance(metadata, dict):
        for key in _STAMP_KEYS:
            metadata.pop(key, None)
    return metadata


def stamp(metadata: Optional[Dict[str, Any]], reasons: List[str]) -> Optional[Dict[str, Any]]:
    """Stamp `metadata` IN PLACE when the flag is on and `reasons` is non-empty; returns it.

    A real row (no reasons), or any row with the flag off, is returned unchanged. A None metadata
    on a test row becomes a dict holding only the stamp.
    """
    if not reasons or not stamp_enabled():
        return metadata
    target = metadata if isinstance(metadata, dict) else {}
    target[STAMP_KEY] = True
    target[REASONS_KEY] = list(reasons)
    return target


def not_test_traffic_sql(metadata_column: str) -> str:
    """NULL-safe SQL: TRUE unless `metadata_column` (a JSON/JSONB column) carries the stamp.

    Cast-free on purpose: a `::boolean` cast of a stray non-boolean value would make the whole
    roll-up fail, where this reads it as not-test. '1' is listed because SQLite's ->> returns a
    JSON true as the integer 1 (Postgres returns the text 'true').
    """
    return f"COALESCE(CAST({metadata_column}->>'{STAMP_KEY}' AS TEXT), '') NOT IN ('true', '1')"
