// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Constants
//!
//! Centralized constants mirroring `app/constants/` in the Python gateway.
//! Only includes values relevant to the proxy layer.

// =========================================================================
// Proxy Types (app/constants/proxy.py)
// =========================================================================

pub mod proxy {
    pub const MCP: &str = "mcp";
    pub const VIRTUAL_MCP: &str = "virtual-mcp";
    pub const LLM: &str = "llm";
    pub const SKILL: &str = "skill";
    pub const OAUTH: &str = "oauth";
    pub const A2A: &str = "a2a";
    // The batch surface (`/v1/proxy/batch`): job submit / read / cancel rows
    // and the submit-time refusals (residency). An LLM surface to the
    // evidence writer (`evidence::mint::surface_of`).
    pub const BATCH: &str = "batch";
    // The end-user Portal API (`/v1/portal/*`: login, listings, portal
    // actions). Rows written by `portal_auth::write_portal_log_and_metric`.
    pub const PORTAL: &str = "portal";

    /// Every `proxy_type` the core writes. `evidence::mint` is tested
    /// against each (a refusal on any of them is sealed), and a static test
    /// fails if a row is written with a proxy_type missing here — the batch
    /// surface's refusals went unsealed because `"batch"` was a literal no
    /// list knew about.
    pub const ALL: &[&str] = &[MCP, VIRTUAL_MCP, LLM, SKILL, OAUTH, A2A, BATCH, PORTAL];
}

// =========================================================================
// proxy_logs.proxy_subtype values — the WIRE CONTRACT for proxy_logs.
//
// IMPORTANT: these strings are read by the Python control plane (e.g.
// `app/services/agent_metrics_service.py` filters `proxy_subtype ==
// "semantic_policy_shadow"`). They are mirrored in
// `brutor-gateway-control/app/constants/proxy.py::ProxySubtype` and pinned by
// `tests` in BOTH repos — change a value here and you MUST change it there, or
// the cross-repo query silently breaks. Do not inline these as literals.
// =========================================================================

pub mod proxy_subtype {
    // Per-agent runtime authz outcomes.
    pub const AGENT_AUTHZ_BLOCK: &str = "agent_authz_block";
    pub const AGENT_AUTHZ_SHADOW: &str = "agent_authz_shadow";
    // Semantic governance policy outcomes.
    pub const SEMANTIC_POLICY_BLOCK: &str = "semantic_policy_block";
    pub const SEMANTIC_POLICY_SHADOW: &str = "semantic_policy_shadow";
    // Human-in-the-loop approval lifecycle.
    pub const APPROVAL_REQUIRED: &str = "approval_required";
    pub const APPROVAL_APPROVED: &str = "approval_approved";
    pub const APPROVAL_REJECTED: &str = "approval_rejected";
    // An operator stopped this specific run. Distinct from
    // `autonomy_denied`, which is the whole agent or AI System: the operator
    // needs to tell "I stopped this one" from "the system is suspended".
    // Pinned in `brutor-gateway-control/app/constants/proxy.py` too, per the
    // wire-contract rule.
    pub const RUN_ABORTED: &str = "run_aborted";
    // A per-run ceiling (calls, tokens, cost,
    // depth, duration) refused the step. A `denied` verdict, not an error:
    // before this subtype the row read as a failed call (`errored`) on the
    // action surfaces and was not sealed at all on inference.
    pub const RUN_CAP_REACHED: &str = "run_cap_reached";
    // An access-control refusal that no more specific
    // subtype names: a skill not assigned to the group, execution disabled,
    // a key restricted to other skills, an unauthorised explicit group, an
    // A2A card not bound to the caller's group. Sealed `denied`.
    pub const ACCESS_DENIED: &str = "access_denied";
    // Skill capacity planes, so a refused execution is sealed as the verdict
    // it was (`denied`) rather than as a failed run. Agent-grant, autonomy
    // and abort refusals keep their shared subtypes (AGENT_AUTHZ_BLOCK /
    // AUTONOMY_BLOCK / RUN_ABORTED).
    pub const SKILL_QUOTA_EXCEEDED: &str = "skill_quota_exceeded";
    pub const SKILL_CONCURRENCY_LIMIT: &str = "skill_concurrency_limit";
    // A one-time approval token that was presented but did not validate
    // (expired, consumed, bound to other arguments). A policy refusal.
    pub const APPROVAL_TOKEN_INVALID: &str = "approval_token_invalid";
    // A gate failed closed because its engine could not decide (the authz
    // store or a guardrail provider was unreachable). Sealed
    // `engine_failure` — not a policy denial the policy never made.
    pub const ENGINE_FAILURE: &str = "engine_failure";
    // Governance / residency.
    pub const RESIDENCY_VIOLATION: &str = "residency_violation";
    // Autonomy denying a caller that is *not* an agent
    // principal. Agent callers land on AGENT_AUTHZ_BLOCK instead, since for
    // them autonomy is the last gate inside the agent-authz decision — the
    // two are kept distinct so "suspension stopped a script" and "grants
    // stopped an agent" stay separable in the audit trail.
    pub const AUTONOMY_BLOCK: &str = "autonomy_block";
    // A shared service with `enforce_accepted_callers` on
    // refused an A2A call from a caller system not in its
    // `intended_client_systems`. Refused BEFORE the upstream is contacted;
    // sealed `denied`; the run ledger's segment reads it as `blocked`.
    // Mirrored in the CP's `ProxySubtype` (app/constants/proxy.py).
    pub const UNACCEPTED_CALLER: &str = "unaccepted_caller";
    // An LLM-surface governance policy refused the request before the
    // provider was called: a request-parameter policy (temperature / tools /
    // banned patterns / context window on chat, responses, completions,
    // Anthropic, Gemini) or a per-mode media policy (embeddings dimension /
    // batch size, image / speech / transcription / video whitelists and
    // caps). Sealed `denied`. Written only through `services::llm_refusal`.
    // Mirrored in the CP's `ProxySubtype` (app/constants/proxy.py).
    pub const LLM_GOVERNANCE_BLOCK: &str = "llm_governance_block";
    // A2A directional subtypes.
    pub const INBOUND: &str = "inbound";
    pub const OUTBOUND: &str = "outbound";
    pub const V1_INBOUND: &str = "v1-inbound";
    // LLM / surface operation subtypes.
    pub const CHAT_COMPLETIONS: &str = "chat_completions";
    pub const RESPONSES: &str = "responses";
    pub const EMBEDDINGS: &str = "embeddings";
    pub const ANTHROPIC_MESSAGES: &str = "anthropic_messages";
    pub const GEMINI_GENERATE_CONTENT: &str = "gemini_generate_content";
    pub const GEMINI_STREAM_GENERATE_CONTENT: &str = "gemini_stream_generate_content";
    pub const SSE_STREAM: &str = "sse_stream";
    pub const NON_SSE_PROBE: &str = "non_sse_probe";
    pub const SYSTEM_SERVER: &str = "system_server";
    // Governed Agent Skills progressive-disclosure layers. One
    // proxy_log row per L1→L3 step, sharing the call's correlation id so the
    // whole skill workflow renders as a single audited trace.
    pub const SKILL_LOAD: &str = "skill_load";
    pub const SKILL_READ: &str = "skill_read";
    pub const SKILL_RUN: &str = "skill_run";
    pub const SKILL_RENDER: &str = "skill_render";
}

// =========================================================================
// agent_authz_decision values — also a Rust↔Python wire contract (read by the
// CP's agent-activity metrics). Single-sourced here; `agent_authz::Decision::
// as_str` returns these. Mirrored in the CP + pinned in both repos.
// =========================================================================

pub mod agent_decision {
    pub const ALLOW: &str = "allow";
    pub const DENY: &str = "deny";
    pub const APPROVAL_REQUIRED: &str = "approval_required";
}

// =========================================================================
// Internal route paths — centralized so the duplicated proxy/log endpoint
// strings live in one place (within-repo; not a cross-repo contract). NOTE:
// `#[utoipa::path("…")]` attributes require string literals and cannot use
// these consts — those stay inline.
// =========================================================================

pub mod routes {
    pub const LLM_CHAT_COMPLETIONS: &str = "/v1/proxy/llm/chat/completions";
    pub const LLM_EMBEDDINGS: &str = "/v1/proxy/llm/embeddings";
    pub const SKILLS_EXECUTE: &str = "/v1/proxy/skills/execute";
    pub const A2A_OUTBOUND: &str = "/v1/proxy/a2a/outbound";
    pub const A2A_MESSAGE_SEND: &str = "/a2a/message:send";
    /// The MCP surfaces whose JSON-RPC bodies carry `clientInfo`.
    pub const MCP_PROXY_PREFIX: &str = "/v1/proxy/mcp/";
    pub const VMCP_PROXY_PREFIX: &str = "/v1/proxy/vmcp/";
}

// =========================================================================
// Self-call base URL — the gateway calls its own governed endpoints (embeddings
// for the semantic cache / KB, and the chat endpoint for the semantic-policy
// judge). One definition so a port/bind change is a single edit.
// =========================================================================

pub mod self_call {
    pub const BASE_URL: &str = "http://127.0.0.1:8100";
}

// =========================================================================
// HTTP Headers (app/constants/http.py)
// =========================================================================

pub mod headers {
    pub const X_TENANT_ID: &str = "x-tenant-id";
    pub const X_AUTH_TYPE: &str = "x-auth-type";
    pub const X_CORRELATION_ID: &str = "x-correlation-id";
    pub const X_REQUEST_TYPE: &str = "x-request-type";
    pub const X_API_KEY: &str = "x-api-key";
    pub const X_GATEWAY_AUTHORIZATION: &str = "x-gateway-authorization";
    pub const X_GATEWAY_REQUEST_ID: &str = "x-gateway-request-id";
    pub const X_GATEWAY_ERROR: &str = "x-gateway-error";
    /// The run (`root_task_id`) this response belongs to, returned so a
    /// client can attribute feedback to the task it just saw.
    ///
    /// Deliberately the *response* counterpart of the inbound
    /// `x-brutor-run-id` a client may assert: same identifier, same name,
    /// opposite direction. Returning it means feedback works for any client
    /// rather than only the ones we ship, and the server never has to guess
    /// which run a rating refers to from timing.
    pub const X_BRUTOR_RUN_ID: &str = "x-brutor-run-id";
    pub const X_SYSTEM_MCP_SERVER: &str = "x-system-mcp-server";
    pub const X_BRUTOR_USAGE_UTILIZATION: &str = "x-brutor-usage-utilization";
    pub const X_BRUTOR_USAGE_WARNING: &str = "x-brutor-usage-warning";
    pub const MCP_SESSION_ID: &str = "mcp-session-id";

    /// Marks the gateway's own self-call to the chat endpoint for the
    /// semantic-policy judge, so the LLM SGP hook skips it (recursion guard).
    /// WRITER (`services/semantic_policy.rs`) and READER (`routes/llm.rs`) MUST
    /// use this same constant.
    pub const X_BRUTOR_SEMANTIC_POLICY: &str = "x-brutor-semantic-policy";
    /// Marks an embedding self-call of the platform's own code (the
    /// semantic cache, the Knowledge Base). Its value is
    /// `services::internal_call::embedding_assertion`: the purpose and this
    /// process's internal token, so a caller cannot assert it.
    pub const X_BRUTOR_INTERNAL_EMBEDDING: &str = "x-brutor-internal-embedding";
    /// One-time approval token replayed to retry an `approval_required` action.
    /// ISSUED by `routes/approvals.rs`; VALIDATED on every action surface
    /// (mcp/a2a/skill).
    pub const X_APPROVAL_TOKEN: &str = "x-approval-token";

    /// Marks an LLM completion response as AI-generated so
    /// clients and exports can label it. Content-level marking is not
    /// provided; this is the transport-level label.
    pub const X_BRUTOR_AI_GENERATED: &str = "x-brutor-ai-generated";
    /// The portal's per-conversation opaque handle (a fresh
    /// ULID, never the session or the user). Stored clear on the row's
    /// `custom_metadata.conversation_handle` so the first assistant turn's
    /// record can cite the conversation's notice record.
    pub const X_BRUTOR_CONVERSATION_HANDLE: &str = "x-brutor-conversation-handle";

    /// Prefix for Brutor API keys in Bearer tokens.
    pub const BEARER_API_KEY_PREFIX: &str = "sk_brutor_api_";
}

// =========================================================================
// Agent implementation identity — wire contract
// =========================================================================

/// The carriers the core accepts an agent's implementation
/// name / version / build from, and the vocabularies it writes for them.
///
/// Cross-repo contract: the control plane reads `source` / `trust` values by
/// string equality (`app/constants/agent_implementation.py`, pinned by its
/// `tests/test_wire_contract.py`), and agents send the header / baggage /
/// claim names. Pinned by `tests::wire_contract_agent_impl_pinned`. A rename
/// is a deliberate edit in BOTH repos.
///
/// These describe the caller; they never authorise it. The
/// static guard `tests/agent_impl_guards.rs` fails if an access path reads
/// any of them.
pub mod agent_impl {
    /// Brutor's own headers, the last carrier that is read, plus the explicit client label
    /// (`X-Brutor-Client`). Lower-case: HTTP names are case-insensitive, and `HeaderName`
    /// only accepts lower-case statics. On the wire they are documented as
    /// `X-Brutor-Agent-Name` etc.
    pub mod headers {
        pub const X_BRUTOR_AGENT_NAME: &str = "x-brutor-agent-name";
        pub const X_BRUTOR_AGENT_VERSION: &str = "x-brutor-agent-version";
        pub const X_BRUTOR_AGENT_BUILD: &str = "x-brutor-agent-build";
        pub const X_BRUTOR_CLIENT: &str = "x-brutor-client";
        /// W3C Baggage. The third carrier that is read; never forwarded upstream.
        pub const BAGGAGE: &str = "baggage";
    }

    /// OpenTelemetry GenAI baggage members, read after the token claims and the MCP carriers. `gen_ai.*` is in
    /// "Development" status upstream, so every key lives here: a rename is
    /// a one-line change. `build` is a Brutor extension.
    pub mod baggage {
        pub const NAME: &str = "gen_ai.agent.name";
        pub const VERSION: &str = "gen_ai.agent.version";
        pub const BUILD: &str = "gen_ai.agent.build";
    }

    /// Verified workload-token claims, the first carrier that is read: RFC 7591 software
    /// statement names, plus the `software_build` extension.
    pub mod claims {
        pub const SOFTWARE_ID: &str = "software_id";
        pub const SOFTWARE_VERSION: &str = "software_version";
        pub const SOFTWARE_BUILD: &str = "software_build";
    }

    /// MCP carriers, read after the token claims: the per-request `_meta` key
    /// ([`super::mcp_protocol::meta::CLIENT_INFO`]) and the `initialize`
    /// request's `params.clientInfo`.
    pub mod mcp {
        pub const INITIALIZE_METHOD: &str = "initialize";
        pub const INITIALIZE_CLIENT_INFO: &str = "clientInfo";
    }

    /// `proxy_logs.agent_impl_source`: the database accepts these values only.
    pub mod source {
        pub const TOKEN: &str = "token";
        pub const MCP_CLIENT_INFO: &str = "mcp_client_info";
        pub const BAGGAGE: &str = "baggage";
        pub const HEADER: &str = "header";
        /// In precedence order (highest trust first).
        pub const ALL: [&str; 4] = [TOKEN, MCP_CLIENT_INFO, BAGGAGE, HEADER];
    }

    /// `agent_impl_trust` / `agent_implementation_releases.trust`.
    pub mod trust {
        pub const VERIFIED: &str = "verified";
        pub const DECLARED: &str = "declared";
        pub const ALL: [&str; 2] = [VERIFIED, DECLARED];
    }

    /// The label every `Mozilla/…` User-Agent collapses to.
    pub const BROWSER_CLIENT_LABEL: &str = "browser";
}

// =========================================================================
// MCP protocol versions
// =========================================================================

/// MCP spec versions the gateway can speak. Synthesized servers (VMCP,
/// system servers) echo one of these on `initialize`; the client side
/// pins one when discovering upstreams.
///
/// `V2026_07_28` is the STATELESS redesign (initialize and
/// `Mcp-Session-Id` removed, `_meta` carries version/capabilities, new
/// `server/discover`). The gateway's synthesized servers are dual-era:
/// they answer both `initialize` and `server/discover` unconditionally,
/// shaping each response to the request's dialect.
pub mod mcp_protocol {
    pub const V2024_11_05: &str = "2024-11-05";
    pub const V2025_03_26: &str = "2025-03-26";
    pub const V2025_06_18: &str = "2025-06-18";
    /// MCP 2026-07-28 — stateless core.
    pub const V2026_07_28: &str = "2026-07-28";

    /// Wire dialects stored in `mcp_servers.dialect`. NULL is
    /// treated as `HANDSHAKE` — every server predates discovery.
    /// `DUAL` marks a server that speaks both eras (the gateway's own
    /// synthesized system servers + VMCP).
    pub mod dialect {
        pub const HANDSHAKE: &str = "handshake";
        pub const STATELESS: &str = "stateless";
        pub const DUAL: &str = "dual";
    }

    /// Streamable-HTTP headers (2026-07-28, SEP-2243). Lowercase — axum
    /// header lookups are case-insensitive but we normalize on write.
    pub mod headers {
        pub const PROTOCOL_VERSION: &str = "mcp-protocol-version";
        pub const METHOD: &str = "mcp-method";
        pub const NAME: &str = "mcp-name";
    }

    /// Reserved `params._meta` keys (2026-07-28). Reverse-DNS per spec.
    pub mod meta {
        pub const PROTOCOL_VERSION: &str = "io.modelcontextprotocol/protocolVersion";
        pub const CLIENT_CAPABILITIES: &str = "io.modelcontextprotocol/clientCapabilities";
        pub const CLIENT_INFO: &str = "io.modelcontextprotocol/clientInfo";
        pub const SERVER_INFO: &str = "io.modelcontextprotocol/serverInfo";
    }

    /// Spec error codes. NOTE: the RC used -32001/-32003/-32004; the
    /// draft renumbered to the -320xx block below (changelog item 12).
    pub mod error {
        pub const HEADER_MISMATCH: i64 = -32020;
        pub const MISSING_CLIENT_CAPABILITY: i64 = -32021;
        pub const UNSUPPORTED_PROTOCOL_VERSION: i64 = -32022;
        /// Retired in 2026-07-28 (resource-missing → -32602); still
        /// accepted FROM older servers.
        pub const LEGACY_RESOURCE_NOT_FOUND: i64 = -32002;
    }

    /// `resultType` values required on every modern result.
    pub mod result_type {
        pub const COMPLETE: &str = "complete";
        pub const INPUT_REQUIRED: &str = "input_required";
    }

    /// `cacheScope` values (SEP-2549). Access-scoped results (skills
    /// catalog, group-filtered VMCP lists) MUST be `PRIVATE`.
    pub mod cache_scope {
        pub const PUBLIC: &str = "public";
        pub const PRIVATE: &str = "private";
    }
}

// =========================================================================
// Resource Groups (app/constants/groups.py)
// =========================================================================

pub mod groups {
    pub const DEFAULT_GROUP_NAME: &str = "Default";
    /// The system group the platform service key's calls are billed
    /// to (health checks, the assurance judge, KB embeddings, the KB
    /// uploader). Identified by `is_system` AND this name. Control plane
    /// twin: `ResourceGroupName.UNBOUND`.
    pub const UNBOUND_GROUP_NAME: &str = "Unbound";
}

// =========================================================================
// LLM Model Modes (app/constants/llm.py — LLModelMode)
// =========================================================================

pub mod llm_mode {
    pub const CHAT: &str = "chat";
    pub const RESPONSES: &str = "responses";
    pub const COMPLETION: &str = "completion";
    pub const EMBEDDING: &str = "embedding";
    pub const IMAGE_GENERATION: &str = "image_generation";
    pub const AUDIO_TRANSCRIPTION: &str = "audio_transcription";
    pub const AUDIO_SPEECH: &str = "audio_speech";
    pub const RERANK: &str = "rerank";
    pub const MODERATION: &str = "moderation";
    pub const ROUTING: &str = "routing";
}

// =========================================================================
// Guardrail surface tokens (app/constants/proxy.py — GuardrailSurface)
// =========================================================================

/// The `surface` token every guardrail call site passes.
///
/// Why this is a constant module and not a string literal at the call site:
/// the per-surface gate (`guardrails::surface_enabled`) matches the token
/// against a closed set of arms and returns `false` — "not enabled" — for
/// anything it does not recognise. A typo therefore does not fail loudly;
/// it silently disables EVERY check on that surface. `"llm"` was such a
/// typo, and it turned off output guardrails on a whole route until someone
/// noticed. Pinning the vocabulary here means a bad token is a compile
/// error rather than a governance hole.
///
/// These strings are wire contract in both directions: the control plane
/// writes them into the `*_surfaces` JSON columns and the `*_per_surface`
/// objects, and this proxy reads them back. Mirrored in
/// `brutor-gateway-control/app/constants/proxy.py::GuardrailSurface` — a
/// rename is a coordinated change in both repos, same release.
pub mod guardrail_surface {
    // Per-mode LLM surfaces. Direction is encoded in the token itself,
    // which is why these never need a `SurfaceKind` alongside them.
    pub const CHAT_INPUT: &str = "chat_input";
    pub const CHAT_OUTPUT: &str = "chat_output";
    pub const EMBEDDINGS_INPUT: &str = "embeddings_input";
    pub const IMAGE_GEN_INPUT: &str = "image_gen_input";
    pub const IMAGE_GEN_OUTPUT: &str = "image_gen_output";
    pub const AUDIO_TTS_INPUT: &str = "audio_tts_input";
    pub const AUDIO_TTS_OUTPUT: &str = "audio_tts_output";
    pub const AUDIO_STT_INPUT: &str = "audio_stt_input";
    pub const AUDIO_STT_OUTPUT: &str = "audio_stt_output";
    pub const VIDEO_GEN_INPUT: &str = "video_gen_input";
    pub const VIDEO_GEN_OUTPUT: &str = "video_gen_output";
    pub const BATCH_INPUT: &str = "batch_input";
    pub const BATCH_OUTPUT: &str = "batch_output";
    pub const MODERATION_INPUT: &str = "moderation_input";

    // Non-LLM surface kinds. These carry a `SurfaceKind` at the gate
    // because one token covers both directions.
    pub const MCP: &str = "mcp";
    pub const SKILL: &str = "skill";
    pub const A2A_INBOUND: &str = "a2a_inbound";
    pub const A2A_OUTBOUND: &str = "a2a_outbound";

    /// Every per-mode LLM token, in the order the control plane lists them.
    /// Used by the vocabulary guard to prove the constants and the gate's
    /// match arms have not drifted apart.
    pub const LLM_ALL: &[&str] = &[
        CHAT_INPUT,
        CHAT_OUTPUT,
        EMBEDDINGS_INPUT,
        IMAGE_GEN_INPUT,
        IMAGE_GEN_OUTPUT,
        AUDIO_TTS_INPUT,
        AUDIO_TTS_OUTPUT,
        AUDIO_STT_INPUT,
        AUDIO_STT_OUTPUT,
        VIDEO_GEN_INPUT,
        VIDEO_GEN_OUTPUT,
        BATCH_INPUT,
        BATCH_OUTPUT,
        MODERATION_INPUT,
    ];
}

// =========================================================================
// LLM Providers (app/constants/llm.py — LLModelProvider)
// =========================================================================

pub mod provider {
    // Tier 1: Major Direct API Providers
    pub const OPENAI: &str = "openai";
    pub const ANTHROPIC: &str = "anthropic";
    pub const GOOGLE_AI_STUDIO: &str = "gemini";
    pub const VERTEX_AI: &str = "vertex_ai";
    pub const AZURE: &str = "azure";
    pub const AZURE_AI: &str = "azure_ai";
    pub const BEDROCK: &str = "bedrock";
    pub const BEDROCK_CONVERSE: &str = "bedrock_converse";
    pub const MISTRAL: &str = "mistral";
    pub const COHERE: &str = "cohere";
    pub const XAI: &str = "xai";
    pub const DEEPSEEK: &str = "deepseek";

    // Tier 2: Inference Platforms
    pub const GROQ: &str = "groq";
    pub const TOGETHER_AI: &str = "together_ai";
    pub const FIREWORKS_AI: &str = "fireworks_ai";
    pub const PERPLEXITY: &str = "perplexity";
    pub const OPENROUTER: &str = "openrouter";
    pub const SAMBANOVA: &str = "sambanova";
    pub const CEREBRAS: &str = "cerebras";
    pub const AI21: &str = "ai21";
    pub const DEEPINFRA: &str = "deepinfra";

    // Tier 3: Self-Hosted / Specialized
    pub const OLLAMA: &str = "ollama";
    pub const VLLM: &str = "vllm";
    pub const NVIDIA_NIM: &str = "nvidia_nim";
    pub const DATABRICKS: &str = "databricks";
    pub const HUGGINGFACE: &str = "huggingface";
    pub const SAGEMAKER: &str = "sagemaker";
    pub const LM_STUDIO: &str = "lm_studio";

    // Specialized Services
    pub const VOYAGE: &str = "voyage";
    pub const JINA_AI: &str = "jina_ai";
    pub const ELEVENLABS: &str = "elevenlabs";
    pub const DEEPGRAM: &str = "deepgram";
    pub const STABILITY_AI: &str = "stability_ai";
    pub const FAL_AI: &str = "fal_ai";

    // OpenAI-Compatible Generic
    pub const OPENAI_COMPATIBLE: &str = "openai_compatible";

    // Internal
    pub const ROUTING_GROUP: &str = "routing_group";
}

// =========================================================================
// OAuth (app/constants/oauth.py)
// =========================================================================

pub mod oauth {
    pub const GRANT_AUTHORIZATION_CODE_PKCE: &str = "authorization_code_pkce";
    pub const GRANT_AUTHORIZATION_CODE: &str = "authorization_code";
    pub const GRANT_CLIENT_CREDENTIALS: &str = "client_credentials";
    pub const GRANT_REFRESH_TOKEN: &str = "refresh_token";

    pub const CLIENT_TYPE_PUBLIC: &str = "public";
    pub const CLIENT_TYPE_PUBLIC_WITH_SECRET: &str = "public_with_secret";
    pub const CLIENT_TYPE_CONFIDENTIAL: &str = "confidential";

    pub const JWT_TOKEN_TYPE_MCP_AUTH: &str = "mcp_auth";
}

// =========================================================================
// System MCP Servers (app/constants/system_server.py)
// =========================================================================

pub mod system_server {
    /// System servers use this URL prefix. The proxy intercepts requests
    /// to these URLs and handles them internally instead of forwarding.
    pub const URL_PREFIX: &str = "http://system/";

    /// Check if a server URL indicates a system-managed virtual server.
    pub fn is_system_server(base_url: &str) -> bool {
        base_url.starts_with(URL_PREFIX)
    }

    pub mod search {
        pub const SERVER_ID: &str = "system-search-server";
        pub const NAME: &str = "Web Search";
        pub const BASE_URL: &str = "http://system/search-server";
        pub const TOOL_PREFIX: &str = "search";

        pub const TOOL_WEB: &str = "search__web";
        pub const TOOL_NEWS: &str = "search__news";
        pub const TOOL_EXTRACT: &str = "search__extract";
        pub const TOOL_IMAGES: &str = "search__images";
    }

    pub mod agent_skill {
        pub const SERVER_ID: &str = "system-agent-skill-server";
        pub const NAME: &str = "Agent Skills";
        pub const BASE_URL: &str = "http://system/agent-skill-server";
        pub const TOOL_PREFIX: &str = "skill";

        // -----------------------------------------------------------------
        // Governed progressive-disclosure tool contract.
        //
        // A skill is consumed ONLY by a model doing progressive disclosure:
        //   L1  tools/list          — per-skill `skill__<name>` entries
        //                             (name + short description), plus the
        //                             generic `skills__load` loader; the L1
        //                             protocol preamble lives in the MCP
        //                             `initialize` result's `instructions`.
        //   L2  skills__load        — returns SKILL.md + an injected protocol
        //                             preamble + the manifest of scripts /
        //                             resources / templates.
        //   L3  skills__read_resource / skills__run_script /
        //       skills__render_template — act, each governed, in the sandbox.
        //
        // There is deliberately NO one-shot "execute the skill" — that is a
        // *tool*, and offering it is exactly what makes a skill look like a
        // tool.
        // -----------------------------------------------------------------

        /// L1 discovery verb — the CLIENT-INDEPENDENT way to learn which skills
        /// exist. `tools/list` is always surfaced by MCP clients (the tool
        /// palette), whereas `initialize.instructions` is only an optional hint
        /// many clients ignore — so discovery must be reachable as a tool, not
        /// just a hint. `skills__list` returns the caller's access-scoped catalog.
        pub const TOOL_LIST: &str = "skills__list";
        pub const TOOL_LOAD: &str = "skills__load";
        pub const TOOL_READ_RESOURCE: &str = "skills__read_resource";
        pub const TOOL_RUN_SCRIPT: &str = "skills__run_script";
        pub const TOOL_RENDER_TEMPLATE: &str = "skills__render_template";

        /// L1 protocol preamble — returned in the MCP `initialize` result's
        /// `instructions` field (a hint clients MAY fold into context). The
        /// catalog is ALSO appended here, but discovery does not depend on it:
        /// `skills__list` (a tool) is the robust, client-independent path.
        pub const INITIALIZE_INSTRUCTIONS: &str = concat!(
            "These tools give you access to governed Agent Skills. Skills are guided workflows, ",
            "not tools. Call `skills__list` to see which skills are available to you (the catalog ",
            "is also listed below). To use one, call `skills__load` with its name to read its full ",
            "instructions (SKILL.md) and its manifest of scripts, resources and templates; then ",
            "follow those instructions using `skills__read_resource`, `skills__run_script`, and ",
            "`skills__render_template`. Skill code runs only inside Brutor's governed sandbox — ",
            "never attempt to fetch or execute it another way."
        );

        /// L2 protocol preamble — injected ahead of the SKILL.md body in the
        /// `skills__load` response. Adapts the (portable, unmodified) SKILL.md
        /// so the model runs everything through Brutor, not a local shell.
        pub const LOAD_PROTOCOL_PREAMBLE: &str = concat!(
            "[Brutor progressive-disclosure protocol] You have loaded this skill's instructions. ",
            "Follow the SKILL.md below. Whenever it tells you to run a script, call ",
            "`skills__run_script` with this skill's name, the script filename (from the manifest), ",
            "and its arguments. To read a referenced file, call `skills__read_resource`. To render ",
            "a template or asset, call `skills__render_template`. Use the manifest for the exact ",
            "script / resource / template names and their input schemas. Do not use a local shell ",
            "or fetch files directly — all execution happens in Brutor's governed sandbox."
        );

        /// Convert a skill name to its MCP tool name.
        /// Example: "query-sales-data" -> "skill__query-sales-data"
        pub fn tool_name(skill_name: &str) -> String {
            format!("{TOOL_PREFIX}__{skill_name}")
        }

        /// Extract skill name from MCP tool name.
        /// Example: "skill__query-sales-data" -> Some("query-sales-data")
        ///
        /// Note: the generic `skills__*` tools carry the plural `skills__`
        /// prefix and are NOT matched here (e.g. "skills__load" does not start
        /// with "skill__"), so they never parse as a per-skill entry.
        pub fn skill_name_from_tool(tool_name: &str) -> Option<&str> {
            let prefix = concat!("skill", "__");
            // Guard against the plural generic tools sharing the "skill" stem.
            if tool_name.starts_with("skills__") {
                return None;
            }
            tool_name.strip_prefix(prefix)
        }
    }
}

// =========================================================================
// Server Auth Types (app/constants/auth_type.py)
// =========================================================================

pub mod server_auth {
    pub const OAUTH: &str = "oauth";
    pub const PASSTHRU_OAUTH: &str = "passthru_oauth";
    pub const API_KEY: &str = "api_key";
    pub const ANONYMOUS: &str = "anonymous";
}

// =========================================================================
// Drift resolution the detector honours
// (the control plane's app/database/models/ai_system_drift.py)
// =========================================================================

/// How a closed drift finding was closed, and the durable facts that
/// closing records. The control plane WRITES these (resolve / false
/// positive / incident-window CRUD); the core READS them to decide which
/// samples may be judged (`services::drift::exclusion`). No compile link, so
/// both repos pin the literals.
pub mod drift_resolution {
    /// `ai_system_drift_events.resolution_kind`.
    pub mod kind {
        /// The shift is intended: a baseline epoch starts at the change point.
        pub const ACCEPTED_CHANGE: &str = "accepted_change";
        /// Something was wrong and was fixed: an incident window is declared.
        pub const CAUSE_FIXED: &str = "cause_fixed";
        /// The detector was wrong: the finding's evidence window is suppressed.
        pub const FALSE_POSITIVE: &str = "false_positive";
        /// Closed by the producer itself (dependency / agent findings that
        /// clear on their own). No effect on detection.
        pub const AUTO_CLEARED: &str = "auto_cleared";

        pub const ALL: [&str; 4] = [ACCEPTED_CHANGE, CAUSE_FIXED, FALSE_POSITIVE, AUTO_CLEARED];
        /// The kinds a human chooses, each with a defined effect on detection.
        pub const HUMAN: [&str; 3] = [ACCEPTED_CHANGE, CAUSE_FIXED, FALSE_POSITIVE];
    }

    /// `ai_system_baseline_epochs.kind` — what wrote an epoch. The core
    /// honours an epoch WHATEVER its kind (`drift::exclusion::Epochs` loads
    /// with no kind filter, pinned by `epochs_are_loaded_whatever_their_kind`),
    /// so a new kind needs no core change to take effect — only this pin.
    pub mod epoch_kind {
        /// A human accepted the shift: the baseline re-learns from the change point.
        pub const ACCEPTED_CHANGE: &str = "accepted_change";
        /// A human said the detector was wrong: re-learn from detection.
        pub const FALSE_POSITIVE: &str = "false_positive";
        /// A platform upgrade changed how the metric is MEASURED; the
        /// control plane writes it at startup from its versioned registry
        /// (`app/constants/measurement_changes.py`).
        pub const MEASUREMENT_CHANGE: &str = "measurement_change";

        pub const ALL: [&str; 3] = [ACCEPTED_CHANGE, FALSE_POSITIVE, MEASUREMENT_CHANGE];
    }

    /// `ai_system_incident_windows.cause_category`.
    pub mod cause_category {
        pub const PROVIDER_OUTAGE: &str = "provider_outage";
        pub const PLATFORM_INCIDENT: &str = "platform_incident";
        pub const MAINTENANCE: &str = "maintenance";
        pub const OTHER: &str = "other";

        pub const ALL: [&str; 4] = [PROVIDER_OUTAGE, PLATFORM_INCIDENT, MAINTENANCE, OTHER];
    }

    /// The two tables the exclusion rule reads.
    pub const INCIDENT_WINDOWS_TABLE: &str = "ai_system_incident_windows";
    pub const BASELINE_EPOCHS_TABLE: &str = "ai_system_baseline_epochs";
    /// An epoch keyed on this caller applies to the aggregate AND every
    /// per-caller sub-baseline of the same metric (same sentinel as
    /// `ai_system_baselines.caller_system_id`).
    pub const ALL_CALLERS: &str = "all";
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The resolution vocabulary and the exclusion rule's inputs.
    /// Mirrored in the control plane (`app/database/models/ai_system_drift.py`)
    /// and pinned there by `tests/test_wire_contract.py`.
    #[test]
    fn wire_contract_drift_resolution_pinned() {
        use drift_resolution::{cause_category, kind};
        assert_eq!(
            kind::ALL,
            [
                "accepted_change",
                "cause_fixed",
                "false_positive",
                "auto_cleared"
            ]
        );
        assert_eq!(
            kind::HUMAN,
            ["accepted_change", "cause_fixed", "false_positive"]
        );
        assert_eq!(
            drift_resolution::epoch_kind::ALL,
            ["accepted_change", "false_positive", "measurement_change"]
        );
        assert_eq!(
            cause_category::ALL,
            [
                "provider_outage",
                "platform_incident",
                "maintenance",
                "other"
            ]
        );
        assert_eq!(
            drift_resolution::INCIDENT_WINDOWS_TABLE,
            "ai_system_incident_windows"
        );
        assert_eq!(
            drift_resolution::BASELINE_EPOCHS_TABLE,
            "ai_system_baseline_epochs"
        );
        assert_eq!(drift_resolution::ALL_CALLERS, "all");
        assert_eq!(
            drift_resolution::ALL_CALLERS,
            crate::services::drift::baseline::ALL_CALLERS
        );
    }

    // -----------------------------------------------------------------------
    // system_server::is_system_server tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_is_system_server_agent_skill() {
        assert!(system_server::is_system_server(
            "http://system/agent-skill-server"
        ));
    }

    #[test]
    fn test_is_system_server_other_path() {
        assert!(system_server::is_system_server("http://system/other"));
    }

    #[test]
    fn test_is_system_server_external_url() {
        assert!(!system_server::is_system_server("https://api.openai.com"));
    }

    #[test]
    fn test_is_system_server_empty_string() {
        assert!(!system_server::is_system_server(""));
    }

    // -----------------------------------------------------------------------
    // system_server::agent_skill utility tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_agent_skill_tool_name() {
        assert_eq!(
            system_server::agent_skill::tool_name("query-sales-data"),
            "skill__query-sales-data"
        );
    }

    #[test]
    fn test_agent_skill_skill_name_from_tool_valid() {
        assert_eq!(
            system_server::agent_skill::skill_name_from_tool("skill__query-sales-data"),
            Some("query-sales-data")
        );
    }

    #[test]
    fn test_agent_skill_skill_name_from_tool_no_match() {
        assert_eq!(
            system_server::agent_skill::skill_name_from_tool("other__thing"),
            None
        );
    }

    #[test]
    fn test_agent_skill_skill_name_from_tool_empty_suffix() {
        assert_eq!(
            system_server::agent_skill::skill_name_from_tool("skill__"),
            Some("")
        );
    }

    // -----------------------------------------------------------------------
    // WIRE CONTRACT pin — these proxy_type / proxy_subtype / agent_decision
    // strings are read by the Python control plane (and mirrored in
    // `brutor-gateway-control/app/constants/proxy.py` + pinned by
    // `tests/test_wire_contract.py`). Changing a value here WILL silently break
    // the cross-repo query unless mirrored there. This test exists to make any
    // such rename a deliberate, loud, two-repo change rather than silent drift.
    // -----------------------------------------------------------------------

    #[test]
    fn wire_contract_proxy_types_pinned() {
        assert_eq!(proxy::MCP, "mcp");
        assert_eq!(proxy::VIRTUAL_MCP, "virtual-mcp");
        assert_eq!(proxy::LLM, "llm");
        assert_eq!(proxy::SKILL, "skill");
        assert_eq!(proxy::OAUTH, "oauth");
        assert_eq!(proxy::A2A, "a2a");
        assert_eq!(proxy::BATCH, "batch");
        assert_eq!(proxy::PORTAL, "portal");
    }

    #[test]
    fn wire_contract_agent_decisions_pinned() {
        assert_eq!(agent_decision::ALLOW, "allow");
        assert_eq!(agent_decision::DENY, "deny");
        assert_eq!(agent_decision::APPROVAL_REQUIRED, "approval_required");
    }

    #[test]
    fn wire_contract_proxy_subtypes_pinned() {
        // The subtypes the control plane queries by string equality today.
        assert_eq!(proxy_subtype::AGENT_AUTHZ_BLOCK, "agent_authz_block");
        assert_eq!(proxy_subtype::AGENT_AUTHZ_SHADOW, "agent_authz_shadow");
        assert_eq!(
            proxy_subtype::SEMANTIC_POLICY_BLOCK,
            "semantic_policy_block"
        );
        assert_eq!(
            proxy_subtype::SEMANTIC_POLICY_SHADOW,
            "semantic_policy_shadow"
        );
        assert_eq!(proxy_subtype::APPROVAL_REQUIRED, "approval_required");
        assert_eq!(proxy_subtype::RESIDENCY_VIOLATION, "residency_violation");
        assert_eq!(proxy_subtype::AUTONOMY_BLOCK, "autonomy_block");
        assert_eq!(proxy_subtype::RUN_ABORTED, "run_aborted");
        // Refusal subtypes the evidence layer maps to `denied`;
        // mirrored in the CP's `ProxySubtype` (capture-gap resolvers).
        assert_eq!(proxy_subtype::RUN_CAP_REACHED, "run_cap_reached");
        assert_eq!(proxy_subtype::ACCESS_DENIED, "access_denied");
        assert_eq!(proxy_subtype::ENGINE_FAILURE, "engine_failure");
        assert_eq!(proxy_subtype::SKILL_QUOTA_EXCEEDED, "skill_quota_exceeded");
        assert_eq!(
            proxy_subtype::SKILL_CONCURRENCY_LIMIT,
            "skill_concurrency_limit"
        );
        assert_eq!(
            proxy_subtype::APPROVAL_TOKEN_INVALID,
            "approval_token_invalid"
        );
        // Accepted-caller enforcement on a shared service.
        assert_eq!(proxy_subtype::UNACCEPTED_CALLER, "unaccepted_caller");
        assert_eq!(proxy_subtype::LLM_GOVERNANCE_BLOCK, "llm_governance_block");
    }

    /// The portal and exports read these by name.
    #[test]
    fn evidence_headers_are_pinned() {
        assert_eq!(headers::X_BRUTOR_AI_GENERATED, "x-brutor-ai-generated");
        assert_eq!(
            headers::X_BRUTOR_CONVERSATION_HANDLE,
            "x-brutor-conversation-handle"
        );
    }

    /// Guardrail surface tokens are a silent-failure vocabulary.
    ///
    /// `guardrails::surface_enabled` answers "not enabled" for a token it
    /// does not know, so a token that drifts from the control plane's
    /// spelling turns the checks on that surface OFF without any error. The
    /// literal values are therefore pinned here, and every one of them must
    /// still be recognised by the gate — the second assertion is what makes
    /// this more than a tautology.
    #[test]
    fn wire_contract_guardrail_surfaces_pinned() {
        assert_eq!(guardrail_surface::CHAT_INPUT, "chat_input");
        assert_eq!(guardrail_surface::CHAT_OUTPUT, "chat_output");
        assert_eq!(guardrail_surface::EMBEDDINGS_INPUT, "embeddings_input");
        assert_eq!(guardrail_surface::MODERATION_INPUT, "moderation_input");
        assert_eq!(guardrail_surface::MCP, "mcp");
        assert_eq!(guardrail_surface::SKILL, "skill");
        assert_eq!(guardrail_surface::A2A_INBOUND, "a2a_inbound");
        assert_eq!(guardrail_surface::A2A_OUTBOUND, "a2a_outbound");
    }

    /// Every pinned LLM surface must be one the per-check surface resolver
    /// recognises. `surface_token` returns `None` for an unknown token, and
    /// `None` means "skip the check" — so a constant the resolver has never
    /// heard of is a check that never runs.
    #[test]
    fn every_pinned_llm_surface_is_known_to_the_resolver() {
        for token in guardrail_surface::LLM_ALL {
            assert_eq!(
                crate::services::guardrails::surface_token(
                    token,
                    crate::services::guardrails::SurfaceKind::Input,
                ),
                Some(*token),
                "surface `{token}` is pinned in constants but unknown to the guardrail resolver,                  which means every check on it is silently skipped"
            );
        }
    }

    /// The run-id header is a wire contract in BOTH directions.
    ///
    /// Inbound, a client may assert `x-brutor-run-id` to group its own hops.
    /// Outbound, we return the same header naming the run a response belongs
    /// to, so feedback can be attributed to the task the user actually saw
    /// instead of being guessed from timing. Renaming either half silently
    /// breaks the other, so the constant is pinned.
    #[test]
    fn run_id_header_is_stable_in_both_directions() {
        assert_eq!(headers::X_BRUTOR_RUN_ID, "x-brutor-run-id");
        // Must match the inbound constant the delegation layer reads —
        // one identifier, one spelling.
        assert_eq!(
            headers::X_BRUTOR_RUN_ID,
            crate::services::a2a_delegation::HEADER_RUN_ID,
            "the response header must be the same name a client asserts"
        );
    }

    /// Every string an agent sends or the control plane reads.
    #[test]
    fn wire_contract_agent_impl_pinned() {
        use agent_impl::headers as h;
        use agent_impl::{baggage, claims, mcp, source, trust};
        assert_eq!(h::X_BRUTOR_AGENT_NAME, "x-brutor-agent-name");
        assert_eq!(h::X_BRUTOR_AGENT_VERSION, "x-brutor-agent-version");
        assert_eq!(h::X_BRUTOR_AGENT_BUILD, "x-brutor-agent-build");
        assert_eq!(h::X_BRUTOR_CLIENT, "x-brutor-client");
        // The documented spelling is the canonical-case one.
        for (c, doc) in [
            (h::X_BRUTOR_AGENT_NAME, "X-Brutor-Agent-Name"),
            (h::X_BRUTOR_AGENT_VERSION, "X-Brutor-Agent-Version"),
            (h::X_BRUTOR_AGENT_BUILD, "X-Brutor-Agent-Build"),
            (h::X_BRUTOR_CLIENT, "X-Brutor-Client"),
        ] {
            assert!(c.eq_ignore_ascii_case(doc));
        }
        assert_eq!(h::BAGGAGE, "baggage");
        assert_eq!(baggage::NAME, "gen_ai.agent.name");
        assert_eq!(baggage::VERSION, "gen_ai.agent.version");
        assert_eq!(baggage::BUILD, "gen_ai.agent.build");
        assert_eq!(claims::SOFTWARE_ID, "software_id");
        assert_eq!(claims::SOFTWARE_VERSION, "software_version");
        assert_eq!(claims::SOFTWARE_BUILD, "software_build");
        assert_eq!(
            mcp_protocol::meta::CLIENT_INFO,
            "io.modelcontextprotocol/clientInfo"
        );
        assert_eq!(mcp::INITIALIZE_METHOD, "initialize");
        assert_eq!(mcp::INITIALIZE_CLIENT_INFO, "clientInfo");
        assert_eq!(
            source::ALL,
            ["token", "mcp_client_info", "baggage", "header"]
        );
        assert_eq!(trust::ALL, ["verified", "declared"]);
        assert_eq!(agent_impl::BROWSER_CLIENT_LABEL, "browser");
    }
}
