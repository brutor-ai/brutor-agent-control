# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Workload identity: the vocabulary

The credential facts the core records on every call (`proxy_logs`) and run
(`ai_system_runs`), and the identity policy's members. Every value crosses a
repository boundary: the core writes the columns and judges calls, this
plane validates policies and reads the columns. The CHECKs of the schema
are written from these tuples, and the shared resolver fixture the core
keeps pins the ladder and the members.
"""

from __future__ import annotations

from typing import Tuple

#: Credential levels, weakest first. A policy's `min_assurance` is one of
#: these; a call's `credential_assurance` is one of these or NULL (a kind
#: outside the ladder).
CREDENTIAL_LEVELS: Tuple[str, ...] = ("key", "bound_key", "workload", "workload_pop")

#: What authenticated a call.
CREDENTIAL_KINDS: Tuple[str, ...] = (
    "api_key",
    "workload_token",
    "x509_svid",
    "brutor_token",
    "human_session",
    "platform_service",
    "anonymous",
)

#: The kinds a policy judges: those with a level. A person's session and
#: the platform's own credential are exempt; anonymous is below every level.
LADDER_KINDS: Tuple[str, ...] = ("api_key", "workload_token", "x509_svid", "brutor_token")

#: The judge's verdict on a call.
CREDENTIAL_POLICY_OUTCOMES: Tuple[str, ...] = ("met", "below_policy", "refused", "exempt")

#: How a policy acts on a call below it, weakest first.
POLICY_MODES: Tuple[str, ...] = ("audit", "enforce")

#: The members of an identity policy, in their fixed order.
POLICY_FIELDS: Tuple[str, ...] = (
    "mode",
    "min_assurance",
    "allow_user_keys",
    "allowed_issuers",
    "max_token_lifetime_s",
    "max_key_ttl_days",
)

#: Lifecycle stages in which an AI System is in production.
PRODUCTION_STAGES: Tuple[str, ...] = ("active", "deprecated", "retired")

#: Risk tiers that carry the high-risk floor.
HIGH_RISK_TIERS: Tuple[str, ...] = ("high", "unacceptable")


def sql_in(column: str, values: Tuple[str, ...]) -> str:
    """`<column> IS NULL OR <column> IN ('a', 'b', …)`: the CHECK of a
    nullable column with a closed vocabulary."""
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IS NULL OR {column} IN ({listed})"
