# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The local key provider: the installation's Fernet key, derived from
`ENCRYPTION_KEY`.

PBKDF2-HMAC-SHA256 over the configured key with a fixed salt and
iteration count gives the 32 bytes of a Fernet key (16 for the HMAC, 16
for AES). The derivation is deterministic, and the Core Gateway does the
same (`src/services/encryption.rs`): the salt, the iteration count and
the length are part of what the two components share, and changing one
strands every stored secret.
"""

from __future__ import annotations

import hashlib

PBKDF2_SALT = b"brutor_mcp_gateway_salt_v1"
PBKDF2_ITERATIONS = 100_000
PBKDF2_KEYLEN = 32


class EncryptionKeyNotConfigured(ValueError):
    """`ENCRYPTION_KEY` is unset or still a placeholder."""


def derive_fernet_key(master_key: str) -> bytes:
    """The 32-byte Fernet key of `master_key`."""
    return hashlib.pbkdf2_hmac(
        "sha256", master_key.encode("utf-8"), PBKDF2_SALT, PBKDF2_ITERATIONS, dklen=PBKDF2_KEYLEN
    )


class LocalKeyProvider:
    """The key derived from `ENCRYPTION_KEY`."""

    def __init__(self, master_key: str):
        if not master_key or master_key.startswith("your-") or master_key == "change-this-key":
            raise EncryptionKeyNotConfigured(
                "ENCRYPTION_KEY is not configured. Generate one with: python -c \"import secrets,base64; "
                "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\""
            )
        self._key = derive_fernet_key(master_key)

    @property
    def fernet_key(self) -> bytes:
        """The 32 bytes: the first 16 sign, the last 16 encrypt."""
        return bytes(self._key)
