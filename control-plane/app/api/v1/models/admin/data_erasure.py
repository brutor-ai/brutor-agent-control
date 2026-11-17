# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Data-subject erasure request/response shapes. See https://docs.brutor.ai/governance/data-handling/
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class DataErasureRequest(BaseModel):
    end_user_id: Optional[int] = Field(None, description="end_users.id")
    idp_user_id: Optional[str] = Field(None, description="IdP subject as stamped on proxy logs")
    email: Optional[str] = Field(None, description="Email, matched against end_users and proxy-log idp_user_email")
    subject_id: Optional[str] = Field(
        None,
        description="Delegation subject as stamped on proxy logs (e.g. the user id an agent acted for)",
    )
    dry_run: bool = Field(False, description="Count what would be removed without changing anything")

    @model_validator(mode="after")
    def _at_least_one(self) -> "DataErasureRequest":
        if not any((self.end_user_id, self.idp_user_id, self.email, self.subject_id)):
            raise ValueError("at least one of end_user_id, idp_user_id, email, subject_id is required")
        return self


class DataErasureResponse(BaseModel):
    id: str
    tenant_id: str
    subject_fingerprint: str
    subject_kinds: List[str]
    requested_by: str
    requested_at: Optional[str]
    dry_run: bool
    counts: Dict[str, int]


class DataErasureListResponse(BaseModel):
    events: List[DataErasureResponse]
    total: int
