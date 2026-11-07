"""`utils.token_cipher`: third-party tokens at rest, failing closed.

The older `utils/encryption.py` generates a random key when its env is missing,
so it "works" until the next restart and then nothing decrypts. These tests pin
that this cipher refuses to exist without a key, binds every ciphertext to its
storage context, and never puts secret material into an error or a repr.
"""

from __future__ import annotations

import base64
import os

import pytest

from utils.token_cipher import (
    ENVELOPE_PREFIX,
    TokenCipher,
    TokenCipherConfigError,
    TokenCipherError,
    generate_key_entry,
)

PLAINTEXT = "shop-refresh-token-SHOULD-NOT-LEAK"
AAD = "shop_account_links.refresh_token:link_1"


def _entry(kid: str, key: bytes) -> str:
    return f"{kid}:{base64.urlsafe_b64encode(key).rstrip(b'=').decode()}"


@pytest.fixture
def ring():
    return _entry("k2", os.urandom(32)) + "," + _entry("k1", os.urandom(32))


def test_round_trip_uses_the_first_key(ring):
    cipher = TokenCipher.from_key_ring(ring)

    sealed = cipher.encrypt(PLAINTEXT, aad=AAD)

    assert sealed.startswith(f"{ENVELOPE_PREFIX}.k2.")
    assert PLAINTEXT not in sealed
    assert cipher.decrypt(sealed, aad=AAD) == PLAINTEXT


def test_each_encryption_uses_a_fresh_nonce(ring):
    cipher = TokenCipher.from_key_ring(ring)

    assert cipher.encrypt(PLAINTEXT, aad=AAD) != cipher.encrypt(PLAINTEXT, aad=AAD)


@pytest.mark.parametrize(
    "other_aad",
    ["shop_account_links.refresh_token:link_2", "shop_link_attempts.code_verifier:link_1", AAD + " "],
)
def test_a_ciphertext_moved_to_another_row_or_column_does_not_open(ring, other_aad):
    cipher = TokenCipher.from_key_ring(ring)
    sealed = cipher.encrypt(PLAINTEXT, aad=AAD)

    with pytest.raises(TokenCipherError, match="authentication failed"):
        cipher.decrypt(sealed, aad=other_aad)


def test_rotation_old_key_still_decrypts_and_is_flagged(ring):
    old_only = TokenCipher.from_key_ring(ring.split(",")[1])
    sealed_old = old_only.encrypt(PLAINTEXT, aad=AAD)
    rotated = TokenCipher.from_key_ring(ring)

    assert rotated.decrypt(sealed_old, aad=AAD) == PLAINTEXT
    assert rotated.needs_rotation(sealed_old) is True
    assert rotated.needs_rotation(rotated.encrypt(PLAINTEXT, aad=AAD)) is False


def test_a_retired_key_no_longer_decrypts(ring):
    sealed_old = TokenCipher.from_key_ring(ring.split(",")[1]).encrypt(PLAINTEXT, aad=AAD)
    new_only = TokenCipher.from_key_ring(ring.split(",")[0])

    with pytest.raises(TokenCipherError, match="unknown key id"):
        new_only.decrypt(sealed_old, aad=AAD)


def test_relabelling_the_key_id_fails_even_when_both_keys_are_in_the_ring():
    key = os.urandom(32)
    cipher = TokenCipher.from_key_ring(_entry("a", key) + "," + _entry("b", key))
    sealed = cipher.encrypt(PLAINTEXT, aad=AAD)
    relabelled = sealed.replace(".a.", ".b.", 1)

    with pytest.raises(TokenCipherError, match="authentication failed"):
        cipher.decrypt(relabelled, aad=AAD)


def test_a_tampered_body_fails(ring):
    cipher = TokenCipher.from_key_ring(ring)
    sealed = cipher.encrypt(PLAINTEXT, aad=AAD)
    prefix, kid, body = sealed.split(".")
    flipped = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1]

    with pytest.raises(TokenCipherError):
        cipher.decrypt(f"{prefix}.{kid}.{flipped}", aad=AAD)


@pytest.mark.parametrize("bad", ["", "plaintext", "tc0.k2.abc", "tc1.k2", "tc1.k2.%%%", "tc1.k2.AAAA"])
def test_malformed_envelopes_are_refused(ring, bad):
    with pytest.raises(TokenCipherError):
        TokenCipher.from_key_ring(ring).decrypt(bad, aad=AAD)


# Fixed bytes, not os.urandom: parametrize values become test ids, and the
# sharded CI sweep requires every shard to collect the same ids.
@pytest.mark.parametrize(
    "raw, needle",
    [
        pytest.param("", "is not set", id="empty"),
        pytest.param("   ,  ", "is not set", id="blank-entries"),
        pytest.param("nokid", "must be '<kid>:<base64url key>'", id="no-separator"),
        pytest.param("bad kid!:AAAA", "must be '<kid>:<base64url key>'", id="bad-kid"),
        pytest.param(
            "k1:" + base64.urlsafe_b64encode(b"\x01" * 16).rstrip(b"=").decode(),
            "must decode to 32 bytes",
            id="short-key",
        ),
        pytest.param("k1:@@@@", "is not base64url", id="not-base64url"),
        pytest.param(
            _entry("k1", b"\x02" * 32) + "," + _entry("k1", b"\x03" * 32), "repeats key id", id="repeated-kid"
        ),
    ],
)
def test_a_missing_or_malformed_ring_refuses_to_build(raw, needle):
    with pytest.raises(TokenCipherConfigError, match=needle):
        TokenCipher.from_key_ring(raw, source="SHOP_TOKEN_ENCRYPTION_KEY")


def test_from_env_fails_closed_when_unset(monkeypatch):
    monkeypatch.delenv("SHOP_TOKEN_ENCRYPTION_KEY", raising=False)

    with pytest.raises(TokenCipherConfigError, match="SHOP_TOKEN_ENCRYPTION_KEY is not set"):
        TokenCipher.from_env("SHOP_TOKEN_ENCRYPTION_KEY")


def test_from_env_reads_the_ring(monkeypatch, ring):
    monkeypatch.setenv("SHOP_TOKEN_ENCRYPTION_KEY", ring)

    cipher = TokenCipher.from_env("SHOP_TOKEN_ENCRYPTION_KEY")

    assert cipher.active_key_id == "k2"


def test_no_secret_material_in_errors_or_repr(ring):
    cipher = TokenCipher.from_key_ring(ring)
    sealed = cipher.encrypt(PLAINTEXT, aad=AAD)
    key_text = ring.split(",")[0].split(":")[1]

    with pytest.raises(TokenCipherError) as excinfo:
        cipher.decrypt(sealed, aad="elsewhere")

    key_bytes = base64.urlsafe_b64decode(key_text + "=" * (-len(key_text) % 4))
    for text in (str(excinfo.value), repr(cipher)):
        assert PLAINTEXT not in text
        assert key_text not in text
        assert repr(key_bytes) not in text  # a default repr would print the key as a bytes literal
        assert sealed.split(".")[2] not in text


@pytest.mark.parametrize("plaintext, aad", [("", AAD), (PLAINTEXT, "")])
def test_empty_plaintext_or_context_is_refused(ring, plaintext, aad):
    with pytest.raises(TokenCipherError):
        TokenCipher.from_key_ring(ring).encrypt(plaintext, aad=aad)


def test_generated_entries_parse_and_differ():
    a, b = generate_key_entry("k9"), generate_key_entry("k9")

    assert a != b
    assert TokenCipher.from_key_ring(a).active_key_id == "k9"
    with pytest.raises(TokenCipherConfigError):
        generate_key_entry("bad kid")


def test_a_trailing_newline_is_not_base64url_and_not_a_kid(ring):
    cipher = TokenCipher.from_key_ring(ring)
    sealed = cipher.encrypt(PLAINTEXT, aad=AAD)

    with pytest.raises(TokenCipherError):
        cipher.decrypt(sealed + "\n", aad=AAD)
    with pytest.raises(TokenCipherConfigError):
        TokenCipher.from_key_ring("k1\n:" + ring.split(",")[0].split(":")[1])


@pytest.mark.parametrize(
    "ring_tuple",
    [(), (("k1", b"short"),), (("bad kid", b"x" * 32),), (("k1", b"x" * 32), ("k1", b"y" * 32))],
)
def test_direct_construction_meets_the_same_rules(ring_tuple):
    with pytest.raises(TokenCipherConfigError):
        TokenCipher(ring_tuple)


# --- review follow-ups (be#2710 P3-1, P3-2, P3-4) ----------------------------


def _sealed_parts(ring):
    cipher = TokenCipher.from_key_ring(ring)
    prefix, kid, body = cipher.encrypt(PLAINTEXT, aad=AAD).split(".")
    return cipher, prefix, kid, body


@pytest.mark.parametrize("padding", ["=", "==", "===="])
def test_a_padded_envelope_body_is_refused(ring, padding):
    """One ciphertext must have exactly one valid envelope string."""
    cipher, prefix, kid, body = _sealed_parts(ring)

    with pytest.raises(TokenCipherError, match="not base64url"):
        cipher.decrypt(f"{prefix}.{kid}.{body}{padding}", aad=AAD)


def test_a_non_canonical_last_character_is_refused(ring):
    """A body whose length leaves spare bits in its last character decodes the
    same for several last characters; only the canonical one is accepted."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for _ in range(20):
        cipher, prefix, kid, body = _sealed_parts(ring)
        if len(body) % 4:
            break
    else:
        pytest.skip("no envelope with spare bits in 20 tries")
    spare = {2: 4, 3: 2}[len(body) % 4]  # unused low bits in the final character
    last = alphabet.index(body[-1])
    twin = alphabet[last ^ 1] if spare else None
    assert twin and twin != body[-1]

    assert cipher.decrypt(f"{prefix}.{kid}.{body}", aad=AAD) == PLAINTEXT
    with pytest.raises(TokenCipherError, match="not base64url"):
        cipher.decrypt(f"{prefix}.{kid}.{body[:-1]}{twin}", aad=AAD)


@pytest.mark.parametrize("padding", ["=", "==", "====="])
def test_a_padded_ring_entry_is_refused(padding):
    entry = _entry("k1", b"\x04" * 32)

    with pytest.raises(TokenCipherConfigError, match="is not base64url"):
        TokenCipher.from_key_ring(entry + padding)


def test_key_bytes_are_not_reachable_through_asdict_or_equality(ring):
    import dataclasses

    a, b = TokenCipher.from_key_ring(ring), TokenCipher.from_key_ring(ring)

    assert not dataclasses.is_dataclass(a)
    assert a != b  # identity equality: nothing compares the keys
    assert not hasattr(a, "__dict__")


def _all_frame_locals(exc):
    """Locals of every frame the exception passed through below the test itself
    (the first traceback entry is the test's own frame, which holds the key it made)."""
    tb = exc.__traceback__.tb_next
    while tb is not None:
        yield from tb.tb_frame.f_locals.values()
        tb = tb.tb_next


def _holds(value, needles):
    if isinstance(value, (bytes, str)):
        return any(n in value for n in needles if type(n) is type(value))
    if isinstance(value, (tuple, list)):
        return any(_holds(v, needles) for v in value)
    if isinstance(value, dict):
        return any(_holds(v, needles) for v in value.values())
    return False


def test_a_failed_decrypt_traceback_holds_no_key_bytes():
    key = b"\x05" * 32
    cipher = TokenCipher.from_key_ring(_entry("k1", key))
    sealed = cipher.encrypt(PLAINTEXT, aad=AAD)

    with pytest.raises(TokenCipherError) as excinfo:
        cipher.decrypt(sealed, aad="elsewhere")

    assert not any(_holds(v, [key]) for v in _all_frame_locals(excinfo.value))


def test_a_config_error_traceback_holds_no_valid_key_from_the_ring():
    good = _entry("k1", b"\x06" * 32)

    with pytest.raises(TokenCipherConfigError) as excinfo:
        TokenCipher.from_key_ring(good + ",k2:@@@@", source="SHOP_TOKEN_ENCRYPTION_KEY")

    secret = good.split(":")[1]
    assert not any(_holds(v, [secret, b"\x06" * 32]) for v in _all_frame_locals(excinfo.value))
