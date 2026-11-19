# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Identity policy: the request and response models.

`IdentityPolicyModel` and `IdentityPolicyDefaultModel` accept exactly what
`app/services/identity_policy.py` validates (every member required, no
other, strict types and ranges); `tests/test_identity_policy_resolver.py`
holds both to the cases the core keeps. A refused body is Pydantic's
standard 422.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

Level = Literal["key", "bound_key", "workload", "workload_pop"]
Mode = Literal["audit", "enforce"]


class IdentityPolicyModel(BaseModel):
    """A group's identity policy: all six members, no other."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode = Field(..., description="audit: record calls below the policy; enforce: refuse them")
    min_assurance: Level = Field(..., description="The lowest credential level a call may present")
    allow_user_keys: StrictBool = Field(..., description="Whether personal API keys are accepted")
    allowed_issuers: Optional[List[StrictStr]] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="OIDC IdP ids whose workload tokens are accepted; null = any issuer",
    )
    max_token_lifetime_s: StrictInt = Field(..., ge=60, le=3600, description="Longest accepted workload token lifetime")
    max_key_ttl_days: Optional[StrictInt] = Field(
        ..., ge=1, le=3650, description="Longest API key lifetime in days; null = no ceiling"
    )

    @field_validator("allowed_issuers")
    @classmethod
    def _distinct_issuers(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        if any(not v for v in value):
            raise ValueError("entries are IdP ids")
        if len(set(value)) != len(value):
            raise ValueError("entries must be distinct")
        return sorted(value)


class IdentityPolicyDefaultModel(IdentityPolicyModel):
    """The tenant's identity default: a policy plus the personal-key ceiling."""

    max_user_key_ttl_days: StrictInt = Field(
        ..., ge=1, le=3650, description="Longest personal API key lifetime in days"
    )


class IdentityPolicyPut(BaseModel):
    """`PUT …/identity-policy`: the wrapper lets null (clear) be said."""

    model_config = ConfigDict(extra="forbid")

    policy: Optional[IdentityPolicyModel] = Field(..., description="The explicit policy, or null to clear it")


class IdentityPolicyResponse(BaseModel):
    group_id: str
    group_name: str
    is_ai_system: bool
    lifecycle_stage: Optional[str] = None
    eu_ai_act_risk_tier: Optional[str] = None
    attributed_to_ai_system: bool
    policy: Optional[Dict[str, Any]] = None
    effective: Dict[str, Any]
    inherited_floor: Dict[str, Any]
    sources: Dict[str, Dict[str, Any]]
    max_user_key_ttl_days: int


class IdentityCallsByLevel(BaseModel):
    key: int = 0
    bound_key: int = 0
    workload: int = 0
    workload_pop: int = 0
    outside_ladder: int = 0


class IdentityOutcomes(BaseModel):
    met: int = 0
    below_policy: int = 0
    refused: int = 0
    exempt: int = 0


class IdentityRunsByMinLevel(BaseModel):
    key: int = 0
    bound_key: int = 0
    workload: int = 0
    workload_pop: int = 0
    none: int = 0


class IdentitySummaryResponse(BaseModel):
    ai_system_id: str
    window_days: int
    effective: Dict[str, Any]
    calls_by_level: IdentityCallsByLevel
    outcomes: IdentityOutcomes
    runs_by_min_level: IdentityRunsByMinLevel
    credentials: int


class IdentityCredentialItem(BaseModel):
    credential_ref: Optional[str] = None
    credential_kind: Optional[str] = None
    credential_assurance: Optional[str] = None
    calls: int
    below_policy_calls: int
    refused_calls: int
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    api_key_id: Optional[str] = None
    api_key_name: Optional[str] = None
    agent_id: Optional[str] = None


class IdentityCredentialsResponse(BaseModel):
    items: List[IdentityCredentialItem]
    total: int
