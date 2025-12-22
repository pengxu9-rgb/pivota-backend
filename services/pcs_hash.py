import hashlib
import json
from typing import Any, Optional


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    """
    Canonicalize JSON for stable hashing/idempotency.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_json(obj: Any) -> str:
    return sha256_hex(canonical_json_bytes(obj))


def chain_hash(prev_chain_hash: Optional[str], payload_sha256: str, idempotency_key: str, occurred_at_iso: str) -> str:
    """
    chain_hash = sha256(prev_chain_hash || payload_sha256 || idempotency_key || occurred_at_iso)
    """
    prev = prev_chain_hash or ""
    raw = (prev + payload_sha256 + idempotency_key + occurred_at_iso).encode("utf-8")
    return sha256_hex(raw)

