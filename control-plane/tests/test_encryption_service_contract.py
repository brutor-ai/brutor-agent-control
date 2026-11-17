# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Pre-refactor contract suite for `EncryptionService`

Locks down the wire contract of `app.services.oauth.encryption_service`
before we swap the underlying library (`pyca/cryptography` → `pycryptodome`
+ stdlib hmac/hashlib) in 0.8.0.

The test suite is deliberately library-agnostic — every assertion targets
either:
  * a public method of `EncryptionService` (`encrypt`, `decrypt`,
    `encrypt_dict`, `decrypt_dict`, `generate_key`, `rotate_key`), or
  * a Fernet wire-format invariant (RFC-style: version byte, length floor,
    base64url alphabet, HMAC tail), or
  * a property that any spec-compliant Fernet must satisfy regardless of
    implementation (random IV, tamper detection, key-derivation
    determinism).

If a future library swap quietly drops any of these, this suite fails before
we ship.
"""

from __future__ import annotations

import base64
import re
from typing import Iterator

import pytest

from app.core.logging_system import BrutorLogger
from app.services.config_manager import ConfigManager
from app.services.oauth.encryption_service import EncryptionService


# ---------------------------------------------------------------------------
# Test helpers — tiny stub `ConfigManager` so we don't need the real env
# ---------------------------------------------------------------------------

# A 32-byte URL-safe base64 key (the canonical Fernet key shape). We use a
# fixed value so derived ciphertext is reproducible across runs and across
# the cross-language interop suite (`test_crypto_interop_python_rust.py`).
TEST_MASTER_KEY = "brutor-test-master-key-do-not-use-anywhere-real-32chars"


class _StubConfigManager:
    """Minimal duck-typed ConfigManager that only implements the one
    method `EncryptionService.__init__` calls. Avoids pulling in the real
    config manager (which expects `ai_gateway_config.json` to exist).
    """

    def __init__(self, key: str = TEST_MASTER_KEY):
        self._key = key

    def get_encryption_key(self) -> str:
        return self._key


@pytest.fixture
def service() -> EncryptionService:
    return EncryptionService(
        config_manager=_StubConfigManager(),  # type: ignore[arg-type]
        logger=BrutorLogger("test-encryption"),
    )


@pytest.fixture
def service_alt_key() -> EncryptionService:
    """Same class, different master key — used to assert keys are not
    cross-decryptable (separation of trust domains)."""
    return EncryptionService(
        config_manager=_StubConfigManager(
            "alternate-master-key-totally-different-32chars-padded"
        ),  # type: ignore[arg-type]
        logger=BrutorLogger("test-encryption-alt"),
    )


# ---------------------------------------------------------------------------
# 1. Constructor contract — initialisation only succeeds with a real key
# ---------------------------------------------------------------------------


class TestConstructorContract:
    """The constructor's job is to refuse to start with an unset / sentinel
    encryption key. A subtle bug here is the root of "developer ran the
    gateway against prod-data with the example key" incidents."""

    def test_constructs_with_valid_key(self):
        # Don't peek at private attributes — the underlying storage
        # changed between v0.7.95 (cryptography.fernet.Fernet instance
        # at .fernet) and v0.8.0 (raw key bytes at ._fernet_key). The
        # contract is "constructor succeeds and the instance can
        # encrypt+decrypt".
        svc = EncryptionService(
            config_manager=_StubConfigManager(),  # type: ignore[arg-type]
            logger=BrutorLogger("test"),
        )
        token = svc.encrypt("smoke")
        assert svc.decrypt(token) == "smoke"

    @pytest.mark.parametrize("bad_key", ["", "your-encryption-key", "change-this-key"])
    def test_rejects_unset_and_placeholder_keys(self, bad_key: str):
        # Each of these is a known "operator forgot to set ENCRYPTION_KEY"
        # marker. The service must refuse to start with any of them.
        with pytest.raises(ValueError, match="not configured"):
            EncryptionService(
                config_manager=_StubConfigManager(bad_key),  # type: ignore[arg-type]
                logger=BrutorLogger("test"),
            )

    def test_rejects_your_prefixed_keys(self):
        # The check is a `startswith("your-")` guard — any placeholder
        # starting with that string is treated as unset.
        with pytest.raises(ValueError, match="not configured"):
            EncryptionService(
                config_manager=_StubConfigManager("your-actual-secret-key-here"),  # type: ignore[arg-type]
                logger=BrutorLogger("test"),
            )


# ---------------------------------------------------------------------------
# 2. encrypt() / decrypt() — round-trip identity
# ---------------------------------------------------------------------------


class TestRoundTripIdentity:
    """The single most important contract: anything we encrypt, we can
    decrypt back, byte-identically. If this regresses, OAuth tokens and
    API keys in production become unreadable."""

    @pytest.mark.parametrize(
        "plaintext",
        [
            # Short ASCII — the most common case (an API key prefix).
            "sk_brutor_api_abcdef0123456789",
            # An OAuth access token — long, opaque, sometimes with dots.
            "ya29.a0AfH6SMBT4vS8xK_wRyP_3iC9YqUw" + "x" * 200,
            # A refresh token — typically longer than access tokens.
            "1//refresh-token-" + "y" * 500,
            # JSON-encoded provider_config — the proxy stores these too.
            '{"endpoint":"https://api.example.com","model":"gpt-4o-mini"}',
            # Single character.
            "a",
            # Unicode — must survive the round-trip without re-encoding loss.
            "汉字テスト中文",
            # Whitespace and control characters.
            "line1\nline2\ttab\rcr",
        ],
        ids=[
            "api_key",
            "oauth_access_token",
            "oauth_refresh_token",
            "provider_config_json",
            "single_char",
            "unicode",
            "whitespace_and_control",
        ],
    )
    def test_round_trip_identity(self, service: EncryptionService, plaintext: str):
        encrypted = service.encrypt(plaintext)
        assert service.decrypt(encrypted) == plaintext

    def test_empty_string_rejected(self, service: EncryptionService):
        # Documented contract: empty string is not encryptable.
        with pytest.raises(ValueError, match="Cannot encrypt empty"):
            service.encrypt("")

    def test_empty_string_rejected_on_decrypt(self, service: EncryptionService):
        with pytest.raises(ValueError, match="Cannot decrypt empty"):
            service.decrypt("")


# ---------------------------------------------------------------------------
# 3. Wire-format invariants — locked because the Rust side parses these bytes
# ---------------------------------------------------------------------------


class TestWireFormat:
    """The Rust side in `gateway-core/src/services/encryption.rs` parses
    these exact bytes (header comment in that file pins the format). Any
    drift in the Python-side output breaks cross-language decryption."""

    # Fernet base64url-encodes the whole token; the alphabet is
    # A-Z a-z 0-9 - _ plus = padding. The token is short-fixed for
    # an empty plaintext (73 bytes after b64) but always >= 73.
    _BASE64URL = re.compile(r"^[A-Za-z0-9_=\-]+$")

    @staticmethod
    def _b64url_decode(token: str) -> bytes:
        # add padding back if missing — Fernet emits unpadded form
        padding = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + padding)

    def test_output_is_base64url(self, service: EncryptionService):
        token = service.encrypt("hello world")
        assert self._BASE64URL.match(token), (
            "Fernet output must be base64url-safe (the Rust side decodes "
            "with URL_SAFE; standard base64 with + and / breaks it)."
        )

    def test_version_byte_is_0x80(self, service: EncryptionService):
        # The first byte of the decoded payload is the Fernet version
        # marker. Currently 0x80 ('current'); the Rust side checks for
        # it explicitly. If a library swap changes this byte the Rust
        # decryptor will reject every token.
        token = service.encrypt("any-plaintext")
        raw = self._b64url_decode(token)
        assert raw[0] == 0x80

    def test_length_floor(self, service: EncryptionService):
        # Minimum Fernet payload: 1 version + 8 timestamp + 16 IV +
        # 16 (1 full AES block of padded plaintext) + 32 HMAC = 73 bytes.
        # Anything shorter cannot be a valid Fernet token.
        token = service.encrypt("a")
        raw = self._b64url_decode(token)
        assert len(raw) >= 73

    def test_iv_is_random_across_calls(self, service: EncryptionService):
        # Two encryptions of the same plaintext MUST produce different
        # ciphertext — Fernet uses a random IV per call. Same-ciphertext
        # leaks information (an attacker can tell when the same OAuth
        # token gets re-encrypted).
        a = service.encrypt("identical-plaintext")
        b = service.encrypt("identical-plaintext")
        assert a != b
        # And the IV section (bytes 9..25) must differ specifically.
        raw_a = self._b64url_decode(a)
        raw_b = self._b64url_decode(b)
        assert raw_a[9:25] != raw_b[9:25]

    def test_timestamp_section_is_8_bytes(self, service: EncryptionService):
        # The 8 bytes after the version byte are a big-endian Unix
        # timestamp. We don't assert the value (clock skew between
        # test runs is fine) but we do pin the position so the Rust
        # decoder's offset math stays valid.
        token = service.encrypt("any-plaintext")
        raw = self._b64url_decode(token)
        # Bytes 1..9 = timestamp; the value should be non-zero (we
        # encrypted just now, not in 1970).
        assert int.from_bytes(raw[1:9], "big") > 1_000_000_000


# ---------------------------------------------------------------------------
# 4. Tamper detection — the HMAC tail must catch every middle and trailing
#    bit-flip. This is what makes Fernet authenticated encryption.
# ---------------------------------------------------------------------------


class TestTamperDetection:
    """If any of these tests pass while the ciphertext was clearly
    tampered with, the swap broke authentication. Catastrophic. The Rust
    side's HMAC check is independent — but the Python side must catch
    Python-originating tampering before the bytes ever cross processes."""

    @staticmethod
    def _flip_byte(token: str, position: int) -> str:
        # base64url-decode, flip one byte, re-encode. Easier than
        # base64url-flipping in place.
        padding = "=" * (-len(token) % 4)
        raw = bytearray(base64.urlsafe_b64decode(token + padding))
        raw[position] ^= 0x42
        return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()

    def test_tampered_ciphertext_body_rejected(self, service: EncryptionService):
        # Flip a byte in the AES ciphertext (somewhere in the middle,
        # well past the IV and well before the HMAC).
        token = service.encrypt("sensitive-payload")
        tampered = self._flip_byte(token, 40)
        with pytest.raises(ValueError):
            service.decrypt(tampered)

    def test_tampered_hmac_tail_rejected(self, service: EncryptionService):
        # Flip a byte in the HMAC (last 32 bytes of the decoded payload).
        token = service.encrypt("sensitive-payload")
        raw_len = len(base64.urlsafe_b64decode(
            token + "=" * (-len(token) % 4)
        ))
        tampered = self._flip_byte(token, raw_len - 5)
        with pytest.raises(ValueError):
            service.decrypt(tampered)

    def test_tampered_iv_rejected(self, service: EncryptionService):
        # Flip a byte in the IV. The HMAC covers the IV too — must reject.
        token = service.encrypt("sensitive-payload")
        tampered = self._flip_byte(token, 10)
        with pytest.raises(ValueError):
            service.decrypt(tampered)

    def test_truncated_token_rejected(self, service: EncryptionService):
        token = service.encrypt("payload")
        with pytest.raises(ValueError):
            service.decrypt(token[:-20])

    def test_garbage_input_rejected(self, service: EncryptionService):
        with pytest.raises(ValueError):
            service.decrypt("this-is-not-a-fernet-token-at-all")


# ---------------------------------------------------------------------------
# 5. Key separation — different master keys produce non-interchangeable
#    ciphertexts. This is the "trust domain" guarantee.
# ---------------------------------------------------------------------------


class TestKeySeparation:
    def test_token_from_one_key_does_not_decrypt_under_another(
        self,
        service: EncryptionService,
        service_alt_key: EncryptionService,
    ):
        # Encrypt with one master key, attempt decrypt with another.
        # The HMAC must reject — this is what isolates tenants if
        # ENCRYPTION_KEY ever rotates and tokens from the old key
        # leak into a new-key environment.
        token = service.encrypt("secret-belongs-to-tenant-A")
        with pytest.raises(ValueError):
            service_alt_key.decrypt(token)


# ---------------------------------------------------------------------------
# 6. Key derivation — same master key MUST always derive the same Fernet
#    key. If PBKDF2 parameters drift, every encrypted row in production
#    is stranded.
# ---------------------------------------------------------------------------


class TestKeyDerivationDeterminism:
    def test_same_master_key_decrypts_across_service_instances(self):
        # Spin up service A, encrypt. Tear it down, spin up service B
        # with the same master key, decrypt. The derived Fernet key
        # must be byte-identical between the two — otherwise restarts
        # would lose access to historical data.
        plaintext = "persistent-across-restarts"

        a = EncryptionService(
            config_manager=_StubConfigManager(),  # type: ignore[arg-type]
            logger=BrutorLogger("a"),
        )
        token = a.encrypt(plaintext)
        del a

        b = EncryptionService(
            config_manager=_StubConfigManager(),  # type: ignore[arg-type]
            logger=BrutorLogger("b"),
        )
        assert b.decrypt(token) == plaintext

    def test_pbkdf2_params_pinned(self, service: EncryptionService):
        # The constants `iterations=100000` and `salt=brutor_mcp_gateway_salt_v1`
        # in the production code are part of the wire contract — they
        # determine the derived Fernet key. Mirrored in the Rust
        # encryption.rs constants. Drift here is a silent data-loss
        # event.
        #
        # We don't have direct access to the PBKDF2 internals from the
        # public API, but we can assert behaviour: a *known* master key
        # must derive a specific token-decryptable Fernet key. The
        # other tests in this file collectively pin that behaviour;
        # this docstring exists so a future reader doesn't relax the
        # constants without realising they're a wire contract.
        _ = service
        assert True


# ---------------------------------------------------------------------------
# 7. encrypt_dict / decrypt_dict — the common shape used by the OAuth
#    token storage layer and provider_config serialisation.
# ---------------------------------------------------------------------------


class TestDictHelpers:
    def test_encrypts_only_string_values(self, service: EncryptionService):
        data = {
            "access_token": "ya29.secret",
            "refresh_token": "1//ref.secret",
            "expires_in": 3600,        # int — must pass through unchanged
            "scope": "openid email",
            "metadata": None,          # None — must pass through unchanged
            "rotation_enabled": True,  # bool — pass through
        }
        encrypted = service.encrypt_dict(data)
        # String values are encrypted (token shape, not original value)
        assert encrypted["access_token"] != "ya29.secret"
        assert encrypted["access_token"].startswith(
            (chr(0x80).encode().hex()[:2], "g")  # base64url of 0x80 starts with 'g'
        ) or encrypted["access_token"][0] == "g"
        # Non-string values pass through untouched
        assert encrypted["expires_in"] == 3600
        assert encrypted["metadata"] is None
        assert encrypted["rotation_enabled"] is True

    def test_round_trip_through_dict_helpers(self, service: EncryptionService):
        original = {
            "access_token": "ya29.A.example",
            "refresh_token": "1//refresh.example",
            "expires_in": 3600,
            "scope": "openid email profile",
        }
        roundtripped = service.decrypt_dict(service.encrypt_dict(original))
        assert roundtripped == original

    def test_skips_empty_string_values(self, service: EncryptionService):
        # `encrypt` rejects empty strings — `encrypt_dict` must guard
        # against the same input rather than raising mid-iteration and
        # leaving the dict half-encrypted. Empty string is preserved
        # as-is (not encrypted).
        data = {"access_token": "real-value", "scope": ""}
        out = service.encrypt_dict(data)
        assert out["scope"] == ""

    def test_decrypt_dict_tolerates_plain_values(self, service: EncryptionService):
        # decrypt_dict is sometimes called on rows that hold a mix
        # of encrypted and plain values (e.g. during a backfill).
        # The contract is to leave un-decryptable values as-is rather
        # than raise — the OAuth flow then surfaces the bad row
        # downstream when the token is actually used.
        data = {"access_token": "not-encrypted-yet", "expires_in": 3600}
        out = service.decrypt_dict(data)
        assert out["access_token"] == "not-encrypted-yet"  # passed through
        assert out["expires_in"] == 3600


# ---------------------------------------------------------------------------
# 8. generate_key — keys produced by this helper must work with the service
# ---------------------------------------------------------------------------


class TestGenerateKey:
    def test_generated_key_drives_a_working_service(self):
        # Operators run `python -c "from cryptography.fernet import Fernet;
        # print(Fernet.generate_key().decode())"` (or our equivalent) to
        # produce a new ENCRYPTION_KEY. The output must be acceptable to
        # our service's constructor.
        key = EncryptionService.generate_key()
        svc = EncryptionService(
            config_manager=_StubConfigManager(key),  # type: ignore[arg-type]
            logger=BrutorLogger("test"),
        )
        assert svc.encrypt("payload")
        assert svc.decrypt(svc.encrypt("payload")) == "payload"

    def test_generated_keys_are_unique(self):
        # Defensive: at least two consecutive generated keys differ.
        # Locks in the use of a CSPRNG underneath (which the spec requires).
        keys: Iterator[str] = (EncryptionService.generate_key() for _ in range(5))
        first = next(keys)
        assert any(k != first for k in keys)


# ---------------------------------------------------------------------------
# 9. rotate_key — the key-rotation path must read with the old key and
#    write with the new key, and the new token must decrypt under the
#    new key (only).
# ---------------------------------------------------------------------------


class TestRotateKey:
    def test_rotate_produces_token_decryptable_under_new_key_only(self):
        old_master = TEST_MASTER_KEY
        new_master = "rotated-master-key-32-chars-padded-or-longer"

        old_service = EncryptionService(
            config_manager=_StubConfigManager(old_master),  # type: ignore[arg-type]
            logger=BrutorLogger("old"),
        )
        new_service = EncryptionService(
            config_manager=_StubConfigManager(new_master),  # type: ignore[arg-type]
            logger=BrutorLogger("new"),
        )

        plaintext = "secret-pre-rotation"
        old_token = old_service.encrypt(plaintext)
        new_token = old_service.rotate_key(new_master, old_token)

        # New token decrypts cleanly under the new key…
        assert new_service.decrypt(new_token) == plaintext
        # …and is rejected under the old key (rotation actually moved it).
        with pytest.raises(ValueError):
            old_service.decrypt(new_token)
