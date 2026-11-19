# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tenant settings: what the administrator of an installation reads and changes
on its own tenant (`/v1/admin/tenants/current`, Community Edition).

The model of the update lists every field that route accepts and forbids
anything else: quotas, feature flags, billing, status and branding are not
settings of the installation, they are tenant management (Enterprise Edition,
`TenantUpdate` in tenant.py, which extends this model).
"""

import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.v1.models.admin.identity_policy import IdentityPolicyDefaultModel
from app.api.v1.models.admin.residency import DataResidencyProfile
from app.api.v1.models.admin.retention import DataRetentionPolicy
from app.api.v1.models.admin.tenant_theme import TenantThemeConfig
from app.services.mfa.policy import MfaPolicy


class TenantSettingsUpdate(BaseModel):
    """Request model for updating the caller's own tenant.

    A field that is not declared here is refused (422), never dropped: a
    caller that sends a quota, a feature flag or a status is told so.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(None, min_length=2, max_length=255)
    organization_name: Optional[str] = Field(None, max_length=500)
    domain: Optional[str] = Field(None, max_length=255)
    primary_contact_email: Optional[str] = None
    primary_contact_name: Optional[str] = Field(None, max_length=255)

    # Free-form settings of the installation.
    settings: Optional[Dict[str, Any]] = None

    # The tenant's two-factor policy. One of three values, or the field
    # is refused; omitted (or null) leaves the stored policy alone. It is
    # enforced, not just stored: see `app/services/mfa/policy.py` and
    # `apply_mfa_policy_change` in the tenant settings endpoint.
    mfa_policy: Optional[MfaPolicy] = Field(
        None,
        description=(
            "Two-factor policy of the tenant. `optional`: nobody is required to "
            "have 2FA. `required_admins`: every administrator of the tenant must "
            "have 2FA; one without it gets no session at sign-in until they enrol. "
            "`required_all`: the same, and Portal users too. A tenant administrator "
            "can set a value other than `optional` only when their own account has "
            "2FA (409 otherwise). Making the policy stricter signs out the people "
            "it now covers who have no 2FA. Omit to leave unchanged."
        ),
    )

    # Data residency profile (JSONB). Null clears
    # enforcement; an object with allowed_regions + block_global turns
    # it on. Tenant admins are allowed to write this on their own
    # tenant.
    data_residency: Optional[DataResidencyProfile] = Field(
        None,
        description=(
            "Per-tenant residency profile. Pass null to disable "
            "enforcement; pass an object with allowed_regions + "
            "block_global to enable. Field is omitted from the "
            "request entirely to leave the existing value untouched."
        ),
    )

    # Retention policy. Same explicit-null contract as residency:
    # null clears the policy (store everything, keep everything), an object
    # sets it, an omitted field leaves the stored value alone. Tenant admins
    # own their own ledger's retention.
    data_retention: Optional[DataRetentionPolicy] = Field(
        None,
        description=(
            "Per-tenant data retention policy. Pass null to store and keep "
            "everything; pass an object to set store_bodies and the "
            "body/log/run windows. Omit to leave unchanged."
        ),
    )

    # Tenant-wide default embedding model — applies to every KB collection
    # and semantic cache config. Pass None explicitly to clear.
    default_embedding_model_id: Optional[str] = Field(
        None,
        description=(
            "FK to llm_model.id (must be an enabled embedding model in this "
            "tenant's catalog). Changing this requires re-embedding all KB "
            "content and flushing the semantic cache."
        ),
    )

    # ── AI System assurance ─────────────────────────────────────────────
    # Both carry explicit-null semantics: null is a *decision* (disable
    # scheduled capture / fall back to the built-in gate requirements), not
    # "leave alone". `apply_tenant_update` re-adds them from `model_fields_set`
    # so `exclude_none` cannot silently drop the clear.
    #
    # Neither is reserved to tenant management. They are operational
    # posture for the tenant's own AI Systems, in the same category as
    # data_residency — not quota, not billing. A tenant that must capture
    # evidence nightly should not have to open a support ticket to do it.
    assurance_capture_interval_hours: Optional[int] = Field(
        None,
        ge=1,
        le=8760,
        description=(
            "Hours between automatic AI System assurance captures. Pass null "
            "to disable scheduled capture (the default). Health is refreshed "
            "on every scheduler tick regardless; this controls only how often "
            "an immutable report is captured. Max 8760 = one year."
        ),
    )

    # The tenant's default approval window. Explicit-null clears it
    # (back to the platform's 300 s), so it is handled like the two above.
    default_approval_window_seconds: Optional[int] = Field(
        None,
        ge=60,
        le=604800,
        description=(
            "Default seconds a human-approval hold stays open when neither the "
            "policy that raised it nor any Resource Group in its chain states a "
            "window. Pass null for the platform default (300). 60 .. 604800 (7 days)."
        ),
    )

    ai_system_gate_policy: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Per-tenant lifecycle gate requirements, keyed "
            "'from_stage->to_stage' with a list of requirement keys. Pass "
            "null to revert to the built-in defaults. A tenant may tighten "
            "or relax any transition, but human approval on a high-risk "
            "system is a floor the policy cannot remove."
        ),
    )

    identity_policy_default: Optional[IdentityPolicyDefaultModel] = Field(
        None,
        description=(
            "The tenant's identity default: an identity policy (all six members) plus "
            "`max_user_key_ttl_days`, the longest a personal API key may live. It takes part "
            "in every group's effective identity policy, stricter wins. Pass null for no "
            "tenant default. Setting it also needs `identity_policy:manage`."
        ),
    )

    # Tenant-wide default judge model — used by Semantic Governance Policies
    # when a policy doesn't pin its own judge.
    default_judge_model_id: Optional[str] = Field(
        None,
        description=(
            "FK to llm_model.id (an enabled chat model). Semantic policies "
            "without their own judge_model_id are evaluated by this model; "
            "NULL falls back to the tenant's first enabled chat model."
        ),
    )

    # The Portal's Google Drive picker. Public values (see the columns of
    # `Tenant`): stored as written. Both set or both clear after the
    # update; an empty string or null clears.
    portal_google_drive_client_id: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "OAuth client id of the Google Drive picker in the Portal's chat (Enterprise Edition), "
            "ending in .apps.googleusercontent.com. Public by design. Its Authorized JavaScript "
            "origins must list the Portal's origin. Set and cleared together with "
            "portal_google_drive_api_key (an update that would leave one without the other is "
            "refused, 422); an empty string or null clears."
        ),
    )
    portal_google_drive_api_key: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Google browser API key of the Drive picker (AIza followed by 35 characters). Public "
            "by design: every Portal browser receives it, so restrict it by HTTP referrer to the "
            "Portal's origin and to the Picker API. Set and cleared together with "
            "portal_google_drive_client_id."
        ),
    )

    @model_validator(mode="after")
    def _google_drive_picker_shape(self):
        """Each value has its shape; an empty one is a clear (null). That
        the two end up set together is checked against the stored row
        (`update_tenant_record`)."""
        for name, pattern, what in (
            ("portal_google_drive_client_id", _GOOGLE_CLIENT_ID,
             "a Google OAuth client id (….apps.googleusercontent.com)"),
            ("portal_google_drive_api_key", _GOOGLE_API_KEY, "a Google API key (AIza followed by 35 characters)"),
        ):
            if name not in self.model_fields_set:
                continue
            value = (getattr(self, name) or "").strip()
            if value and not pattern.fullmatch(value):
                raise ValueError(f"{name} must be {what}")
            setattr(self, name, value or None)
        return self


# The fields of the Portal's Drive picker: written by `update_tenant_record`
# even when they clear (null).
GOOGLE_DRIVE_PICKER_FIELDS = ("portal_google_drive_client_id", "portal_google_drive_api_key")
_GOOGLE_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.apps\.googleusercontent\.com")
_GOOGLE_API_KEY = re.compile(r"AIza[A-Za-z0-9_-]{35}")


class TenantResponse(BaseModel):
    """Response model for tenant details"""
    id: str
    name: str
    display_name: str
    organization_name: Optional[str]
    domain: Optional[str]
    primary_contact_email: str
    primary_contact_name: Optional[str]
    is_active: bool
    is_default: bool
    is_trial: bool
    trial_ends_at: Optional[str]
    suspended: bool
    suspended_at: Optional[str]
    suspension_reason: Optional[str]
    quotas: Dict[str, Optional[int]]
    features: Dict[str, bool]
    settings: Dict[str, Any]
    # The effective two-factor policy (an unrecognised stored value
    # reads as `required_all`, which is how it is enforced).
    mfa_policy: MfaPolicy = "optional"
    default_embedding_model_id: Optional[str] = None
    default_judge_model_id: Optional[str] = None
    portal_google_drive_client_id: Optional[str] = None
    portal_google_drive_api_key: Optional[str] = None
    billing_plan: Optional[str]
    billing_email: Optional[str]
    theme: Optional[TenantThemeConfig] = Field(None, description="Tenant theme configuration")
    # Surfaces the data residency JSONB profile in GET/PATCH
    # responses so the admin UI can render its enabled/disabled state.
    # AI System assurance posture — readable so the console can
    # render current state, not just accept a write.
    assurance_capture_interval_hours: Optional[int] = None
    default_approval_window_seconds: Optional[int] = None
    ai_system_gate_policy: Optional[Dict[str, Any]] = None
    identity_policy_default: Optional[Dict[str, Any]] = None

    data_residency: Optional[DataResidencyProfile] = Field(
        None, description="Per-tenant residency enforcement profile (null = disabled)"
    )
    data_retention: Optional[DataRetentionPolicy] = Field(
        None, description="Per-tenant data retention policy (null = keep everything)"
    )
    stats: Optional[Dict[str, int]] = Field(None, description="Resource counts")
    created_by: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    last_active_at: Optional[str]

    class Config:
        from_attributes = True


class CurrentTenantResponse(TenantResponse):
    """The caller's own tenant."""

    data_retention_effective: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "The retention policy as it is enforced: `data_retention` composed with the "
            "retention floors of the tenant's compliance frameworks. Null when it could not "
            "be composed."
        ),
    )


class NoCurrentTenant(BaseModel):
    """What a system administrator is answered: it belongs to no tenant.
    `id` is null and the three objects are empty."""

    id: None = Field(None, description="Always null: the caller has no tenant of its own.")
    features: Dict[str, bool] = Field(default_factory=dict)
    quotas: Dict[str, Optional[int]] = Field(default_factory=dict)
    settings: Dict[str, Any] = Field(default_factory=dict)
