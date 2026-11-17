# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the maintenance sub-endpoints.

MaintenancePatchRequest already lives in system_ops.py; only the
restart-rolling + db-vacuum bodies are net-new here. Lives in a
small sibling module so the slice can land without touching
system_ops.py and inviting merge conflicts with parallel slices.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RestartRollingRequest(BaseModel):
    """POST /v1/admin/system/maintenance/restart-rolling.

    No process actually restarts — this is the audit / announce
    surface so the Audit tab records the operator's intent. The
    actual rollout is kubectl / ECS operator-driven.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Target tenant for the restart announcement. NULL = use "
            "the caller's tenant context. Sysadmin only when set "
            "explicitly."
        ),
    )
    reason: Optional[str] = Field(default=None, max_length=4096)


class RestartRollingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str = Field(description="system_operation_log row id.")
    recorded_at: str = Field(description="ISO-8601 timestamp.")


class DbVacuumRequest(BaseModel):
    """POST /v1/admin/system/maintenance/db-vacuum.

    `table` MUST be in ALLOWED_VACUUM_TABLES on the backend; the
    service layer rejects anything else with 400.
    """

    model_config = ConfigDict(extra="forbid")

    table: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Table to vacuum. Must be in the whitelist defined in "
            "app/services/system_ops/maintenance.py — proxy_logs, "
            "usage_logs, system_operation_log, admin_audit_log, "
            "proxy_log_request_chunks, proxy_log_response_chunks, "
            "tool_approvals."
        ),
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Target tenant (audit row scope). NULL = caller's tenant.",
    )
    reason: Optional[str] = Field(default=None, max_length=4096)


class DbVacuumResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    duration_ms: int = Field(ge=0)
    audit_id: str
    completed_at: str
