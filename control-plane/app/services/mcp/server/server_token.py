# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The static token of an MCP server (`mcp_servers.auth_token`).

The gateway sends it to the server as a bearer, and so does this control
plane's capability discovery. The column holds ciphertext of the
encryption service, written the way a model's `api_key` is. The token is
never returned by the API, a control plane with no encryption service
stores none, and a stored value that does not decrypt is not a token: it
is never sent.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

NO_ENCRYPTION = (
    "auth_token cannot be stored: this control plane has no encryption service "
    "(ENCRYPTION_KEY), and a credential is never stored in clear. Nothing was saved."
)
UNREADABLE = (
    "The server's stored token cannot be decrypted with this control plane's encryption key "
    "(ENCRYPTION_KEY), so the server was not called. Edit the server and save the token again."
)


class TokenUnreadable(Exception):
    """A token is stored and cannot be read: no encryption service, or a
    value that does not decrypt with it."""

    def __init__(self) -> None:
        super().__init__(UNREADABLE)


def require_storable(encryption_service: Any, token: Optional[str]) -> None:
    """503 where `token` is given and could not be stored. Asked before a
    request that would store one later does anything else."""
    if token and encryption_service is None:
        raise HTTPException(status_code=503, detail=NO_ENCRYPTION)


def seal(encryption_service: Any, token: Optional[str]) -> Optional[str]:
    """`token` as it is stored: encrypted. Empty is no token (`None`).
    503 where there is nothing to encrypt it with."""
    if not token:
        return None
    if encryption_service is None:
        raise HTTPException(status_code=503, detail=NO_ENCRYPTION)
    return encryption_service.encrypt(token)


def open_token(encryption_service: Any, stored: Optional[str]) -> Optional[str]:
    """The stored token in clear; `None` where none is stored. Raises
    `TokenUnreadable` where one is stored and cannot be read: the stored
    value itself is never used as the token."""
    if not stored:
        return None
    if encryption_service is None:
        raise TokenUnreadable()
    try:
        return encryption_service.decrypt(stored)
    except Exception:
        raise TokenUnreadable() from None
