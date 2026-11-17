# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The password of an account the control plane creates itself.

There is no default password. An account that is created at first start, or
with a tenant, gets the password the operator configured; when none is
configured the account is not created and the caller is told which variable
to set. Nothing is generated and nothing is printed: the control plane runs
as several workers and replicas, and its log is shipped and kept.

`initial_password` is the one place that reads such a variable, so that the
rule ("unset or blank is refused, never replaced") is written once.
"""

from __future__ import annotations

from app.core.password_rule import check_password
from app.env_registry import EnvVar


class InitialPasswordMissing(RuntimeError):
    """An account has to be created and its password is not configured."""

    def __init__(self, variable: str, account: str) -> None:
        self.variable = variable
        self.account = account
        super().__init__(
            f"{variable} is not set. It is the password of {account}, which has to be created now. "
            f"Set {variable} in the environment and start again: there is no default password."
        )


def configured_password(variable: EnvVar) -> str:
    """The value of `variable`, exactly as set, or "" when it is unset or blank.

    A value that is set meets the password rule, or it is refused."""
    value = variable.exact() or ""
    if value:
        check_password(value, variable.name)
    return value


def initial_password(variable: EnvVar, account: str) -> str:
    """The password configured in `variable` for `account`, exactly as set.

    Raises `InitialPasswordMissing` when the variable is unset or blank."""
    value = configured_password(variable)
    if not value:
        raise InitialPasswordMissing(variable.name, account)
    return check_password(value, f"{variable.name} (the password of {account})")
