"""Single use for a request-bound issuing-agent assertion's nonce (`jti`, services/bound_issuing_assertion.py).

`claim(jti, ttl)` answers True the FIRST time a nonce is seen and False on every later sighting
within `ttl` seconds. The TTL covers the token's whole acceptance window (2 x MAX_SKEW_SECONDS_V2
plus slack), so a token can never be accepted twice while it is fresh, and an expired one is refused
by its timestamp anyway.

STORES, in order:
  1. Redis, when REDIS_URL is configured (utils/redis_client.py): `SET pia:jti:<sha256(jti)> 1 NX EX
     ttl`. Shared by every instance, so a replay to another Cloud Run instance is refused too.
  2. An in-process map, when there is no Redis client or a Redis call fails. BOUNDED: at most
     PARTNER_ASSERTION_REPLAY_CACHE_MAX entries (default 50 000), expired entries are dropped first,
     and when the map is full of live entries a new nonce is REFUSED (fail closed) rather than an
     older one evicted, because evicting a live nonce would reopen its replay. It is per instance:
     with no Redis, a replay that lands on ANOTHER instance within the window is not detected. The
     binding (method, path, resource id, body hash) and the 60 s window still hold there; the
     order-create replay rule (be#2760) and quote ownership still apply on top.
The store used is returned with the verdict so callers can log it.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict
from typing import Optional, Tuple

from utils.logger import logger

_REDIS_PREFIX = "pia:jti:"
_DEFAULT_MAX = 50_000

_local: "OrderedDict[str, float]" = OrderedDict()


def _max_entries() -> int:
    try:
        value = int(str(os.getenv("PARTNER_ASSERTION_REPLAY_CACHE_MAX") or "").strip())
    except ValueError:
        return _DEFAULT_MAX
    return value if value > 0 else _DEFAULT_MAX


def _key(jti: str) -> str:
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _claim_local(key: str, ttl: int, now: float) -> bool:
    while _local:
        oldest_key, expires_at = next(iter(_local.items()))
        if expires_at > now:
            break
        _local.popitem(last=False)
    expires = _local.get(key)
    if expires is not None and expires > now:
        return False
    if len(_local) >= _max_entries():
        logger.warning("assertion_replay_guard: in-process nonce store full; refusing a new nonce")
        return False
    _local[key] = now + ttl
    _local.move_to_end(key)
    return True


async def claim(jti: str, ttl: int, *, now: Optional[float] = None) -> Tuple[bool, str]:
    """(first_use, store). Never raises."""
    key = _key(jti)
    clock = time.time() if now is None else now
    try:
        from utils.redis_client import get_redis_client

        client = get_redis_client()
    except Exception:  # noqa: BLE001
        client = None
    if client is not None:
        try:
            ok = await client.set(f"{_REDIS_PREFIX}{key}", "1", nx=True, ex=int(ttl))
            return bool(ok), "redis"
        except Exception as exc:  # noqa: BLE001 -- fall back to the bounded local store, loudly
            logger.warning("assertion_replay_guard: redis claim failed (%s); using the in-process store",
                           type(exc).__name__)
    return _claim_local(key, int(ttl), clock), "local"


def reset_local_store() -> None:
    """Tests only."""
    _local.clear()
