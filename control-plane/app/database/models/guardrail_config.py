# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guardrail Configuration Database Model

Guardrail settings controlling content filtering across all
proxy surfaces (LLM, MCP, Agent Skills).

Multiple configs per tenant are supported. Each config can be
associated with one or more Resource Groups — exactly like LLM models,
MCP servers, and Agent Skills.

- A config with NO resource group associations is the **tenant default**
  and applies to all users.
- A config associated with specific groups ONLY applies to users in
  those groups.
- When a user belongs to multiple groups, all applicable configs are
  merged using a "most restrictive wins" strategy.

This lets admins create strict guardrails for "External Partners" while
keeping lighter rules for "Internal Developers", etc.

Association table:
    resource_group_guardrails  (resource_group_id, guardrail_config_id)
"""

import json
from sqlalchemy import (
    Column, String, Boolean, DateTime, JSON, Float, Table,
    ForeignKey, Index, Integer, text
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import validates, relationship
from typing import Dict, Any, List

from app.database import Base


# =============================================================================
# Default surface sets
# =============================================================================
# These mirror the backfill values in migration `a067_per_check_surfaces` — both
# places need to stay in lockstep. The model defaults serve newly-
# created rows that never went through the migration backfill and
# legacy rows where the migration is still pending in another env.
# Config-level surface tokens (still accepted in policy-as-code YAML and
# in configs without per-mode surfaces).
# Per-mode LLM surfaces — one token per operationally distinct route
# pair with one token per operationally distinct route. Order: input
# modes first then output modes, grouped by media type, so callers that
# iterate get a stable UI ordering.
LLM_INPUT_MODE_SURFACES = [
    "chat_input",
    "embeddings_input",
    "image_gen_input",
    "audio_tts_input",
    "audio_stt_input",
    "video_gen_input",
    "batch_input",
    "moderation_input",
]
LLM_OUTPUT_MODE_SURFACES = [
    "chat_output",
    "image_gen_output",
    "audio_tts_output",
    "audio_stt_output",
    "video_gen_output",
    "batch_output",
]
LLM_MODE_SURFACES = LLM_INPUT_MODE_SURFACES + LLM_OUTPUT_MODE_SURFACES

# Modes that produce text output today. image_gen_output /
# audio_tts_output / video_gen_output are listed in
# LLM_OUTPUT_MODE_SURFACES because the gate columns exist, but
# they're behind future NSFW classifiers — running text-based checks
# on them is a no-op until those land.
LLM_TEXT_OUTPUT_MODE_SURFACES = [
    "chat_output",
    "audio_stt_output",
    "batch_output",
]

ALL_SURFACES = (
    LLM_MODE_SURFACES + [
        "mcp_input", "mcp_output",
        "skill_input", "skill_output",
        "a2a_inbound", "a2a_outbound",
    ]
)
# `INPUT_SURFACES` — the set of surfaces the orchestrator considers
# "input-direction" for per-check defaults. Spans every LLM input
# mode (chat / embeddings / etc.) plus the input-side non-LLM kinds.
INPUT_SURFACES = (
    LLM_INPUT_MODE_SURFACES + ["mcp_input", "skill_input", "a2a_inbound"]
)
LLM_MCP_SURFACES = LLM_MODE_SURFACES + ["mcp_input", "mcp_output"]

_DEFAULT_SURFACES_ALL = list(ALL_SURFACES)
_DEFAULT_SURFACES_INPUT_ONLY = list(INPUT_SURFACES)
_DEFAULT_SURFACES_LLM_MCP = list(LLM_MCP_SURFACES)

# Audit-recommended per-check surface sets. These are the
# narrowed defaults applied by the "Apply recommended per-mode
# defaults" admin action and by the new-tenant seed. They keep the
# strict checks (jailbreak / prompt_injection / toxic_content) away
# from operator-controlled corpora (embeddings / batch) and the
# safety-classifier loop (moderation_input), while keeping PII /
# secrets wide because those concerns apply uniformly.
CHAT_STYLE_INPUT_MODE_SURFACES = [
    s for s in LLM_INPUT_MODE_SURFACES
    if s not in ("embeddings_input", "batch_input", "moderation_input")
]
AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES = (
    CHAT_STYLE_INPUT_MODE_SURFACES
    + ["mcp_input", "skill_input", "a2a_inbound"]
)
AUDIT_RECOMMENDED_TOXIC_CONTENT_SURFACES = (
    CHAT_STYLE_INPUT_MODE_SURFACES
    + ["chat_output", "audio_stt_output", "batch_output",
       "mcp_input", "mcp_output",
       "skill_input", "skill_output",
       "a2a_inbound", "a2a_outbound"]
)
AUDIT_RECOMMENDED_WIDE_SURFACES = (
    LLM_INPUT_MODE_SURFACES
    + LLM_TEXT_OUTPUT_MODE_SURFACES
    + ["mcp_input", "mcp_output", "skill_input", "skill_output",
       "a2a_inbound", "a2a_outbound"]
)


# Secrets detection.
#
# Defaults baked into the migration backfill (`a068_secrets_detection`) AND the Rust
# `SecretsDetector::default_categories()`. All three must stay in
# lockstep — a drift would mean "first-write through the model"
# gets a different category set than "row created pre-a068 then
# read after the backfill". The drift test in
# `tests/test_a068_secrets_migration.py` pins this.
DEFAULT_SECRETS_CATEGORIES = [
    "aws_access_key",
    "aws_secret_key",
    "github_token",
    "openai_key",
    "gcp_service_account",
    "private_key_pem",
    "jwt",
]
ALL_SECRETS_CATEGORIES = DEFAULT_SECRETS_CATEGORIES + ["generic_high_entropy"]


# =============================================================================
# Many-to-many association table (matches resource_group_llm_models pattern)
# =============================================================================

resource_group_guardrails = Table(
    "resource_group_guardrails",
    Base.metadata,
    Column(
        "resource_group_id",
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "guardrail_config_id",
        String(255),
        ForeignKey("guardrail_configs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    comment="Associates guardrail configs with resource groups",
)


def _public_provider_entry(entry):
    """A provider entry without its secrets (`api_key`, the AWS secret and
    session token of a Bedrock entry's `config`)."""
    from app.services.guardrails.provider_secrets import public_entry

    return public_entry(entry)


class GuardrailConfig(Base):
    """
    Guardrail configuration — assignable to Resource Groups.

    Multiple configs per tenant.  Configs with no group associations act as
    the tenant-wide default.  Group-specific configs override the default
    for users in those groups using a "most restrictive wins" merge.

    Usage:
        # Tenant default (no group associations):
        default = db.query(GuardrailConfig).filter_by(
            tenant_id=t, is_default=True
        ).first()

        # Group-specific configs for a user:
        from app.services.guardrails.guardrails_service import GuardrailsService
        result = await service.check_input(
            content=prompt, surface="chat_input", tenant_id=t,
            user_id=u, db=db,
        )
    """

    __tablename__ = "guardrail_configs"

    # =========================================================================
    # IDENTITY
    # =========================================================================

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        # NOTE: unique=True removed — multiple configs per tenant now
        comment="Owning tenant",
    )

    name = Column(
        String(255), nullable=False, default="Default Guardrails",
        comment="Human-readable name (unique within tenant)",
    )

    description = Column(
        String(1024), nullable=True,
        comment="Admin-facing description of this guardrail config's purpose",
    )

    is_default = Column(
        Boolean, default=False, server_default=text("false"), nullable=False,
        comment=(
            "If True, this config applies to ALL users in the tenant "
            "(regardless of group membership).  Only one default per tenant."
        ),
    )

    priority = Column(
        Integer, default=0,
        comment=(
            "When multiple configs match, higher priority runs first.  "
            "Most restrictive action still wins regardless of order."
        ),
    )

    # =========================================================================
    # PER-CALL SCOPE
    #
    # Optional narrowing filters. When set, the config only fires for
    # the matching server / tool. NULL = applies to all calls. See its
    # migration for the full semantics. The Rust orchestrator applies these filters AFTER
    # the existing tenant / group resolution, so they compose with
    # `is_default` and resource-group bindings rather than replacing
    # them.
    # =========================================================================

    scope_mcp_server_id = Column(
        String(255),
        ForeignKey("mcp_servers.id", ondelete="SET NULL"),
        nullable=True,
        comment="If set, this config only applies to calls on this MCP server",
    )
    scope_tool_name = Column(
        String(255),
        nullable=True,
        comment="If set, this config only applies to tools/call where params.name matches",
    )

    # =========================================================================
    # RESOURCE GROUP ASSOCIATION
    # =========================================================================

    groups = relationship(
        "ResourceGroup",
        secondary=resource_group_guardrails,
        back_populates="guardrail_configs",
        lazy="selectin",
    )

    # =========================================================================
    # GLOBAL TOGGLE
    # =========================================================================

    enabled = Column(
        Boolean, default=False, server_default=text("false"), nullable=False,
        comment="Master switch — False disables ALL guardrails for this config",
    )

    # =========================================================================
    # SURFACE TOGGLES
    # =========================================================================

    # Per-mode LLM surface gates. Each one matches a single
    # operational route in the proxy.
    # NOTE (hardening): every boolean surface gate is NOT NULL with a DB-level
    # server_default. A NULL here would fail the Rust `SELECT gc.*` row decode
    # and silently drop the WHOLE config (fail-open, no guardrails). Both the
    # Python default (create path) and server_default (any other inserter) are
    # set so a row can never carry NULL.
    chat_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan user prompts before forwarding to chat/completions/responses/messages")
    chat_output_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment="Scan model responses before returning to client; streamed responses follow streaming_output_mode")

    # Streaming (SSE) output-guardrail posture. Non-streaming output
    # always runs the full orchestrator; on a stream you can't un-send a
    # token, so this picks the safety↔latency trade-off (cf. AWS Bedrock
    # sync vs async). 'auto' = sync when a redact output check is active
    # (redaction in flight), else async-interrupt (block-only, faster).
    streaming_output_mode = Column(String(10), default="auto", server_default="auto", nullable=False,
        comment="Streaming output guardrail posture: 'auto' | 'sync' | 'async'")

    # New tenants get embeddings_input narrowed to PII + secrets only
    # via seed_guardrails.py; the column gate stays True so the
    # orchestrator still enters the dispatch path. Jailbreak / injection
    # / toxic-content are excluded from `embeddings_input` in each
    # check's *_surfaces array.
    embeddings_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan documents before embedding. Default-narrowed at seed time to PII + secrets only.")

    image_gen_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan prompts before image generation")
    image_gen_output_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment="Scan generated images (reserved for future NSFW classifier)")

    audio_tts_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan text before voice synthesis")
    audio_tts_output_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment="Scan synthesised audio (reserved for future NSFW classifier)")

    audio_stt_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan optional context prompt before transcription")
    audio_stt_output_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan transcribed text — policy-relevant once it lands in downstream chat")

    video_gen_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan prompts before video generation")
    video_gen_output_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment="Scan generated video (reserved for future NSFW classifier)")

    batch_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan inputs of async batch jobs")
    batch_output_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment="Scan outputs of async batch jobs")

    # Moderation calls are meta-calls into safety classifiers. Running
    # jailbreak / injection / toxic-content checks on a moderation
    # request body is recursive nonsense; the Rust orchestrator hard-
    # skips those three checks whenever surface == moderation_input,
    # regardless of this gate. PII + secrets still run if enabled (a
    # moderation call carrying an SSN is still a real policy concern).
    moderation_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Scan moderation request bodies. Jailbreak/injection/toxic are auto-bypassed at the orchestrator.")

    mcp_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Check MCP tool call arguments")
    mcp_output_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Check MCP tool call results")
    mcp_registration_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment=(
            "Scan tool descriptions from MCP servers' tools/list "
            "responses for prompt-injection payloads at server "
            "registration and on every catalog refresh. Triggers a "
            "system_alert (resource_type=mcp_server, error_code="
            "mcp_tool_description_poisoned) when a description "
            "matches the prompt-injection detector."
        ))

    skill_input_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Check skill execution inputs")
    skill_output_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Check skill script output and template renders")

    # A2A surfaces. Default off until an operator opts in.
    a2a_inbound_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment=(
            "Check incoming A2A v1.0 message bodies (tasks.send / "
            "message.send) when an external agent invokes one of "
            "this tenant's hosted Agent Cards"
        ))
    a2a_outbound_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment=(
            "Check outgoing A2A delegations (the payload Brutor "
            "sends when one of its hosted agents calls a peer "
            "agent at another vendor / business unit)"
        ))

    # =========================================================================
    # BUILT-IN CHECKS (ContentFilter)
    # =========================================================================

    pii_detection_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False)
    pii_action = Column(String(20), default="redact",
        comment="Action for PII: block, redact, warn, allow")
    # Provider selector + per-tenant category opt-in.
    pii_provider = Column(String(32), nullable=True,
        comment="PII detector provider. 'built_in' default; reserved for future external providers.")
    pii_categories = Column(JSON, nullable=True,
        comment="Opt-in subset of built-in PII categories. Null = run all 12.")
    # Confidence-score threshold for the Presidio analyzer
    # path. Forwarded as the `score_threshold` request parameter on
    # `/analyze`. The built-in regex detector ignores this column —
    # there is no continuous score to compare against. NULL falls
    # back to Presidio's own default (~0.5).
    pii_threshold = Column(Float, nullable=True,
        comment="Confidence threshold for the Presidio PII analyzer "
                "(forwarded as /analyze.score_threshold). Built-in "
                "regex detector ignores this column. NULL = Presidio "
                "default.")

    prompt_injection_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False)
    prompt_injection_action = Column(String(20), default="block")
    prompt_injection_sensitivity = Column(Float, default=0.7)
    prompt_injection_provider = Column(String(32), nullable=True,
        comment="'built_in' (default) or 'lakera_guard' (requires api_key in providers JSON).")

    jailbreak_detection_enabled = Column(Boolean, default=True, server_default=text("true"), nullable=False)
    jailbreak_action = Column(String(20), default="block")
    jailbreak_provider = Column(String(32), nullable=True,
        comment="'built_in' (default) or 'lakera_guard'.")

    toxic_content_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False)
    toxic_content_action = Column(String(20), default="warn")
    toxic_content_threshold = Column(Float, default=0.8)
    toxic_content_provider = Column(String(32), nullable=True,
        comment="'built_in' (default — bundled profanity wordlist, no API key needed) or 'openai_moderation' (requires OpenAI API key in External Providers).")
    toxic_content_category_thresholds = Column(JSON, nullable=True,
        comment="Per-category thresholds e.g. {\"hate\":0.7}. Empty falls back to toxic_content_threshold.")

    banned_words = Column(JSON, nullable=True, default=list)
    banned_patterns = Column(JSON, nullable=True, default=list)
    banned_words_match_mode = Column(String(16), nullable=True, default="substring",
        comment="'substring' (default, back-compat) or 'word_boundary' (avoids Scunthorpe-problem false positives).")

    # =========================================================================
    # PER-CHECK SURFACE GRANULARITY
    # =========================================================================
    # Each check now carries its own surface list. The config-level
    # *_enabled flags above remain as the OUTER gate (the set of
    # surfaces this config touches at all); the orchestrator
    # intersects the per-check surfaces with the config-level set
    # so an operator cannot fire a check on a surface the config
    # doesn't cover. A check with no list is stored as SQL NULL
    # (`none_as_null`), which the gateway reads as its default set for
    # the check, the set `to_dict()` shows. A JSON null would be read as
    # a list that cannot be used: the check would run nowhere.
    pii_surfaces = Column(JSONB(none_as_null=True), nullable=True,
        comment="Subset of per-mode + MCP / Skill / A2A surface tokens on which PII detection fires. See ALLOWED_CHECK_SURFACES in app/api/v1/models/admin/guardrail.py.")
    prompt_injection_surfaces = Column(JSONB(none_as_null=True), nullable=True,
        comment="Surfaces on which prompt-injection detection fires (typically input-side only).")
    jailbreak_surfaces = Column(JSONB(none_as_null=True), nullable=True,
        comment="Surfaces on which jailbreak detection fires (typically input-side only).")
    toxic_content_surfaces = Column(JSONB(none_as_null=True), nullable=True,
        comment="Surfaces on which toxic-content detection fires.")
    banned_patterns_surfaces = Column(JSONB(none_as_null=True), nullable=True,
        comment="Surfaces on which banned-pattern matching fires.")
    banned_words_surfaces = Column(JSONB(none_as_null=True), nullable=True,
        comment="Surfaces on which banned-word matching fires.")

    # =========================================================================
    # SECRETS DETECTION
    # =========================================================================
    # First-class check alongside PII. Categories: aws_access_key,
    # aws_secret_key, github_token, openai_key, gcp_service_account,
    # private_key_pem, jwt + opt-in generic_high_entropy. Disabled by
    # default per tenant; operators enable it on the default config
    # to roll out secret scanning at their own pace.
    secrets_detection_enabled = Column(Boolean, default=False, server_default=text("false"), nullable=False,
        comment="Enable secrets detection across configured surfaces.")
    secrets_action = Column(String(20), default="redact", server_default="redact", nullable=False,
        comment="Action when a secret is detected: block | redact | warn | allow.")
    secrets_categories = Column(JSONB, nullable=True,
        comment=(
            "Opt-in subset of secret categories. Null falls back to the "
            "documented defaults (the 7 deterministic detectors; "
            "generic_high_entropy is opt-in only)."
        ),
        # Mirrors the migration so fresh installs carry the same DB default.
        server_default=text("'" + json.dumps(DEFAULT_SECRETS_CATEGORIES, separators=(",", ":")) + "'"),
    )
    secrets_surfaces = Column(JSONB(none_as_null=True), nullable=True,
        comment="Subset of per-mode + MCP / Skill / A2A surface tokens on which secrets detection fires.",
    )
    secrets_threshold = Column(Float, default=0.8, nullable=True,
        comment=(
            "Provider confidence threshold. Reserved for the Presidio "
            "extension; the built-in detector is deterministic and "
            "ignores this value."
        ), server_default=text("0.8"))

    # =========================================================================
    # PER-SURFACE OVERRIDES
    # =========================================================================
    #
    # Each `<check>_per_surface` JSONB column carries optional overrides
    # keyed by surface token. When the orchestrator dispatches a check
    # on a specific surface, it consults this map first and falls back
    # to the scalar `<check>_action` / `<check>_provider` / `<check>_threshold`
    # columns above when:
    #   * the column itself is NULL, OR
    #   * the surface key is missing, OR
    #   * the specific field (action / provider / ...) is missing.
    #
    # Wire shape per entry:
    #   {
    #     "chat_input":  {"enabled": true, "action": "block",
    #                     "provider": "bedrock_guardrails", "threshold": 0.7},
    #     "chat_output": {"action": "redact"},
    #     ...
    #   }
    #
    # The same column on banned_words / banned_patterns also accepts
    # `"words"` / `"patterns"` lists, letting an operator have e.g. a
    # "destructive SQL" list only on mcp_input and a "competitor names"
    # list only on chat_output.

    pii_per_surface = Column(
        JSONB, nullable=True,
        comment=(
            "Per-surface PII overrides. See app/services/guardrails/per_surface.py."
        ),
    )
    prompt_injection_per_surface = Column(
        JSONB, nullable=True,
        comment="Per-surface prompt-injection overrides.",
    )
    jailbreak_per_surface = Column(
        JSONB, nullable=True,
        comment="Per-surface jailbreak overrides.",
    )
    toxic_content_per_surface = Column(
        JSONB, nullable=True,
        comment="Per-surface toxic-content overrides (incl. per-surface threshold).",
    )
    secrets_per_surface = Column(
        JSONB, nullable=True,
        comment=(
            "Per-surface secrets overrides. NOTE: secrets does not "
            "support per-surface provider — the built-in detector is "
            "the only provider today; provider key here is ignored."
        ),
    )
    banned_words_per_surface = Column(
        JSONB, nullable=True,
        comment=(
            "Per-surface banned-words overrides. Per-surface 'words' "
            "list REPLACES the global banned_words list for that "
            "surface (override, not merge)."
        ),
    )
    banned_patterns_per_surface = Column(
        JSONB, nullable=True,
        comment=(
            "Per-surface banned-patterns overrides. Per-surface "
            "'patterns' list REPLACES the global banned_patterns list "
            "for that surface."
        ),
    )

    # =========================================================================
    # EXTERNAL PROVIDER CONFIGS
    # =========================================================================

    providers = Column(
        JSON, nullable=True,
        comment="""
        List of external guardrail provider configurations.
        Each entry:
        {
            "provider": "lakera",
            "enabled": true,
            "api_key": "...",
            "endpoint": "https://...",
            "config": { ... }
        }
        A provider has no mode and no surfaces of its own: where it runs
        is the configuration's surfaces and its check's surface list.

        Supported: lakera, presidio, bedrock, openai_moderation, custom
        """
    )

    # =========================================================================
    # RATE LIMITING / PERFORMANCE
    # =========================================================================

    max_check_timeout_ms = Column(Float, default=2000.0,
        comment=(
            "Max time for guardrail checks (ms). A timeout is treated exactly "
            "like a check failure, so `fail_open` governs what happens next."
        ))
    fail_open = Column(Boolean, default=False, server_default="false",
        nullable=False,
        comment=(
            "What happens when a check cannot run (provider error or timeout). "
            "FALSE (default) = fail closed: refuse the call, because a "
            "governance control that silently stopped running is worse than a "
            "refused request. TRUE = log-and-allow: the operator has decided "
            "availability wins for this config. Per-check exceptions go in "
            "fail_open_overrides."
        ))
    log_all_checks = Column(Boolean, default=False,
        comment="Log all checks, not just violations")
    fail_open_overrides = Column(JSON, nullable=True,
        comment="Per-check override of fail_open keyed by check name (pii, prompt_injection, jailbreak, toxic_content).")

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_guardrail_configs_tenant", "tenant_id"),
        Index("ix_guardrail_configs_tenant_default", "tenant_id", "is_default"),
        Index("ix_guardrail_configs_tenant_name", "tenant_id", "name", unique=True),
        {"comment": "Guardrail configurations assignable to resource groups"},
    )

    # =========================================================================
    # VALIDATION
    # =========================================================================

    VALID_ACTIONS = {"block", "redact", "warn", "allow"}

    @validates("pii_action", "prompt_injection_action", "jailbreak_action", "toxic_content_action")
    def validate_action(self, key, value):
        if value and value not in self.VALID_ACTIONS:
            raise ValueError(f"{key} must be one of: {', '.join(self.VALID_ACTIONS)}")
        return value

    # =========================================================================
    # HELPERS
    # =========================================================================

    @property
    def group_ids(self) -> List[str]:
        """Return list of associated resource group IDs."""
        return [g.id for g in (self.groups or [])]

    def surface_flag(self, surface: str, direction: str) -> bool:
        """The surface's own switch, regardless of the config's master switch."""
        # A per-mode token (chat_input, a2a_outbound) already names its direction
        # and is the column prefix; a bare kind (mcp, skill) takes the direction.
        attr = f"{surface}_enabled"
        if not hasattr(self, attr):
            attr = f"{surface}_{direction}_enabled"
        return bool(getattr(self, attr, False))

    def get_active_providers(self, surface: str) -> List[Dict[str, Any]]:
        """Get enabled external providers for a given surface."""
        if not self.providers:
            return []
        return [
            p for p in self.providers
            if p.get("enabled", False) and surface in p.get("surfaces", [])
        ]

    def to_content_policy_kwargs(self) -> Dict[str, Any]:
        """Map to ContentPolicy constructor kwargs."""
        return {
            "detect_email": self.pii_detection_enabled,
            "detect_phone": self.pii_detection_enabled,
            "detect_ssn": self.pii_detection_enabled,
            "detect_credit_card": self.pii_detection_enabled,
            "detect_api_keys": self.pii_detection_enabled,
            "detect_prompt_injection": self.prompt_injection_enabled,
            "prompt_injection_sensitivity": self.prompt_injection_sensitivity or 0.7,
            "detect_toxic_content": self.toxic_content_enabled,
            "toxic_threshold": self.toxic_content_threshold or 0.8,
            "detect_jailbreak": self.jailbreak_detection_enabled,
            "banned_words": self.banned_words or [],
            "banned_patterns": self.banned_patterns or [],
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for API responses."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "is_default": self.is_default,
            "priority": self.priority,
            "enabled": self.enabled,
            "group_ids": self.group_ids,
            # Streaming output-guardrail posture.
            "streaming_output_mode": self.streaming_output_mode or "auto",
            # Per-call scope filters
            "scope_mcp_server_id": self.scope_mcp_server_id,
            "scope_tool_name": self.scope_tool_name,
            "surfaces": {
                # Per-mode LLM surfaces. The admin UI's Surfaces
                # tab reads every one of these — missing keys here mean
                # the form falls back to its hard-coded defaults and
                # the operator's edit silently round-trips back to
                # whatever they last enabled, masking the lack of
                # persistence. Keep this list aligned with
                # GuardrailSurfaceToggles.
                "chat_input": self.chat_input_enabled,
                "chat_output": self.chat_output_enabled,
                "embeddings_input": self.embeddings_input_enabled,
                "image_gen_input": self.image_gen_input_enabled,
                "image_gen_output": self.image_gen_output_enabled,
                "audio_tts_input": self.audio_tts_input_enabled,
                "audio_tts_output": self.audio_tts_output_enabled,
                "audio_stt_input": self.audio_stt_input_enabled,
                "audio_stt_output": self.audio_stt_output_enabled,
                "video_gen_input": self.video_gen_input_enabled,
                "video_gen_output": self.video_gen_output_enabled,
                "batch_input": self.batch_input_enabled,
                "batch_output": self.batch_output_enabled,
                "moderation_input": self.moderation_input_enabled,
                "mcp_input": self.mcp_input_enabled,
                "mcp_output": self.mcp_output_enabled,
                "mcp_registration": self.mcp_registration_enabled,
                "skill_input": self.skill_input_enabled,
                "skill_output": self.skill_output_enabled,
                "a2a_inbound": self.a2a_inbound_enabled,
                "a2a_outbound": self.a2a_outbound_enabled,
            },
            "builtin": {
                "pii": {
                    "enabled": self.pii_detection_enabled,
                    "action": self.pii_action,
                    "provider": self.pii_provider or "built_in",
                    "categories": self.pii_categories,
                    # Forwarded to Presidio's
                    # /analyze.score_threshold. Built-in regex detector
                    # ignores it. May be None (use Presidio's default).
                    "threshold": self.pii_threshold,
                    "surfaces": self.pii_surfaces if self.pii_surfaces is not None else _DEFAULT_SURFACES_ALL,
                    # Per-surface override map. Each entry may
                    # carry {enabled?, action?, provider?, threshold?}
                    # to override the scalars above on a specific
                    # surface. NULL = no overrides; orchestrator falls
                    # through to the scalars for every surface.
                    "per_surface": self.pii_per_surface,
                },
                "prompt_injection": {
                    "enabled": self.prompt_injection_enabled,
                    "action": self.prompt_injection_action,
                    "sensitivity": self.prompt_injection_sensitivity,
                    "provider": self.prompt_injection_provider or "built_in",
                    "surfaces": self.prompt_injection_surfaces if self.prompt_injection_surfaces is not None else _DEFAULT_SURFACES_INPUT_ONLY,
                    "per_surface": self.prompt_injection_per_surface,
                },
                "jailbreak": {
                    "enabled": self.jailbreak_detection_enabled,
                    "action": self.jailbreak_action,
                    "provider": self.jailbreak_provider or "built_in",
                    "surfaces": self.jailbreak_surfaces if self.jailbreak_surfaces is not None else _DEFAULT_SURFACES_INPUT_ONLY,
                    "per_surface": self.jailbreak_per_surface,
                },
                "toxic_content": {
                    "enabled": self.toxic_content_enabled,
                    "action": self.toxic_content_action,
                    "threshold": self.toxic_content_threshold,
                    # Default to "built_in" (bundled profanity wordlist)
                    # so an unconfigured / NULL row works out of the
                    # box. OpenAI Moderation requires an API key the
                    # operator must add via External Providers first.
                    "provider": self.toxic_content_provider or "built_in",
                    "category_thresholds": self.toxic_content_category_thresholds,
                    "surfaces": self.toxic_content_surfaces if self.toxic_content_surfaces is not None else _DEFAULT_SURFACES_ALL,
                    "per_surface": self.toxic_content_per_surface,
                },
                "banned_words": self.banned_words or [],
                "banned_patterns": self.banned_patterns or [],
                "banned_words_match_mode": self.banned_words_match_mode or "substring",
                "banned_words_surfaces": self.banned_words_surfaces if self.banned_words_surfaces is not None else _DEFAULT_SURFACES_LLM_MCP,
                "banned_patterns_surfaces": self.banned_patterns_surfaces if self.banned_patterns_surfaces is not None else _DEFAULT_SURFACES_LLM_MCP,
                # Banned words/patterns per-surface entries can
                # carry a `words` / `patterns` list that REPLACES the
                # global list for that surface. NULL = use the globals
                # on every surface in the surfaces[] list above.
                "banned_words_per_surface": self.banned_words_per_surface,
                "banned_patterns_per_surface": self.banned_patterns_per_surface,
                "secrets": {
                    "enabled": self.secrets_detection_enabled if self.secrets_detection_enabled is not None else False,
                    "action": self.secrets_action or "redact",
                    "categories": self.secrets_categories if self.secrets_categories is not None else list(DEFAULT_SECRETS_CATEGORIES),
                    "surfaces": self.secrets_surfaces if self.secrets_surfaces is not None else _DEFAULT_SURFACES_ALL,
                    "threshold": self.secrets_threshold if self.secrets_threshold is not None else 0.8,
                    # Per-surface override. {action?, threshold?,
                    # enabled?, categories?}. Provider key here is
                    # ignored — secrets has only the built-in detector
                    # today; that may change in a future release.
                    "per_surface": self.secrets_per_surface,
                },
            },
            "providers": [_public_provider_entry(p) for p in (self.providers or [])],
            "max_check_timeout_ms": self.max_check_timeout_ms,
            "fail_open": self.fail_open,
            "fail_open_overrides": self.fail_open_overrides,
            "log_all_checks": self.log_all_checks,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @staticmethod
    def default_config(tenant_id: str) -> "GuardrailConfig":
        """Return an in-memory default config (not persisted)."""
        return GuardrailConfig(
            id=f"guardrail-default-{tenant_id}",
            tenant_id=tenant_id,
            name="Default Guardrails",
            is_default=True,
            enabled=False,
        )

    def __repr__(self):
        return (
            f"<GuardrailConfig(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"name={self.name!r}, enabled={self.enabled}, "
            f"is_default={self.is_default}, groups={len(self.groups or [])})>"
        )
