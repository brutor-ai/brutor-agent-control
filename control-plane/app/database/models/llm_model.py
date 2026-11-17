# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Large Language Model (LLM) Database Model
Manages LLM configurations with group-based access control and routing groups for load balancing
"""

from sqlalchemy import Column, String, Boolean, Integer, Float, JSON, Date, DateTime, Text, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import validates, relationship, Mapped
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from app.database.models.resource_mixins import ResourceAuthorshipMixin, ResourceVersionMixin
from app.database import Base
from app.constants.llm import LLModelMode

if TYPE_CHECKING:
    # Relationship annotations only (`Mapped[List["X"]]`): SQLAlchemy resolves
    # the names through its registry, and importing them here at run time would
    # be a cycle.
    from app.database.models.resource_group import ResourceGroup

class LLModel(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """
    LLM configuration model with group-based access control and routing groups

    All models belong to at least the 'Default' Resource Group. Model visibility is
    determined entirely by group membership.

    Models in the 'Default' group are accessible to all users.

    Models can be:
    1. Standalone - used independently (routing_group_id = None)
    2. Part of a routing group - load balanced with other models (routing_group_id set)

    The gateway's router handles load balancing, fallbacks, and routing strategies.
    """

    __tablename__ = "llm_model"

    # Basic Information
    id = Column(String(100), primary_key=True)

    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    name = Column(String(200), nullable=False, comment="Display name for this model configuration")
    description = Column(Text, nullable=True)
    usage = Column(Text, nullable=True, comment="Usage description and use cases")

    # Provider Information
    # The legacy `provider` string column was promoted to a first-class
    # entity in migration `a027_llm_providers_table`. The
    # FK below replaces it; read the slug via `model.provider.key`.
    provider_id = Column(
        String(64),
        ForeignKey("llm_providers.id", ondelete="RESTRICT"),
        nullable=False,
        comment=(
            "Foreign key into llm_providers.id. The Rust adapter "
            "dispatch reads `provider.key` from the joined row."
        ),
    )
    model_name = Column(String(200), nullable=False)  # gpt-4o, claude-sonnet-4-5, etc.
    mode = Column(
        String(20),
        nullable=False,
        default=LLModelMode.CHAT.value,
        comment="Model interface mode: 'chat' (conversational), 'embedding' (vector embeddings), 'completion' (text completion), 'responses' (structural), 'routing' (load-balancing group)"
    )

    # Model Characteristics (for filtering)
    parameters = Column(Float, nullable=True, comment="Model size in billions of parameters")
    tags = Column(JSON, nullable=True, comment="Tags for filtering (cost, speed, capabilities, use cases)")

    # Connection Details
    api_url_auto_discovered = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="If True, the provider's standard API URL is used. If False, api_url must be provided for custom deployments."
    )
    api_url = Column(
        String(500),
        nullable=True,
        comment="Custom API URL for deployments like Azure, Bedrock, Ollama. Leave empty for standard providers (the standard URL is used)."
    )

    # Authentication
    auth_type = Column(String(50), default="api_key")  # api_key, oauth, none, aws_iam, azure_ad, gcp_service_account
    api_key = Column(Text, nullable=True)  # Fernet-encrypted at rest (standard API key)

    # Cloud provider credentials (Fernet-encrypted JSON)
    # Stores provider-specific auth config:
    #   aws_iam: { aws_access_key_id, aws_secret_access_key, aws_region, aws_session_token? }
    #   azure_ad: { azure_client_id, azure_client_secret, azure_tenant_id }
    #   gcp_service_account: { gcp_project_id, gcp_location, gcp_credentials_json }
    provider_config = Column(Text, nullable=True)  # Fernet-encrypted JSON

    # =========================================================================
    # Completion parameters
    # These parameters control model behavior and can be configured per model
    # =========================================================================

    # Model Configuration - Defaults
    default_temperature = Column(Float, default=0.7, comment="Sampling temperature (0-2)")
    default_max_tokens = Column(Integer, default=4096, comment="Maximum tokens to generate")

    # System Prompt Configuration
    system_prompt = Column(
        Text,
        nullable=True,
        comment="""
        Default system prompt to prepend to all completion requests.
        This is inserted as the first message with role='system'.
        Can be overridden on a per-request basis.
        Typical size: 500-10,000 characters. Supports up to 1GB in PostgreSQL TEXT.
        """
    )

    # Core Generation Parameters
    top_p = Column(Float, nullable=True, comment="Nucleus sampling threshold (0-1). Alternative to temperature")
    n = Column(Integer, default=1, comment="Number of completions to generate per request")
    stop_sequences = Column(JSON, nullable=True, comment="List of sequences where API will stop generating (max 4)")
    seed = Column(Integer, nullable=True, comment="Seed for deterministic sampling (reproducibility)")

    # Penalties (Token Repetition Control)
    presence_penalty = Column(Float, nullable=True, comment="Penalize new tokens based on presence in text (-2.0 to 2.0)")
    frequency_penalty = Column(Float, nullable=True, comment="Penalize new tokens based on frequency in text (-2.0 to 2.0)")
    logit_bias = Column(JSON, nullable=True, comment="Modify probability of specific tokens (token_id: bias_value)")

    # Response Format & Output Control
    response_format = Column(JSON, nullable=True, comment='Force response format, e.g. {"type": "json_object"} for JSON mode')
    logprobs = Column(Boolean, default=False, comment="Return log probabilities of output tokens")
    top_logprobs = Column(Integer, nullable=True, comment="Number of most likely tokens to return with logprobs (0-5)")

    # Streaming Configuration
    stream_options = Column(JSON, nullable=True, comment='Streaming options, e.g. {"include_usage": true}')

    # Model Capabilities
    supports_streaming = Column(Boolean, default=True)
    supports_function_calling = Column(Boolean, default=False)
    supports_vision = Column(Boolean, default=False)
    supports_batch = Column(Boolean, default=False)

    # Tools/Functions Configuration
    tools = Column(JSON, nullable=True, comment="List of tools/functions the model can call")
    tool_choice = Column(String(50), nullable=True, comment="Tool choice strategy: 'none', 'auto', 'required', or specific function")
    parallel_tool_calls = Column(Boolean, default=True, comment="Enable parallel function calling (OpenAI default: true)")

    # Safety & Monitoring
    user_identifier = Column(String(255), nullable=True, comment="Unique identifier for end-user (for abuse monitoring)")

    # Provider-Specific Configuration
    api_version = Column(String(50), nullable=True, comment="API version (Azure, Bedrock, etc.)")
    extra_headers = Column(JSON, nullable=True, comment="Additional headers to send with requests")

    # Advanced configuration
    #
    # NOTE: there is no `context_window_fallback` here (that concern belongs
    # to the routing group). Model-level `fallback_models` is the
    # industry-standard model→model failover chain:
    # an ordered list of model ids to try, in order, when THIS model fails.
    fallback_models = Column(
        JSONB,
        nullable=True,
        comment="Model-level failover chain: ordered list of model ids to try, in order, "
                "when this model's own call fails with a retryable error."
    )
    model_metadata = Column(JSON, nullable=True, comment="Custom metadata for logging/tracking (sent to logging integrations)")
    drop_params = Column(Boolean, default=True, server_default=text("true"), comment="Apply the provider's dropped-parameters list (unset reads as on)")
    additional_drop_params = Column(
        JSON,
        nullable=True,
        comment="List of specific parameter names to drop for this model (e.g. ['top_p', 'presence_penalty']). "
                "Used when drop_params=False to selectively remove known unsupported parameters before sending to provider."
    )
    thinking_enabled = Column(
        Boolean,
        default=False,
        nullable=False,
        comment=(
            "Enable extended-thinking / reasoning mode by default for this model. "
            "Generic switch translated by the proxy adapter into the provider's "
            "specific parameter at request time: Anthropic adds "
            "`thinking: {type: 'enabled', budget_tokens: ...}` (and forces "
            "temperature to 1.0); OpenAI o-series sets `reasoning_effort` and "
            "strips temperature/top_p; Google Gemini 2.5 Thinking adds "
            "`thinking_config: {thinking_budget: ...}`. Only meaningful for "
            "models that actually support reasoning — flipping it on for a "
            "non-reasoning model is a no-op at the adapter level."
        ),
        server_default=text("false"),
    )

    # Routing configuration
    # These are used by the router for load balancing
    routing_weight = Column(Integer, default=1, comment="Weight for load balancing (higher = more traffic)")
    rate_limit_rpm = Column(Integer, nullable=True, comment="Requests per minute limit")
    rate_limit_tpm = Column(Integer, nullable=True, comment="Tokens per minute limit")

    routing_group_id = Column(
        String(100),
        ForeignKey("llm_routing_group.id", ondelete="SET NULL"),
        nullable=True,
        comment="""
        Routing group ID for load balancing.
        If set, this model is part of a routing group and will be load balanced with other models in the group.
        The routing group ID becomes the model_name used in API calls.
        Example: If routing_group_id='my-gpt-4', call router.completion(model='my-gpt-4')
        If None, model is standalone and used independently.
        """
    )

    # Cost Management (for cost-based routing)
    cost_per_input_token = Column(Float, nullable=True, comment="USD per 1M input tokens")
    cost_per_output_token = Column(Float, nullable=True, comment="USD per 1M output tokens")

    # =========================================================================
    # Image generation parameters (mode='image_generation')
    #
    # Promoted from `capabilities` JSONB to top-level columns so the proxy
    # can validate size/quality at request time without parsing JSON, and
    # so per-image cost rolls up cleanly in spend dashboards (separate
    # from cost_per_input_token / cost_per_output_token).
    # =========================================================================
    cost_per_image = Column(
        Float,
        nullable=True,
        comment=(
            "USD cost per generated image. Used for image-mode models. "
            "Distinct from cost_per_input_token / cost_per_output_token "
            "so per-image and per-token spend roll up cleanly. "
            "Stored as DOUBLE PRECISION (matches the rest of the cost "
            "columns); 8 bytes is plenty for sub-cent precision USD."
        ),
    )
    # Generic JSON in the SQLAlchemy model (portable across SQLite test
    # fixtures and Postgres prod). The migration physically creates the
    # columns as JSONB in Postgres — generic JSON is just the read/write
    # contract the ORM uses; the underlying column type is whatever the
    # database engine has. This pattern matches `tags`, `capabilities`,
    # and the other JSON columns in this file.
    supported_image_sizes = Column(
        JSONB,
        nullable=True,
        comment=(
            "Whitelist of accepted image sizes for image-mode models, "
            'e.g. ["1024x1024","1024x1792","1792x1024"]. Enforced at the '
            "proxy edge."
        ),
    )
    supported_image_qualities = Column(
        JSONB,
        nullable=True,
        comment=(
            "Whitelist of accepted quality tiers, e.g. "
            '["standard","hd"]. Enforced at the proxy edge.'
        ),
    )
    max_images_per_request = Column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment=(
            "Maximum number of images permitted per single request "
            "(`n` parameter). DALL-E 3 = 1, DALL-E 2 = 10."
        ),
    )

    # =========================================================================
    # Audio TTS parameters (mode='audio_speech')
    # =========================================================================
    cost_per_audio_char = Column(
        Float,
        nullable=True,
        comment="USD per generated character (TTS).",
    )
    supported_voices = Column(
        JSON,
        nullable=True,
        comment=(
            'Whitelist of accepted voice ids, e.g. ["alloy","echo",'
            '"fable","onyx","nova","shimmer"].'
        ),
    )
    supported_audio_formats = Column(
        JSON,
        nullable=True,
        comment=(
            'Whitelist of accepted output formats, e.g. '
            '["mp3","opus","aac","flac","wav","pcm"].'
        ),
    )

    # =========================================================================
    # Audio STT parameters (mode='audio_transcription')
    # =========================================================================
    cost_per_audio_minute = Column(
        Float,
        nullable=True,
        comment="USD per minute of input audio (STT). whisper-1 ~ 0.006.",
    )
    max_audio_duration_seconds = Column(
        Integer,
        nullable=True,
        comment=(
            "Maximum input clip duration the proxy will accept. "
            "Mirrors provider caps."
        ),
    )
    supported_input_formats = Column(
        JSON,
        nullable=True,
        comment=(
            'Whitelist of accepted input formats, e.g. '
            '["mp3","mp4","wav","m4a","webm"].'
        ),
    )
    supported_response_formats = Column(
        JSONB,
        nullable=True,
        comment=(
            'Response formats this model accepts upstream '
            '(STT today; pattern extends to image/video as needed). '
            'whisper-1 supports verbose_json (carries `duration` for '
            'cost calc) but gpt-4o-transcribe-family models reject it '
            'with a 400. NULL = whitelist not declared, proxy falls '
            'back to a model-name heuristic for one release cycle.'
        ),
    )

    # =========================================================================
    # Video generation parameters (mode='video_generation')
    # =========================================================================
    cost_per_video_second = Column(
        Float,
        nullable=True,
        comment="USD per second of generated video. Luma Dream Machine ~ 0.07.",
    )
    supported_video_resolutions = Column(
        JSON,
        nullable=True,
        comment='Whitelist of accepted resolutions, e.g. ["720p","1080p"].',
    )
    supported_video_durations = Column(
        JSON,
        nullable=True,
        comment="Whitelist of accepted clip durations in seconds, e.g. [5, 10].",
    )
    max_video_duration_seconds = Column(
        Integer,
        nullable=True,
        comment="Hard ceiling on a single clip in seconds.",
    )

    # =========================================================================
    # RESILIENCE — per-model circuit breaker + timeout (KEPT)
    # =========================================================================
    #
    # `allowed_fails` + `cooldown_time` configure the PER-MODEL CIRCUIT
    # BREAKER in the Rust proxy (`services/llm_resilience.rs::gate_dispatch`,
    # keyed by model id). The breaker gates EVERY dispatch — direct
    # (non-routed) AND routed. After `allowed_fails` consecutive failures
    # the breaker OPENS and subsequent calls fast-fail with a graceful
    # error carrying a `Retry-After` header for `cooldown_time` seconds;
    # it then HALF-OPENS to retry a single probe and closes on success.
    # This means an unreachable model never hangs the caller and recovers
    # automatically, even when the model is NOT a member of a routing
    # group (so a standalone model still gets protection).
    #
    # `timeout` bounds each individual upstream attempt (default 120s).
    # The proxy additionally applies a short `connect_timeout`
    # so an unreachable host fails in seconds rather than hanging until
    # the full request timeout elapses.
    #
    # WHY retry/fallback are NOT on the model:
    #   * RETRY and FALLBACK are now ROUTING-GROUP concerns. The model-level
    #     `retry_attempts` / `retry_after` columns were dead (the proxy
    #     read only the routing-group / HTTP `Retry-After` values), and
    #     `fallback_models` / `context_window_fallback` referenced other
    #     instance ids and duplicated the router's job — use a routing
    #     group's fallback chain instead.
    #   * `cooldown_window` was removed: the breaker is driven by
    #     `allowed_fails` + `cooldown_time`, never a model-level failure
    #     window.

    # Connection Settings
    timeout = Column(
        Float,
        default=60.0,
        comment="Request timeout (seconds) bounding each upstream attempt (proxy default 120s).",
    )
    max_concurrent_requests = Column(
        Integer,
        nullable=True,
        comment=(
            "Max simultaneous in-flight requests to this model. "
            "Enforced by a per-model semaphore in the Rust proxy. "
            "NULL = unbounded (honour only rate-limit rpm/tpm)."
        ),
    )

    # Circuit Breaker / Cooldown (per-model, gates every dispatch — direct AND routed)
    allowed_fails = Column(
        Integer,
        nullable=True,
        comment="Circuit-breaker failure threshold: open the breaker after this many consecutive failures.",
    )
    cooldown_time = Column(
        Integer,
        nullable=True,
        comment="Circuit-breaker cooldown (seconds): how long the breaker stays open / Retry-After before half-open retry.",
    )

    # Status
    enabled = Column(Boolean, default=True, comment="Whether this model is enabled")

    # =========================================================================
    # TENANT-WIDE GOVERNANCE (per-resource defaults, min-wins with RG)
    # =========================================================================
    # Structured limit/policy blobs that apply TENANT-WIDE as this model's
    # default posture. Same shapes the Resource Group forms use
    # (llm_global_limits / llm_global_governance). At enforcement the Rust
    # proxy folds these into the effective (RG, model) limits / param-caps
    # with min-wins (the stricter value wins). Set via Resources → AI Models →
    # model edit → Limits / Policies tabs.
    tenant_limits = Column(
        JSON(none_as_null=True), nullable=True,
        comment="Tenant-wide default LLM usage limits (throughput/concurrency/budget/tokens/requests); min-wins with RG.",
    )
    tenant_governance = Column(
        JSON(none_as_null=True), nullable=True,
        comment="Tenant-wide default LLM governance/policy (param caps, residency, guardrails); min-wins with RG.",
    )

    # =========================================================================
    # RETIREMENT METADATA (provider-announced end-of-life)
    # =========================================================================
    # Providers retire models on a published schedule; without these
    # columns a retired model's health check flips red overnight
    # without any operator warning. These columns let the seed
    # carry an authoritative retire_date forward, and the
    # `llm_model_retirement_warner` service emits a `system_alerts`
    # row starting at `retirement_reminder_date` so the operator has
    # time to wire a replacement before users hit the wall.
    #
    # All nullable — most models aren't on a retirement schedule.
    # Date (not DateTime) because providers announce calendar
    # dates; intra-day precision is noise.
    retire_date = Column(
        Date,
        nullable=True,
        comment=(
            "Provider's announced retirement date. After this date "
            "the model is expected to stop responding. Used by the "
            "retirement alert generator to surface operator warnings."
        ),
    )
    retirement_announced_at = Column(
        Date,
        nullable=True,
        comment=(
            "Date the provider announced the retirement. Informational "
            "— surfaced in the admin UI as 'announced N days ago' "
            "context, useful when judging whether downstream users "
            "have had reasonable notice."
        ),
    )
    retirement_reminder_date = Column(
        Date,
        nullable=True,
        comment=(
            "Date from which the operator alert should start firing. "
            "Defaults to retire_date - 14 days at write time but is "
            "stored explicitly so operators can lengthen the lead "
            "time for business-critical models."
        ),
    )
    retirement_replacement_model_name = Column(
        String(255),
        nullable=True,
        comment=(
            "Provider-side model name of the recommended successor. "
            "Free-form string rather than an FK so cross-provider "
            "replacements work (e.g. DALL-E 3 → FLUX 1.1 Pro on "
            "Replicate). Admin UI autocompletes against existing "
            "llm_model rows but allows free entry."
        ),
    )
    retirement_replacement_provider_key = Column(
        String(100),
        nullable=True,
        comment=(
            "Provider key of the replacement (`openai`, `anthropic`, "
            "`fal_ai`, ...). Null when the replacement is on the same "
            "provider as the retiring model. Drives the cross-link "
            "from the retirement banner to the replacement model "
            "row in the admin UI."
        ),
    )
    retirement_notes = Column(
        Text,
        nullable=True,
        comment=(
            "Free-form markdown. Recommended content: link to the "
            "provider's announcement, breaking-change summary, any "
            "migration notes operators should know about (different "
            "pricing tier, different output format, etc)."
        ),
    )

    # Model Capabilities (JSON for flexibility)
    capabilities = Column(JSON, nullable=True, comment="Additional capabilities and metadata")

    # Catalog provenance — back-ref to the global llm_catalog entry this
    # instance was imported from (NULL for hand-rolled models). Lets the
    # asset registry resolve the catalog icon and tells us an instance's
    # origin. SET NULL on catalog delete so an instance survives a catalog
    # cleanup.
    catalog_id = Column(
        String(255),
        ForeignKey("llm_catalog.id", ondelete="SET NULL"),
        nullable=True,
        comment="Origin llm_catalog.id this model was imported from (provenance).",
    )

    # Provenance (Traffic Data Import). 'manual' (default) |
    # 'import' (model discovered from a vendor usage-import). Discovered models
    # are created enabled=False (observability-only) until an operator promotes.
    source = Column(
        String(16),
        nullable=False,
        server_default="manual",
        default="manual",
        comment="manual | import",
    )
    source_external_id = Column(
        String(255),
        nullable=True,
        comment="Vendor model id this row was discovered under (import)",
    )

    # =========================================================================
    # GROUP-BASED ACCESS CONTROL
    # =========================================================================

    # If groups are empty, model is available to ALL users
    # If groups are specified, model is only available to users in those groups

    # Relationships
    #
    # NOTE: kept as default `lazy="select"` (a real list, not an
    # AppenderQuery). The previous `lazy="dynamic"` produced an N+1
    # storm in the admin list endpoint: `to_response()` accesses
    # `model.resource_groups` three times per model (is_global check,
    # group_ids, optional groups detail), and dynamic relationships
    # re-query on every iteration. With ~300 seeded models that
    # ballooned the GET /v1/admin/llm-models call to ~3s. Code that
    # needs an AppenderQuery interface (filter/count/.all() on the
    # relationship) does not exist in the codebase as of this commit
    # — verified by grep before flipping. If a future caller wants
    # query-style access, fetch the link rows directly rather than
    # re-introducing dynamic loading.
    resource_groups: Mapped[List["ResourceGroup"]] = relationship(
        "ResourceGroup",
        secondary="resource_group_llm_models",
        back_populates="llm_models",
    )

    # Routing Group Relationship
    routing_group = relationship(
        "LLMRoutingGroup",
        back_populates="models",
        foreign_keys=[routing_group_id],
        lazy="selectin"
    )

    # Provider Relationship — replaces the legacy free-form
    # `provider` string column with a FK into `llm_providers`.
    # `lazy="selectin"` matches `routing_group` so a `to_dict()`
    # call doesn't trigger N+1 queries when serialising lists.
    provider = relationship(
        "LlmProvider",
        back_populates="models",
        foreign_keys=[provider_id],
        lazy="selectin",
    )

    # =========================================================================
    # HEALTH STATUS & PERFORMANCE METRICS
    # =========================================================================

    # Health Status. Recognised values:
    #   - healthy        : last probe passed
    #   - unhealthy      : last probe failed (real health failure)
    #   - degraded       : partial failure / slow path
    #   - requires_tier  : last probe rejected because the configured
    #                       API key isn't eligible for this model's
    #                       tier (Codex preview, Pro, o1-pro, …).
    #                       Distinct from "unhealthy" so the UI can
    #                       render "Requires <Tier>" as a separate
    #                       badge and the runtime router can skip the
    #                       model without colouring dashboards red.
    #   - unknown        : never probed (default)
    health_status = Column(String(50), default="unknown")
    last_health_check = Column(DateTime(timezone=True), nullable=True)
    last_health_success = Column(DateTime(timezone=True), nullable=True)
    consecutive_health_failures = Column(Integer, default=0)
    health_check_error = Column(Text, nullable=True)
    # Last probe taxonomy — drives the admin UI's secondary badge:
    #   - "catalog"   : provider /models endpoint hit, model_name verified
    #   - "endpoint"  : real (cheap) inference call to the mode endpoint
    #   - "synthetic" : Brutor-side validation only (no upstream call)
    # See `app/services/llm/model/llm_models_manager.py` for the full
    # taxonomy. Nullable for back-compat with rows seeded before this
    # column existed; the API response surfaces NULL as None and the
    # UI falls back to no badge.
    last_probe_type = Column(String(20), nullable=True)

    # Performance Metrics
    total_requests = Column(Integer, default=0, comment="Total requests made to this model")
    failed_requests = Column(Integer, default=0, comment="Number of failed requests")
    total_tokens_consumed = Column(Integer, default=0, comment="Total tokens consumed")
    avg_latency_ms = Column(Float, nullable=True, comment="Average latency in milliseconds")

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # =========================================================================
    # INDEXES & CONSTRAINTS
    # =========================================================================

    __table_args__ = (
        # Multi-tenant queries
        Index('ix_llm_model_tenant_enabled', 'tenant_id', 'enabled'),
        Index('ix_llm_model_tenant_name', 'tenant_id', 'name'),

        # Provider and model lookups
        Index('ix_llm_model_provider_id', 'tenant_id', 'provider_id', 'enabled'),
        Index('ix_llm_model_model_name', 'tenant_id', 'model_name', 'enabled'),

        # Health monitoring
        Index('ix_llm_model_health_status', 'tenant_id', 'health_status', 'enabled'),
        Index('ix_llm_model_health_check', 'tenant_id', 'last_health_check'),

        # Performance queries
        Index('ix_llm_model_latency', 'tenant_id', 'avg_latency_ms', 'enabled'),

        # Filtering indexes
        Index('ix_llm_model_parameters', 'tenant_id', 'parameters', 'enabled'),

        # Audit queries
        Index('ix_llm_model_created_at', 'tenant_id', 'created_at'),
    )

    def get_decrypted_api_key(self, encryption_service) -> Optional[str]:
        """Decrypt the api_key using the provided EncryptionService.
        Returns the decrypted key, or None if no key is set."""
        if not self.api_key:
            return None
        try:
            return encryption_service.decrypt(self.api_key)
        except Exception:
            return self.api_key  # Return as-is if decryption fails

    @validates('api_url', 'api_url_auto_discovered')
    def validate_api_url(self, key, value):
        """
        Validation for api_url and auto-discovery:
        - If api_url_auto_discovered is False, api_url must be provided
        - Strip trailing slashes from api_url
        """
        if key == 'api_url':
            # Strip trailing slashes
            if value and value.endswith('/'):
                return value.rstrip('/')
            return value

        if key == 'api_url_auto_discovered':
            # Note: We don't validate api_url here because during __init__,
            # attributes are set in order and api_url might not be set yet.
            # Validation is handled at the application level.
            pass

        return value

    @validates('mode')
    def validate_mode(self, key, value):
        """Validate that mode is one of the allowed LLModelMode values."""
        valid_modes = [m.value for m in LLModelMode]
        if value not in valid_modes:
            raise ValueError(f"mode must be one of {valid_modes}, got: {value}")
        return value

    # =========================================================================
    # Group Access Methods
    # =========================================================================

    def get_group_ids(self) -> List[str]:
        """Get list of group IDs this model is assigned to"""
        return [group.id for group in self.resource_groups]  # ← Use resource_groups

    def is_accessible_to_user_groups(self, user_group_ids: List[str]) -> bool:
        """Check if model is accessible to given user groups"""
        if not user_group_ids:
            return False

        model_groups = self.resource_groups  # ← Use resource_groups
        if not model_groups:
            return True  # Global model

        model_group_ids = {g.id for g in model_groups}
        return bool(set(user_group_ids) & model_group_ids)

    # =========================================================================
    # ROUTING GROUP METHODS
    # =========================================================================

    def is_in_routing_group(self) -> bool:
        """Check if this model is part of a routing group"""
        return self.routing_group_id is not None

    def get_routing_group_name(self) -> Optional[str]:
        """Get the routing group name/ID that should be used in API calls"""
        return self.routing_group_id

    # =========================================================================
    # HEALTH STATUS METHODS
    # =========================================================================

    def update_health_status(
        self,
        status: str,
        response_time_ms: Optional[float] = None,
        error_message: Optional[str] = None,
        probe_type: Optional[str] = None,
    ) -> None:
        """Update model health status.

        ``probe_type`` records which kind of health check produced
        this status update — drives the admin UI's secondary badge.
        Pass `None` to leave the previous value intact (safe for
        any caller that doesn't yet know the new taxonomy).
        """
        self.health_status = status
        self.last_health_check = datetime.now(timezone.utc)
        if probe_type:
            self.last_probe_type = probe_type

        if status == "healthy":
            self.last_health_success = datetime.now(timezone.utc)
            self.consecutive_health_failures = 0
            self.health_check_error = None

            if response_time_ms:
                # Update rolling average latency
                if self.avg_latency_ms is None:
                    self.avg_latency_ms = response_time_ms
                else:
                    self.avg_latency_ms = (self.avg_latency_ms * 0.9) + (response_time_ms * 0.1)
        else:
            self.consecutive_health_failures += 1
            self.health_check_error = error_message

        self.updated_at = datetime.now(timezone.utc)

    def get_health_status_dict(self) -> Dict[str, Any]:
        """Get health status as dictionary"""
        return {
            "status": self.health_status or "unknown",
            "last_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "last_success": self.last_health_success.isoformat() if self.last_health_success else None,
            "consecutive_failures": self.consecutive_health_failures or 0,
            "error_message": self.health_check_error,
            "avg_latency_ms": self.avg_latency_ms,
            # `probe_type` surfaces in the admin UI as the
            # "Catalog Probe" / "Endpoint Probe" / "Synthetic
            # Probe" badge alongside the Healthy/Unhealthy state.
            "probe_type": self.last_probe_type,
        }

    # =========================================================================
    # PERFORMANCE TRACKING METHODS
    # =========================================================================

    def record_request(
        self,
        success: bool,
        tokens_consumed: int = 0,
        latency_ms: Optional[float] = None
    ) -> None:
        """Record request metrics"""
        self.total_requests += 1
        if not success:
            self.failed_requests += 1

        if tokens_consumed > 0:
            self.total_tokens_consumed += tokens_consumed

        if latency_ms and success:
            if self.avg_latency_ms is None:
                self.avg_latency_ms = latency_ms
            else:
                # Exponential moving average
                self.avg_latency_ms = (self.avg_latency_ms * 0.9) + (latency_ms * 0.1)

        self.updated_at = datetime.now(timezone.utc)

    def get_performance_metrics_dict(self) -> Dict[str, Any]:
        """Get performance metrics as dictionary"""
        success_rate = None
        if self.total_requests and self.total_requests > 0:
            success_rate = ((self.total_requests - self.failed_requests) / self.total_requests) * 100

        return {
            "total_requests": self.total_requests or 0,
            "failed_requests": self.failed_requests or 0,
            "success_rate": round(success_rate, 2) if success_rate is not None else 0.0,
            "avg_latency_ms": round(self.avg_latency_ms, 2) if self.avg_latency_ms else None,
            "total_tokens_consumed": self.total_tokens_consumed or 0
        }

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    @property
    def is_chat(self):
        return self.mode == LLModelMode.CHAT.value

    @property
    def is_completion(self):
        return self.mode == LLModelMode.COMPLETION.value

    @property
    def is_embedding(self):
        return self.mode == LLModelMode.EMBEDDING.value

    @property
    def is_routing(self):
        return self.mode == LLModelMode.ROUTING.value

    @property
    def is_responses(self):
        return self.mode == LLModelMode.RESPONSES.value

    @property
    def supports_responses_api(self) -> bool:
        """Check if model can be used with the Responses API.
        True for responses-mode models AND chat models with supports_responses capability."""
        if self.is_responses:
            return True
        if self.is_chat and self.capabilities:
            return self.capabilities.get("supports_responses", False)
        return False

    @property
    def supports_chat_api(self) -> bool:
        """Check if model can be used with the Chat Completions API.
        True for chat-mode models AND responses-mode models (via the responses bridge)."""
        return self.is_chat or self.is_responses

    @property
    def has_provider_config(self) -> bool:
        """Whether cloud provider credentials are configured."""
        return bool(self.provider_config)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            # AI Resource identity block — version is operator-authored,
            # authorship is system-stamped. Emitted here because Pydantic
            # strips anything to_dict() does not produce.
            "version": self.version,
            **self.authorship_dict(),
            # Provider — both the FK (`provider_id`) and a denormalised
            # `provider` slug (read from the joined LlmProvider row's
            # `key` field) are surfaced. The slug is what every legacy
            # caller of `to_dict()` already expected to see; the
            # `provider_id` is what the admin UI dropdown writes back
            # on edit. Nested `provider_info` carries the rest of the
            # provider row (display_name, doc URLs, etc.) for the
            # admin UI to render without a second roundtrip.
            "provider_id": self.provider_id,
            "provider": self.provider.key if self.provider else None,
            "provider_info": self.provider.to_dict() if self.provider else None,
            "catalog_id": self.catalog_id,
            "source": self.source,
            "source_external_id": self.source_external_id,
            "model_name": self.model_name,
            "mode": self.mode,
            "routing_group_id": self.routing_group_id,
            "routing_group_name": self.routing_group.name if self.routing_group else None,
            "fallback_models": self.fallback_models,
            "enabled": self.enabled,
            "tenant_limits": self.tenant_limits,
            "tenant_governance": self.tenant_governance,
            "supports_streaming": self.supports_streaming,
            "supports_function_calling": self.supports_function_calling,
            "supports_vision": self.supports_vision,
            "supports_batch": self.supports_batch,
            "cost_per_image": self.cost_per_image,
            "supported_image_sizes": self.supported_image_sizes,
            "supported_image_qualities": self.supported_image_qualities,
            "max_images_per_request": self.max_images_per_request,
            "cost_per_audio_char": self.cost_per_audio_char,
            "supported_voices": self.supported_voices,
            "supported_audio_formats": self.supported_audio_formats,
            "cost_per_audio_minute": self.cost_per_audio_minute,
            "max_audio_duration_seconds": self.max_audio_duration_seconds,
            "supported_input_formats": self.supported_input_formats,
            "supported_response_formats": self.supported_response_formats,
            "cost_per_video_second": self.cost_per_video_second,
            "supported_video_resolutions": self.supported_video_resolutions,
            "supported_video_durations": self.supported_video_durations,
            "max_video_duration_seconds": self.max_video_duration_seconds,
            "health_status": self.get_health_status_dict(),
            "performance_metrics": self.get_performance_metrics_dict(),
            # Retirement metadata — date columns serialise to ISO
            # strings (YYYY-MM-DD) for frontend ease. None when no
            # retirement is scheduled.
            "retire_date": self.retire_date.isoformat() if self.retire_date else None,
            "retirement_announced_at": (
                self.retirement_announced_at.isoformat()
                if self.retirement_announced_at else None
            ),
            "retirement_reminder_date": (
                self.retirement_reminder_date.isoformat()
                if self.retirement_reminder_date else None
            ),
            "retirement_replacement_model_name": self.retirement_replacement_model_name,
            "retirement_replacement_provider_key": self.retirement_replacement_provider_key,
            "retirement_notes": self.retirement_notes,
            "groups": [
                {"id": g.id,
                 'group_type': g.group_type,
                 "name": g.name,
                 'is_active': g.is_active,
                 'parent_group_id':
                     g.parent_group_id} for g in self.resource_groups] if self.resource_groups else [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        routing_info = f", routing_group={self.routing_group_id}" if self.routing_group_id else ""
        virtual_info = " [VIRTUAL]" if self.is_routing else ""
        provider_slug = self.provider.key if self.provider else self.provider_id
        return f"<LLModel(id={self.id}, name={self.name}, provider={provider_slug}, enabled={self.enabled}{routing_info}{virtual_info})>"