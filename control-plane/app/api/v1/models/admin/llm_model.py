# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Model Pydantic Models for API Requests/Responses
With group-based access control and comprehensive completion parameters
"""

from datetime import date
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from app.constants.llm import LLModelMode, LLModelProvider
from app.api.v1.models.admin.resource_fields import ResourceVersionField
from app.api.v1.models.admin.resource_fields import ResourceMetaFields
from app.services import release_scope
from app.database.models.resource_group import llm_governance_value_problem


# =============================================================================
# Shared Validators (used by both Create and Update)
# =============================================================================

# Source of truth: ``LLModelProvider`` enum in ``app/constants/llm.py``.
# Deriving the validator's allowlist from the enum stops the two from
# drifting when a new provider is added — historically this list was
# maintained by hand and silently rejected newly-seeded providers
# (xAI, Moonshot, MiniMax, Sambanova, etc.) as "unknown", forcing them
# to fall back to ``provider=custom`` in the admin UI.
#
# The ``routing_group`` and ``custom`` synthetic providers are not part
# of the enum (they're admin-UI constructs / fallback bucket); they
# stay in the list explicitly.
ALLOWED_PROVIDERS = sorted(
    {p.value for p in LLModelProvider} | {"routing_group", "custom"}
)

ALLOWED_AUTH_TYPES = ['api_key', 'oauth', 'none', 'aws_iam', 'azure_ad', 'gcp_service_account']


def _validate_mode(v: Optional[str]) -> Optional[str]:
    """Validate mode is a valid LLModelMode value."""
    if v is None:
        return v
    valid_modes = [m.value for m in LLModelMode]
    if v.lower() not in valid_modes:
        raise ValueError(f"Mode must be one of: {', '.join(valid_modes)}")
    return v.lower()


def _validate_provider(v: Optional[str]) -> Optional[str]:
    """Light-touch normalisation of the legacy provider slug.

    Providers are a first-class entity, so the
    canonical "is this a real provider?" check happens at the
    endpoint by resolving the slug against the `llm_providers`
    table. We keep the validator only to lowercase the slug —
    that way the (tenant_id, lower(provider)) → provider_id
    resolution doesn't have to handle case variants from older
    callers.

    `ALLOWED_PROVIDERS` is still exported for back-compat with
    callers (admin UI dropdown population, tests) but is no longer
    a hard schema constraint.
    """
    if v is None:
        return v
    return v.lower()


def _validate_auth_type(v: Optional[str]) -> Optional[str]:
    """Validate auth_type is in the allowed list."""
    if v is None:
        return v
    if v.lower() not in ALLOWED_AUTH_TYPES:
        raise ValueError(f"Auth type must be one of: {', '.join(ALLOWED_AUTH_TYPES)}")
    return v.lower()


def _validate_system_prompt(v: Optional[str]) -> Optional[str]:
    """Validate and clean system prompt."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if len(v) > 100000:
        raise ValueError(
            f"System prompt too long ({len(v):,} characters). "
            f"Maximum is 100,000 characters."
        )
    return v


# =============================================================================
# Group Information (Nested in responses)
# =============================================================================


DROP_PARAMS_DESCRIPTION = (
    "Apply the provider's dropped-parameters list: the parameters named on the provider "
    "record's list are removed from a request before it is sent. On by default (the gateway "
    "reads an unset value as on); false keeps them. It removes nothing else: a parameter the "
    "provider does not take and no list names is sent. The model's own "
    "`additional_drop_params` are removed either way."
)
RETIRE_DATE_DESCRIPTION = (
    "The date the model retires (YYYY-MM-DD, UTC). From that date on the gateway does not "
    "serve the model: a call that names it is answered as a call to a disabled model is, and "
    "it is never a routing-group member or a fallback. A model that is retired, or whose "
    "provider is disabled, cannot be saved as a member of a routing group or in a fallback list."
)
_FALLBACK_NOT_COUNTED = (
    " A call that reaches this model as another model's fallback is not counted against "
    "this limit in this release."
)
RATE_LIMIT_RPM_DESCRIPTION = (
    "Requests per minute the model takes, for the whole tenant. Applied by the gateway to a "
    "direct call and to a call routed to the model as a routing-group member." + _FALLBACK_NOT_COUNTED
)
RATE_LIMIT_TPM_DESCRIPTION = (
    "Tokens per minute the model takes, for the whole tenant. Applied by the gateway to a "
    "direct call and to a call routed to the model as a routing-group member." + _FALLBACK_NOT_COUNTED
)

class LLMModelGroupInfo(BaseModel):
    """Nested group information in LLM model responses"""
    id: str
    name: str
    display_name: Optional[str] = None
    group_type: Optional[str] = None
    is_active: bool
    parent_group_id: Optional[str] = None

    class Config:
        from_attributes = True


# =============================================================================
# Request Models
# =============================================================================

# The model's circuit breaker. The gateway configures it from the pair when
# both are set and uses its own defaults otherwise, so one without the other
# would be stored and never used.
ALLOWED_FAILS_MAX = 100
COOLDOWN_TIME_MAX_SECONDS = 3600


def circuit_breaker_pair_problem(allowed_fails: Optional[int], cooldown_time: Optional[int]) -> Optional[str]:
    """Why this pair cannot be saved, or None."""
    if (allowed_fails is None) != (cooldown_time is None):
        return (
            "allowed_fails and cooldown_time configure the model's circuit breaker together: "
            "set both, or neither (the gateway then uses its defaults)."
        )
    return None



def _tenant_limits_in_this_release(value):
    problem = release_scope.llm_limits_problem(value)
    if problem:
        raise ValueError(problem)
    return value


def _tenant_governance_in_this_release(value):
    # An enforce mode or tool access the gateway does not know is read as
    # the most restrictive one (`usage::collect_effective_governance`
    # folds this document into the group's LLM governance): refused here.
    problem = llm_governance_value_problem(value) or release_scope.llm_governance_problem(value)
    if problem:
        raise ValueError(problem)
    return value


class LLModelCreate(ResourceVersionField):
    """
    Request model for creating LLM model
    Matches database model: app.database.models.llm_model.LLModel
    """
    # Basic Information
    name: str = Field(..., min_length=1, max_length=200, description="Display name")
    description: Optional[str] = Field(None, max_length=1000, description="Model description")
    usage: Optional[str] = Field(None, max_length=1000, description="Usage description and use cases")
    tags: Optional[List[str]] = Field(None, description="Tags for filtering (cost, speed, capabilities, use cases)")

    # Provider Information
    # Providers are first-class entities; the form
    # accepts EITHER `provider_id` (preferred — the admin UI
    # dropdown emits this) OR `provider` (legacy slug — the API
    # resolves it to a provider_id by `(tenant_id, key)` lookup).
    # At least one MUST be supplied. The endpoint validates this.
    provider_id: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "Foreign key into llm_providers.id. Preferred over the "
            "legacy `provider` slug — the admin UI provider dropdown "
            "emits this directly."
        ),
    )
    provider: Optional[str] = Field(
        None,
        max_length=50,
        description=(
            "Legacy provider slug ('openai', 'anthropic', etc.). "
            "Kept as a back-compat seam for older clients (and the "
            "seed pipeline) — the admin endpoint resolves the slug "
            "to a `provider_id` via "
            "`(tenant_id, lower(provider) = key)`. Either this or "
            "`provider_id` must be supplied."
        ),
    )
    model_name: str = Field(..., max_length=200, description="Model name (gpt-5.2, claude-sonnet-5-5, etc.)")
    mode: str = Field(..., max_length=20, description="(chat, completion, embedding, routing, responses)")

    # Connection Details - Auto-Discovery Support
    api_url_auto_discovered: bool = Field(
        default=True,
        description="If True, the provider's standard API URL is used. If False, api_url must be provided."
    )
    api_url: Optional[str] = Field(
        None,
        max_length=500,
        description="Custom API URL (Azure, Bedrock, Ollama). Leave empty for standard providers."
    )

    # Authentication
    auth_type: str = Field(default="api_key", max_length=50, description="Authentication type")
    api_key: Optional[str] = Field(None, max_length=500, description="API key (encrypted)")
    provider_config: Optional[Dict[str, Any]] = Field(None, description="Cloud provider credentials (encrypted JSON)")

    # Model Configuration - Defaults
    default_temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="Sampling temperature (0-2)")
    # `ge=0` (not `ge=1`) — image / audio / video / embedding modes
    # don't generate tokens, so admins legitimately leave this at 0.
    # Token-mode models still see the per-provider default applied
    # downstream via `default_max_tokens or 4096` at adapter time.
    default_max_tokens: int = Field(default=4096, ge=0, description="Maximum tokens to generate (0 for non-text modes)")

    # System Prompt Configuration
    system_prompt: Optional[str] = Field(
        None,
        max_length=100000,
        description="Default system prompt to prepend to all completion requests. "
                    "This is inserted as the first message with role='system'. "
                    "Can be overridden on a per-request basis. "
                    "Typical size: 500-10,000 characters. Max: 100,000 chars."
    )

    # =========================================================================
    # Completion parameters
    # =========================================================================

    # Core Generation Parameters
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0, description="Nucleus sampling threshold")
    n: Optional[int] = Field(None, ge=1, le=10, description="Number of completions to generate")
    stop_sequences: Optional[List[str]] = Field(None, max_length=4, description="Stop sequences (max 4)")
    seed: Optional[int] = Field(None, description="Seed for deterministic sampling")

    # Penalties
    presence_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0, description="Presence penalty (-2 to 2)")
    frequency_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0, description="Frequency penalty (-2 to 2)")
    logit_bias: Optional[Dict[str, float]] = Field(None, description="Token ID to bias mapping")

    # Response Format & Output Control
    response_format: Optional[Dict[str, Any]] = Field(None, description='e.g. {"type": "json_object"}')
    logprobs: Optional[bool] = Field(None, description="Return log probabilities")
    top_logprobs: Optional[int] = Field(None, ge=0, le=5, description="Number of top logprobs (0-5)")

    # Streaming Configuration
    stream_options: Optional[Dict[str, Any]] = Field(None, description='e.g. {"include_usage": true}')

    # Capabilities
    supports_streaming: bool = Field(default=True, description="Supports streaming")
    supports_function_calling: bool = Field(default=False, description="Supports function calling")
    supports_vision: bool = Field(default=False, description="Supports vision/images")
    supports_batch: bool = Field(default=False, description="Supports batch processing")

    # Tools/Functions Configuration
    tools: Optional[List[Dict[str, Any]]] = Field(None, description="Predefined tools/functions")
    tool_choice: Optional[str] = Field(None, max_length=50, description="'none', 'auto', 'required', or function name")
    parallel_tool_calls: Optional[bool] = Field(None, description="Enable parallel function calling")

    # Safety & Monitoring
    user_identifier: Optional[str] = Field(None, max_length=255, description="End-user identifier for tracking")

    # Provider-Specific Configuration
    api_version: Optional[str] = Field(None, max_length=50, description="API version (Azure, Bedrock, etc.)")
    extra_headers: Optional[Dict[str, str]] = Field(None, description="Additional HTTP headers")

    # Advanced configuration
    # NOTE: retry/fallback are routing-group concerns — a model has no
    # fallback_models / context_window_fallback.
    model_metadata: Optional[Dict[str, Any]] = Field(None, description="Custom metadata for logging")
    drop_params: Optional[bool] = Field(default=True, description=DROP_PARAMS_DESCRIPTION)
    additional_drop_params: Optional[List[str]] = Field(
        None,
        description="List of specific parameter names to drop for this model (e.g. ['top_p', 'presence_penalty']). "
                    "Removed from every request to the model, whatever drop_params says."
    )
    thinking_enabled: Optional[bool] = Field(
        default=False,
        description=(
            "Enable extended-thinking / reasoning mode. The proxy adapter "
            "translates this into the provider's specific parameter — "
            "Anthropic `thinking: {type: enabled}` (with temperature forced "
            "to 1.0), OpenAI o-series `reasoning_effort` (with temperature "
            "stripped), Gemini 2.5 `thinking_config`. Only meaningful for "
            "reasoning-capable models; a no-op on plain chat models."
        ),
    )

    # Model Size
    parameters: Optional[float] = Field(
        None, ge=0,
        description="Model size in billions of parameters (e.g. 8.0, 70.0, 405.0)"
    )

    # Routing configuration
    routing_group_id: Optional[str] = Field(None, max_length=100, description="Routing group for load balancing")
    # ge=0 (not ge=1) so operators can set the primary-with-fallback pattern:
    # weight=1 for the primary, weight=0 for the fallback. Under Weighted
    # Shuffle the zero-weight peer is never picked while the primary is
    # healthy, but the cooldown logic excludes the primary on failure and
    # leaves the zero-weight peer as the only eligible target. See
    # llm_router.rs::select_simple_shuffle.
    routing_weight: int = Field(default=1, ge=0, description="Weight for load balancing (0 = fallback-only under Weighted Shuffle)")
    fallback_models: Optional[List[str]] = Field(
        None,
        description="Model-level failover chain: ordered list of model ids to try, in order, "
                    "when this model's own call fails with a retryable error.",
    )
    rate_limit_rpm: Optional[int] = Field(None, ge=1, description=RATE_LIMIT_RPM_DESCRIPTION)
    rate_limit_tpm: Optional[int] = Field(None, ge=1, description=RATE_LIMIT_TPM_DESCRIPTION)

    # Cost Management
    cost_per_input_token: Optional[float] = Field(None, ge=0, description="USD per 1M input tokens")
    cost_per_output_token: Optional[float] = Field(None, ge=0, description="USD per 1M output tokens")

    # Image generation parameters (mode='image_generation')
    cost_per_image: Optional[float] = Field(
        None, ge=0,
        description="USD cost per generated image. For image-mode models only."
    )
    supported_image_sizes: Optional[List[str]] = Field(
        None,
        description='Whitelist of accepted image sizes, e.g. ["1024x1024","1024x1792"]. '
                    "Enforced at the proxy edge."
    )
    supported_image_qualities: Optional[List[str]] = Field(
        None,
        description='Whitelist of accepted quality tiers, e.g. ["standard","hd"]. '
                    "Enforced at the proxy edge."
    )
    max_images_per_request: int = Field(
        default=1, ge=1, le=10,
        description="Maximum number of images permitted per single request (`n` parameter). "
                    "DALL-E 3 = 1, DALL-E 2 = 10."
    )

    # Audio TTS parameters (mode='audio_speech')
    cost_per_audio_char: Optional[float] = Field(
        None, ge=0, description="USD per generated character (TTS).")
    supported_voices: Optional[List[str]] = Field(
        None, description='Whitelist of accepted voice ids.')
    supported_audio_formats: Optional[List[str]] = Field(
        None, description='Whitelist of accepted output formats.')

    # Audio STT parameters (mode='audio_transcription')
    cost_per_audio_minute: Optional[float] = Field(
        None, ge=0, description="USD per minute of input audio (STT).")
    max_audio_duration_seconds: Optional[int] = Field(
        None, ge=1, description="Maximum input clip duration in seconds.")
    supported_input_formats: Optional[List[str]] = Field(
        None, description='Whitelist of accepted input formats.')
    supported_response_formats: Optional[List[str]] = Field(
        None,
        description=(
            "Whitelist of accepted Whisper-style response formats "
            "(`json`, `text`, `verbose_json`, `srt`, `vtt`). The "
            "Rust proxy's `pick_upstream_format` falls back to a "
            "safe per-model default when this is null."
        ),
    )

    # Video generation parameters (mode='video_generation')
    cost_per_video_second: Optional[float] = Field(
        None, ge=0, description="USD per second of generated video.")
    supported_video_resolutions: Optional[List[str]] = Field(
        None, description='Whitelist of accepted resolutions.')
    supported_video_durations: Optional[List[int]] = Field(
        None, description='Whitelist of accepted clip durations in seconds.')
    max_video_duration_seconds: Optional[int] = Field(
        None, ge=1, description="Hard ceiling on a single clip in seconds.")

    # Connection Settings
    timeout: float = Field(
        default=60.0,
        ge=1.0,
        le=1800.0,
        description=(
            "Request timeout (seconds). Up to 30 min to accommodate async-queue "
            "video models (luma-dream-machine 600s, kling 900s) — sync chat/image/"
            "audio models stay at the 60-300s default."
        ),
    )
    max_concurrent_requests: Optional[int] = Field(
        default=None, ge=1, le=1024,
        description=(
            "Max simultaneous in-flight requests to this model, enforced by a "
            "per-model semaphore in the Rust proxy. Leave empty for unbounded "
            "(defer to the rate-limit rpm/tpm knobs instead)."
        ),
    )

    allowed_fails: Optional[int] = Field(
        default=None, ge=1, le=ALLOWED_FAILS_MAX,
        description=(
            "The model's circuit breaker: after this many consecutive failed "
            "calls the gateway stops sending to the model and answers at once "
            "with a Retry-After, for `cooldown_time` seconds; it then lets one "
            "call through and closes on its success. 1 to 100. Set together "
            "with `cooldown_time`, or neither: with neither the gateway's "
            "defaults apply (5 failures, 30 seconds)."
        ),
    )
    cooldown_time: Optional[int] = Field(
        default=None, ge=1, le=COOLDOWN_TIME_MAX_SECONDS,
        description=(
            "Seconds the model's circuit breaker stays open once "
            "`allowed_fails` consecutive calls have failed, and the "
            "Retry-After of a call refused meanwhile. 1 to 3600. Set together "
            "with `allowed_fails`, or neither."
        ),
    )

    # Status
    enabled: bool = Field(default=True, description="Whether model is enabled")

    # Tenant-wide default limit/policy posture for this model (min-wins with RG).
    tenant_limits: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Tenant-wide default LLM usage limits (throughput/concurrency/budget/tokens/requests).",
    )
    tenant_governance: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Tenant-wide default LLM governance/policy (param caps, residency, guardrails).",
    )

    # Retirement metadata. All nullable — most models aren't on
    # a published end-of-life schedule. When `retire_date` is set,
    # the `llm_model_retirement_warner` job emits a system alert
    # starting on `retirement_reminder_date` (defaulted to
    # retire_date - 14d at API write time when the operator
    # doesn't supply an explicit value).
    retire_date: Optional[date] = Field(
        None,
        description=RETIRE_DATE_DESCRIPTION,
    )
    retirement_announced_at: Optional[date] = Field(
        None,
        description="Date the provider announced the retirement.",
    )
    retirement_reminder_date: Optional[date] = Field(
        None,
        description=(
            "Date from which operator alerts should start firing. "
            "Defaults to retire_date - 14d when retire_date is set "
            "but reminder_date isn't."
        ),
    )
    retirement_replacement_model_name: Optional[str] = Field(
        None, max_length=255,
        description="Provider-side name of the recommended successor.",
    )
    retirement_replacement_provider_key: Optional[str] = Field(
        None, max_length=100,
        description=(
            "Provider key of the replacement (`openai`, `anthropic`, "
            "`fal_ai`, ...). Null when the replacement is on the same "
            "provider."
        ),
    )
    retirement_notes: Optional[str] = Field(
        None,
        description=(
            "Free-form markdown. Recommended: announcement URL, "
            "breaking-change summary, migration notes."
        ),
    )

    # Capabilities JSON
    capabilities: Optional[Dict[str, Any]] = Field(None, description="Additional capabilities")

    # =========================================================================
    # GROUP-BASED ACCESS CONTROL
    # =========================================================================

    group_ids: Optional[List[str]] = Field(
        default=None,
        description="Resource group ids to bind the model to. Empty or null binds the tenant's Default group."
    )

    # ── Shared validators ───────────────────────────────────────────────
    _validate_mode = field_validator('mode')(_validate_mode)
    _tenant_limits = field_validator('tenant_limits')(_tenant_limits_in_this_release)
    _tenant_governance = field_validator('tenant_governance')(_tenant_governance_in_this_release)
    _validate_provider = field_validator('provider')(_validate_provider)
    _validate_auth_type = field_validator('auth_type')(_validate_auth_type)
    _validate_system_prompt = field_validator('system_prompt')(_validate_system_prompt)

    @model_validator(mode="after")
    def _circuit_breaker_pair(self):
        problem = circuit_breaker_pair_problem(self.allowed_fails, self.cooldown_time)
        if problem:
            raise ValueError(problem)
        return self

    # ── Create-specific validator ───────────────────────────────────────
    @field_validator('api_url')
    @classmethod
    def validate_api_url(cls, v: Optional[str], info) -> Optional[str]:
        """Validate that api_url is provided when auto_discovered is False"""
        if info.data.get('api_url_auto_discovered') is False and not v:
            raise ValueError("api_url is required when api_url_auto_discovered is False")
        if v:
            # The proxy fetches this URL with its own network position; an
            # IP-literal pointing at the metadata service (or, in a
            # multi-tenant deployment, at a private range) is refused here
            # so the admin sees why, and again at fetch time by the proxy.
            from app.services.outbound_policy import check_url, refusal_message
            reason = check_url(v)
            if reason:
                raise ValueError(refusal_message(v, "api_url", reason))
        return v


class LLModelUpdate(ResourceVersionField):
    """
    Request model for updating LLM model
    All fields optional - only updates provided fields
    """

    model_config = ConfigDict(extra="forbid")
    # Basic Information
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    usage: Optional[str] = Field(None, max_length=1000, description="Usage description and use cases")
    tags: Optional[List[str]] = Field(None, description="Tags for filtering")

    # Provider Information — see LLModelCreate for the rationale.
    provider_id: Optional[str] = Field(None, max_length=64)
    provider: Optional[str] = Field(None, max_length=50)
    model_name: Optional[str] = Field(None, max_length=200)
    mode: Optional[str] = Field(None, max_length=20)

    # Connection Details
    api_url_auto_discovered: Optional[bool] = Field(None, description="Auto-discover URL flag")
    api_url: Optional[str] = Field(None, max_length=500)

    # Authentication
    auth_type: Optional[str] = Field(None, max_length=50)
    api_key: Optional[str] = Field(None, max_length=500)
    provider_config: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Cloud provider credentials, merged into what is stored: a key present replaces, a key "
            "sent as null removes, a key absent is kept; null clears the whole value. The result is "
            "validated for Amazon Bedrock models."
        ),
    )

    # Model Configuration
    default_temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    # See create-payload note above re: `ge=0` for non-text modes.
    default_max_tokens: Optional[int] = Field(None, ge=0)

    # System Prompt Configuration
    system_prompt: Optional[str] = Field(
        None,
        max_length=100000,
        description="Update system prompt. Set to empty string or null to remove existing prompt. "
                    "Default system prompt prepended to all completion requests."
    )

    # Completion parameters
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    n: Optional[int] = Field(None, ge=1, le=10)
    stop_sequences: Optional[List[str]] = Field(None, max_length=4)
    seed: Optional[int] = None
    presence_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    logit_bias: Optional[Dict[str, float]] = None
    response_format: Optional[Dict[str, Any]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = Field(None, ge=0, le=5)
    stream_options: Optional[Dict[str, Any]] = None

    # Capabilities
    supports_streaming: Optional[bool] = None
    supports_function_calling: Optional[bool] = None
    supports_vision: Optional[bool] = None
    supports_batch: Optional[bool] = None

    # Tools/Functions
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = Field(None, max_length=50)
    parallel_tool_calls: Optional[bool] = None

    # Safety & Monitoring
    user_identifier: Optional[str] = Field(None, max_length=255)

    # Provider-Specific
    api_version: Optional[str] = Field(None, max_length=50)
    extra_headers: Optional[Dict[str, str]] = None

    # Advanced (retry/fallback are routing-group concerns)
    model_metadata: Optional[Dict[str, Any]] = None
    drop_params: Optional[bool] = Field(None, description=DROP_PARAMS_DESCRIPTION)
    additional_drop_params: Optional[List[str]] = Field(
        None,
        description="Update list of parameters to drop. Set to empty list to clear."
    )
    thinking_enabled: Optional[bool] = Field(
        None,
        description=(
            "Toggle extended-thinking / reasoning mode for this model. "
            "When enabled, the proxy injects the provider-specific reasoning "
            "parameter and (for Anthropic) coerces temperature to 1.0."
        ),
    )

    # Model Size
    parameters: Optional[float] = Field(None, ge=0)

    # Routing
    routing_group_id: Optional[str] = Field(None, max_length=100)
    # ge=0 mirrors the create model: 0 is a valid weight for the
    # primary-with-fallback pattern under Weighted Shuffle.
    routing_weight: Optional[int] = Field(None, ge=0)
    fallback_models: Optional[List[str]] = Field(
        None,
        description="Model-level failover chain: ordered list of model ids to try, in order, "
                    "when this model's own call fails with a retryable error.",
    )
    rate_limit_rpm: Optional[int] = Field(None, ge=1, description=RATE_LIMIT_RPM_DESCRIPTION)
    rate_limit_tpm: Optional[int] = Field(None, ge=1, description=RATE_LIMIT_TPM_DESCRIPTION)

    # Cost Management
    cost_per_input_token: Optional[float] = Field(None, ge=0)
    cost_per_output_token: Optional[float] = Field(None, ge=0)

    # Image generation parameters (mode='image_generation')
    cost_per_image: Optional[float] = Field(None, ge=0)
    supported_image_sizes: Optional[List[str]] = None
    supported_image_qualities: Optional[List[str]] = None
    max_images_per_request: Optional[int] = Field(None, ge=1, le=10)

    # Audio TTS (mode='audio_speech')
    cost_per_audio_char: Optional[float] = Field(None, ge=0)
    supported_voices: Optional[List[str]] = None
    supported_audio_formats: Optional[List[str]] = None

    # Audio STT (mode='audio_transcription')
    cost_per_audio_minute: Optional[float] = Field(None, ge=0)
    max_audio_duration_seconds: Optional[int] = Field(None, ge=1)
    supported_input_formats: Optional[List[str]] = None
    supported_response_formats: Optional[List[str]] = None

    # Video generation (mode='video_generation')
    cost_per_video_second: Optional[float] = Field(None, ge=0)
    supported_video_resolutions: Optional[List[str]] = None
    supported_video_durations: Optional[List[int]] = None
    max_video_duration_seconds: Optional[int] = Field(None, ge=1)

    # Connection Settings
    # Match the Create-payload upper bound — 1800s covers async-queue
    # video models (luma-dream-machine 600s, kling 900s) without
    # capping the field below the seed's defaults.
    timeout: Optional[float] = Field(None, ge=1.0, le=1800.0)
    max_concurrent_requests: Optional[int] = Field(None, ge=1, le=1024)
    allowed_fails: Optional[int] = Field(
        None, ge=1, le=ALLOWED_FAILS_MAX,
        description=(
            "The model's circuit breaker: after this many consecutive failed "
            "calls the gateway stops sending to the model and answers at once "
            "with a Retry-After, for `cooldown_time` seconds; it then lets one "
            "call through and closes on its success. 1 to 100. Set together "
            "with `cooldown_time`, or neither: with neither the gateway's "
            "defaults apply (5 failures, 30 seconds). "
            "Send both as null to go back to the defaults."
        ),
    )
    cooldown_time: Optional[int] = Field(
        None, ge=1, le=COOLDOWN_TIME_MAX_SECONDS,
        description=(
            "Seconds the model's circuit breaker stays open once "
            "`allowed_fails` consecutive calls have failed, and the "
            "Retry-After of a call refused meanwhile. 1 to 3600. Set together "
            "with `allowed_fails`, or neither."
        ),
    )

    # Status
    enabled: Optional[bool] = None

    # Tenant-wide default limit/policy posture (min-wins with RG).
    tenant_limits: Optional[Dict[str, Any]] = None
    tenant_governance: Optional[Dict[str, Any]] = None

    # Retirement metadata — see LLModelCreate for semantics.
    retire_date: Optional[date] = Field(None, description=RETIRE_DATE_DESCRIPTION)
    retirement_announced_at: Optional[date] = None
    retirement_reminder_date: Optional[date] = None
    retirement_replacement_model_name: Optional[str] = Field(None, max_length=255)
    retirement_replacement_provider_key: Optional[str] = Field(None, max_length=100)
    retirement_notes: Optional[str] = None

    # Capabilities JSON
    capabilities: Optional[Dict[str, Any]] = None

    # Group Access Control
    group_ids: Optional[List[str]] = Field(
        None,
        description="Replaces the model's resource groups with these ids. An empty list removes every binding; leave the field out to keep them."
    )

    # ── Shared validators ───────────────────────────────────────────────
    _validate_mode = field_validator('mode')(_validate_mode)
    _tenant_limits = field_validator('tenant_limits')(_tenant_limits_in_this_release)
    _tenant_governance = field_validator('tenant_governance')(_tenant_governance_in_this_release)
    _validate_provider = field_validator('provider')(_validate_provider)
    _validate_auth_type = field_validator('auth_type')(_validate_auth_type)
    _validate_system_prompt = field_validator('system_prompt')(_validate_system_prompt)


# =============================================================================
# Response Models
# =============================================================================

class LLMModelHealthStatusResponse(BaseModel):
    """Health status nested object"""
    status: str
    last_check: Optional[str] = None
    last_success: Optional[str] = None
    consecutive_failures: int = 0
    error_message: Optional[str] = None
    avg_latency_ms: Optional[float] = None
    # Probe taxonomy for the admin UI's secondary badge:
    # "catalog" | "endpoint" | "synthetic" | None.
    # See `services/llm/model/llm_models_manager.py` for the
    # full description.
    probe_type: Optional[str] = None


class PerformanceMetricsResponse(BaseModel):
    """Performance metrics nested object"""
    total_requests: int = 0
    failed_requests: int = 0
    success_rate: float = 0.0
    avg_latency_ms: Optional[float] = None
    total_tokens_consumed: int = 0


class LLModelResponse(ResourceMetaFields):
    """
    Response model for LLM model
    Includes all fields plus computed properties
    """
    # Basic Information
    id: str
    name: str
    description: Optional[str] = None
    usage: Optional[str] = None
    tags: Optional[List[str]] = None

    # Provider Information
    # The provider is a first-class entity. The response
    # surfaces three things:
    #   * `provider_id`  — FK into llm_providers.id
    #   * `provider`     — denormalised slug (= provider_info.key),
    #                      kept for back-compat with older admin UI
    #                      code that grouped models by string slug.
    #   * `provider_info` — the full nested provider row (display
    #                      name, doc URLs, etc.) so the UI can
    #                      render rich provider chips without a
    #                      second roundtrip.
    provider_id: Optional[str] = None
    provider: Optional[str] = None
    provider_info: Optional[Dict[str, Any]] = None
    # Catalog provenance — the global llm_catalog entry this model was
    # imported from (NULL for hand-rolled models).
    catalog_id: Optional[str] = None
    model_name: str
    mode: str

    # Connection Details
    api_url_auto_discovered: bool
    api_url: Optional[str] = None

    # Authentication
    auth_type: str
    has_provider_config: bool = False  # True if cloud credentials are configured
    # Note: api_key and provider_config NOT included in response for security.
    # What is no secret of `provider_config`:
    aws_region: Optional[str] = Field(
        None, description="The AWS region stored in provider_config (Amazon Bedrock); null when none is"
    )
    gcp_project_id: Optional[str] = Field(
        None, description="The Google Cloud project stored in provider_config (Vertex AI); null when none is"
    )
    gcp_location: Optional[str] = Field(
        None, description="The Google Cloud location stored in provider_config (Vertex AI); null when none is"
    )
    provider_config_keys: Dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "For each secret key of provider_config (aws_access_key_id, aws_secret_access_key, "
            "aws_session_token, gcp_credentials_json): whether a value is stored. The values are "
            "never returned."
        ),
    )

    # Model Configuration
    default_temperature: float
    default_max_tokens: int

    # System Prompt
    system_prompt: Optional[str] = Field(
        None,
        description="Default system prompt for this model (if configured)"
    )

    # Completion parameters (all optional in response)
    top_p: Optional[float] = None
    n: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    seed: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, float]] = None
    response_format: Optional[Dict[str, Any]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    stream_options: Optional[Dict[str, Any]] = None

    # Capabilities
    supports_streaming: bool
    supports_function_calling: bool
    supports_vision: bool
    supports_batch: bool = False

    # Tools/Functions
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = None
    parallel_tool_calls: Optional[bool] = None

    # Safety & Monitoring
    user_identifier: Optional[str] = None

    # Provider-Specific
    api_version: Optional[str] = None
    extra_headers: Optional[Dict[str, str]] = None

    # Advanced (retry/fallback are routing-group concerns)
    model_metadata: Optional[Dict[str, Any]] = None
    drop_params: Optional[bool] = Field(None, description=DROP_PARAMS_DESCRIPTION)
    additional_drop_params: Optional[List[str]] = Field(
        None,
        description="List of parameter names automatically dropped for this model"
    )
    thinking_enabled: Optional[bool] = Field(
        None,
        description="Whether extended-thinking / reasoning mode is enabled for this model",
    )

    # Model Size
    parameters: Optional[float] = Field(
        None,
        description="Model size in billions of parameters (e.g. 8.0, 70.0, 405.0)"
    )

    # Routing
    routing_group_id: Optional[str] = None
    routing_weight: int
    fallback_models: Optional[List[str]] = None
    rate_limit_rpm: Optional[int] = None
    rate_limit_tpm: Optional[int] = None

    # Cost Management
    cost_per_input_token: Optional[float] = None
    cost_per_output_token: Optional[float] = None

    # Image generation parameters (mode='image_generation')
    cost_per_image: Optional[float] = Field(
        None,
        description="USD cost per generated image. Image-mode models only.",
    )
    supported_image_sizes: Optional[List[str]] = Field(
        None,
        description='Whitelist of accepted image sizes, e.g. ["1024x1024","1024x1792"].',
    )
    supported_image_qualities: Optional[List[str]] = Field(
        None,
        description='Whitelist of accepted quality tiers, e.g. ["standard","hd"].',
    )
    max_images_per_request: Optional[int] = Field(
        None,
        description="Maximum images per single request (`n`). DALL-E 3 = 1, DALL-E 2 = 10.",
    )

    # Audio TTS (mode='audio_speech')
    cost_per_audio_char: Optional[float] = Field(
        None, description="USD per generated character (TTS).")
    supported_voices: Optional[List[str]] = Field(
        None, description="Whitelist of accepted voice ids.")
    supported_audio_formats: Optional[List[str]] = Field(
        None, description="Whitelist of accepted output formats.")

    # Audio STT (mode='audio_transcription')
    cost_per_audio_minute: Optional[float] = Field(
        None, description="USD per minute of input audio (STT).")
    max_audio_duration_seconds: Optional[int] = Field(
        None, description="Maximum input clip duration in seconds.")
    supported_input_formats: Optional[List[str]] = Field(
        None, description="Whitelist of accepted input formats.")
    supported_response_formats: Optional[List[str]] = Field(
        None,
        description=(
            "Whitelist of accepted Whisper-style response formats "
            "(json, text, verbose_json, srt, vtt)."
        ),
    )

    # Video generation (mode='video_generation')
    cost_per_video_second: Optional[float] = Field(
        None, description="USD per second of generated video.")
    supported_video_resolutions: Optional[List[str]] = Field(
        None, description="Whitelist of accepted resolutions.")
    supported_video_durations: Optional[List[int]] = Field(
        None, description="Whitelist of accepted clip durations in seconds.")
    max_video_duration_seconds: Optional[int] = Field(
        None, description="Hard ceiling on a single clip in seconds.")

    # Connection Settings
    timeout: float
    max_concurrent_requests: Optional[int] = None
    allowed_fails: Optional[int] = Field(
        None,
        description=(
            "The model's circuit breaker: after this many consecutive failed "
            "calls the gateway stops sending to the model and answers at once "
            "with a Retry-After, for `cooldown_time` seconds; it then lets one "
            "call through and closes on its success. 1 to 100. Set together "
            "with `cooldown_time`, or neither: with neither the gateway's "
            "defaults apply (5 failures, 30 seconds)."
        ),
    )
    cooldown_time: Optional[int] = Field(
        None,
        description=(
            "Seconds the model's circuit breaker stays open once "
            "`allowed_fails` consecutive calls have failed, and the "
            "Retry-After of a call refused meanwhile. 1 to 3600. Set together "
            "with `allowed_fails`, or neither."
        ),
    )

    # Status
    enabled: bool

    # Tenant-wide default limit/policy posture (min-wins with RG).
    tenant_limits: Optional[Dict[str, Any]] = None
    tenant_governance: Optional[Dict[str, Any]] = None

    # Provenance: 'manual' (operator-added) vs 'import' (discovered via Traffic
    # Data Import; observability-only until an operator promotes it).
    source: str = "manual"

    # Retirement metadata. Date columns serialise to ISO `YYYY-MM-DD`
    # strings; admin UI parses them back into Date pickers.
    retire_date: Optional[str] = None
    retirement_announced_at: Optional[str] = None
    retirement_reminder_date: Optional[str] = None
    retirement_replacement_model_name: Optional[str] = None
    retirement_replacement_provider_key: Optional[str] = None
    retirement_notes: Optional[str] = None

    # Capabilities JSON
    capabilities: Optional[Dict[str, Any]] = None

    # Access Control
    is_global: bool = False
    group_ids: List[str] = []
    groups: Optional[List[LLMModelGroupInfo]] = None  # Group memberships

    # Health & Performance (nested objects)
    health_status: LLMModelHealthStatusResponse
    performance_metrics: PerformanceMetricsResponse

    # Multi-tenancy
    tenant_id: str

    # Timestamps
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


# =============================================================================
# List/Detail Response Wrappers
# =============================================================================

class LLModelListResponse(BaseModel):
    """Response model for listing LLM models"""
    models: Dict[str, LLModelResponse] = Field(..., description="Dictionary of models by ID")
    total: int = Field(..., description="Total number of models")

class LLModelDetailResponse(BaseModel):
    """Response model for the single LLM model detail"""
    model: LLModelResponse = Field(..., description="Model details")


# =============================================================================
# Group Assignment Models
# =============================================================================

class ModelGroupAssignment(BaseModel):
    """Request model for assigning model to groups"""
    group_ids: List[str] = Field(..., description="Group IDs to assign", min_length=1)


class ModelGroupUnassignment(BaseModel):
    """Request model for removing model from groups"""
    group_ids: List[str] = Field(..., description="Group IDs to remove", min_length=1)


# =============================================================================
# User Access Query Models
# =============================================================================

class UserAccessibleModelsRequest(BaseModel):
    """Request to get models accessible to a specific user"""
    user_id: Optional[str] = Field(None, description="User ID (if not provided, uses authenticated user)")
    include_global: bool = Field(default=True, description="Include global models")
    enabled_only: bool = Field(default=True, description="Only return enabled models")


class UserAccessibleModelsResponse(BaseModel):
    """Response with models accessible to user"""
    user_id: str
    user_groups: List[str]  # Group IDs user belongs to
    models: List[LLModelResponse]
    total: int


# =============================================================================
# Health Check Models
# =============================================================================

class HealthCheckRequest(BaseModel):
    """Request to perform health check on model"""
    model_id: str = Field(..., description="Model ID to health check")


class HealthCheckResponse(BaseModel):
    """Response from health check"""
    model_id: str
    model_name: str
    status: str  # 'healthy', 'unhealthy', 'timeout'
    response_time_ms: Optional[float] = None
    error_message: Optional[str] = None
    timestamp: str
    probe_type: Optional[str] = Field(
        default=None,
        description=(
            "Which probe produced this verdict: 'catalog' (provider /models "
            "listing, no generation), 'endpoint' (real inference call) or "
            "'synthetic' (Brutor-side validation only). Null when no probe "
            "ran (routing model, unexpected error, or backed off)."
        ),
    )
    backed_off: bool = Field(
        default=False,
        description=(
            "True when a background sweep skipped this model because its "
            "last failure_threshold checks failed with a non-transient "
            "(config / model) error; status and error_message are the last "
            "recorded verdict, not a fresh probe."
        ),
    )


# =============================================================================
# Bulk Health Check Models
# =============================================================================

class BulkHealthCheckRequest(BaseModel):
    """Request to perform health check on multiple models"""
    model_ids: Optional[List[str]] = Field(
        None,
        description="Specific model IDs to check (if None, checks all enabled models)"
    )
    enabled_only: bool = Field(
        default=True,
        description="Only check enabled models"
    )
    timeout_seconds: Optional[float] = Field(
        default=None,
        ge=1.0,
        le=60.0,
        description="Override default timeout for health checks"
    )
    probe: Literal["endpoint", "catalog"] = Field(
        default="endpoint",
        description=(
            "'endpoint' (default; operator-initiated) runs the real "
            "end-to-end probe per model. 'catalog' (the Admin UI's "
            "background sweep) uses the free provider /models listing "
            "where the provider has a catalog URL, falls back to the "
            "endpoint probe otherwise, and backs off models that keep "
            "failing for a configuration / model reason."
        ),
    )


class LLMModelBulkHealthCheckResponse(BaseModel):
    """Response from bulk health check"""
    results: List[HealthCheckResponse] = Field(..., description="Health check results for each model")
    total_checked: int = Field(..., description="Total number of models checked")
    healthy_count: int = Field(..., description="Number of healthy models")
    unhealthy_count: int = Field(..., description="Number of unhealthy models")
    timeout_count: int = Field(..., description="Number of models that timed out")
    timestamp: str = Field(..., description="When the bulk check was performed")


# =============================================================================
# Search Models
# =============================================================================

class LLModelSearchRequest(BaseModel):
    """
    Request model for searching LLM models - ADMIN VERSION

    Searches ALL models in the tenant
    """

    # =========================================================================
    # Text Search Filters (partial, case-insensitive)
    # =========================================================================

    name: Optional[str] = Field(
        None,
        max_length=200,
        description="Search by model name (partial match, case-insensitive)"
    )

    description: Optional[str] = Field(
        None,
        max_length=1000,
        description="Search in description (partial match, case-insensitive)"
    )

    model_name: Optional[str] = Field(
        None,
        max_length=200,
        description="Search by model config name like 'gpt-5.2', 'claude-sonnet-5-5' (partial match)"
    )

    # =========================================================================
    # Exact Match Filters
    # =========================================================================

    provider: Optional[str] = Field(
        None,
        max_length=50,
        description="Filter by provider (exact match, case-insensitive). "
                    "Examples: 'openai', 'anthropic', 'gemini'"
    )

    providers: Optional[List[str]] = Field(
        None,
        max_length=20,
        description="Filter by multiple providers (OR logic). "
                    "Example: ['openai', 'anthropic', 'gemini']"
    )

    # =========================================================================
    # Capability Filters
    # =========================================================================

    supports_streaming: Optional[bool] = Field(
        None,
        description="Filter by streaming support. True = only streaming models"
    )

    supports_function_calling: Optional[bool] = Field(
        None,
        description="Filter by function calling support. True = only models with tools/functions"
    )

    supports_vision: Optional[bool] = Field(
        None,
        description="Filter by vision support. True = only models that can process images"
    )

    supports_batch: Optional[bool] = Field(
        None,
        description="Filter by batch processing support. True = only models that support batch jobs"
    )

    # =========================================================================
    # Mode Filter
    # =========================================================================

    mode: Optional[str] = Field(
        None,
        max_length=30,
        description="Filter by mode (exact match, case-insensitive). One of: "
                    "'chat', 'responses', 'completion', 'embedding', "
                    "'image_generation', 'audio_transcription', 'audio_speech', "
                    "'video_generation', 'rerank', 'moderation', 'routing'."
    )

    # =========================================================================
    # Context Window Filters
    # =========================================================================
    #
    # `context_window` is the model's maximum *input* context size (in
    # tokens) — distinct from `default_max_tokens` which caps the
    # generated output. The catalog stores it inside the
    # `capabilities` JSONB column (see e.g.
    # provider_data/openai_models_init.py: `"capabilities":
    # {"context_window": 400000, ...}`), not as a top-level column,
    # so the search service compares via JSONB extraction. Models
    # that omit the field (older seeds, embedding-only / audio /
    # video models) silently fall through the filter — the SQL cast
    # is wrapped in a NULLs-not-matching predicate.

    min_context_window: Optional[int] = Field(
        None,
        ge=0,
        description="Minimum context window size in tokens (inclusive). "
                    "Example: 128000 = only models with ≥128K context."
    )

    max_context_window: Optional[int] = Field(
        None,
        ge=0,
        description="Maximum context window size in tokens (inclusive). "
                    "Example: 200000 = only models with ≤200K context."
    )

    # =========================================================================
    # Cost Filters (USD per 1M tokens)
    # =========================================================================

    min_cost_per_input_token: Optional[float] = Field(
        None,
        ge=0,
        description="Minimum cost per 1M input tokens (inclusive). "
                    "Example: 0.01 = $0.01 per 1M tokens"
    )

    max_cost_per_input_token: Optional[float] = Field(
        None,
        ge=0,
        description="Maximum cost per 1M input tokens (inclusive). "
                    "Example: 10.0 = $10 per 1M tokens"
    )

    min_cost_per_output_token: Optional[float] = Field(
        None,
        ge=0,
        description="Minimum cost per 1M output tokens (inclusive)"
    )

    max_cost_per_output_token: Optional[float] = Field(
        None,
        ge=0,
        description="Maximum cost per 1M output tokens (inclusive)"
    )

    # =========================================================================
    # Tag Search (searches in capabilities JSON)
    # =========================================================================

    tags: Optional[List[str]] = Field(
        None,
        max_length=20,
        description="Search by tags stored in capabilities.tags (OR logic). "
                    "Example: ['production', 'gpt-5', 'high-quality']"
    )

    # =========================================================================
    # Status Filters
    # =========================================================================

    enabled_only: bool = Field(
        default=True,
        description="Only return enabled models. Set to False to include disabled models."
    )

    healthy_only: bool = Field(
        default=False,
        description="Only return models with healthy status (excludes unhealthy/unknown)"
    )

    # =========================================================================
    # Sorting
    # =========================================================================

    sort_by: Literal[
        "name",
        "created_at",
        "updated_at",
        "cost_per_input_token",
        "cost_per_output_token",
        "avg_latency_ms",
        "total_requests"
    ] = Field(
        default="name",
        description="Field to sort by"
    )

    sort_order: Literal["asc", "desc"] = Field(
        default="asc",
        description="Sort order: 'asc' (ascending) or 'desc' (descending)"
    )

    # =========================================================================
    # Pagination
    # =========================================================================

    offset: int = Field(
        default=0,
        ge=0,
        description="Number of records to skip (for pagination)"
    )

    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of records to return (1-500)"
    )

    # =========================================================================
    # Validators
    # =========================================================================

    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return v.lower()

    @field_validator('providers')
    @classmethod
    def validate_providers(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        return [p.lower() for p in v]

    @field_validator('max_cost_per_input_token')
    @classmethod
    def validate_max_input_cost(cls, v: Optional[float], info) -> Optional[float]:
        """Ensure max is greater than min"""
        if v is not None:
            min_val = info.data.get('min_cost_per_input_token')
            if min_val is not None and v < min_val:
                raise ValueError(
                    f"max_cost_per_input_token ({v}) must be >= "
                    f"min_cost_per_input_token ({min_val})"
                )
        return v

    @field_validator('max_cost_per_output_token')
    @classmethod
    def validate_max_output_cost(cls, v: Optional[float], info) -> Optional[float]:
        """Ensure max is greater than min"""
        if v is not None:
            min_val = info.data.get('min_cost_per_output_token')
            if min_val is not None and v < min_val:
                raise ValueError(
                    f"max_cost_per_output_token ({v}) must be >= "
                    f"min_cost_per_output_token ({min_val})"
                )
        return v


class LLModelSearchResponse(BaseModel):
    """
    Response model for LLM model search
    Includes pagination metadata
    """
    models: List['LLModelResponse'] = Field(
        ...,
        description="List of models matching search criteria"
    )

    total: int = Field(
        ...,
        description="Total number of models matching criteria (ignoring pagination)"
    )

    offset: int = Field(
        ...,
        description="Current offset (pagination)"
    )

    limit: int = Field(
        ...,
        description="Current limit (pagination)"
    )

    has_more: bool = Field(
        ...,
        description="Whether there are more results beyond current page"
    )

    filters_applied: dict = Field(
        ...,
        description="Summary of filters applied to this search"
    )

    class Config:
        from_attributes = True


# =============================================================================
# Search Summary Models
# =============================================================================

class LLModelSearchSummary(BaseModel):
    """
    Summary statistics for search results
    Useful for faceted search / filter counts
    """
    total_models: int

    # Provider breakdown
    providers: dict[str, int] = Field(
        ...,
        description="Count of models per provider. Example: {'openai': 5, 'anthropic': 3}"
    )

    # Capability counts
    streaming_capable: int
    function_calling_capable: int
    vision_capable: int
    batch_capable: int

    # Cost statistics
    cost_stats: dict = Field(
        ...,
        description="Cost statistics (min, max, avg for input/output tokens)"
    )

    # Status
    enabled_count: int
    disabled_count: int
    healthy_count: int
    unhealthy_count: int


class LLModelSearchWithSummaryResponse(BaseModel):
    """
    Extended search response with summary statistics
    Useful for building search UIs with faceted filtering
    """
    search_results: LLModelSearchResponse
    summary: LLModelSearchSummary