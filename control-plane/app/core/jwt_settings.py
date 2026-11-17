# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The algorithm administrator and portal tokens are signed with: the one read
of `JWT_ALGORITHM`.

Four places read the variable. Three defaulted it to HS256; the
administrator sign-in path did not and raised when it was unset, so the
process started and every sign-in answered 500. Every reader now asks
`jwt_algorithm()`:

* unset or blank: `DEFAULT_JWT_ALGORITHM`;
* a value the shared secret (`JWT_SECRET`) cannot sign with: `ValueError`.
  `create_app` asks once while the application is built, so a wrong value
  stops the start instead of failing every sign-in afterwards.

The tokens are HMAC-signed with a secret both the control plane and the
gateway hold, so only the HMAC algorithms are accepted.
"""

from __future__ import annotations

from app.env_vars import JWT_ALGORITHM

DEFAULT_JWT_ALGORITHM = JWT_ALGORITHM.default
SUPPORTED_JWT_ALGORITHMS = JWT_ALGORITHM.choices


def jwt_algorithm() -> str:
    """The configured signing algorithm, or the default when none is set.
    A value the shared secret cannot sign with is a `ValueError` that
    names the variable and what it accepts."""
    return JWT_ALGORITHM.require()
