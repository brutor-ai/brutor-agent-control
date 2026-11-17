# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unified Proxy Log Database Model
Single table for all proxy types: MCP, LLM, OAuth, Virtual MCP
"""

from typing import Dict, Any, Optional
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, Column, String, Integer, Float, Boolean, DateTime, Text, Index, ForeignKey, JSON, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func, text

from app.database import Base


class ProxyLog(Base):
    """
    Unified proxy request/response logs for all proxy types

    Supports:
    - MCP Proxy (mcp, virtual_mcp)
    - LLM Proxy (llm)
    - OAuth Proxy (oauth)

    Design principles:
    - Common fields for all proxy types
    - Type-specific fields stored in JSON extras
    - Optimized indexes for cross-proxy queries
    - Thread tracking via correlation_id
    """

    __tablename__ = 'proxy_logs'

    # ==========================================================================
    # Core Identification
    # ==========================================================================

    id = Column(
        String(255),
        primary_key=True,
        comment="Unique log entry ID"
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant this record belongs to"
    )

    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
        comment="Request timestamp"
    )

    # ==========================================================================
    # Proxy Type Classification
    # ==========================================================================

    proxy_type = Column(
        String(20),
        nullable=False,
        index=True,
        comment="Proxy type: mcp, llm, oauth, virtual_mcp"
    )

    proxy_subtype = Column(
        String(50),
        nullable=True,
        comment="Subtype: mcp_sse, mcp_jsonrpc, llm_chat, llm_completion, oauth_discovery, oauth_token"
    )

    # ==========================================================================
    # Agent Skills Attribution
    # ==========================================================================

    skill_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Agent skill that initiated this proxy request (NULL if not skill-triggered)"
    )

    skill_execution_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Skill execution ID for cross-table correlation"
    )

    # ==========================================================================
    # Request Correlation & Tracing
    # ==========================================================================

    correlation_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Correlation ID for tracking request chains (e.g., OAuth flow)"
    )

    parent_request_id = Column(
        String(255),
        nullable=True,
        comment="Parent request ID for nested/chained requests"
    )

    request_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Client-provided request ID (X-Request-ID header)"
    )

    # ==========================================================================
    # Trace / Call-Chain
    #
    # The HMAC-signed delegation chain originally built for A2A
    # (services::a2a_delegation in gateway-core) is now the gateway's
    # canonical call-chain primitive across every governance surface
    # (LLM, MCP, A2A, Skill). Only the gateway, which holds the
    # per-tenant signing key, can mint or extend a valid chain — so the
    # trace identifier carries forensic guarantees beyond what a plain
    # correlation ID does.
    #
    # Chain identity columns are root_task_id / current_task_id /
    # parent_task_id / delegation_depth (defined further down this file,
    # added in an earlier migration for A2A). The two columns below are
    # the only new ones the generalisation requires:
    # ==========================================================================

    trace_root_kind = Column(
        String(32),
        nullable=True,
        comment="llm_user | agent_invocation | skill_execution | scheduled_job | external_api",
    )

    trace_continuity = Column(
        String(32),
        nullable=True,
        comment="How this row joined the chain: minted | verified | client_dropped | sig_failed | client_asserted | external",
    )

    # Where this step was witnessed. `gateway` rows were proxied: the
    # gateway sat in the path and every control it applied is on the row.
    # `otel` rows were ingested from an OpenTelemetry GenAI export: the step
    # is the exporter's account of what happened, with no gateway control
    # evidence behind it. Kept on every row so a report can never mistake
    # reported traffic for witnessed traffic.
    source = Column(
        String(16),
        nullable=False,
        default="gateway",
        server_default="gateway",
        comment="gateway (proxied) | otel (ingested telemetry)",
    )

    # The sealed action record that vouches for this row.
    # NULL on rows the core did not seal (queries, ungoverned traffic, rows
    # older than the programme) — and `records.every_verdict_sealed`
    # counts governed rows where it is NULL, so absence is a finding, not
    # a default.
    record_id = Column(
        String(64),
        nullable=True,
        comment="action_records.record_id of the sealed evidence record for this row",
    )

    # "Explicit" run finalization. Set from the
    # `X-Brutor-Run-End` header on the step that carried it, so the marker and
    # the step are the same row and cannot be reordered by the batched writer.
    # NULL on every step where the client did not signal an end, which is most
    # of them. See the migration for why this is a column rather than a
    # message on the telemetry channel.
    run_end_state = Column(
        String(24),
        nullable=True,
        comment=(
            "Terminal state requested by the client via X-Brutor-Run-End on this "
            "step: completed | completed_degraded | exhausted | errored | "
            "abandoned | cancelled"
        ),
    )

    # The `X-Brutor-Outcome-Id` the client sent on this step
    # (verbatim, truncated to 128), on the same row as the run-end marker so
    # the sweeper's backstop finalize keeps the outcome. NULL on every other
    # step.
    outcome_id = Column(
        String(128),
        nullable=True,
        comment="X-Brutor-Outcome-Id carried by this step (verbatim)",
    )

    # Data residency.
    region_routed_to = Column(
        String(32),
        nullable=True,
        comment=(
            "Region of the provider/server the request actually "
            "reached. NULL when the request was blocked, the "
            "provider has no region set, or the tenant has no "
            "residency profile."
        ),
    )
    residency_class = Column(
        String(32),
        nullable=True,
        comment=(
            "Outcome of the residency check for this request: "
            "allowed | blocked | not_evaluated. NULL: not recorded."
        ),
    )

    # Compliance framework tags. Set per-row
    # by the tagging engine (app/services/compliance/tagging.py).
    # NULL on rows from tenants whose compliance_profile.frameworks
    # is empty — the engine short-circuits and never touches the
    # column. Shape: ["gdpr_art_30", "soc2.cc6.1", "hipaa.phi", ...].
    compliance_tags = Column(
        JSONB,
        nullable=True,
        comment=(
            "Interned compliance regime tags fired by the tagging "
            "engine. NULL = no compliance profile or no tags fired. "
            "See app/services/compliance/tagging.py."
        ),
    )

    # Orchestrator outcome surface for the compliance tagger.
    # All four default to false. The Rust engine
    # treats false as "didn't fire", same shape as the existing
    # `residency_blocked` derivation off `residency_class`.
    guardrail_blocked = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment="Any guardrail check returned BLOCK. SOC 2 CC7.2 driver.",
    )
    argument_policy_denied = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment="Argument policy engine returned DENY. SOC 2 CC6.6 driver.",
    )
    pii_detected = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment=(
            "PII detector found at least one category. GDPR Article 30 "
            "+ HIPAA PHI driver (when categories overlap PHI set)."
        ),
    )
    is_auth_event = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment="Auth-related row (token issue, login). SOC 2 CC6.1 driver.",
    )

    # ==========================================================================
    # User Context (Common to all proxies)
    # ==========================================================================

    gateway_user_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Gateway user ID (from X-Gateway-Authorization header)"
    )

    gateway_user_username = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Gateway username (from X-Gateway-Authorization header)"
    )

    idp_user_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="IdP user ID (from JWT/API key)"
    )

    idp_user_username = Column(
        String(255),
        nullable=True,
        index=True,
        comment="IdP user username"
    )

    idp_user_email = Column(
        String(255),
        nullable=True,
        index=True,
        comment="IdP user email"
    )

    end_user_session_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Session ID for request grouping"
    )

    resource_group_id = Column(
        String(255),
        nullable=True,
        comment="Resource group ID for per-group analytics"
    )

    # Per-key attribution. NULL on JWT-authed requests.
    # The "API Key Usage" report aggregates llm_total_tokens by api_key_id.
    api_key_id = Column(
        String(255),
        nullable=True,
        comment="FK-shaped to api_keys.id. NULL on JWT-authed requests.",
    )

    api_key_name = Column(
        String(255),
        nullable=True,
        comment="Snapshot of api_key.name at request time.",
    )

    client_ip = Column(
        String(45),
        nullable=True,
        comment="Client IP address (IPv6 compatible)"
    )

    user_agent = Column(
        Text,
        nullable=True,
        comment="User agent string"
    )

    # ==========================================================================
    # Target Server/Resource
    # ==========================================================================

    server_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Target server ID (MCP/LLM server)"
    )

    idp_server_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Identity Provider ID (for OAuth)"
    )

    # ==========================================================================
    # Request Details
    # ==========================================================================

    http_method = Column(
        String(10),
        nullable=False,
        comment="HTTP method: GET, POST, etc."
    )

    endpoint = Column(
        Text,
        nullable=False,
        comment="Request endpoint/path"
    )

    target_url = Column(
        Text,
        nullable=True,
        comment="Full target URL (if proxied to external service)"
    )

    request_size_bytes = Column(
        Integer,
        nullable=True,
        comment="Request body size in bytes"
    )

    request_headers = Column(
        Text,
        nullable=True,
        comment="Request headers (JSON string, sanitized)"
    )

    request_body_preview = Column(
        Text,
        nullable=True,
        comment="Request body as stored under the tenant's data_retention.store_bodies policy (full / PII-redacted / none), capped at 64 KiB"
    )

    # ==========================================================================
    # Response Details
    # ==========================================================================

    http_status_code = Column(
        Integer,
        nullable=True,
        index=True,
        comment="HTTP response status code"
    )

    response_size_bytes = Column(
        Integer,
        nullable=True,
        comment="Response body size in bytes"
    )

    response_headers = Column(
        Text,
        nullable=True,
        comment="Response headers (JSON string, sanitized)"
    )

    response_body_preview = Column(
        Text,
        nullable=True,
        comment="Response body as stored under the tenant's data_retention.store_bodies policy (full / PII-redacted / none), capped at 64 KiB"
    )

    # ==========================================================================
    # Success/Error Status
    # ==========================================================================

    success = Column(
        Boolean,
        nullable=False,
        index=True,
        comment="Whether request was successful"
    )

    error_message = Column(
        Text,
        nullable=True,
        comment="Error message if request failed"
    )

    error_type = Column(
        String(100),
        nullable=True,
        index=True,
        comment="Error type/category"
    )

    duration_ms = Column(
        Float,
        nullable=True,
        comment="Request duration in milliseconds")

    # ==========================================================================
    # MCP-Specific Fields
    # ==========================================================================

    mcp_method = Column(
        String(100),
        nullable=True,
        index=True,
        comment="MCP protocol method (tools/call, resources/read, etc.)"
    )

    mcp_request_id = Column(
        String(255),
        nullable=True,
        comment="MCP JSONRPC request ID"
    )

    mcp_capability_name = Column(
        String(255),
        nullable=True,
        index=True,
        comment="MCP tool/resource/prompt name"
    )

    # ==========================================================================
    # LLM-Specific Fields
    # ==========================================================================

    llm_model_requested = Column(
        String(200),
        nullable=True,
        index=True,
        comment="LLM model requested by client"
    )

    llm_model_used = Column(
        String(200),
        nullable=True,
        index=True,
        comment="Actual LLM model used (after routing)"
    )

    llm_provider = Column(
        String(100),
        nullable=True,
        comment="LLM provider: openai, anthropic, etc."
    )

    llm_prompt_tokens = Column(
        Integer,
        nullable=True,
        comment="Number of prompt tokens"
    )

    llm_completion_tokens = Column(
        Integer,
        nullable=True,
        comment="Number of completion tokens"
    )

    llm_total_tokens = Column(
        Integer,
        nullable=True,
        comment="Total tokens used"
    )

    llm_streaming = Column(
        Boolean,
        nullable=True,
        comment="Whether streaming was used"
    )

    llm_temperature = Column(
        Float,
        nullable=True,
        comment="Temperature parameter"
    )

    llm_max_tokens = Column(
        Integer,
        nullable=True,
        comment="Max tokens parameter"
    )

    llm_function_calling = Column(
        Boolean,
        default=False,
        comment="Whether function calling was used"
    )

    llm_used_fallback = Column(
        Boolean,
        default=False,
        comment="Whether fallback provider was used"
    )

    llm_fallback_reason = Column(
        String(100),
        nullable=True,
        comment="Reason for fallback"
    )

    llm_estimated_cost_usd = Column(
        Float,
        nullable=True,
        comment="Estimated cost in USD"
    )

    # ==========================================================================
    # Media Unit Counters
    #
    # The token columns above only carry meaning for chat / completion /
    # responses / embedding modes. Media modes (image_generation,
    # audio_speech, audio_transcription, video_generation) bill in their
    # OWN units — images, characters, seconds — and lumping them into a
    # token column would either skew per-token analytics (wrong) or hide
    # the work entirely (also wrong; operators currently see a "$0.79
    # spent on 481 LLM requests, ~107K tokens" line that doesn't account
    # for the dall-e-3 / luma / whisper traffic at all).
    #
    # Each row carries the counter that matches its mode. Non-applicable
    # counters stay NULL so SUM() ignores them, and per-mode aggregation
    # boils down to a single GROUP BY on `proxy_subtype` (or on the
    # joined llm_model.mode where subtype isn't enough).
    #
    # The cost contribution lives in `llm_estimated_cost_usd` regardless
    # of mode — that's already mode-agnostic — so the dashboard's "$ in
    # media" line is just `SUM(llm_estimated_cost_usd) WHERE
    # proxy_subtype IN (media subtypes)`.
    # ==========================================================================

    media_images_generated = Column(
        Integer,
        nullable=True,
        comment="Number of images returned by an image_generation request"
    )

    media_audio_chars = Column(
        BigInteger,
        nullable=True,
        comment="TTS input character count (audio_speech mode)"
    )

    media_audio_seconds_input = Column(
        Float,
        nullable=True,
        comment="Audio duration in seconds processed by an audio_transcription request"
    )

    media_video_seconds_output = Column(
        Float,
        nullable=True,
        comment="Output video clip duration in seconds (video_generation mode)"
    )

    # ==========================================================================
    # LLM Cache Fields
    # ==========================================================================

    llm_cache_hit = Column(
        Boolean,
        nullable=True,
        comment="Whether this request was served from semantic cache"
    )

    llm_cache_layer = Column(
        String(20),
        nullable=True,
        comment="Cache layer that served the response: exact, semantic, shared"
    )

    llm_tokens_saved = Column(
        Integer,
        nullable=True,
        comment="Number of tokens saved by cache hit (original request token count)"
    )

    llm_cache_similarity_score = Column(
        Float,
        nullable=True,
        comment="Cosine similarity score for semantic cache hits (0.0-1.0)"
    )

    # ==========================================================================
    # Prompt Cache Fields (provider-side prompt caching — Anthropic, OpenAI)
    #
    # Mirrors the three columns added by Alembic migration
    # alembic/versions/20260420_1600_a003_prompt_cache.py. Nullable so
    # adapters that don't report cache usage (Ollama, Replicate, etc.)
    # insert cleanly. Keep this block in sync with that migration —
    # fresh-DB installs build the schema from this model via
    # `Base.metadata.create_all`, so a missing column here is a silent
    # insert failure on every subsequent proxy write.
    # ==========================================================================

    prompt_cache_write_tokens = Column(
        Integer,
        nullable=True,
        comment="Tokens written to provider prompt cache on this request"
    )

    prompt_cache_read_tokens = Column(
        Integer,
        nullable=True,
        comment="Tokens served from provider prompt cache on this request"
    )

    prompt_cache_savings_usd = Column(
        Numeric(precision=12, scale=6),
        nullable=True,
        comment="Estimated USD saved by prompt-cache hits on this request"
    )

    llm_context_tokens = Column(
        Integer,
        nullable=True,
        comment=(
            "Context this call sent its model, cached tokens included "
            "(prompt_cache::context_tokens). NULL = not measured or not a "
            "model call: an embedding or a response-cache hit"
        ),
    )

    # ==========================================================================
    # RAG Knowledge Base Fields
    # ==========================================================================

    rag_enabled = Column(
        Boolean,
        nullable=True,
        comment="Whether RAG context was injected for this request"
    )

    rag_chunks_retrieved = Column(
        Integer,
        nullable=True,
        comment="Number of document chunks retrieved from Qdrant"
    )

    rag_context_tokens = Column(
        Integer,
        nullable=True,
        comment="Approximate token count of injected RAG context"
    )

    rag_collection_ids = Column(
        String(1024),
        nullable=True,
        comment="Comma-separated KB collection IDs that were searched"
    )

    # SHA-256 of the instruction text in the request. A fingerprint,
    # never the prompt: it exists so the drift cause ladder can say "the
    # instructions changed at 14:02" without anyone storing what they were.
    prompt_fingerprint = Column(
        String(64),
        nullable=True,
        comment="SHA-256 of the instruction text sent; a hash, never the prompt",
    )

    rag_top_similarity_score = Column(
        Float,
        nullable=True,
        comment="Highest cosine similarity score among retrieved chunks"
    )

    rag_retrieval_duration_ms = Column(
        Float,
        nullable=True,
        comment="Time spent on KB retrieval (embedding + Qdrant search) in ms"
    )

    # ==========================================================================
    # OAuth-Specific Fields
    # ==========================================================================

    oauth_flow_type = Column(
        String(50),
        nullable=True,
        comment="OAuth flow: authorization_code, refresh_token, client_credentials"
    )

    oauth_scope = Column(
        Text,
        nullable=True,
        comment="OAuth scopes requested/granted"
    )

    # ==========================================================================
    # A2A Delegation
    # ==========================================================================
    # Four columns together identify a step in the delegation graph:
    #   * root_task_id     — groups steps of the same chain
    #   * delegation_depth — 0 for the root step
    #   * current_task_id  — this step's own ULID
    #   * parent_task_id   — the step that invoked us; null at root
    # All nullable — populated only for proxy_type='a2a'.

    root_task_id = Column(
        String(64),
        nullable=True,
        comment="Root A2A task ULID; null for non-A2A traffic",
    )

    delegation_depth = Column(
        Integer,
        nullable=True,
        comment="0 for root step, incremented per downstream step; null for non-A2A",
    )

    current_task_id = Column(
        String(64),
        nullable=True,
        comment="ULID the gateway minted for this A2A step; null for non-A2A",
    )

    parent_task_id = Column(
        String(64),
        nullable=True,
        comment="current_task_id of the step that invoked this one; null at root / non-A2A",
    )

    # v1.0 upgrade — dialect used for this step ("0.3" or "1.0"). Drives
    # Mission Control's migration-progress view. Nullable: non-A2A
    # traffic leaves it unset.
    a2a_protocol_version = Column(
        String(8),
        nullable=True,
        comment="A2A dialect used for this step: '0.3' or '1.0'; null for non-A2A.",
    )

    # The terminal A2A task state the caller
    # received, normalised by the core (TASK_STATE_ prefix dropped,
    # lower-case, '-' -> '_'). Written on outbound A2A rows only; the run
    # segments and the external boundary metrics both read it.
    a2a_task_state = Column(
        String(32),
        nullable=True,
        comment="Terminal A2A task state the caller received (normalised); "
        "outbound A2A rows only",
    )

    # WHAT software made the call and WHICH
    # release, resolved by the core from the first carrier present, in
    # order of trust: verified token claims, MCP clientInfo, OpenTelemetry
    # baggage, Brutor headers. Normalised and bounded by the core (anything
    # outside the bounds is dropped, never stored raw). Describes only —
    # never read by an access decision.
    agent_impl_name = Column(
        String(64),
        nullable=True,
        comment="Declared/verified agent implementation name",
    )
    agent_impl_version = Column(
        String(64),
        nullable=True,
        comment="Agent implementation version",
    )
    agent_impl_build = Column(
        String(128),
        nullable=True,
        comment="Agent implementation build (git SHA or image digest)",
    )
    agent_impl_source = Column(
        String(16),
        nullable=True,
        comment="Carrier the release came from: token | mcp_client_info | baggage | header",
    )
    # The client is a CHANNEL: X-Brutor-Client, else the
    # first User-Agent product name without its version. `user_agent` keeps
    # the raw header.
    client_label = Column(
        String(64),
        nullable=True,
        comment="Normalised client label; raw user_agent unchanged",
    )

    # ── Agent Identity (subject/actor) ───────────────────────────────────
    # When the request's actor is an agent principal, these capture WHO acted
    # (the agent) and on whose behalf (the delegating subject), plus the
    # per-agent runtime-authz decision for evidence. All nullable — set only
    # when an agent principal is present.
    actor_agent_id = Column(
        String(255),
        nullable=True,
        comment="Agent Identity that performed this action (the actor); null for human/api-key callers",
    )
    subject_id = Column(
        String(255),
        nullable=True,
        comment="Delegating principal on whose behalf the agent acted (human user id or upstream agent id)",
    )
    subject_kind = Column(
        String(16),
        nullable=True,
        comment="Kind of subject: 'user' | 'agent' | 'service'; null when no agent actor",
    )
    agent_authz_decision = Column(
        String(20),
        nullable=True,
        comment="Per-agent runtime-authz outcome: 'allow' | 'approval_required' | 'deny'; null when no agent actor",
    )
    agent_grant_id = Column(
        String(255),
        nullable=True,
        comment="The agent_grant that drove the decision, when the agent plane decided",
    )
    semantic_policy_id = Column(
        String(255),
        nullable=True,
        comment="The semantic governance policy that fired (block or shadow), when SGP decided",
    )

    # --------------------------------------------------------------------------
    # Turn identity. Mirrors its migration exactly — String(64) /
    # Integer, both nullable. A fresh install builds this table from the model,
    # not from the migrations, so a column that lives only in Alembic exists on
    # every upgraded database and on none of the new ones.
    # --------------------------------------------------------------------------

    step_id = Column(
        String(64),
        nullable=True,
        comment=(
            "Client-asserted logical phase (X-Brutor-Step-Id). Actions sharing "
            "one belong to the same step of the plan; NULL means the client "
            "asserted no phase — steps are never inferred"
        ),
    )
    step_name = Column(
        String(128),
        nullable=True,
        comment="Optional display label for the step (X-Brutor-Step-Name)",
    )
    turn_id = Column(
        String(64),
        nullable=True,
        comment=(
            "Client-asserted turn identifier (X-Brutor-Turn-Id). "
            "Actions sharing one belong to the same pass of the agent loop; NULL "
            "means turns are inferred from model-call boundaries"
        ),
    )
    turn_seq = Column(
        Integer,
        nullable=True,
        comment=(
            "Optional client-supplied turn ordinal within the run, so turns "
            "can be ordered without relying on batched-writer timestamps"
        ),
    )

    # --------------------------------------------------------------------------
    # Decision record — the minimum action-evidence bundle.
    #
    # Each of these existed at the moment of the decision and was discarded
    # before the row was written, which left the audit trail able to say that
    # *something* denied a call but not which thing. NULL means "no such
    # decision was taken on this call", never "we didn't bother".
    # --------------------------------------------------------------------------

    argument_policy_id = Column(
        String(64),
        nullable=True,
        comment=(
            "Argument policy that produced the decision; pairs with the "
            "argument_policy_denied flag, which says only that something did"
        ),
    )
    guardrail_config_id = Column(
        String(64),
        nullable=True,
        comment="Guardrail config whose check fired; null when no guardrail hit",
    )
    guardrail_check = Column(
        String(48),
        nullable=True,
        comment=(
            "Which check fired: pii | prompt_injection | jailbreak | "
            "toxic_content | banned_words | banned_pattern | secrets"
        ),
    )
    guardrail_action = Column(
        String(16),
        nullable=True,
        comment=(
            "What it did: block | redact | warn. Separate from guardrail_blocked "
            "because a redaction changed what went upstream without blocking, and "
            "a warn changed nothing — the boolean collapsed three outcomes into one"
        ),
    )
    guardrails_degraded = Column(
        JSONB,
        nullable=True,
        comment=(
            "Checks that RAN but on a lesser rung of the resilience "
            "ladder: [{config, check, tier}] where tier is retried|fallback. "
            "NULL on the happy path. Deliberately separate from "
            "guardrails_skipped: that column means the check did not run, "
            "this one means it ran but not on the detector you configured. "
            "Merging them would either count a lower-fidelity pass as a full "
            "pass, or count a real inspection as none at all."
        ),
    )
    guardrails_skipped = Column(
        JSONB,
        nullable=True,
        comment=(
            "Checks CONFIGURED but not run on this call, because the "
            "detector failed and the config opted into log-and-allow "
            "(fail_open=true). [{config, check, reason}]; NULL when every "
            "configured check ran. The other guardrail_* fields describe a "
            "check that fired; this is the only thing separating 'checked and "
            "clean' from 'never checked', without which the assurance report "
            "would claim coverage the call never had."
        ),
    )
    autonomy_level = Column(
        String(24),
        nullable=True,
        comment=(
            "Autonomy in force: autonomous | approval_required | restricted | "
            "suspended; null when denied before the autonomy gate ran"
        ),
    )
    autonomy_provenance = Column(
        String(24),
        nullable=True,
        comment=(
            "How autonomy_level was obtained: live | last_known_good | floor. "
            "Anything but 'live' means the proxy enforced on degraded information"
        ),
    )
    consumed_approval_request_id = Column(
        String(64),
        nullable=True,
        comment=(
            "tool_approval_requests row whose one-time token authorised this "
            "call — set on the retried call, which is what makes approval -> "
            "action walkable forwards"
        ),
    )
    pre_action_state = Column(
        JSONB,
        nullable=True,
        comment=(
            "State the quota/budget decision was taken against, as "
            "{kind, ...observed aggregates}. Limits are not duplicated — they "
            "are pinned by the AI System's contract_hash. NULL when no quota "
            "decision was taken on this call"
        ),
    )

    # --------------------------------------------------------------------------
    # Tamper evidence.
    #
    # Each row's hash covers its predecessor, so altering any row breaks every
    # hash after it. The writer signs batch heads into audit_chain_checkpoints;
    # forging a row therefore requires forging every later hash AND a signature.
    # A row with NULL here verifies as "unchained" — honest, and not the
    # same claim as "verified".
    # --------------------------------------------------------------------------

    chain_id = Column(
        String(64),
        nullable=True,
        comment="Writer chain this row belongs to (one per proxy process)",
    )
    chain_seq = Column(
        BigInteger,
        nullable=True,
        comment="Dense position within chain_id; a gap means rows were deleted",
    )
    prev_hash = Column(
        String(64),
        nullable=True,
        comment="row_hash of the preceding row; genesis carries 64 zeroes",
    )
    row_hash = Column(
        String(64),
        nullable=True,
        comment="SHA-256 over this row's canonical bytes plus prev_hash",
    )
    # The canonical format the row was sealed under. The verifier
    # dispatches on it; NULL = v1/v2 (recovered by
    # recomputation because the tag is the first field inside the hashed
    # bytes). Editing it cannot re-label a row: the tag is inside the hash.
    chain_version = Column(
        String(8),
        nullable=True,
        comment=(
            "Canonical format the row hash was sealed under (v3 or v4); "
            "NULL = v1/v2, recovered by recomputation"
        ),
    )

    # Data retention / erasure markers. Bodies and headers are NOT
    # covered by the audit hash chain (see audit_chain::CoveredFields), so
    # removing them leaves `row_hash` verifiable. These timestamps are how
    # the UI tells "never captured" from "removed by policy" from "erased
    # on request" — three different facts that all render as an empty body.
    bodies_pruned_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the retention sweeper removed bodies/headers from this row",
    )
    erased_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When a data-subject erasure scrubbed this row's personal data",
    )

    upstream_request_id = Column(
        String(128),
        nullable=True,
        comment=(
            "Upstream provider's own request id (OpenAI/Anthropic x-request-id); "
            "the only join between our audit trail and the vendor's"
        ),
    )

    # ==========================================================================
    # Content digests, trace context, tool provenance, deciding model.
    # Written by the Rust core; the control plane only reads them.
    # ==========================================================================
    request_content_hash = Column(
        String(64),
        nullable=True,
        comment=(
            "SHA-256 (hex) of the request content as the gateway captured it, "
            "sealed before any store_bodies mode or body prune; covered by row_hash"
        ),
    )
    response_content_hash = Column(
        String(64),
        nullable=True,
        comment="SHA-256 (hex) of the response content as captured; covered by row_hash",
    )
    trace_id = Column(
        String(32),
        nullable=True,
        comment="W3C trace id the caller asserted (traceparent), or the OTel span's own; not hashed",
    )
    span_id = Column(
        String(16),
        nullable=True,
        comment="W3C span id the caller asserted, or the OTel span's own; not hashed",
    )
    server_version = Column(
        String(64),
        nullable=True,
        comment="Version the MCP server announced at initialize; covered by row_hash",
    )
    tool_definition_hash = Column(
        String(64),
        nullable=True,
        comment="SHA-256 of the canonical tool definition in force for this call; covered by row_hash",
    )
    deciding_model = Column(
        String(255),
        nullable=True,
        comment=(
            "Model whose completion decided this action (the run's most recent "
            "model call); derived at write time, not hashed"
        ),
    )

    # ==========================================================================
    # Additional Metadata (Type-Specific)
    # ==========================================================================

    custom_metadata = Column(
        JSON,
        nullable=True,
        comment="Additional type-specific metadata (JSON)"
    )

    tags = Column(
        JSON,
        nullable=True,
        comment="Searchable tags for filtering (JSON array)"
    )

    # ==========================================================================
    # Audit Fields
    # ==========================================================================

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Record creation timestamp"
    )

    # ==========================================================================
    # Indexes for Performance
    # ==========================================================================

    __table_args__ = (
        # The carrier vocabulary is a wire contract with the core.
        CheckConstraint(
            "agent_impl_source IN ('token', 'mcp_client_info', 'baggage', 'header')",
            name="ck_proxy_logs_agent_impl_source",
        ),
        # The canonical formats the verifier knows how to dispatch.
        CheckConstraint(
            "chain_version IN ('v1', 'v2', 'v3', 'v4')",
            name="ck_proxy_logs_chain_version",
        ),
        # ==========================
        # Audit chain — MUST be declared here, not only in the
        # migration. A fresh install builds this table with create_all from
        # the model, so an index that exists only in Alembic is absent on
        # every new deployment: the partial unique index is what makes a gap
        # in `chain_seq` mean "rows were deleted" rather than "two writers
        # may have collided", and without it the tamper-evidence claim is
        # unbacked on exactly the installs nobody has inspected yet.
        Index(
            'uq_proxy_logs_chain_seq',
            'chain_id',
            'chain_seq',
            unique=True,
            postgresql_where=text('chain_id IS NOT NULL'),
        ),

        # Decision record and turn layer — same reasoning as
        # the audit-chain index above: migration-only indexes are absent on
        # every fresh install, because create_all builds from this model.
        Index(
            'ix_proxy_logs_consumed_approval',
            'tenant_id', 'consumed_approval_request_id',
            postgresql_where=text('consumed_approval_request_id IS NOT NULL'),
        ),
        Index(
            'ix_proxy_logs_guardrail_config',
            'tenant_id', 'guardrail_config_id', 'timestamp',
            postgresql_where=text('guardrail_config_id IS NOT NULL'),
        ),
        Index(
            'ix_proxy_logs_autonomy_degraded',
            'tenant_id', 'timestamp',
            postgresql_where=text(
                "autonomy_provenance IS NOT NULL AND autonomy_provenance <> 'live'"
            ),
        ),
        Index(
            'ix_proxy_logs_turn',
            'tenant_id', 'root_task_id', 'turn_id',
            postgresql_where=text('turn_id IS NOT NULL'),
        ),

        # Trajectory search — the two that keep
        # Run > Step > Turn > Action a GROUP BY rather than a table scan.
        Index('ix_proxy_logs_run_traj', 'tenant_id', 'root_task_id', 'timestamp', 'id'),
        Index(
            'ix_proxy_logs_step',
            'tenant_id', 'root_task_id', 'step_id',
            postgresql_where=text('step_id IS NOT NULL'),
        ),

        # Join from a caller's own trace to the rows it produced.
        Index(
            'ix_proxy_logs_trace',
            'tenant_id', 'trace_id',
            postgresql_where=text('trace_id IS NOT NULL'),
        ),

        # The run-ledger sweeper's candidate scan. It re-aggregates 24h
        # of rows every 30 seconds looking for runs to finalize, and the only
        # index that could serve it led with `tenant_id`, which a scan starting
        # from time cannot use. Declared here as well as in the migration
        # because fresh installs build this table with create_all.
        Index(
            'ix_proxy_logs_sweep',
            'timestamp', 'tenant_id', 'root_task_id',
            postgresql_where=text('root_task_id IS NOT NULL'),
            postgresql_include=['resource_group_id', 'id'],
        ),

        # ==========================
        # Common Cross-Proxy Queries
        # ==========================

        # Time-series queries (most common)
        Index('ix_upl_tenant_timestamp', 'tenant_id', 'timestamp'),
        # The scheduled export reads in the order rows were written.
        Index('ix_upl_tenant_created_at', 'tenant_id', 'created_at', 'id'),
        Index('ix_upl_tenant_proxy_timestamp', 'tenant_id', 'proxy_type', 'timestamp'),

        # Success/failure analysis
        Index('ix_upl_tenant_success_timestamp', 'tenant_id', 'success', 'timestamp'),
        Index('ix_upl_tenant_status_timestamp', 'tenant_id', 'http_status_code', 'timestamp'),

        # Performance analysis - "Show slow requests"
        Index('ix_upl_tenant_duration', 'tenant_id', 'duration_ms', 'timestamp'),

        # User activity tracking (cross-proxy) - Use gateway/idp fields
        Index('ix_upl_tenant_gateway_user_timestamp', 'tenant_id', 'gateway_user_id', 'timestamp'),
        Index('ix_upl_tenant_gateway_username_timestamp', 'tenant_id', 'gateway_user_username', 'timestamp'),
        Index('ix_upl_tenant_idp_user_timestamp', 'tenant_id', 'idp_user_id', 'timestamp'),
        Index('ix_upl_tenant_idp_username_timestamp', 'tenant_id', 'idp_user_username', 'timestamp'),
        Index('ix_upl_tenant_session_timestamp', 'tenant_id', 'end_user_session_id', 'timestamp'),

        # Request correlation (thread tracking)
        Index('ix_upl_tenant_correlation', 'tenant_id', 'correlation_id', 'timestamp'),
        Index('ix_upl_tenant_parent_request', 'tenant_id', 'parent_request_id', 'timestamp'),
        Index('ix_upl_tenant_request_id', 'tenant_id', 'request_id'),

        # Error analysis
        Index('ix_upl_tenant_error_type', 'tenant_id', 'error_type', 'timestamp'),

        # ==========================
        # MCP-Specific Queries
        # ==========================

        # MCP server queries
        Index('ix_upl_mcp_server_timestamp', 'tenant_id', 'proxy_type', 'server_id', 'timestamp'),

        # MCP method analysis
        Index('ix_upl_mcp_method_timestamp', 'tenant_id', 'mcp_method', 'timestamp'),

        # MCP capability usage (tool/resource/prompt)
        Index('ix_upl_mcp_capability_timestamp', 'tenant_id', 'mcp_capability_name', 'timestamp'),

        # ==========================
        # LLM-Specific Queries
        # ==========================

        # LLM model usage
        Index('ix_upl_llm_model_req_timestamp', 'tenant_id', 'llm_model_requested', 'timestamp'),
        Index('ix_upl_llm_model_used_timestamp', 'tenant_id', 'llm_model_used', 'timestamp'),
        Index('ix_upl_llm_provider_timestamp', 'tenant_id', 'llm_provider', 'timestamp'),

        # LLM routing analysis
        Index('ix_upl_llm_fallback_timestamp', 'tenant_id', 'llm_used_fallback', 'timestamp'),

        # LLM cost analysis
        Index('ix_upl_llm_cost_timestamp', 'tenant_id', 'llm_estimated_cost_usd', 'timestamp'),

        # LLM cache analysis
        Index('ix_upl_llm_cache_hit_timestamp', 'tenant_id', 'llm_cache_hit', 'timestamp'),

        # ==========================
        # OAuth-Specific Queries
        # ==========================

        # OAuth flow tracking
        Index('ix_upl_oauth_idp_timestamp', 'tenant_id', 'idp_server_id', 'timestamp'),
        Index('ix_upl_oauth_flow_timestamp', 'tenant_id', 'oauth_flow_type', 'timestamp'),

        # ==========================
        # Agent Skills Queries
        # ==========================

        # Skill execution correlation - "Get all proxy requests for a skill execution"
        Index('ix_upl_skill_execution', 'skill_execution_id'),

        # Skill attribution - "Get all proxy requests triggered by a skill"
        Index('ix_upl_tenant_skill_timestamp', 'tenant_id', 'skill_id', 'timestamp'),

        # The run ledger reads the explicit end marker per
        # (tenant, root_task_id) when closing a run, and the sweeper repeats
        # that for every candidate on every tick. Partial: almost no step
        # carries a marker, so indexing only those that do keeps this small
        # instead of one entry per proxy_logs row.
        Index(
            'ix_proxy_logs_run_end_state',
            'tenant_id',
            'root_task_id',
            postgresql_where=text('run_end_state IS NOT NULL'),
        ),

        # ==========================
        # Governance / attribution (the same indexes the migrations create,
        # so fresh create_all installs have them too; see
        # test_migration_created_indexes_*).
        # ==========================

        # Data residency — partial: only rows that carry a class.
        Index(
            'ix_proxy_logs_residency_class',
            'tenant_id',
            'residency_class',
            postgresql_where=text('residency_class IS NOT NULL'),
        ),

        # Compliance tagging — partial B-tree + partial GIN, both
        # scoped to tagged rows only.
        Index(
            'ix_proxy_logs_compliance_tenant_timestamp',
            'tenant_id',
            'timestamp',
            postgresql_where=text('compliance_tags IS NOT NULL'),
        ),
        Index(
            'ix_proxy_logs_compliance_tags_gin',
            'compliance_tags',
            postgresql_using='gin',
            postgresql_where=text('compliance_tags IS NOT NULL'),
        ),

        # Sealed evidence pointer — partial: only sealed rows.
        Index(
            'ix_proxy_logs_record_id',
            'tenant_id',
            'record_id',
            postgresql_where=text('record_id IS NOT NULL'),
        ),

        # Per-agent attribution and API-key attribution.
        Index(
            'ix_proxy_logs_tenant_actor_agent',
            'tenant_id',
            'actor_agent_id',
            'timestamp',
        ),
        Index('ix_proxy_logs_tenant_api_key', 'tenant_id', 'api_key_id'),

        # "Show me every call that went out unchecked". Partial, so the
        # index stays proportional to the rare fail-open case rather than the
        # whole ledger.
        Index(
            'ix_proxy_logs_guardrails_skipped',
            'tenant_id',
            'timestamp',
            postgresql_where=text('guardrails_skipped IS NOT NULL'),
        ),
        Index(
            'ix_proxy_logs_guardrails_degraded',
            'tenant_id',
            'timestamp',
            postgresql_where=text('guardrails_degraded IS NOT NULL'),
        ),

        {
            'comment': 'Unified proxy logs for Skills, MCP, LLM, OAuth, and Virtual MCP proxies'
        }
    )

    def __repr__(self):
        return (
            f"<ProxyLog(id={self.id}, proxy_type={self.proxy_type}, "
            f"timestamp={self.timestamp}, user={self.idp_user_id or self.gateway_user_id})>"
        )

    # Body-state vocabulary. Shared with the admin UI, which renders the
    # word rather than "No preview available" for all four empties.
    BODY_AVAILABLE = "available"   # stored, and the caller may read it
    BODY_WITHHELD = "withheld"     # stored, caller lacks proxy:logs:read_bodies
    BODY_ERASED = "erased"         # removed by a data-subject erasure
    BODY_PRUNED = "pruned"         # removed by the retention sweeper
    BODY_NONE = "none"             # never captured (store_bodies=none, or no body)

    def _body_state(self, stored: Optional[str], include_sensitive: bool) -> str:
        if self.erased_at is not None:
            return self.BODY_ERASED
        if self.bodies_pruned_at is not None:
            return self.BODY_PRUNED
        if not stored:
            return self.BODY_NONE
        return self.BODY_AVAILABLE if include_sensitive else self.BODY_WITHHELD

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses

        Args:
            include_sensitive: Whether to include request/response bodies
        """
        base_dict = {
            "id": self.id,
            # Tenant the row belongs to. The admin proxy-log "View
            # Details" modal surfaces this; without it the field renders
            # empty. Safe to expose — the query is already tenant-scoped.
            "tenant_id": self.tenant_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "proxy_type": self.proxy_type,
            "proxy_subtype": self.proxy_subtype,

            # Correlation
            "correlation_id": self.correlation_id,
            "parent_request_id": self.parent_request_id,
            "request_id": self.request_id,

            # Trace / Call-Chain — the four task-id
            # columns identify this step in the delegation graph; the two
            # *_kind/*_continuity columns describe how the chain was joined.
            # (Populated for every stamped surface — OpenAI-format LLM, MCP,
            # Skills, A2A — not only A2A; root steps have parent_task_id=None.)
            "root_task_id": self.root_task_id,
            "current_task_id": self.current_task_id,
            "parent_task_id": self.parent_task_id,
            "delegation_depth": self.delegation_depth,
            "trace_root_kind": self.trace_root_kind,
            "trace_continuity": self.trace_continuity,
            "run_end_state": self.run_end_state,
            "outcome_id": self.outcome_id,
            "source": self.source or "gateway",
            "record_id": self.record_id,

            # Agent Identity (subject/actor) + per-agent authz decision
            "actor_agent_id": self.actor_agent_id,
            "subject_id": self.subject_id,
            "subject_kind": self.subject_kind,
            "agent_authz_decision": self.agent_authz_decision,
            "agent_grant_id": self.agent_grant_id,
            "semantic_policy_id": self.semantic_policy_id,

            # Decision record — which policy decided, under what
            # authority, via which exception path, against which upstream call.
            "step_id": self.step_id,
            "step_name": self.step_name,
            "turn_id": self.turn_id,
            "turn_seq": self.turn_seq,
            "argument_policy_id": self.argument_policy_id,
            "guardrail_config_id": self.guardrail_config_id,
            "guardrail_check": self.guardrail_check,
            "guardrail_action": self.guardrail_action,
            "guardrails_skipped": self.guardrails_skipped,
            "guardrails_degraded": self.guardrails_degraded,
            "autonomy_level": self.autonomy_level,
            "autonomy_provenance": self.autonomy_provenance,
            "consumed_approval_request_id": self.consumed_approval_request_id,
            "upstream_request_id": self.upstream_request_id,
            "pre_action_state": self.pre_action_state,

            # Content digests, trace context, tool provenance, deciding model
            "request_content_hash": self.request_content_hash,
            "response_content_hash": self.response_content_hash,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "server_version": self.server_version,
            "tool_definition_hash": self.tool_definition_hash,
            "deciding_model": self.deciding_model,

            # Tamper evidence
            "chain_id": self.chain_id,
            "chain_seq": self.chain_seq,
            "prev_hash": self.prev_hash,
            "row_hash": self.row_hash,
            "chain_version": self.chain_version,

            # Data residency
            "region_routed_to": self.region_routed_to,
            "residency_class": self.residency_class,
            "compliance_tags": self.compliance_tags,
            "guardrail_blocked": self.guardrail_blocked,
            "argument_policy_denied": self.argument_policy_denied,
            "pii_detected": self.pii_detected,
            "is_auth_event": self.is_auth_event,

            # User context - Using gateway/idp fields
            "gateway_user_id": self.gateway_user_id,
            "gateway_user_username": self.gateway_user_username,
            "idp_user_id": self.idp_user_id,
            "idp_user_username": self.idp_user_username,
            "idp_user_email": self.idp_user_email,
            "session_id": self.end_user_session_id,
            "resource_group_id": self.resource_group_id,
            "client_ip": self.client_ip,
            "user_agent": self.user_agent,
            "client_label": self.client_label,
            "agent_impl_name": self.agent_impl_name,
            "agent_impl_version": self.agent_impl_version,
            "agent_impl_build": self.agent_impl_build,
            "agent_impl_source": self.agent_impl_source,

            # Server IDs
            "server_id": self.server_id,
            "idp_server_id": self.idp_server_id,

            # Request
            "request": {
                "method": self.http_method,
                "endpoint": self.endpoint,
                "target_url": self.target_url,
                "size_bytes": self.request_size_bytes,
                "body_state": self._body_state(self.request_body_preview, include_sensitive),
            },

            # Response
            "response": {
                "status_code": self.http_status_code,
                "size_bytes": self.response_size_bytes,
                "duration_ms": self.duration_ms,
                "body_state": self._body_state(self.response_body_preview, include_sensitive),
            },

            # Status
            "success": self.success,
            "error_message": self.error_message,
            "error_type": self.error_type,

            # Agent Skills
            "skill_id": self.skill_id,
            "skill_execution_id": self.skill_execution_id,
        }

        # Add type-specific fields
        # 'skill' rows carry the reached verb (skills__run_script / __load / …) in
        # mcp_capability_name too, so surface the same `mcp` block for them — else
        # the Capability column reads '-' for every skill call.
        if self.proxy_type in ['mcp', 'virtual-mcp', 'skill']:
            base_dict['mcp'] = {
                "method": self.mcp_method,
                "request_id": self.mcp_request_id,
                "capability_name": self.mcp_capability_name,
            }

        if self.proxy_type == 'llm':
            base_dict['llm'] = {
                "model_requested": self.llm_model_requested,
                "model_used": self.llm_model_used,
                "provider": self.llm_provider,
                "prompt_tokens": self.llm_prompt_tokens,
                "completion_tokens": self.llm_completion_tokens,
                "total_tokens": self.llm_total_tokens,
                "streaming": self.llm_streaming,
                "temperature": self.llm_temperature,
                "max_tokens": self.llm_max_tokens,
                "function_calling": self.llm_function_calling,
                "used_fallback": self.llm_used_fallback,
                "fallback_reason": self.llm_fallback_reason,
                "estimated_cost_usd": self.llm_estimated_cost_usd,
                "cache_hit": self.llm_cache_hit,
                "cache_layer": self.llm_cache_layer,
                "tokens_saved": self.llm_tokens_saved,
                "cache_similarity_score": self.llm_cache_similarity_score,
                "rag": {
                    "enabled": self.rag_enabled,
                    "chunks_retrieved": self.rag_chunks_retrieved,
                    "context_tokens": self.rag_context_tokens,
                    "collection_ids": self.rag_collection_ids,
                    "top_similarity_score": self.rag_top_similarity_score,
                    "retrieval_duration_ms": self.rag_retrieval_duration_ms,
                } if self.rag_enabled else None,
            }

        if self.proxy_type == 'oauth':
            base_dict['oauth'] = {
                "flow_type": self.oauth_flow_type,
                "scope": self.oauth_scope,
            }

        # Include sensitive data if requested
        if include_sensitive:
            base_dict['request']['headers'] = self.request_headers
            base_dict['request']['body_preview'] = self.request_body_preview
            base_dict['response']['headers'] = self.response_headers
            base_dict['response']['body_preview'] = self.response_body_preview

        base_dict['bodies_pruned_at'] = (
            self.bodies_pruned_at.isoformat() if self.bodies_pruned_at else None
        )
        base_dict['erased_at'] = self.erased_at.isoformat() if self.erased_at else None

        # Include metadata
        if self.custom_metadata:
            base_dict['custom_metadata'] = self.custom_metadata

        if self.tags:
            base_dict['tags'] = self.tags

        return base_dict

    @classmethod
    def from_mcp_request(
        cls,
        log_id: str,
        tenant_id: str,
        timestamp: datetime,
        user_context: Dict[str, Any],
        request_data: Dict[str, Any],
        response_data: Optional[Dict[str, Any]] = None,
        mcp_data: Optional[Dict[str, Any]] = None,
        skill_id: Optional[str] = None,
        skill_execution_id: Optional[str] = None,
    ) -> 'ProxyLog':
        """Create log entry from MCP proxy request

        Args:
            user_context: Dictionary with user context fields:
                - gateway_user_id: Gateway user ID
                - gateway_username: Gateway username
                - idp_user_id: Identity provider user ID
                - idp_username: Identity provider username
                - idp_user_email: Identity provider email
                - session_id: Session ID
                - client_ip: Client IP address
                - user_agent: User agent string
        """

        return cls(
            id=log_id,
            tenant_id=tenant_id,
            timestamp=timestamp,
            proxy_type='mcp',
            proxy_subtype=mcp_data.get('subtype') if mcp_data else None,

            # User context - Extract from dictionary
            gateway_user_id=user_context.get('gateway_user_id'),
            gateway_user_username=user_context.get('gateway_username'),
            idp_user_id=user_context.get('idp_user_id'),
            idp_user_username=user_context.get('idp_username'),
            idp_user_email=user_context.get('idp_user_email'),
            end_user_session_id=user_context.get('session_id'),
            client_ip=user_context.get('client_ip'),
            user_agent=user_context.get('user_agent'),

            # Request
            http_method=request_data.get('method'),
            endpoint=request_data.get('endpoint'),
            target_url=request_data.get('target_url'),
            request_size_bytes=request_data.get('size_bytes'),
            request_headers=request_data.get('headers'),
            request_body_preview=request_data.get('body_preview'),

            # Response
            http_status_code=response_data.get('status_code') if response_data else None,
            response_size_bytes=response_data.get('size_bytes') if response_data else None,
            response_headers=response_data.get('headers') if response_data else None,
            response_body_preview=response_data.get('body_preview') if response_data else None,
            duration_ms=response_data.get('duration_ms') if response_data else None,

            # Success
            success=response_data.get('success', False) if response_data else False,
            error_message=response_data.get('error_message') if response_data else None,
            error_type=response_data.get('error_type') if response_data else None,

            # MCP-specific - Use mcp_capability_name for tool/resource/prompt
            server_id=mcp_data.get('server_id') if mcp_data else None,
            mcp_method=mcp_data.get('method') if mcp_data else None,
            mcp_request_id=mcp_data.get('request_id') if mcp_data else None,
            mcp_capability_name=mcp_data.get('capability_name') or mcp_data.get('tool_name') or mcp_data.get('resource_uri') or mcp_data.get('prompt_name') if mcp_data else None,

            # Correlation
            correlation_id=request_data.get('correlation_id'),
            parent_request_id=request_data.get('parent_request_id'),
            request_id=request_data.get('request_id'),

            # Agent Skills
            skill_id=skill_id,
            skill_execution_id=skill_execution_id,
        )

    @classmethod
    def from_llm_request(
        cls,
        log_id: str,
        tenant_id: str,
        timestamp: datetime,
        user_context: Dict[str, Any],
        request_data: Dict[str, Any],
        response_data: Optional[Dict[str, Any]] = None,
        llm_data: Optional[Dict[str, Any]] = None,
        skill_id: Optional[str] = None,
        skill_execution_id: Optional[str] = None,
    ) -> 'ProxyLog':
        """Create log entry from LLM proxy request

        Args:
            user_context: Dictionary with user context fields (see from_mcp_request)
        """

        return cls(
            id=log_id,
            tenant_id=tenant_id,
            timestamp=timestamp,
            proxy_type='llm',
            proxy_subtype=llm_data.get('subtype') if llm_data else None,

            # User context - Extract from dictionary
            gateway_user_id=user_context.get('gateway_user_id'),
            gateway_user_username=user_context.get('gateway_username'),
            idp_user_id=user_context.get('idp_user_id'),
            idp_user_username=user_context.get('idp_username'),
            idp_user_email=user_context.get('idp_user_email'),
            end_user_session_id=user_context.get('session_id'),
            client_ip=user_context.get('client_ip'),
            user_agent=user_context.get('user_agent'),

            # Request
            http_method=request_data.get('method'),
            endpoint=request_data.get('endpoint'),
            target_url=request_data.get('target_url'),
            request_size_bytes=request_data.get('size_bytes'),
            request_headers=request_data.get('headers'),
            request_body_preview=request_data.get('body_preview'),

            # Response
            http_status_code=response_data.get('status_code') if response_data else None,
            response_size_bytes=response_data.get('size_bytes') if response_data else None,
            response_headers=response_data.get('headers') if response_data else None,
            response_body_preview=response_data.get('body_preview') if response_data else None,
            duration_ms=response_data.get('duration_ms') if response_data else None,

            # Success
            success=response_data.get('success', False) if response_data else False,
            error_message=response_data.get('error_message') if response_data else None,
            error_type=response_data.get('error_type') if response_data else None,

            # LLM-specific
            server_id=llm_data.get('server_id') if llm_data else None,
            llm_model_requested=llm_data.get('model_requested') if llm_data else None,
            llm_model_used=llm_data.get('model_used') if llm_data else None,
            llm_provider=llm_data.get('provider') if llm_data else None,
            llm_prompt_tokens=llm_data.get('prompt_tokens') if llm_data else None,
            llm_completion_tokens=llm_data.get('completion_tokens') if llm_data else None,
            llm_total_tokens=llm_data.get('total_tokens') if llm_data else None,
            llm_streaming=llm_data.get('streaming') if llm_data else None,
            llm_temperature=llm_data.get('temperature') if llm_data else None,
            llm_max_tokens=llm_data.get('max_tokens') if llm_data else None,
            llm_function_calling=llm_data.get('function_calling', False) if llm_data else False,
            llm_used_fallback=llm_data.get('used_fallback', False) if llm_data else False,
            llm_fallback_reason=llm_data.get('fallback_reason') if llm_data else None,
            llm_estimated_cost_usd=llm_data.get('estimated_cost_usd') if llm_data else None,

            # Correlation
            correlation_id=request_data.get('correlation_id'),
            parent_request_id=request_data.get('parent_request_id'),
            request_id=request_data.get('request_id'),

            # Agent Skills
            skill_id=skill_id,
            skill_execution_id=skill_execution_id,
        )

    @classmethod
    def from_oauth_request(
        cls,
        log_id: str,
        tenant_id: str,
        timestamp: datetime,
        user_context: Dict[str, Any],
        request_data: Dict[str, Any],
        response_data: Optional[Dict[str, Any]] = None,
        oauth_data: Optional[Dict[str, Any]] = None,
        skill_id: Optional[str] = None,
        skill_execution_id: Optional[str] = None,
    ) -> 'ProxyLog':
        """Create log entry from OAuth proxy request

        Args:
            user_context: Dictionary with user context fields (see from_mcp_request)
        """

        return cls(
            id=log_id,
            tenant_id=tenant_id,
            timestamp=timestamp,
            proxy_type='oauth',
            proxy_subtype=oauth_data.get('subtype') if oauth_data else None,

            # User context - Extract from dictionary
            gateway_user_id=user_context.get('gateway_user_id'),
            gateway_user_username=user_context.get('gateway_username'),
            idp_user_id=user_context.get('idp_user_id'),
            idp_user_username=user_context.get('idp_username'),
            idp_user_email=user_context.get('idp_user_email'),
            end_user_session_id=user_context.get('session_id'),
            client_ip=user_context.get('client_ip'),
            user_agent=user_context.get('user_agent'),

            # Request
            http_method=request_data.get('method'),
            endpoint=request_data.get('endpoint'),
            target_url=request_data.get('target_url'),
            request_size_bytes=request_data.get('size_bytes'),
            request_headers=request_data.get('headers'),
            request_body_preview=request_data.get('body_preview'),

            # Response
            http_status_code=response_data.get('status_code') if response_data else None,
            response_size_bytes=response_data.get('size_bytes') if response_data else None,
            response_headers=response_data.get('headers') if response_data else None,
            response_body_preview=response_data.get('body_preview') if response_data else None,
            duration_ms=response_data.get('duration_ms') if response_data else None,

            # Success
            success=response_data.get('success', False) if response_data else False,
            error_message=response_data.get('error_message') if response_data else None,
            error_type=response_data.get('error_type') if response_data else None,

            # OAuth-specific
            idp_server_id=oauth_data.get('idp_server_id') if oauth_data else None,
            server_id=oauth_data.get('mcp_server_id') if oauth_data else None,  # MCP server for OAuth
            oauth_flow_type=oauth_data.get('flow_type') if oauth_data else None,
            oauth_scope=oauth_data.get('scope') if oauth_data else None,

            # Correlation
            correlation_id=request_data.get('correlation_id'),
            parent_request_id=request_data.get('parent_request_id'),
            request_id=request_data.get('request_id'),

            # Agent Skills
            skill_id=skill_id,
            skill_execution_id=skill_execution_id,
        )