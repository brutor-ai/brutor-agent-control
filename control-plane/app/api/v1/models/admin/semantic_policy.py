# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic request/response models for the Semantic Governance Policy admin API.
Every response field mirrors `SemanticPolicy.to_dict()` (the strip-field gotcha:
a key missing here is silently dropped from the API response).
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict

# Declare the vocabularies the database already enforces.
#
# `SemanticPolicy._v_action` / `_v_mode` reject anything outside these sets on
# write, so the values are constrained in fact. The response models declared
# plain `str`, which threw that constraint away at the API boundary: the
# OpenAPI schema said "string", the generated TypeScript said `string`, and the
# Admin Console re-asserted the real union by hand
# (`PolicyAction = 'block' | 'warn'` in semanticPolicyTypes.ts). A hand-written
# narrowing is an assertion nothing verifies — if the server ever widened the
# set, the console would keep compiling and silently mis-handle the new value.
#
# Mirrors app/database/models/semantic_policy.py::POLICY_ACTIONS / POLICY_MODES.
PolicyAction = Literal["block", "warn"]
PolicyMode = Literal["shadow", "enforce"]


class SemanticPolicyResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    constraint_text: str
    action: PolicyAction
    mode: PolicyMode
    # Required, not defaulted. `SemanticPolicy.to_dict()` always
    # emits both (`self.surfaces or []`, and the group ids as a list), so a
    # `default_factory` here declared an always-present field as optional. That
    # is a response model describing a response the server never sends: the
    # generated TypeScript came out `string[] | undefined`, and the console
    # either re-asserted it as required by hand or guarded a case that cannot
    # happen. Defaults belong on the REQUEST models below, where a client may
    # genuinely omit the key.
    surfaces: List[str]
    judge_model_id: Optional[str] = None
    enabled: bool
    resource_group_ids: List[str]
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SemanticPolicyListResponse(BaseModel):
    policies: List[SemanticPolicyResponse]
    total: int


class SemanticPolicyCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    constraint_text: str
    action: PolicyAction = "block"
    mode: PolicyMode = "shadow"
    surfaces: Optional[List[str]] = None
    judge_model_id: Optional[str] = None
    resource_group_ids: Optional[List[str]] = None


class SemanticPolicyUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    description: Optional[str] = None
    constraint_text: Optional[str] = None
    action: Optional[PolicyAction] = None
    mode: Optional[PolicyMode] = None
    surfaces: Optional[List[str]] = None
    judge_model_id: Optional[str] = None
    enabled: Optional[bool] = None
    resource_group_ids: Optional[List[str]] = None
