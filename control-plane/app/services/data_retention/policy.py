# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Reading a tenant's retention policy off the JSONB column, tolerantly.

The sweeper runs unattended against every tenant; a malformed policy must
neither crash the loop nor be silently treated as "delete everything". A
policy that does not parse is treated as *no policy* — keep everything —
and logged, which is the failure direction that cannot lose evidence.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import ValidationError

from app.api.v1.models.admin.retention import DataRetentionPolicy
from app.database.models.tenant import Tenant

log = logging.getLogger("brutor.retention")


def effective_policy(tenant: Tenant) -> Optional[DataRetentionPolicy]:
    """The tenant's policy, or None when nothing is to be enforced."""
    raw = getattr(tenant, "data_retention", None)
    if not raw:
        return None
    try:
        policy = DataRetentionPolicy.model_validate(raw)
    except ValidationError as exc:
        log.error(
            "tenant %s has a malformed data_retention policy — enforcing nothing: %s",
            tenant.id,
            exc,
        )
        return None
    if policy.body_days is None and policy.log_days is None and policy.run_days is None:
        return None
    return policy


def retention_disclosure(tenant: Tenant) -> list[str]:
    """Sentences for the assurance report's 'what this does not cover'.

    A report that leans on a 90-day ledger should say the ledger is 90 days
    deep. Written here so the wording lives with the policy it describes.
    """
    policy = effective_policy(tenant)
    if policy is None:
        return []
    out: list[str] = []
    if policy.run_days is not None:
        out.append(
            f"Run-ledger rows are retained for {policy.run_days} days by tenant "
            "policy; behaviour before that window is not evidence here."
        )
    if policy.log_days is not None:
        out.append(
            f"Proxy-log rows are retained for {policy.log_days} days by tenant "
            "policy and their deletion is declared to the audit-chain verifier."
        )
    if policy.body_days is not None:
        out.append(
            f"Request and response bodies are removed from rows older than "
            f"{policy.body_days} days; content-level review beyond that window "
            "is not possible."
        )
    return out
