# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
TOTP-based Two-Factor Authentication service.

Uses pyotp (RFC 6238) for TOTP generation/verification.
Compatible with Google Authenticator, Authy, 1Password, etc.
"""

import base64
import hashlib
import io
import logging
import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

import bcrypt
import pyotp
import qrcode

from app.services.config_manager import ConfigManager
from app.services.oauth.encryption_service import EncryptionService

log = logging.getLogger("brutor-mfa")


@dataclass
class TotpSetupResult:
    """Result of 2FA setup initiation."""
    secret: str  # Base32-encoded TOTP secret
    provisioning_uri: str  # otpauth:// URI for QR code
    qr_code_base64: str  # PNG QR code as base64 data URI


@dataclass
class TotpVerifyResult:
    """Result of 2FA verification and activation."""
    success: bool
    backup_codes: List[str]  # Plaintext (shown once), hashed versions stored in DB


def generate_totp_setup(
    username: str,
    email: str,
    issuer: str = "Brutor AI",
) -> TotpSetupResult:
    """
    Generate a new TOTP secret and QR code for 2FA setup.

    The secret is NOT yet stored — the user must verify a code first.
    """
    secret = pyotp.random_base32(length=32)
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(
        name=email or username,
        issuer_name=issuer,
    )

    # Generate QR code as base64 PNG
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return TotpSetupResult(
        secret=secret,
        provisioning_uri=provisioning_uri,
        qr_code_base64=f"data:image/png;base64,{qr_b64}",
    )


@lru_cache(maxsize=1)
def _encryption() -> EncryptionService:
    return EncryptionService(config_manager=ConfigManager())


def seal_totp_secret(secret: str) -> str:
    """Encrypt a TOTP secret for storage. Whoever reads it can mint codes."""
    return _encryption().encrypt(secret)


def verify_sealed_totp_code(sealed: str, code: str) -> bool:
    """Verify a code against a stored secret. An unreadable secret fails closed."""
    try:
        secret = _encryption().decrypt(sealed)
    except Exception as e:
        log.error("Stored TOTP secret could not be decrypted: %s", type(e).__name__)
        return False
    return verify_totp_code(secret, code)


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code against a secret. Allows 1 window of drift."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def generate_backup_codes(count: int = 10) -> tuple[list[str], list[str]]:
    """
    Generate one-time backup codes.

    Returns (plaintext_codes, hashed_codes).
    Plaintext shown to user once; hashed versions stored in DB.
    """
    plaintext = [secrets.token_hex(4).upper() for _ in range(count)]  # e.g. "A1B2C3D4"
    hashed = [
        bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        for code in plaintext
    ]
    return plaintext, hashed


def verify_backup_code(code: str, hashed_codes: list[str]) -> Optional[int]:
    """
    Verify a backup code against the stored hashed codes.

    Returns the index of the matching code (so it can be removed), or None.
    """
    code_bytes = code.strip().upper().encode("utf-8")
    for i, hashed in enumerate(hashed_codes):
        if bcrypt.checkpw(code_bytes, hashed.encode("utf-8")):
            return i
    return None
