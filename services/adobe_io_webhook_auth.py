from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Dict, Optional, Tuple

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


ADOBE_IO_PUBLIC_KEY_ORIGIN = "https://static.adobeioevents.com"
ADOBE_IO_KEY_CACHE_SECONDS = 23 * 60 * 60
ADOBE_IO_NEGATIVE_CACHE_SECONDS = 30
_PUBLIC_KEY_PATH_RE = re.compile(
    r"^/prod/keys/pub-key-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.pem$"
)
_key_cache: Dict[str, Tuple[float, rsa.RSAPublicKey]] = {}
_negative_key_cache: Dict[str, float] = {}
_key_fetch_tasks: Dict[str, asyncio.Task[rsa.RSAPublicKey]] = {}
_key_fetch_state_lock = asyncio.Lock()
_key_fetch_slots = asyncio.Semaphore(4)


class AdobeIOWebhookAuthError(ValueError):
    pass


class AdobeIOPublicKeyUnavailable(RuntimeError):
    pass


def _validate_public_key_path(value: Optional[str]) -> str:
    path = str(value or "").strip()
    if not _PUBLIC_KEY_PATH_RE.fullmatch(path):
        raise AdobeIOWebhookAuthError("Invalid Adobe I/O public key path")
    return path


def is_adobe_io_key_cached(path: Optional[str]) -> bool:
    try:
        normalized = _validate_public_key_path(path)
    except AdobeIOWebhookAuthError:
        return False
    cached = _key_cache.get(normalized)
    return bool(cached and cached[0] > time.monotonic())


async def _remove_completed_key_fetch(path: str, task: asyncio.Task[rsa.RSAPublicKey]) -> None:
    async with _key_fetch_state_lock:
        if _key_fetch_tasks.get(path) is task:
            _key_fetch_tasks.pop(path, None)


def _key_fetch_done(path: str, task: asyncio.Task[rsa.RSAPublicKey]) -> None:
    # Always retrieve a background exception, including when every HTTP waiter
    # disconnected before the shared fetch completed.
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    asyncio.create_task(_remove_completed_key_fetch(path, task))


async def _download_public_key(path: str) -> rsa.RSAPublicKey:
    try:
        async with asyncio.timeout(6.0):
            async with _key_fetch_slots:
                async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                    response = await client.get(f"{ADOBE_IO_PUBLIC_KEY_ORIGIN}{path}")
    except (TimeoutError, httpx.HTTPError) as exc:
        raise AdobeIOPublicKeyUnavailable("Adobe I/O public key service is unavailable") from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise AdobeIOPublicKeyUnavailable("Adobe I/O public key service is unavailable")
    if response.status_code != 200 or len(response.content) > 64_000:
        raise AdobeIOWebhookAuthError("Unable to load Adobe I/O public key")
    try:
        key = serialization.load_pem_public_key(response.content)
    except (TypeError, ValueError) as exc:
        raise AdobeIOWebhookAuthError("Invalid Adobe I/O public key") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise AdobeIOWebhookAuthError("Invalid Adobe I/O public key")
    return key


async def _fetch_public_key(path: str) -> rsa.RSAPublicKey:
    path = _validate_public_key_path(path)
    now = time.monotonic()
    cached = _key_cache.get(path)
    if cached and cached[0] > now:
        return cached[1]
    if _negative_key_cache.get(path, 0) > now:
        raise AdobeIOWebhookAuthError("Unable to load Adobe I/O public key")

    async with _key_fetch_state_lock:
        now = time.monotonic()
        cached = _key_cache.get(path)
        if cached and cached[0] > now:
            return cached[1]
        if _negative_key_cache.get(path, 0) > now:
            raise AdobeIOWebhookAuthError("Unable to load Adobe I/O public key")
        task = _key_fetch_tasks.get(path)
        if task is None:
            task = asyncio.create_task(_download_public_key(path))
            _key_fetch_tasks[path] = task
            task.add_done_callback(
                lambda completed, key_path=path: _key_fetch_done(key_path, completed)
            )
    try:
        # A client disconnect cancels only that request waiter, never the shared
        # Adobe key download needed by concurrent legitimate deliveries.
        key = await asyncio.shield(task)
    except AdobeIOWebhookAuthError:
        _negative_key_cache[path] = time.monotonic() + ADOBE_IO_NEGATIVE_CACHE_SECONDS
        if len(_negative_key_cache) > 64:
            oldest = min(_negative_key_cache, key=_negative_key_cache.get)
            _negative_key_cache.pop(oldest, None)
        raise
    finally:
        async with _key_fetch_state_lock:
            if _key_fetch_tasks.get(path) is task and task.done():
                _key_fetch_tasks.pop(path, None)

    now = time.monotonic()
    if len(_key_cache) >= 16:
        oldest = min(_key_cache, key=lambda cache_path: _key_cache[cache_path][0])
        _key_cache.pop(oldest, None)
    _key_cache[path] = (now + ADOBE_IO_KEY_CACHE_SECONDS, key)
    return key


async def verify_adobe_io_signature(
    raw_body: bytes,
    *,
    signature_1: Optional[str],
    signature_2: Optional[str],
    public_key_path_1: Optional[str],
    public_key_path_2: Optional[str],
) -> bool:
    pairs = (
        (signature_1, public_key_path_1),
        (signature_2, public_key_path_2),
    )
    attempted = False
    unavailable = False
    for signature, raw_path in pairs:
        if not signature or not raw_path:
            continue
        attempted = True
        try:
            path = _validate_public_key_path(raw_path)
            decoded = base64.b64decode(signature.strip(), validate=True)
            public_key = await _fetch_public_key(path)
            public_key.verify(decoded, raw_body, padding.PKCS1v15(), hashes.SHA256())
            return True
        except AdobeIOPublicKeyUnavailable:
            unavailable = True
            continue
        except (AdobeIOWebhookAuthError, InvalidSignature, TypeError, ValueError):
            continue
    if unavailable:
        raise AdobeIOPublicKeyUnavailable("Adobe I/O public key service is unavailable")
    return False if attempted else False
