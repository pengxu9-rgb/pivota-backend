"""Encrypt third-party tokens at rest, failing closed.

Built for the Shop account link (refresh tokens Pivota holds for a buyer), and
usable by any later per-buyer credential. It exists because the older helper,
`utils/encryption.py`, FAILS OPEN: when `DATA_ENCRYPTION_KEY` is missing or bad
it generates a random key, so the data it wrote becomes undecryptable after the
next restart, and nothing fails at boot. For a buyer's refresh token that turns
into a silent "every buyer must reconnect" the first time a deploy loses the env.

THE RULES:

* **No key means no cipher.** `TokenCipher.from_env(name)` raises
  `TokenCipherConfigError` when the variable is unset, empty or malformed. There
  is no generated key and no fallback. A caller turns that into "this lane is
  off", never into "store it in plaintext".
* **AES-256-GCM with a fresh 96-bit nonce per encryption.** The key is 32 random
  bytes.
* **Every ciphertext is bound to where it is stored.** The caller passes an
  `aad` context, for example `shop_account_links.refresh_token:<link id>`.
  A ciphertext copied into another row, or into another column, does not
  decrypt. That stops a database-level swap of one buyer's token into another
  buyer's row.
* **Rotation through a key ring.** The variable holds `kid:base64url(key)`
  entries separated by commas. The FIRST entry encrypts; every entry decrypts.
  Rotating means prepending a new key, re-encrypting rows at leisure, then
  dropping the old one.
* **Nothing secret reaches an exception or a log.** Errors name the problem
  (`unknown key id`, `authentication failed`) and never carry the plaintext, the
  key or the ciphertext.

Envelope format: ``tc1.<kid>.<base64url(nonce || ciphertext+tag)>``. The `kid`
travels in clear so the right key is chosen. The version prefix lets a later
scheme (for example Cloud KMS envelope encryption) live beside this one.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_PREFIX = "tc1"
KEY_BYTES = 32
NONCE_BYTES = 12
_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


class TokenCipherConfigError(RuntimeError):
    """The key ring is missing or malformed. The caller must treat the lane as off."""


class TokenCipherError(ValueError):
    """A ciphertext could not be opened: wrong context, unknown key, or tampered."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")


def _b64url_decode(text: str) -> bytes:
    # base64 silently DISCARDS characters outside its alphabet, so "@@@@"
    # would decode to b"" and surface as a misleading length error.
    text = (text or "").rstrip("=")
    if not _B64URL_RE.fullmatch(text):
        raise ValueError("not base64url")
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _parse_key_ring(raw: str, *, source: str) -> List[Tuple[str, bytes]]:
    entries = [e.strip() for e in (raw or "").split(",") if e.strip()]
    if not entries:
        raise TokenCipherConfigError(f"{source} is not set")
    ring: List[Tuple[str, bytes]] = []
    seen = set()
    for index, entry in enumerate(entries):
        kid, sep, encoded = entry.partition(":")
        if not sep or not _KID_RE.fullmatch(kid):
            raise TokenCipherConfigError(f"{source} entry {index} must be '<kid>:<base64url key>'")
        if kid in seen:
            raise TokenCipherConfigError(f"{source} repeats key id {kid!r}")
        try:
            key = _b64url_decode(encoded)
        except (binascii.Error, ValueError):
            raise TokenCipherConfigError(f"{source} entry {index} is not base64url") from None
        if len(key) != KEY_BYTES:
            raise TokenCipherConfigError(f"{source} entry {index} must decode to {KEY_BYTES} bytes")
        seen.add(kid)
        ring.append((kid, key))
    return ring


@dataclass(frozen=True)
class TokenCipher:
    """A key ring. Construct it with `from_env` or `from_key_ring`, never directly."""

    _ring: Tuple[Tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        # Direct construction must meet the same rules as a parsed ring.
        if not self._ring:
            raise TokenCipherConfigError("key ring is empty")
        kids = [kid for kid, _ in self._ring]
        if len(set(kids)) != len(kids):
            raise TokenCipherConfigError("key ring repeats a key id")
        for kid, key in self._ring:
            if not isinstance(kid, str) or not _KID_RE.fullmatch(kid):
                raise TokenCipherConfigError("key ring has an invalid key id")
            if not isinstance(key, bytes) or len(key) != KEY_BYTES:
                raise TokenCipherConfigError(f"key ring keys must be {KEY_BYTES} bytes")

    @classmethod
    def from_key_ring(cls, raw: str, *, source: str = "key ring") -> "TokenCipher":
        return cls(tuple(_parse_key_ring(raw, source=source)))

    @classmethod
    def from_env(cls, name: str) -> "TokenCipher":
        return cls.from_key_ring(os.getenv(name) or "", source=name)

    @property
    def active_key_id(self) -> str:
        return self._ring[0][0]

    def _keys(self) -> Dict[str, bytes]:
        return dict(self._ring)

    def encrypt(self, plaintext: str, *, aad: str) -> str:
        if not isinstance(plaintext, str) or not plaintext:
            raise TokenCipherError("plaintext must be a non-empty string")
        if not isinstance(aad, str) or not aad:
            raise TokenCipherError("aad context is required")
        kid, key = self._ring[0]
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), self._aad(kid, aad))
        return f"{ENVELOPE_PREFIX}.{kid}.{_b64url_encode(nonce + sealed)}"

    def decrypt(self, envelope: str, *, aad: str) -> str:
        if not isinstance(aad, str) or not aad:
            raise TokenCipherError("aad context is required")
        parts = str(envelope or "").split(".")
        if len(parts) != 3 or parts[0] != ENVELOPE_PREFIX:
            raise TokenCipherError("not a token-cipher envelope")
        _, kid, body = parts
        key = self._keys().get(kid)
        if key is None:
            raise TokenCipherError("unknown key id")
        try:
            blob = _b64url_decode(body)
        except (binascii.Error, ValueError):
            raise TokenCipherError("envelope body is not base64url") from None
        if len(blob) <= NONCE_BYTES:
            raise TokenCipherError("envelope body is too short")
        try:
            opened = AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], self._aad(kid, aad))
        except InvalidTag:
            raise TokenCipherError("authentication failed") from None
        return opened.decode("utf-8")

    def needs_rotation(self, envelope: str) -> bool:
        """True when the envelope was sealed with a key other than the active one."""
        parts = str(envelope or "").split(".")
        return len(parts) != 3 or parts[1] != self.active_key_id

    @staticmethod
    def _aad(kid: str, context: str) -> bytes:
        # The kid is authenticated too, so an attacker cannot relabel an
        # envelope to a different key id of the ring.
        return f"{ENVELOPE_PREFIX}|{kid}|{context}".encode("utf-8")

    def __repr__(self) -> str:  # never print key material
        return f"TokenCipher(active={self.active_key_id!r}, keys={len(self._ring)})"


def generate_key_entry(kid: str) -> str:
    """A new `kid:key` ring entry, for an operator to put in Secret Manager."""
    if not _KID_RE.fullmatch(kid or ""):
        raise TokenCipherConfigError("kid must be 1-32 characters of [A-Za-z0-9_-]")
    return f"{kid}:{_b64url_encode(os.urandom(KEY_BYTES))}"
