"""Alert lines (FIX_PLAN R-03 today; the module is the place every money-path alert event goes):
ONE structured line per event.

There is no alerting transport in this repo (services/merchant_order_gap_alert.py says why), so an
alert is a LOG LINE that a log-based metric counts and a policy pages on. The definitions live in
infra/gcp/monitoring/money_path_alerts.json; every metric there keys on the text this module
writes, and tests/test_money_path_alerts.py evaluates those filters against lines the real
producers emit.

THE LINE. Through the "pivota" logger (utils/logger.py), which is the only logger whose lines
survive in production at every level (nothing configures root; module INFO is dropped). Its
handler writes `[ts] LEVEL - <message>` to stdout, so Cloud Logging stores the line as
textPayload (not jsonPayload: the timestamp prefix stops it parsing as JSON). The message is

    money_alert: {"event":"<name>",...}

compact JSON with sorted keys, so a filter can match `"event":"<name>"` exactly.

NO PII, BY CONSTRUCTION AS FAR AS THIS MODULE CAN: values must be scalars (str, int, float, bool,
None); strings are cut to 128 characters with control characters removed; keys must be short
snake_case names, and the obvious contact names are refused outright. Callers pass ids, codes and
counts. Never raises: an alert that crashes the money path it reports on is worse than no alert.
"""

from __future__ import annotations

import json
import re
from typing import Any

from utils.logger import logger

PREFIX = "money_alert: "

#: Every event name a producer may emit, and the metric each one feeds. The JSON definitions file
#: names the same strings; a test holds the two together.
EVENTS = frozenset({
    "freshness_directory_past_limit",        # R-03: a directory merchant's fact is stale or missing
})

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_REFUSED_KEYS = frozenset({
    "email", "buyer_email", "customer_email", "shipping_address", "address", "phone", "name",
    "customer_name", "card", "pan", "token", "secret", "api_key", "password",
})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_TEXT = 128


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    return _CONTROL.sub("", str(value))[:MAX_TEXT]


def render(event: str, /, **fields: Any) -> str:
    """The message text (without the logger's prefix). Raises ValueError on a bad event or key."""
    if event not in EVENTS:
        raise ValueError(f"unknown money alert event {event!r}")
    body = {"event": event}
    for key, value in fields.items():
        if not _KEY_RE.match(key) or key in _REFUSED_KEYS or key == "event":
            raise ValueError(f"money alert field {key!r} is not allowed")
        body[key] = _scalar(value)
    return PREFIX + json.dumps(body, sort_keys=True, separators=(",", ":"))


def emit(event: str, /, *, level: str = "error", **fields: Any) -> None:
    """Write one alert line. `level` is "error" or "warning". Never raises."""
    try:
        line = render(event, **fields)
    except Exception as exc:  # noqa: BLE001 - a programming error must still leave a trace
        try:
            logger.error("money_alert: render failed for %r (%s)", event, type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        (logger.warning if level == "warning" else logger.error)("%s", line)
    except Exception:  # noqa: BLE001
        pass
