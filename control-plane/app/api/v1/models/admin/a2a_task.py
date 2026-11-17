# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic response models for the A2A v1.0 stateful task admin API.

The Rust proxy writes `a2a_tasks` rows directly via sqlx; admins read
them through the FastAPI layer for Mission Control-style browsing.
These models are read-only — the admin surface doesn't mutate tasks.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class A2aTaskListItem(BaseModel):
    """Compact row for the list view — drops the JSONB task body."""

    id: str
    tenant_id: str
    resource_group_id: Optional[str] = None
    context_id: Optional[str] = None
    state: str
    status_timestamp: str
    created_at: str
    updated_at: str


class A2aTaskListResponse(BaseModel):
    tasks: List[A2aTaskListItem]
    total: int
    limit: int
    offset: int


class A2aTaskDetailResponse(BaseModel):
    """Full row including the verbatim v1.0 Task JSON."""

    id: str
    tenant_id: str
    resource_group_id: Optional[str] = None
    context_id: Optional[str] = None
    state: str
    status_timestamp: str
    task_json: Dict[str, Any] = Field(
        ..., description="The verbatim A2A v1.0 Task JSON written by the proxy."
    )
    created_at: str
    updated_at: str


class A2aPushNotificationConfigListItem(BaseModel):
    """Compact row for push notification configs attached to a task."""

    id: str
    tenant_id: str
    task_id: str
    config_json: Dict[str, Any]
    created_at: str


class A2aPushNotificationConfigListResponse(BaseModel):
    configs: List[A2aPushNotificationConfigListItem]
    total: int
