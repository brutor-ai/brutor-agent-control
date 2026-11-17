# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Per-tenant data retention policy — the shape stored at
`tenants.data_retention` (JSONB) and read by both the control plane's
retention sweeper and the Rust proxy's audit writer.

The ledger stores prompts, tool arguments and model output: the most
sensitive dataset a tenant has. Until this policy existed, retention was a
manual button, bodies were stored in full regardless of any PII guardrail,
and nothing distinguished "not captured" from "removed". This object is the
one place a data-protection officer can read the answer:

* ``store_bodies`` — what the proxy writes at all. ``full`` is the historic
  behaviour; ``redacted`` runs every stored body through the local PII
  redactor at write time, independent of whether a PII guardrail is
  configured for the request; ``none`` stores no body or header text.
* ``body_days`` — after this many days, bodies and headers are removed from
  rows that keep their hashes and counters. Bodies are not covered by the
  audit hash chain, so this never breaks verification.
* ``log_days`` — after this many days, whole proxy-log rows are deleted.
  Every deletion is declared in ``audit_chain_prunes`` so the verifier can
  tell a retention prune from an undeclared gap.
* ``run_days`` — after this many days, run-ledger rows are deleted.

``None`` for any window means "keep". A body window longer than the log
window is a contradiction (the rows would be gone first) and is rejected.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

StoreBodies = Literal["full", "redacted", "none"]

STORE_BODIES_FULL = "full"
STORE_BODIES_REDACTED = "redacted"
STORE_BODIES_NONE = "none"

MAX_RETENTION_DAYS = 3650


class DataRetentionPolicy(BaseModel):
    store_bodies: StoreBodies = Field(
        STORE_BODIES_FULL,
        description=(
            "What the proxy stores for request/response bodies and headers: "
            "'full' (as received), 'redacted' (local PII redaction applied at "
            "write time, independent of guardrails), or 'none'."
        ),
    )
    body_days: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_RETENTION_DAYS,
        description=(
            "Remove bodies and headers from proxy-log rows older than this "
            "many days. The rows, hashes and counters remain. Null = keep."
        ),
    )
    log_days: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_RETENTION_DAYS,
        description=(
            "Delete proxy-log rows older than this many days. Deletions are "
            "declared to the audit-chain verifier as prunes. Null = keep."
        ),
    )
    run_days: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_RETENTION_DAYS,
        description="Delete run-ledger rows older than this many days. Null = keep.",
    )

    @model_validator(mode="after")
    def _windows_nest(self) -> "DataRetentionPolicy":
        if (
            self.body_days is not None
            and self.log_days is not None
            and self.body_days > self.log_days
        ):
            raise ValueError(
                "body_days cannot exceed log_days — the rows would be deleted "
                "before their bodies were due for removal"
            )
        return self
