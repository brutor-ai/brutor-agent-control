# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tenant Model
Supports multi-tenant deployments with data isolation
"""

from sqlalchemy import Column, String, Boolean, Integer, BigInteger, DateTime, Text, JSON, Index, UniqueConstraint, ForeignKey, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import validates
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.constants.tenant import TenantResourceTypes, TenantDefaultTheme, TENANT_FEATURE_KEYS
from app.database import Base
from app.services.mfa.policy import effective_mfa_policy


class Tenant(Base):
    """
    Tenant model for multi-tenant deployments

    Each tenant represents an isolated organization/company using the gateway.
    The 'default' tenant is created on first installation for single-tenant mode.

    Key features:
    - Data isolation per tenant
    - Resource quotas and limits
    - Billing integration ready
    - Feature flags per tenant
    """
    __tablename__ = "tenants"

    # Primary Key
    id = Column(String(255), primary_key=True)

    # Tenant Identity
    name = Column(
        String(255),
        nullable=False,
        comment="Unique tenant name (slug-friendly)"
    )

    display_name = Column(
        String(255),
        nullable=False,
        comment="Display name for UI"
    )

    # Organization Details
    organization_name = Column(
        String(500),
        nullable=True,
        comment="Full organization/company name"
    )

    domain = Column(
        String(255),
        nullable=True,
        comment="Primary domain for this tenant (e.g., 'acme.com')"
    )

    # Contact Information
    primary_contact_email = Column(
        String(255),
        nullable=False,
        comment="Primary contact email for this tenant"
    )

    primary_contact_name = Column(
        String(255),
        nullable=True,
        comment="Primary contact person name"
    )

    # Status
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether tenant is active"
    )

    is_default = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this is the default tenant (single-tenant mode)"
    )

    is_trial = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether tenant is on trial period"
    )

    trial_ends_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When trial period ends"
    )

    suspended = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether tenant is suspended (payment issues, violation, etc.)"
    )

    suspended_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When tenant was suspended"
    )

    suspension_reason = Column(
        Text,
        nullable=True,
        comment="Reason for suspension"
    )

    # Resource Quotas: exactly the tenant-created
    # resources an operator caps — dependent objects (VMCPs, OAuth clients,
    # server configs) follow their parent and are deliberately not quota'd.
    # null = unlimited everywhere.
    max_admin_users = Column(
        Integer,
        nullable=True,
        comment="Maximum number of admin users (null = unlimited)"
    )

    max_end_users = Column(
        Integer,
        nullable=True,
        comment="Maximum number of end users (null = unlimited)"
    )

    max_end_user_groups = Column(
        Integer,
        nullable=True,
        comment="Maximum number of end-user groups (null = unlimited)"
    )

    max_llm_models = Column(
        Integer,
        nullable=True,
        comment="Maximum number of configured (enabled) AI models — not the catalog (null = unlimited)"
    )

    max_mcp_servers = Column(
        Integer,
        nullable=True,
        comment="Maximum number of configured (enabled) MCP servers (null = unlimited)"
    )

    max_mcp_registries = Column(
        Integer,
        nullable=True,
        comment="Maximum number of MCP registry configurations (null = unlimited)"
    )

    max_kb_collections = Column(
        Integer,
        nullable=True,
        comment="Maximum number of knowledge base collections (null = unlimited)"
    )

    max_agent_skills = Column(
        Integer,
        nullable=True,
        comment="Maximum number of agent skills per tenant (null = unlimited)"
    )

    max_agent_cards = Column(
        Integer,
        nullable=True,
        comment="Maximum number of A2A agent cards (null = unlimited)"
    )

    max_agent_identities = Column(
        Integer,
        nullable=True,
        comment="Maximum number of A2A agent identities (null = unlimited)"
    )

    # Media-files quota. Kept OUTSIDE the resource-quota set because it is a live byte ceiling enforced by the
    # Rust core at upload (files.rs), not an object count. Bytes rather than
    # GB for finer per-image control,
    # (b) different lifecycle (logs autopurge, media is user-deletable),
    # and (c) different billing tier mapping (image gen tenants
    # consume orders of magnitude more bytes).
    media_storage_quota_bytes = Column(
        BigInteger,
        nullable=True,
        comment=(
            "Quota for the multimodal Files API (sum of size_bytes "
            "of non-deleted rows in media_files). NULL = unlimited "
            "(self-hosted default). Enforced synchronously at upload."
        )
    )

    # Tenant-wide default embedding model.
    # Used by KB ingestion and semantic cache for every collection and
    # cache config in this tenant. Guarantees a single vector space per
    # tenant — changing this requires a full re-embed (B6 flow).
    default_embedding_model_id = Column(
        String(255),
        ForeignKey("llm_model.id", ondelete="SET NULL"),
        nullable=True,
        comment="Tenant-wide default embedding model (FK to llm_model.id)"
    )

    # Tenant-wide default judge model.
    # Used by Semantic Governance Policies: when a policy doesn't pin its own
    # `judge_model_id`, the runtime LLM judge runs on this model. NULL → the
    # runtime falls back to the tenant's first enabled chat model.
    default_judge_model_id = Column(
        String(255),
        ForeignKey("llm_model.id", ondelete="SET NULL"),
        nullable=True,
        comment="Tenant-wide default semantic-policy judge model (FK to llm_model.id)"
    )

    # The Portal's Google Drive picker (Enterprise Edition: the chat's
    # attachment panel). The OAuth client is a trust relationship with the
    # tenant's Google Workspace organisation, so it is the tenant's. Both
    # values are PUBLIC BY DESIGN and stored as written, not encrypted: the
    # client id is public, and the API key is a browser key that every
    # browser of the Portal receives (it must be restricted by HTTP
    # referrer to the Portal's origin and to the Picker API). Set and
    # cleared together (`TenantSettingsUpdate`); served by the Enterprise
    # Core Gateway to signed-in Portal users only.
    portal_google_drive_client_id = Column(
        String(255),
        nullable=True,
        comment="Google OAuth client id of the Portal's Drive picker (public; NULL = picker not configured)",
    )
    portal_google_drive_api_key = Column(
        String(255),
        nullable=True,
        comment="Google browser API key of the Portal's Drive picker (public by design; restrict by referrer)",
    )

    # Feature Flags
    features = Column(
        JSON,
        nullable=False,
        default=dict,
        comment="Feature flags for this tenant"
    )

    # Settings
    settings = Column(
        JSON,
        nullable=False,
        default=dict,
        comment="Tenant-specific settings"
    )

    ai_system_gate_policy = Column(
        JSON,
        nullable=True,
        comment=(
            "Per-tenant overrides for the evidence each "
            "AI System lifecycle transition requires. NULL means the "
            "built-in defaults, which already tighten "
            "automatically for eu_ai_act_risk_tier='high'. A dedicated "
            "column rather than a key in `settings` because a governance "
            "gate is not a preference: it needs to be greppable, "
            "auditable, and hard to change by accident."
        )
    )

    assurance_capture_interval_hours = Column(
        Integer,
        nullable=True,
        comment=(
            "Hours between automatic AI System assurance "
            "captures. NULL disables scheduled capture: a job nobody switched "
            "on should not silently accumulate snapshots of every system."
        )
    )

    default_approval_window_seconds = Column(
        Integer,
        nullable=True,
        comment=(
            "Default hold window when no group in the chain states one "
            "(seconds). NULL = the platform default"
        ),
    )
    # The tenant's fallback for how long a human-approval hold stays
    # open (see `app/constants/approval_window.py`). 60 s .. 7 days.

    # Security policy
    mfa_policy = Column(
        String(20),
        default="optional",
        nullable=False,
        server_default="optional",
        comment="MFA enforcement: optional, required_admins, required_all"
    )

    # Billing plan label
    billing_plan = Column(
        String(50),
        nullable=True,
        comment="Billing plan: free, starter, professional, enterprise"
    )

    billing_email = Column(
        String(255),
        nullable=True,
        comment="Email for billing notifications"
    )

    billing_metadata = Column(
        JSON,
        nullable=True,
        comment="Additional billing metadata (Stripe customer ID, etc.)"
    )

    # ------------------------------------------------------------------
    # Tenant Branding — Theme / Styling Configuration
    # ------------------------------------------------------------------
    # Legacy flat columns: the fallback for callers that still read the
    # flat shape.
    # New code paths read `theme_variants[admin_light]` instead.
    # When the deprecation window closes, these columns are dropped
    # by a follow-up alembic revision and the read paths below switch
    # to the JSON variant as the sole source of truth.
    theme_primary_color = Column(String(7), nullable=True, default=TenantDefaultTheme.PRIMARY_COLOR.value)
    theme_secondary_color = Column(String(7), nullable=True, default=TenantDefaultTheme.SECONDARY_COLOR.value)
    theme_background_color = Column(String(7), nullable=True, default=TenantDefaultTheme.BACKGROUND_COLOR.value)
    theme_surface_color = Column(String(7), nullable=True, default=TenantDefaultTheme.SURFACE_COLOR.value)
    theme_text_primary = Column(String(7), nullable=True, default=TenantDefaultTheme.TEXT_PRIMARY_COLOR.value)
    theme_text_secondary = Column(String(7), nullable=True, default=TenantDefaultTheme.TEXT_SECONDARY_COLOR.value)
    theme_border_color = Column(String(7), nullable=True, default=TenantDefaultTheme.BORDER_COLOR.value)
    theme_logo_url = Column(String(500), nullable=True)
    theme_favicon_url = Column(String(500), nullable=True)

    # Shared across all four variants — branding always says the
    # same company name regardless of surface or light/dark mode.
    theme_company_name = Column(String(255), nullable=True)

    # Per-variant theme overrides. Shape:
    #   { admin_light: {...9 fields...}, admin_dark: {...},
    #     portal_light: {...}, portal_dark: {...} }
    # Any variant key may be missing — the read path falls back to
    # the appropriate default palette (DEFAULT_THEME_LIGHT / _DARK).
    # The `admin_light` variant additionally falls back to the flat
    # `theme_*` columns above, so existing tenants render unchanged
    # before any operator edits the variant.
    theme_variants = Column(JSONB, nullable=True)

    # ---------------------------------------------------------------------
    # KB connector identity: derivation from a sign-in's email.
    # Nothing reads or writes this column in this release (there is no
    # single sign-on to derive from); it stays in the schema until the
    # migrations are squashed.
    # ---------------------------------------------------------------------
    kb_oidc_auto_derive_enabled = Column(
        Boolean,
        nullable=False,
        server_default="true",
        default=True,
        comment=(
            "Tenant-wide kill switch for OIDC/SAML claim-driven auto-creation "
            "of kb_connector_identity_mappings rows."
        ),
    )

    # ---------------------------------------------------------------------
    # Operator-controlled traffic gate
    # ---------------------------------------------------------------------
    # Read by gateway-core + gateway-control request middleware on
    # every request. Cached 1s; invalidated by `brutor:maintenance`
    # NOTIFY when the operator toggles via the System → Maintenance
    # page.
    #
    # Modes:
    #   'off'       — normal operation (the default)
    #   'read_only' — POST / PATCH / DELETE return 503
    #   'drain'     — new requests 503, in-flight finish, replica
    #                 exits cleanly after `drain_grace_secs`
    #   'paused'    — read and write both 503 (incident response)
    #
    # The history of WHO toggled WHEN lives in `system_operation_log`;
    # these columns only carry the *current* state for the hot-path read.
    maintenance_mode = Column(
        String(16),
        nullable=False,
        server_default="off",
        default="off",
        comment="Operator traffic gate: off | read_only | drain | paused.",
    )
    maintenance_mode_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When maintenance_mode was last changed.",
    )
    maintenance_mode_by = Column(
        String(255),
        nullable=True,
        comment="Admin id that last changed maintenance_mode.",
    )
    drain_grace_secs = Column(
        Integer,
        nullable=True,
        comment=(
            "Per-tenant override for drain-mode grace seconds. NULL = "
            "use global default. Only meaningful when "
            "maintenance_mode = 'drain'."
        ),
    )

    # ---------------------------------------------------------------------
    # Data residency
    # ---------------------------------------------------------------------
    # Per-tenant residency profile read by the Rust runtime at
    # request time. Shape:
    #   {
    #     "allowed_regions": ["eu-west-1", "eu-central-1"],
    #     "block_global": true
    #   }
    # NULL = no enforcement (existing tenants keep working). When
    # set, the proxy intersects provider/server `region` with
    # `allowed_regions`; mismatches return 403 region_not_allowed.
    # `block_global` refuses providers with `region=global` or
    # `region=NULL` even if allowed_regions is empty — useful for
    # tenants that need every upstream pinned to a specific region.
    data_residency = Column(
        JSONB,
        nullable=True,
        comment=(
            "Per-tenant data residency profile: allowed_regions + "
            "block_global. NULL = no enforcement. See "
            "services::residency in the Rust runtime."
        ),
    )

    # Data retention. What the proxy stores for bodies, and how long
    # proxy-log rows, their bodies and run-ledger rows are kept. Read by the
    # control-plane sweeper (`services/data_retention`) and, for
    # `store_bodies`, by the Rust audit writer at flush time. NULL = store
    # everything, keep everything — the pre-a166 behaviour.
    data_retention = Column(
        JSONB,
        nullable=True,
        comment=(
            "Per-tenant retention policy: store_bodies (full|redacted|none), "
            "body_days, log_days, run_days. NULL = keep everything. See "
            "app/api/v1/models/admin/retention.py."
        ),
    )

    # ---------------------------------------------------------------------
    # Compliance framework declaration.
    # Shape: {"frameworks": ["gdpr_art_30", "soc2", "hipaa",
    # "eu_ai_act", "iso_42001"]}. The tagging engine in
    # `app/services/compliance/tagging.py` reads this on every call;
    # an empty frameworks list (the default) short-circuits the
    # engine — no tags are written, `proxy_logs.compliance_tags`
    # stays NULL.
    compliance_profile = Column(
        JSONB,
        nullable=False,
        server_default='{"frameworks": []}',
        comment=(
            "Per-tenant compliance regime declaration. Shape: "
            "{\"frameworks\": [...]}. Empty list = engine no-op. "
            "See app/services/compliance/tagging.py."
        ),
    )

    # Audit Fields
    created_by = Column(
        String(50),
        nullable=True,
        comment="System admin who created this tenant"
    )

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_active_at = Column(DateTime(timezone=True), nullable=True, comment="Last activity from this tenant")

    # Relationships
    # Note: Relationships to other models will be added via ForeignKey on those models

    __table_args__ = (
        CheckConstraint(
            'default_approval_window_seconds IS NULL OR '
            '(default_approval_window_seconds >= 60 AND default_approval_window_seconds <= 604800)',
            name='ck_tenants_default_approval_window',
        ),
        # Unique constraints (name and domain must be globally unique)
        UniqueConstraint('name', name='uix_tenants_name'),
        UniqueConstraint('domain', name='uix_tenants_domain'),

        # Tenant lookup by name (most common)
        Index('ix_tenants_name', 'name'),

        # Active tenants - "Get all active tenants"
        Index('ix_tenants_active', 'is_active'),

        # Default tenant lookup
        Index('ix_tenants_default', 'is_default'),

        # Domain lookup - "Find tenant by domain"
        Index('ix_tenants_domain', 'domain'),

        # Status queries - "Find suspended tenants"
        Index('ix_tenants_suspended', 'suspended'),

        # Trial management - "Find active trial tenants"
        Index('ix_tenants_trial', 'is_trial', 'trial_ends_at'),

        # Billing queries - "Get all enterprise tenants"
        Index('ix_tenants_billing_plan', 'billing_plan'),

        # Activity tracking - "Find inactive tenants"
        Index('ix_tenants_last_active', 'last_active_at'),

        # Audit queries
        Index('ix_tenants_created', 'created_at'),

        {
            'comment': 'Multi-tenant isolation'
        }
    )

    def __repr__(self) -> str:
        return f"<Tenant(id='{self.id}', name='{self.name}', is_default={self.is_default})>"

    @validates('name')
    def validate_name(self, key, value):
        """Validate tenant name is URL-friendly"""
        if not value:
            raise ValueError("Tenant name is required")
        # Allow alphanumeric, hyphens, underscores
        import re
        if not re.match(r'^[a-z0-9][a-z0-9-_]*[a-z0-9]$', value):
            raise ValueError(
                "Tenant name must be lowercase alphanumeric with hyphens/underscores, "
                "starting and ending with alphanumeric"
            )
        if len(value) < 2 or len(value) > 63:
            raise ValueError("Tenant name must be 2-63 characters")
        return value

    def is_quota_exceeded(self, resource_type: TenantResourceTypes, current_count: int) -> bool:
        """
        Check if a resource quota is exceeded

        Args:
            resource_type: Type of resource (admin_users, end_users, mcp_servers, etc.)
            current_count: Current count of resources

        Returns:
            True if quota exceeded, False otherwise
        """
        quota_map = {
            TenantResourceTypes.ADMIN_USERS: self.max_admin_users,
            TenantResourceTypes.END_USERS: self.max_end_users,
            TenantResourceTypes.END_USER_GROUPS: self.max_end_user_groups,
            TenantResourceTypes.LLM_MODELS: self.max_llm_models,
            TenantResourceTypes.MCP_SERVERS: self.max_mcp_servers,
            TenantResourceTypes.REGISTRIES: self.max_mcp_registries,
            TenantResourceTypes.KNOWLEDGE_BASE: self.max_kb_collections,
            TenantResourceTypes.AGENT_SKILLS: self.max_agent_skills,
            TenantResourceTypes.AGENT_CARDS: self.max_agent_cards,
            TenantResourceTypes.AGENT_IDENTITIES: self.max_agent_identities,
        }

        max_limit = quota_map.get(resource_type)

        # If no limit set (None), no quota exceeded
        if max_limit is None:
            return False

        return current_count >= max_limit

    def has_feature(self, feature_name: str) -> bool:
        """
        Check if tenant has a specific feature enabled.

        When features is None (not configured), ALL features are enabled
        by default. This ensures local development and fresh tenants have
        full access. Features are only restricted when explicitly set
        to False in the features JSON.

        Args:
            feature_name: Feature to check

        Returns:
            True if feature is enabled (or no feature flags configured)
        """
        if not self.features:
            return True  # No feature flags configured = all features enabled
        return self.features.get(feature_name, True)

    def enable_feature(self, feature_name: str) -> None:
        """Enable a feature for this tenant"""
        if not self.features:
            self.features = {}
        self.features[feature_name] = True

    def disable_feature(self, feature_name: str) -> None:
        """Disable a feature for this tenant"""
        if not self.features:
            self.features = {}
        self.features[feature_name] = False

    def get_setting(self, setting_name: str, default: Any = None) -> Any:
        """Get a tenant-specific setting"""
        if not self.settings:
            return default
        return self.settings.get(setting_name, default)

    def set_setting(self, setting_name: str, value: Any) -> None:
        """Set a tenant-specific setting"""
        if not self.settings:
            self.settings = {}
        self.settings[setting_name] = value

    def suspend(self, reason: str) -> None:
        """Suspend this tenant"""
        self.suspended = True
        self.suspended_at = datetime.now(timezone.utc)
        self.suspension_reason = reason

    def unsuspend(self) -> None:
        """Unsuspend this tenant"""
        self.suspended = False
        self.suspended_at = None
        self.suspension_reason = None

    def activate(self) -> None:
        """Activate this tenant"""
        self.is_active = True
        self.suspended = False
        self.suspended_at = None
        self.suspension_reason = None

    def deactivate(self) -> None:
        """Deactivate this tenant"""
        self.is_active = False

    def update_last_activity(self) -> None:
        """Update last activity timestamp"""
        self.last_active_at = datetime.now(timezone.utc)

    def is_trial_expired(self) -> bool:
        """Check if trial period has expired"""
        if not self.is_trial or not self.trial_ends_at:
            return False
        ends_at = self.trial_ends_at
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > ends_at

    def to_dict(self) -> dict:
        """Convert tenant to dictionary representation."""
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'organization_name': self.organization_name,
            'domain': self.domain,
            'primary_contact_email': self.primary_contact_email,
            'primary_contact_name': self.primary_contact_name,

            # Status fields
            'is_active': self.is_active,
            'is_default': self.is_default,
            'is_trial': self.is_trial,
            'trial_ends_at': self.trial_ends_at.isoformat() if self.trial_ends_at else None,
            'suspended': self.suspended,
            'suspended_at': self.suspended_at.isoformat() if self.suspended_at else None,
            'suspension_reason': self.suspension_reason,

            # Configuration
            'quotas': self.get_quotas(),
            'features': self.features or {},
            'settings': self.settings or {},
            # The effective policy, i.e. the one that is enforced: an
            # unrecognised stored value reads as `required_all`.
            'mfa_policy': effective_mfa_policy(self.mfa_policy),
            'default_embedding_model_id': self.default_embedding_model_id,
            'default_judge_model_id': self.default_judge_model_id,
            'portal_google_drive_client_id': self.portal_google_drive_client_id,
            'portal_google_drive_api_key': self.portal_google_drive_api_key,

            # Billing
            'billing_plan': self.billing_plan,
            'billing_email': self.billing_email,

            # Theme
            'theme': self.get_theme(),

            # AI System assurance posture. Exposed on read so the
            # admin UI can show current state — a setting you can write but
            # not read back is only half-reachable.
            'assurance_capture_interval_hours': self.assurance_capture_interval_hours,
            'default_approval_window_seconds': self.default_approval_window_seconds,
            'ai_system_gate_policy': self.ai_system_gate_policy,

            # Data residency
            'data_residency': self.data_residency,
            'data_retention': self.data_retention,
            'compliance_profile': self.compliance_profile or {"frameworks": []},


            # Metadata
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_active_at': self.last_active_at.isoformat() if self.last_active_at else None,
        }

    def get_quotas(self) -> dict:
        """
        Get all quota limits for this tenant.

        Returns a dictionary with all quota values.
        None values indicate unlimited resources.

        Returns:
            Dictionary containing all quota limits
        """
        return {
            'max_admin_users': self.max_admin_users,
            'max_end_users': self.max_end_users,
            'max_end_user_groups': self.max_end_user_groups,
            'max_llm_models': self.max_llm_models,
            'max_mcp_servers': self.max_mcp_servers,
            'max_mcp_registries': self.max_mcp_registries,
            'max_kb_collections': self.max_kb_collections,
            'max_agent_skills': self.max_agent_skills,
            'max_agent_cards': self.max_agent_cards,
            'max_agent_identities': self.max_agent_identities,
            # Live byte ceiling (enforced at upload by the core), reported
            # alongside the resource quotas so operators can see it.
            'media_storage_quota_bytes': self.media_storage_quota_bytes,
        }

    def get_theme(self) -> dict:
        """Legacy single-theme accessor. Returns the admin-light
        variant — the same shape as the legacy flat columns. New code should call
        `get_theme_variants()` or `get_theme_variant(name)` instead.
        """
        return self.get_theme_variant("admin_light")

    def get_theme_variant(self, variant: str) -> dict:
        """Read one of admin_light / admin_dark / portal_light /
        portal_dark, layering: explicit per-variant JSON →
        (admin_light only) legacy flat columns → mode-specific
        defaults. `company_name` is appended (shared across
        variants)."""
        from app.constants.tenant import default_theme_for_variant

        defaults = default_theme_for_variant(variant)

        stored = (self.theme_variants or {}).get(variant) or {}

        # admin_light gets the legacy-flat-column fallback so tenants
        # who customised under the old single-theme schema keep their
        # look without operator action.
        legacy = {}
        if variant == "admin_light":
            legacy = {
                "primary_color": self.theme_primary_color,
                "secondary_color": self.theme_secondary_color,
                "background_color": self.theme_background_color,
                "surface_color": self.theme_surface_color,
                "text_primary": self.theme_text_primary,
                "text_secondary": self.theme_text_secondary,
                "border_color": self.theme_border_color,
                "logo_url": self.theme_logo_url,
                "favicon_url": self.theme_favicon_url,
            }
            legacy = {k: v for k, v in legacy.items() if v is not None}

        merged = {**defaults, **legacy, **{k: v for k, v in stored.items() if v is not None}}
        merged["company_name"] = self.theme_company_name or self.display_name
        return merged

    def get_theme_variants(self) -> dict:
        """Return all four variants in one dict — the shape the
        admin theme editor and the public portal endpoint return."""
        from app.constants.tenant import THEME_VARIANTS

        return {
            "company_name": self.theme_company_name or self.display_name,
            "variants": {v: self.get_theme_variant(v) for v in THEME_VARIANTS},
        }

    def set_theme_variant(self, variant: str, payload: dict) -> None:
        """Replace one variant's overrides. Null/missing keys clear
        any previous override so the read path returns the default
        for that field. Drops empty variant entries to keep the JSON
        column compact."""
        from app.constants.tenant import THEME_VARIANTS

        if variant not in THEME_VARIANTS:
            raise ValueError(f"Unknown theme variant {variant!r}")
        current = dict(self.theme_variants or {})
        cleaned = {k: v for k, v in (payload or {}).items() if v not in (None, "")}
        if cleaned:
            current[variant] = cleaned
        else:
            current.pop(variant, None)
        self.theme_variants = current or None

    def to_theme_dict(self) -> Dict[str, Optional[str]]:
        """
        Convert tenant theme configuration to dictionary

        This method extracts all theme-related fields from the tenant
        and returns them in a format suitable for API responses.

        Returns:
            Dictionary containing theme configuration with snake_case keys
            for backend, or can be easily converted to camelCase for frontend
        """
        return {
            "primary_color": self.theme_primary_color or TenantDefaultTheme.PRIMARY_COLOR.value,
            "secondary_color": self.theme_secondary_color or TenantDefaultTheme.SECONDARY_COLOR.value,
            "background_color": self.theme_background_color or TenantDefaultTheme.BACKGROUND_COLOR.value,
            "surface_color": self.theme_surface_color or TenantDefaultTheme.SURFACE_COLOR.value,
            "text_primary": self.theme_text_primary or TenantDefaultTheme.TEXT_PRIMARY_COLOR.value,
            "text_secondary": self.theme_text_secondary or TenantDefaultTheme.TEXT_SECONDARY_COLOR.value,
            "border_color": self.theme_border_color or TenantDefaultTheme.BORDER_COLOR.value,
            "logo_url": self.theme_logo_url,
            "company_name": self.theme_company_name or self.display_name,
            "favicon_url": self.theme_favicon_url
        }

    def has_custom_theme(self) -> bool:
        """
        Check if tenant has customized their theme

        A theme is considered "custom" if:
        1. Any color deviates from default values
        2. A custom logo URL is set
        3. A custom favicon URL is set
        4. A custom company name is set

        Returns:
            True if tenant has any theme customizations, False otherwise
        """
        # Default color values
        default_colors = {
            TenantDefaultTheme.PRIMARY_COLOR.value,  # primary
            TenantDefaultTheme.SECONDARY_COLOR.value,  # secondary
            TenantDefaultTheme.BACKGROUND_COLOR.value,  # background
            TenantDefaultTheme.SURFACE_COLOR.value,  # surface
            TenantDefaultTheme.TEXT_PRIMARY_COLOR.value,  # text primary
            TenantDefaultTheme.TEXT_SECONDARY_COLOR.value,  # text secondary
            TenantDefaultTheme.BORDER_COLOR.value  # border
        }

        # Current color values (normalized to uppercase for comparison)
        current_colors = {
            (self.theme_primary_color or TenantDefaultTheme.PRIMARY_COLOR.value).upper(),
            (self.theme_secondary_color or TenantDefaultTheme.SECONDARY_COLOR.value).upper(),
            (self.theme_background_color or TenantDefaultTheme.BACKGROUND_COLOR.value).upper(),
            (self.theme_surface_color or TenantDefaultTheme.SURFACE_COLOR.value).upper(),
            (self.theme_text_primary or TenantDefaultTheme.TEXT_PRIMARY_COLOR.value).upper(),
            (self.theme_text_secondary or TenantDefaultTheme.TEXT_SECONDARY_COLOR.value).upper(),
            (self.theme_border_color or TenantDefaultTheme.BORDER_COLOR.value).upper()
        }

        # Normalize defaults to uppercase for comparison
        default_colors_upper = {c.upper() for c in default_colors}

        # Check if colors differ from defaults
        has_custom_colors = not current_colors.issubset(default_colors_upper)

        # Check if custom assets are set
        has_custom_assets = bool(
            self.theme_logo_url or
            self.theme_favicon_url or
            self.theme_company_name
        )

        return has_custom_colors or has_custom_assets

    def apply_theme_from_dict(self, theme_dict: Dict[str, Optional[str]]) -> None:
        """
        Apply theme configuration from a dictionary

        This is useful for bulk updates or when creating a tenant
        with initial theme configuration.

        Args:
            theme_dict: Dictionary with theme configuration
                       Keys can be either snake_case or camelCase
        """
        # Map both snake_case and camelCase keys to attribute names
        key_mapping = {
            'primary_color': 'theme_primary_color',
            'primaryColor': 'theme_primary_color',
            'secondary_color': 'theme_secondary_color',
            'secondaryColor': 'theme_secondary_color',
            'background_color': 'theme_background_color',
            'backgroundColor': 'theme_background_color',
            'surface_color': 'theme_surface_color',
            'surfaceColor': 'theme_surface_color',
            'text_primary': 'theme_text_primary',
            'textPrimary': 'theme_text_primary',
            'text_secondary': 'theme_text_secondary',
            'textSecondary': 'theme_text_secondary',
            'border_color': 'theme_border_color',
            'borderColor': 'theme_border_color',
            'logo_url': 'theme_logo_url',
            'logoUrl': 'theme_logo_url',
            'company_name': 'theme_company_name',
            'companyName': 'theme_company_name',
            'favicon_url': 'theme_favicon_url',
            'faviconUrl': 'theme_favicon_url'
        }

        for key, value in theme_dict.items():
            if key in key_mapping:
                attr_name = key_mapping[key]
                if hasattr(self, attr_name):
                    setattr(self, attr_name, value)

    def reset_theme(self) -> None:
        """
        Reset theme to default values

        This removes all theme customizations and reverts
        the tenant to the default Brutor Agent Control Plane theme.
        """
        self.theme_primary_color = TenantDefaultTheme.PRIMARY_COLOR.value
        self.theme_secondary_color = TenantDefaultTheme.SECONDARY_COLOR.value
        self.theme_background_color = TenantDefaultTheme.BACKGROUND_COLOR.value
        self.theme_surface_color = TenantDefaultTheme.SURFACE_COLOR.value
        self.theme_text_primary = TenantDefaultTheme.TEXT_PRIMARY_COLOR.value
        self.theme_text_secondary = TenantDefaultTheme.TEXT_SECONDARY_COLOR.value
        self.theme_border_color = TenantDefaultTheme.BORDER_COLOR.value
        self.theme_logo_url = None
        self.theme_company_name = None
        self.theme_favicon_url = None

    def get_theme_summary(self) -> str:
        """
        Get a human-readable summary of the theme

        Useful for logging and admin interfaces.

        Returns:
            String describing the current theme configuration
        """
        if not self.has_custom_theme():
            return "Default theme (Brutor Agent Control Plane)"

        parts = []

        # Check for custom colors
        if self.theme_primary_color and self.theme_primary_color.upper() != TenantDefaultTheme.PRIMARY_COLOR.value:
            parts.append(f"Primary: {self.theme_primary_color}")

        # Check for custom logo
        if self.theme_logo_url:
            parts.append("Custom logo")

        # Check for custom company name
        if self.theme_company_name:
            parts.append(f"Company: {self.theme_company_name}")

        if parts:
            return f"Custom theme ({', '.join(parts)})"
        else:
            return "Partially customized theme"

    @classmethod
    def create_default_tenant(cls) -> 'Tenant':
        """
        Create the default tenant for single-tenant installations

        Returns:
            Default tenant instance (not yet added to session)
        """
        return cls(
            id='default',
            name='default',
            display_name='Default Organization',
            organization_name='Default Organization',
            primary_contact_email='admin@localhost',
            is_active=True,
            is_default=True,
            is_trial=False,
            suspended=False,
            billing_plan='self_hosted',
            # No resource limits for default tenant
            max_admin_users=None,
            max_end_users=None,
            max_end_user_groups=None,
            max_llm_models=None,
            max_mcp_servers=None,
            max_mcp_registries=None,
            max_kb_collections=None,
            max_agent_skills=None,
            max_agent_cards=None,
            max_agent_identities=None,
            # 2026-08-29 feature standardization: exactly the canonical
            # TENANT_FEATURE_KEYS (constants/tenant.py), all enabled for the
            # default tenant. Retired flags (docker_deployment,
            # oauth_integration, multi_user, user_groups, audit_logs,
            # metrics, semantic_cache, batch_jobs and the hyphenated
            # mcp-servers / virtual-servers keys) are gone — those areas are
            # always on. Keep in sync with the admin UI's
            # lib/tenantFeatureFields.ts and the licence library.
            #
            # "All" means all this edition has. A Community
            # composition leaves the Enterprise features' flags off — it
            # cannot serve them, and its next start would read a flag that
            # is on as Enterprise data.
            features=_default_tenant_feature_flags(),
            settings={},
            created_at=datetime.now(timezone.utc)
        )


def _default_tenant_feature_flags() -> Dict[str, bool]:
    from app.composition.edition import default_tenant_feature_flags

    return default_tenant_feature_flags(TENANT_FEATURE_KEYS)


# Keep the per-request tenant read model honest: any change to a tenant row
# (suspension, deactivation, rename, deletion) drops its cached verdict so
# the next request consults the table again. See app/services/auth_read_model.py.
from sqlalchemy import event as _sa_event  # noqa: E402


@_sa_event.listens_for(Tenant, "after_update")
@_sa_event.listens_for(Tenant, "after_delete")
def _forget_tenant_read_model(mapper, connection, target) -> None:  # noqa: ARG001
    from app.services import auth_read_model
    auth_read_model.forget_tenant(getattr(target, "id", None))
