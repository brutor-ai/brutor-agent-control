# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The tenant's two-factor policy (`tenants.mfa_policy`): what the three values
mean, and the one reading of a stored value.

    optional         nobody is required to have 2FA
    required_admins  every administrator of the tenant must have 2FA
    required_all     administrators and end users (Portal) must have 2FA

Everything that asks "does this tenant require 2FA of X" goes through
`effective_mfa_policy`: sign-in, the 2FA self-service routes, the tenant
update and the compliance resolver. The core proxy reads the same column with
the same rule for Portal sign-in.

Reading is deliberately not symmetric with writing. A write accepts the three
values and nothing else (`MfaPolicy` on the update model). A read has to
answer for whatever is in the column: nothing stored means the default,
`optional`; a string that is none of the three means somebody intended a
policy this code does not know, and the safe reading of an unknown security
setting is the most restrictive one, never "off".
"""

from typing import Literal, Optional

from sqlalchemy.orm import Session

MFA_OPTIONAL = "optional"
MFA_REQUIRED_ADMINS = "required_admins"
MFA_REQUIRED_ALL = "required_all"

#: The values a write may carry.
MfaPolicy = Literal["optional", "required_admins", "required_all"]

# Least to most restrictive.
_ORDER = (MFA_OPTIONAL, MFA_REQUIRED_ADMINS, MFA_REQUIRED_ALL)


def effective_mfa_policy(stored: Optional[str]) -> str:
    """The policy a stored value stands for: NULL / empty is `optional`, one of
    the three values is itself, anything else is `required_all`."""
    if stored is None or not str(stored).strip():
        return MFA_OPTIONAL
    if stored in _ORDER:
        return stored
    return MFA_REQUIRED_ALL


def requires_2fa_for_admins(stored: Optional[str]) -> bool:
    return effective_mfa_policy(stored) in (MFA_REQUIRED_ADMINS, MFA_REQUIRED_ALL)


def requires_2fa_for_end_users(stored: Optional[str]) -> bool:
    return effective_mfa_policy(stored) == MFA_REQUIRED_ALL


def is_stricter(new: Optional[str], *, than: Optional[str]) -> bool:
    """True when `new` requires 2FA of people `than` did not."""
    return _ORDER.index(effective_mfa_policy(new)) > _ORDER.index(effective_mfa_policy(than))


def tenant_mfa_policy(db: Session, tenant_id: Optional[str]) -> str:
    """The effective policy of the tenant with this id.

    A tenant that does not exist has no policy stored (`optional`): its
    administrators and users are deleted with it (ON DELETE CASCADE), so there
    is nobody the answer could apply to.
    """
    from app.database.models.tenant import Tenant

    if not tenant_id:
        return MFA_OPTIONAL
    row = db.query(Tenant.mfa_policy).filter(Tenant.id == tenant_id).first()
    return effective_mfa_policy(row[0] if row is not None else None)


def has_2fa(account) -> bool:
    """An account has 2FA once a code was verified: enabled, with a secret.
    A secret alone is a setup that was started and never confirmed."""
    return bool(getattr(account, "totp_enabled", False) and getattr(account, "totp_secret", None))
