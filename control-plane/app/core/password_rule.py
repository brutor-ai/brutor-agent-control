# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The password rule, stated once.

Every path that sets a password applies it: creating an administrator,
updating one, an administrator changing its own, the accounts a tenant is
created with, the accounts the installation creates from its configured
passwords, and a Portal user an administrator creates. The request models
use `NewPassword`; code that receives a password another way calls
`check_password`; and the places a password becomes a stored hash call it
again, so that a path added later cannot store one the rule refuses.

A sign-in does not apply the rule: it compares what it is given.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from pydantic import AfterValidator, Field

MIN_PASSWORD_LENGTH = 8
PASSWORD_RULE = f"a password has at least {MIN_PASSWORD_LENGTH} characters and is not only spaces"


class PasswordRefused(ValueError):
    """The password does not meet the rule. The message never holds it."""


#: SHA-256 of passwords everyone knows: the stock password of earlier
#: releases. Kept as digests so that no file holds the password itself
#: (CLAUDE.md, "No default credentials").
KNOWN_PASSWORD_DIGESTS = frozenset({
    "3eb3fe66b31e3b4d10fa70b5cad49c7112294af6ae4e476a1c405155d45aa121",
})


def check_password(password: Any, what: str = "The password") -> str:
    """`password`, when it meets the rule; `PasswordRefused` otherwise.

    `what` names the password in the message (a field, a variable)."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH or not password.strip():
        raise PasswordRefused(f"{what} is refused: {PASSWORD_RULE}.")
    if hashlib.sha256(password.encode("utf-8")).hexdigest() in KNOWN_PASSWORD_DIGESTS:
        raise PasswordRefused(
            f"{what} is refused: it is the stock password of earlier releases, which is publicly known."
        )
    return password


# A password a request sets.
NewPassword = Annotated[
    str,
    AfterValidator(check_password),
    Field(
        description=(
            f"The password to set: at least {MIN_PASSWORD_LENGTH} characters, and not only spaces "
            "(or other blank characters). Anything else is refused (422)."
        ),
        # The rule for a client generated from the API description. It is
        # applied by `check_password`, whose refusal never holds the password.
        json_schema_extra={"minLength": MIN_PASSWORD_LENGTH, "pattern": r"\S"},
    ),
]
