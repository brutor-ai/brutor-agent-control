# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Where the key that encrypts stored secrets comes from: `ENCRYPTION_KEY`.

There is one key provider, the local one. It derives the installation's
Fernet key from `ENCRYPTION_KEY`, the way the Core Gateway derives it, so
that both read what either wrote.
"""

from app.services.key_management.local_provider import LocalKeyProvider

__all__ = ["LocalKeyProvider"]
