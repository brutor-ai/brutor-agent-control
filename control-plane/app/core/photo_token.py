# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Capability-token validation for the public profile-photo URLs.

Profile photos are served unauthenticated so the admin UI and portal can
render them in plain ``<img>`` tags (browsers can't attach a bearer token to
an image request). To stop an unauthenticated caller from enumerating photos
by sequential integer user id — and from scraping other tenants' photos —
each photo URL carries ``?h=<token>`` where the token is a prefix of the
photo's SHA-256 content hash (``photo_hash``). A request without a matching
token gets 404: you can't fetch a photo unless you already know its hash, and
the hash is only exposed through the tenant-scoped user APIs.

The matching Rust portal route (`brutor-gateway-core/src/routes/icons.rs`)
enforces the same rule so the capability check holds whichever service the
browser hits.
"""

import hmac
from typing import Optional

# Minimum token length in hex chars. 8 = 32 bits, the legacy URL width;
# new uploads embed the full 64-char hash. Rejecting shorter tokens stops a
# 1-char guess from matching a hash prefix.
MIN_PHOTO_TOKEN_LEN = 8


def photo_token_ok(stored_hash: Optional[str], token: Optional[str]) -> bool:
    """Return True iff ``token`` is a valid capability token for the photo
    whose content hash is ``stored_hash``.

    Accepts any prefix of the stored hash that is at least
    ``MIN_PHOTO_TOKEN_LEN`` chars long (so legacy 8-char ``?h=`` URLs keep
    working while new full-length URLs are cryptographically strong).
    Constant-time comparison avoids leaking the hash byte-by-byte.
    """
    if not stored_hash or not token or len(token) < MIN_PHOTO_TOKEN_LEN:
        return False
    prefix = stored_hash[: len(token)]
    if len(prefix) != len(token):
        return False
    return hmac.compare_digest(prefix, token)
