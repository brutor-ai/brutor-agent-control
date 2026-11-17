# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Encryption Service
==================

Symmetric encryption for sensitive data at rest: OAuth tokens and client
secrets, provider API keys, static tokens, provider configurations.

One key and one format. The key is the installation's, derived from
`ENCRYPTION_KEY` (`app/services/key_management/local_provider.py`); the
Core Gateway derives the same key the same way, so each reads what the
other wrote. A stored secret is a Fernet token:

    version(1) || timestamp_be(8) || iv(16) || ciphertext || hmac(32)

base64url-encoded without padding. AES-128-CBC over the PKCS7-padded
plaintext; the 32-byte key is 16 bytes of HMAC-SHA256 signing key followed
by 16 bytes of AES key; the HMAC covers everything before it.

There is no key provider to choose and no key of a tenant's own.

# Why hand-rolled

Chosen in 0.8.0 to avoid `pyca/cryptography`, after a crash under Colima
on Apple Silicon. That ban was lifted in 2026-11 (`pyproject.toml`, `mcp`),
and this implementation stays because its wire format is pinned: the
primitives are pycryptodome's AES and the standard library's hmac and
hashlib.

# Telling ciphertext from clear text

`has_ciphertext_shape` decides whether a stored value is something this
service wrote, by its shape and without a key, exactly as the gateway
decides it. Code that must not encrypt a value twice asks that, and never
"does it decrypt?": a value encrypted under another key does not decrypt,
and is not clear text.
"""

import base64
import binascii
import hashlib
import hmac
import os
import time
from typing import Optional

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from app.core.logging_system import BrutorLogger
from app.env_vars import ENCRYPTION_KEY
from app.services.config_manager import ConfigManager
from app.services.key_management import LocalKeyProvider
from app.services.key_management.local_provider import (
    PBKDF2_ITERATIONS,
    PBKDF2_KEYLEN,
    PBKDF2_SALT,
    derive_fernet_key as _derive_fernet_key,
)

# Part of the wire contract: the gateway hard-codes the same values in
# `src/services/encryption.rs`.
FERNET_VERSION = 0x80
AES_BLOCK_SIZE = 16  # AES-128 block size in bytes
HMAC_SIZE = 32       # SHA-256 digest
# version(1) + timestamp(8) + IV(16) + HMAC(32), and that plus one AES block.
FERNET_OVERHEAD = 57
FERNET_MIN_LEN = FERNET_OVERHEAD + AES_BLOCK_SIZE
# The prefix of a format this product no longer writes or reads. The
# gateway still takes a value that starts with it for ciphertext (never
# for a clear-text secret), and so does `has_ciphertext_shape`.
RETIRED_ENVELOPE_PREFIX = "BAEv1:"

_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def has_ciphertext_shape(value: object) -> bool:
    """Whether a stored value has the shape of something the encryption
    service writes, as opposed to a clear-text secret. Decided without a
    key, as the gateway's `has_ciphertext_shape` decides it:

    * it starts with the retired envelope prefix; or
    * it is a Fernet token: base64url (padded or not) of at least 73
      bytes that are the version byte 0x80, an 8-byte big-endian timestamp
      whose upper four bytes are zero, a 16-byte IV, one or more whole
      16-byte AES blocks and a 32-byte HMAC.

    A clear-text key that happened to have that whole layout would be
    taken for ciphertext, and refused where it is used: the safe side of
    that mistake.
    """
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(RETIRED_ENVELOPE_PREFIX):
        return True
    body = value.rstrip("=")
    if len(value) - len(body) > 2 or not set(body) <= _BASE64URL:
        return False
    try:
        raw = base64.b64decode(
            body.replace("-", "+").replace("_", "/") + "=" * (-len(body) % 4), validate=True
        )
    except (binascii.Error, ValueError):
        return False
    return (
        len(raw) >= FERNET_MIN_LEN
        and (len(raw) - FERNET_OVERHEAD) % AES_BLOCK_SIZE == 0
        and raw[0] == FERNET_VERSION
        and raw[1:5] == b"\x00\x00\x00\x00"
    )


def _b64url_encode_no_padding(raw: bytes) -> str:
    """`cryptography.fernet.Fernet` emits unpadded base64url. Match that."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode_padded(token: str) -> bytes:
    """Inverse of `_b64url_encode_no_padding`. Adds back the `=` padding
    that base64url decoding needs."""
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode((token + padding).encode("ascii"))


def _split_fernet_key(key: bytes) -> tuple[bytes, bytes]:
    """Split a 32-byte Fernet key into (signing_key, encryption_key)."""
    if len(key) != 32:
        raise ValueError(
            f"Fernet key must be exactly 32 bytes, got {len(key)}"
        )
    return key[:16], key[16:]


def _fernet_encrypt_with_key(plaintext: str, fernet_key: bytes) -> str:
    """Produce a Fernet token using the given 32-byte key."""
    signing_key, encryption_key = _split_fernet_key(fernet_key)
    pt_bytes = plaintext.encode("utf-8")
    padded = pad(pt_bytes, AES_BLOCK_SIZE, style="pkcs7")
    iv = os.urandom(AES_BLOCK_SIZE)
    cipher = AES.new(encryption_key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(padded)
    timestamp = int(time.time())
    ts_bytes = timestamp.to_bytes(8, byteorder="big", signed=False)
    payload = bytes([FERNET_VERSION]) + ts_bytes + iv + ciphertext
    mac = hmac.new(signing_key, payload, hashlib.sha256).digest()
    return _b64url_encode_no_padding(payload + mac)


def _fernet_decrypt_with_key(token: str, fernet_key: bytes) -> str:
    """Decrypt a Fernet token using the given 32-byte key.

    Constant-time HMAC verification (before any cipher touch) and
    PKCS7 unpadding both happen here. ValueError on any failure.
    """
    signing_key, encryption_key = _split_fernet_key(fernet_key)
    raw = _b64url_decode_padded(token)
    min_size = 1 + 8 + AES_BLOCK_SIZE + AES_BLOCK_SIZE + HMAC_SIZE
    if len(raw) < min_size:
        raise ValueError(f"Token too short: {len(raw)} bytes")
    if raw[0] != FERNET_VERSION:
        raise ValueError(
            f"Unsupported Fernet version: {raw[0]:#x} (expected {FERNET_VERSION:#x})"
        )
    hmac_offset = len(raw) - HMAC_SIZE
    payload = raw[:hmac_offset]
    stored_mac = raw[hmac_offset:]
    expected_mac = hmac.new(signing_key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(stored_mac, expected_mac):
        raise ValueError("HMAC verification failed")
    iv = raw[9 : 9 + AES_BLOCK_SIZE]
    ciphertext = raw[9 + AES_BLOCK_SIZE : hmac_offset]
    if not ciphertext or len(ciphertext) % AES_BLOCK_SIZE != 0:
        raise ValueError("Ciphertext length is not a multiple of AES block size")
    cipher = AES.new(encryption_key, AES.MODE_CBC, iv)
    padded_plain = cipher.decrypt(ciphertext)
    plain_bytes = unpad(padded_plain, AES_BLOCK_SIZE, style="pkcs7")
    return plain_bytes.decode("utf-8")


class EncryptionService:
    """
    Encrypts and decrypts stored secrets under the installation's key.

    `EncryptionService(config_manager).encrypt(plaintext)` gives a Fernet
    token; `decrypt` gives the plaintext back, or raises `ValueError` for
    anything that is not a token of this key.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        logger: Optional[BrutorLogger] = None,
    ):
        self.logger = logger or BrutorLogger("encryption-service")

        key = config_manager.get_encryption_key()
        if not key or key.startswith("your-") or key == "change-this-key":
            error_msg = (
                f"{ENCRYPTION_KEY.name} is not configured! "
                f"Please set {ENCRYPTION_KEY.name} environment variable. "
                "Generate one with: python -c \"from app.services.oauth.encryption_service "
                "import EncryptionService; print(EncryptionService.generate_key())\""
            )
            self.logger.error(error_msg)
            raise ValueError(error_msg)
        self._fernet_key = LocalKeyProvider(key).fernet_key

    @property
    def _signing_key(self) -> bytes:
        return self._fernet_key[:16]

    @property
    def _encryption_key(self) -> bytes:
        return self._fernet_key[16:]

    # ------------------------------------------------------------------
    # Core encrypt / decrypt
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a string into a Fernet token.

        Raises:
            ValueError: If plaintext is empty.
        """
        if not plaintext:
            raise ValueError("Cannot encrypt empty string")
        try:
            return _fernet_encrypt_with_key(plaintext, self._fernet_key)
        except Exception as e:
            self.logger.error("Encryption failed", context={"error": str(e)}, exc_info=True)
            raise

    def decrypt(self, encrypted: str) -> str:
        """
        Decrypt a Fernet token of this key.

        Raises:
            ValueError: For anything else: malformed input, another
                key's token, a value in a format this service does not
                write.
        """
        if not encrypted:
            raise ValueError("Cannot decrypt empty string")
        try:
            return _fernet_decrypt_with_key(encrypted, self._fernet_key)
        except ValueError:
            raise
        except Exception as e:
            self.logger.error(
                "Decryption failed",
                context={"error": str(e), "encrypted_length": len(encrypted)},
                exc_info=True,
            )
            raise ValueError(f"Decryption failed: {e}")

    # ------------------------------------------------------------------
    # Bulk helpers (preserved API surface)
    # ------------------------------------------------------------------

    def encrypt_dict(self, data: dict) -> dict:
        """Encrypt all *non-empty string* values in a dictionary; pass
        through non-string and empty-string values unchanged.

        Used by the OAuth token storage layer to encrypt
        access_token / refresh_token in one shot while leaving
        non-secret metadata (expires_in, scope, etc.) alone.
        """
        encrypted = {}
        for k, v in data.items():
            if isinstance(v, str) and v:
                encrypted[k] = self.encrypt(v)
            else:
                encrypted[k] = v
        return encrypted

    def decrypt_dict(self, data: dict) -> dict:
        """Mirror of `encrypt_dict`. Tolerates rows that hold a mix of
        encrypted and plain values (e.g. during a backfill) — if a
        value fails to decrypt, it's passed through as-is rather than
        raising mid-iteration.
        """
        decrypted = {}
        for k, v in data.items():
            if isinstance(v, str) and v:
                try:
                    decrypted[k] = self.decrypt(v)
                except Exception:
                    decrypted[k] = v
            else:
                decrypted[k] = v
        return decrypted

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    @staticmethod
    def generate_key() -> str:
        """Generate a new random master key suitable for `ENCRYPTION_KEY`.

        Returns 32 cryptographically random bytes, base64url-encoded
        (44 ASCII chars including padding) — matches the shape pyca's
        `Fernet.generate_key()` historically produced. Operators paste
        the output into `ENCRYPTION_KEY` and the service derives the
        actual Fernet key from it via PBKDF2 on every startup.
        """
        return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

    def rotate_key(self, new_encryption_key: str, old_data: str) -> str:
        """Re-encrypt `old_data` (ciphertext of the current key) under the
        key derived from `new_encryption_key`."""
        plaintext = self.decrypt(old_data)
        new_key = _derive_fernet_key(new_encryption_key)
        return _fernet_encrypt_with_key(plaintext, new_key)
