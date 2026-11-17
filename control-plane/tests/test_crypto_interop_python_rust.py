# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Cross-language crypto interop — Python ↔ Rust

Brutor's data plane writes encrypted secrets from Python (gateway-control)
that the Rust proxy (gateway-core) reads at request time, and the reverse
also happens (Rust writes delegation chain signatures that Python verifies
when re-signing cards). Both sides reach the same wire format via different
libraries:

  * Python — pyca/cryptography (Fernet) + cryptography.ed25519
  * Rust   — hand-rolled in src/services/encryption.rs (Fernet) + ed25519-dalek

The architectural claim — "same spec, same bytes" — would otherwise be a
trust statement. This file pins it as a test: known-good ciphertexts
produced by EACH side must decrypt cleanly on the OTHER side, and Ed25519
signatures (which RFC 8032 makes deterministic) must be byte-identical
across implementations.

Fixture: `tests/fixtures/crypto_interop_v1.json`. The same file is read by
the matching Rust test in `gateway-core/tests/crypto_interop_v1.rs`. If
either side ever changes the Fernet wire format (or the Ed25519 spec stops
being deterministic — for which see RFC 8032), one of those two test
suites will fail before any user-visible regression escapes.

When and how to regenerate the fixture:
  * Run the Python helper inline at the bottom of this file (kept here so
    a future contributor doesn't have to hunt).
  * Run the Rust helper: `cargo run --example gen_interop_fixture` and
    `cargo run --example gen_ed25519_interop` from gateway-core.
  * Paste the outputs into the fixture. Re-run BOTH test suites.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
# v0.8.0 migrated card_signing from pyca/cryptography to PyNaCl. The
# interop test follows — exercising the same library production uses
# proves we're not regression-testing a different code path.
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from app.core.logging_system import BrutorLogger
from app.services.oauth.encryption_service import EncryptionService

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "crypto_interop_v1.json"


@pytest.fixture(scope="module")
def fixture() -> Dict[str, Any]:
    with FIXTURE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["format_version"] == 1, "Fixture version drift — update tests"
    return data


class _StubConfigManager:
    def __init__(self, key: str):
        self._key = key

    def get_encryption_key(self) -> str:
        return self._key


@pytest.fixture(scope="module")
def service(fixture: Dict[str, Any]) -> EncryptionService:
    return EncryptionService(
        config_manager=_StubConfigManager(fixture["master_key"]),  # type: ignore[arg-type]
        logger=BrutorLogger("interop-test"),
    )


# ---------------------------------------------------------------------------
# Section A — Fernet: Python decrypts what Rust wrote
# ---------------------------------------------------------------------------


class TestFernetRustToPython:
    """For each token in `tokens_from_rust`, Python must decrypt it back
    to the recorded plaintext using the shared master key. This proves
    Python's Fernet implementation reads what Rust's hand-rolled
    Fernet writes."""

    @pytest.mark.parametrize(
        "case_name",
        ["ascii_short", "oauth_token", "unicode", "json_blob", "newlines"],
    )
    def test_decrypts_rust_produced_token(
        self, fixture: Dict[str, Any], service: EncryptionService, case_name: str
    ):
        entry = fixture["tokens_from_rust"][case_name]
        decrypted = service.decrypt(entry["ciphertext"])
        assert decrypted == entry["plaintext"], (
            f"Python failed to decrypt the Rust-produced token for case "
            f"'{case_name}'. Expected plaintext: {entry['plaintext']!r}, got "
            f"{decrypted!r}. This indicates wire-format drift between the "
            f"two Fernet implementations — most often a difference in "
            f"PBKDF2 params, base64 alphabet (URL_SAFE vs STANDARD), AES "
            f"padding mode (PKCS7 vs other), HMAC key split, or version "
            f"byte. Compare src/services/encryption.rs against the "
            f"Python EncryptionService."
        )


# ---------------------------------------------------------------------------
# Section B — Fernet: Python's own tokens still decrypt (self round-trip via
# the fixture). This is the "we didn't accidentally regress our own format"
# check — distinct from the encrypt/decrypt unit tests because the fixture
# tokens are pinned at v0.7.95 production output. If we ever upgrade the
# Python library and a backward-incompat fixture decode breaks, OAuth
# tokens in production DBs would be stranded too.
# ---------------------------------------------------------------------------


class TestFernetPythonSelfRoundTrip:
    """The fixture tokens were minted by Python v0.7.95. Any future
    Python implementation MUST decrypt them — otherwise upgrading
    Brutor leaves OAuth tokens in production unreachable."""

    @pytest.mark.parametrize(
        "case_name",
        ["ascii_short", "oauth_token", "unicode", "json_blob", "newlines"],
    )
    def test_decrypts_legacy_python_token(
        self, fixture: Dict[str, Any], service: EncryptionService, case_name: str
    ):
        entry = fixture["tokens_from_python"][case_name]
        assert service.decrypt(entry["ciphertext"]) == entry["plaintext"]


# ---------------------------------------------------------------------------
# Section C — Ed25519: deterministic-signature parity
#
# Per RFC 8032 §5.1, Ed25519 signatures are deterministic — every
# spec-compliant signer produces the same 64-byte signature for the same
# (private-key, message) pair. If Python's signature ever differs from
# Rust's for the fixture's key+message, ONE of the two libraries is no
# longer RFC-conformant.
# ---------------------------------------------------------------------------


class TestEd25519Determinism:
    """Ed25519 is deterministic per RFC 8032 §5.1. The same (private-key,
    message) pair produces the same 64-byte signature under every
    spec-compliant implementation. We verify that PyNaCl (post-0.8.0
    Python signer) agrees with both the pinned pre-refactor signature
    AND with ed25519-dalek (the Rust signer). Any future
    implementation that breaks this property is non-conformant."""

    def _seed_to_signing_key(self, seed_hex: str) -> SigningKey:
        # PyNaCl's `SigningKey(bytes)` takes the raw 32-byte RFC 8032
        # seed directly — same shape pyca's
        # `Ed25519PrivateKey.from_private_bytes` accepted.
        return SigningKey(bytes.fromhex(seed_hex))

    def test_python_signature_matches_recorded_value(self, fixture: Dict[str, Any]):
        # Python signs the fixture message with the fixture's seed.
        # Result must equal the pinned hex value — proves PyNaCl
        # produces the exact same 64 bytes as the old pyca-cryptography
        # signer did. This is the byte-level compat the swap promises.
        ed = fixture["ed25519"]
        signing = self._seed_to_signing_key(ed["private_key_hex_seed"])
        signed = signing.sign(ed["message_utf8"].encode("utf-8"))
        assert signed.signature.hex() == ed["signature_hex_from_python"]

    def test_python_signature_byte_identical_to_rust(self, fixture: Dict[str, Any]):
        # Cross-language determinism — same property tested differently.
        # PyNaCl-output bytes must equal ed25519-dalek-output bytes
        # for the same input.
        ed = fixture["ed25519"]
        signing = self._seed_to_signing_key(ed["private_key_hex_seed"])
        py_sig = signing.sign(ed["message_utf8"].encode("utf-8")).signature
        assert py_sig.hex() == ed["signature_hex_from_rust"]

    def test_python_verifies_rust_signature(self, fixture: Dict[str, Any]):
        # The production verify-card path: PyNaCl receives a signature
        # produced by ed25519-dalek (the Rust side) and accepts it.
        # PyNaCl's `verify(message, signature)` raises on failure,
        # returns the message bytes on success — note the argument
        # order differs from cryptography's `.verify(sig, msg)`.
        ed = fixture["ed25519"]
        verify_key = VerifyKey(bytes.fromhex(ed["public_key_hex"]))
        sig = bytes.fromhex(ed["signature_hex_from_rust"])
        verify_key.verify(ed["message_utf8"].encode("utf-8"), sig)

    def test_python_rejects_tampered_rust_signature(self, fixture: Dict[str, Any]):
        # Belt-and-braces: flip a byte in the Rust-side signature and
        # confirm PyNaCl's verifier rejects. Catches a (theoretical)
        # over-permissive verifier that would accept any 64 bytes.
        ed = fixture["ed25519"]
        verify_key = VerifyKey(bytes.fromhex(ed["public_key_hex"]))
        sig = bytearray.fromhex(ed["signature_hex_from_rust"])
        sig[10] ^= 0x42
        with pytest.raises(BadSignatureError):
            verify_key.verify(ed["message_utf8"].encode("utf-8"), bytes(sig))


# ---------------------------------------------------------------------------
# Regeneration helper — kept inline so future contributors find it
# ---------------------------------------------------------------------------
#
# If you change anything in the Python EncryptionService that affects the
# wire format (PBKDF2 params, salt, version byte, base64 alphabet, AES
# padding, HMAC key split), the fixture tokens become invalid. To
# regenerate the Python side of the fixture:
#
#   import os; os.environ.update(
#       DATABASE_URL='postgresql://test:test@127.0.0.1:1/test',
#       JWT_SECRET='x',
#       ENCRYPTION_KEY='a'*44 + '=',
#   )
#   from app.services.oauth.encryption_service import EncryptionService
#   from app.core.logging_system import BrutorLogger
#   class S:
#       def __init__(self,k): self.k=k
#       def get_encryption_key(self): return self.k
#   svc = EncryptionService(config_manager=S("<master from fixture>"),
#                           logger=BrutorLogger("regen"))
#   for case_name, pt in [
#       ("ascii_short", "sk_brutor_api_test_xyz"),
#       ("oauth_token", "ya29.a0AfH6SMB-example-access-token"),
#       ("unicode",     "汉字テスト中文"),
#       ("json_blob",   '{"endpoint":"https://api.example.com","model":"gpt-4o-mini"}'),
#       ("newlines",    "line1\nline2\tindented\r"),
#   ]:
#       print(case_name, svc.encrypt(pt))
#
# Then regenerate the Rust side with
#   `cargo run --example gen_interop_fixture` (gateway-core)
# and paste both into `tests/fixtures/crypto_interop_v1.json`.
