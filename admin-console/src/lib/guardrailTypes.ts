/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Guardrail Types
 *
 * Matches the Pydantic models in app/api/v1/models/admin/guardrail.py
 * and the database model in app/database/models/guardrail_config.py
 *
 * Guardrails provide content filtering across all three proxy surfaces
 * (LLM, MCP, Agent Skills) with both built-in checks and external
 * provider integrations.
 */

import type { components } from './generated/control-plane-api';

// ============================================================================
// Enums / Constants
// ============================================================================

export type GuardrailAction = 'block' | 'redact' | 'warn' | 'allow';
export type GuardrailDirection = 'input' | 'output';

// Per-check provider selectors. Mirror the wire
// constants in app/api/v1/models/admin/guardrail.py.
export type PiiProvider = 'built_in' | 'presidio';
export type PromptInjectionProvider = 'built_in' | 'lakera_guard';
export type JailbreakProvider = 'built_in' | 'lakera_guard';
export type ToxicContentProvider = 'openai_moderation' | 'built_in';
export type BannedWordsMatchMode = 'substring' | 'word_boundary';

export const PII_CATEGORIES: { id: string; label: string; description: string }[] = [
    { id: 'email',            label: 'Email',            description: 'Email addresses (alice@example.com)' },
    { id: 'phone',            label: 'Phone',            description: 'Phone numbers (E.164 + common national formats)' },
    { id: 'ssn',              label: 'SSN',              description: 'US Social Security Numbers (with reserved-range filter)' },
    { id: 'credit_card',      label: 'Credit Card',      description: 'Card numbers (Luhn-validated)' },
    { id: 'iban',             label: 'IBAN',             description: 'International Bank Account Numbers (mod-97 validated)' },
    { id: 'ipv4',             label: 'IPv4',             description: 'IPv4 addresses (excluding RFC1918 private blocks)' },
    { id: 'ipv6',             label: 'IPv6',             description: 'IPv6 addresses' },
    { id: 'openai_api_key',   label: 'OpenAI API Key',   description: 'Keys matching the sk- prefix shape' },
    { id: 'anthropic_api_key',label: 'Anthropic API Key',description: 'Keys matching the sk-ant- prefix shape' },
    { id: 'aws_access_key',   label: 'AWS Access Key',   description: 'AKIA / ASIA prefixed access keys' },
    { id: 'jwt',              label: 'JWT',              description: 'Three-segment base64url tokens (header.payload.sig)' },
    { id: 'private_key',      label: 'Private Key',      description: 'PEM-armoured PRIVATE KEY blocks' },
];

export const TOXIC_CATEGORIES: { id: string; label: string }[] = [
    { id: 'hate',                       label: 'Hate' },
    { id: 'hate/threatening',           label: 'Hate / Threatening' },
    { id: 'harassment',                 label: 'Harassment' },
    { id: 'harassment/threatening',     label: 'Harassment / Threatening' },
    { id: 'self-harm',                  label: 'Self-harm' },
    { id: 'self-harm/intent',           label: 'Self-harm / Intent' },
    { id: 'self-harm/instructions',     label: 'Self-harm / Instructions' },
    { id: 'sexual',                     label: 'Sexual' },
    { id: 'sexual/minors',              label: 'Sexual / Minors' },
    { id: 'violence',                   label: 'Violence' },
    { id: 'violence/graphic',           label: 'Violence / Graphic' },
];

export const FAIL_OPEN_OVERRIDE_KEYS: { id: string; label: string }[] = [
    { id: 'pii',               label: 'PII Detection' },
    { id: 'prompt_injection',  label: 'Prompt Injection' },
    { id: 'jailbreak',         label: 'Jailbreak Detection' },
    { id: 'toxic_content',     label: 'Toxic Content' },
    { id: 'banned_words',      label: 'Banned Words' },
    { id: 'banned_patterns',   label: 'Banned Regex Patterns' },
];

export const GUARDRAIL_ACTIONS: { value: GuardrailAction; label: string; color: string; bgColor: string; description: string }[] = [
    { value: 'block',  label: 'Block',  color: 'text-red-700',    bgColor: 'bg-red-100',    description: 'Reject the request entirely' },
    { value: 'redact', label: 'Redact', color: 'text-amber-700',  bgColor: 'bg-amber-100',  description: 'Remove or mask the matched content' },
    { value: 'warn',   label: 'Warn',   color: 'text-yellow-700', bgColor: 'bg-yellow-100', description: 'Log the violation but allow through' },
    { value: 'allow',  label: 'Allow',  color: 'text-green-700',  bgColor: 'bg-green-100',  description: 'No action taken' },
];

// ============================================================================
// Nested Configuration Types
// ============================================================================

export interface GuardrailSurfaceToggles {
    // Per-mode LLM surface gates. Each matches one operational
    // route in the proxy.
    chat_input: boolean;
    chat_output: boolean;
    embeddings_input: boolean;
    image_gen_input: boolean;
    image_gen_output: boolean;
    audio_tts_input: boolean;
    audio_tts_output: boolean;
    audio_stt_input: boolean;
    audio_stt_output: boolean;
    video_gen_input: boolean;
    video_gen_output: boolean;
    batch_input: boolean;
    batch_output: boolean;
    /**
     * Moderation input — system-locked OFF for jailbreak / prompt
     * injection / toxic content checks (the orchestrator hard-bypasses
     * them even when this is true). Running a safety classifier on a
     * moderation-bound body would recurse. PII + secrets still run.
     */
    moderation_input: boolean;
    mcp_input: boolean;
    mcp_output: boolean;
    /**
     * Scan tool descriptions from MCP servers' `tools/list`
     * responses for prompt-injection payloads at server
     * registration and on every catalog refresh. Detections raise
     * a Mission Control system alert. Default true.
     */
    mcp_registration: boolean;
    skill_input: boolean;
    skill_output: boolean;
    /** Incoming A2A v1.0 message bodies. Default false. */
    a2a_inbound: boolean;
    /** Outgoing A2A delegations to peer agents. Default false. */
    a2a_outbound: boolean;
}

/**
 * Collapse the per-mode surface toggles into the coarse surface-group
 * labels shown in the UI (LLM / MCP / Skill / A2A in / A2A out).
 *
 * Shared by the **Governance → Guardrails** list and the **Resource Group →
 * Guardrails** panel so both derive surfaces identically: the backend
 * returns the per-mode tokens `chat_input`, `embeddings_input`, ….
 *
 * Accepts either the typed toggle object or the loose `Record<string,
 * boolean>` the effective-guardrails endpoint returns.
 */
export function surfaceGroupLabels(
    surfaces: GuardrailSurfaceToggles | Record<string, boolean> | null | undefined,
): string[] {
    if (!surfaces) return [];
    const s = surfaces;
    const labels: string[] = [];
    const llmActive =
        s.chat_input || s.chat_output ||
        s.embeddings_input ||
        s.image_gen_input || s.image_gen_output ||
        s.audio_tts_input || s.audio_tts_output ||
        s.audio_stt_input || s.audio_stt_output ||
        s.video_gen_input || s.video_gen_output ||
        s.batch_input || s.batch_output ||
        s.moderation_input;
    if (llmActive)                        labels.push('LLM');
    if (s.mcp_input || s.mcp_output)      labels.push('MCP');
    if (s.skill_input || s.skill_output)  labels.push('Skill');
    if (s.a2a_inbound)                    labels.push('A2A in');
    if (s.a2a_outbound)                   labels.push('A2A out');
    return labels;
}

/**
 * The surfaces a guardrail check can be put on: the names the gateway asks
 * a check's list for, and the only ones the Control Plane accepts in one
 * (`pii_surfaces`, `per_surface`, …). MCP registration is not one of them:
 * it is a switch of the configuration (`surfaces.mcp_registration`), and no
 * check list names it.
 */
export type CheckSurface =
    // Per-mode LLM surfaces. Each matches one operational route.
    | 'chat_input' | 'chat_output'
    | 'embeddings_input'
    | 'image_gen_input' | 'image_gen_output'
    | 'audio_tts_input' | 'audio_tts_output'
    | 'audio_stt_input' | 'audio_stt_output'
    | 'video_gen_input' | 'video_gen_output'
    | 'batch_input' | 'batch_output'
    | 'moderation_input'
    | 'mcp_input' | 'mcp_output'
    | 'skill_input' | 'skill_output'
    | 'a2a_inbound' | 'a2a_outbound';

/** A row of the Surfaces tab: a check surface, or the MCP registration scan. */
export type ConfigSurface = CheckSurface | 'mcp_registration';

/** Per-mode LLM input surfaces. */
export const LLM_INPUT_MODE_SURFACES: CheckSurface[] = [
    'chat_input',
    'embeddings_input',
    'image_gen_input',
    'audio_tts_input',
    'audio_stt_input',
    'video_gen_input',
    'batch_input',
];

/**
 * Per-mode LLM output surfaces that today produce client-visible text.
 * Image/audio-TTS/video outputs are media — no text guardrail applies
 * to them until a future NSFW classifier lands.
 */
export const LLM_TEXT_OUTPUT_MODE_SURFACES: CheckSurface[] = [
    'chat_output',
    'audio_stt_output',
    'batch_output',
];

export const ALL_CHECK_SURFACES: CheckSurface[] = [
    'chat_input', 'chat_output',
    'embeddings_input',
    'image_gen_input', 'image_gen_output',
    'audio_tts_input', 'audio_tts_output',
    'audio_stt_input', 'audio_stt_output',
    'video_gen_input', 'video_gen_output',
    'batch_input', 'batch_output',
    'moderation_input',
    'mcp_input', 'mcp_output',
    'skill_input', 'skill_output',
    'a2a_inbound', 'a2a_outbound',
];



/**
 * Surface metadata: proxy-type grouping, human label, operator
 * description, and the system-locked flag. Single source of truth for
 * the Surfaces tab table and the per-check "Run this check on" table.
 * Order is the render order in the table.
 *
 * The `locked` flag applies to the runtime safety bypass — the
 * moderation_input row's checkbox is always disabled and reads
 * "system-locked off" in the UI because the orchestrator hard-skips
 * jailbreak / prompt_injection / toxic_content on that surface
 * regardless of config (running a safety classifier on a moderation
 * call body would recurse).
 */
export interface SurfaceMetaEntry {
    surface: ConfigSurface;
    proxyType: 'LLM Proxy' | 'MCP Proxy' | 'Agent Skills' | 'A2A';
    label: string;
    description: string;
    locked?: boolean;
}

export const SURFACE_METADATA: SurfaceMetaEntry[] = [
    { surface: 'chat_input',       proxyType: 'LLM Proxy', label: 'Chat input',          description: 'Scan user prompts before sending to chat completions / responses / completions / messages.' },
    { surface: 'chat_output',      proxyType: 'LLM Proxy', label: 'Chat output',         description: 'Scan model responses before returning to the client. On streamed (SSE) responses, behaviour follows the Streaming output behaviour setting below.' },
    { surface: 'embeddings_input', proxyType: 'LLM Proxy', label: 'Embeddings input',    description: 'Scan documents before embedding. Most prompt-injection and jailbreak checks are inappropriate here — they false-positive on legitimate security documentation.' },
    { surface: 'image_gen_input',  proxyType: 'LLM Proxy', label: 'Image gen input',     description: 'Scan prompts before image generation.' },
    { surface: 'image_gen_output', proxyType: 'LLM Proxy', label: 'Image gen output',    description: 'Reserved for a future image NSFW classifier — no text guardrail runs yet.' },
    { surface: 'audio_tts_input',  proxyType: 'LLM Proxy', label: 'Speech (TTS) input',  description: 'Scan text before voice synthesis.' },
    { surface: 'audio_tts_output', proxyType: 'LLM Proxy', label: 'Speech (TTS) output', description: 'Reserved for a future audio NSFW classifier — no text guardrail runs yet.' },
    { surface: 'audio_stt_input',  proxyType: 'LLM Proxy', label: 'Transcription (STT) input',  description: 'Scan the optional context prompt sent with transcription requests.' },
    { surface: 'audio_stt_output', proxyType: 'LLM Proxy', label: 'Transcription (STT) output', description: 'Scan the transcribed text returned to the client.' },
    { surface: 'video_gen_input',  proxyType: 'LLM Proxy', label: 'Video gen input',     description: 'Scan prompts before video generation.' },
    { surface: 'video_gen_output', proxyType: 'LLM Proxy', label: 'Video gen output',    description: 'Reserved for a future video NSFW classifier — no text guardrail runs yet.' },
    { surface: 'batch_input',      proxyType: 'LLM Proxy', label: 'Batch input',         description: 'Scan async batch job inputs.' },
    { surface: 'batch_output',     proxyType: 'LLM Proxy', label: 'Batch output',        description: 'Scan async batch job outputs.' },
    { surface: 'moderation_input', proxyType: 'LLM Proxy', label: 'Moderation input',    description: 'System-locked. Moderation is itself the safety classifier — running jailbreak / prompt-injection / toxic-content checks here would recurse. PII + secrets still run if enabled.', locked: true },

    { surface: 'mcp_input',        proxyType: 'MCP Proxy', label: 'MCP input',           description: 'Scan tool call arguments before forwarding to MCP servers.' },
    { surface: 'mcp_output',       proxyType: 'MCP Proxy', label: 'MCP output',          description: 'Scan tool call results before returning to the client.' },
    { surface: 'mcp_registration', proxyType: 'MCP Proxy', label: 'MCP registration',    description: 'Scan tool descriptions from MCP servers’ tools/list responses at registration and on every catalog refresh. Detections raise a Mission Control system alert.' },

    { surface: 'skill_input',      proxyType: 'Agent Skills', label: 'Skill input',  description: 'Scan skill execution inputs before the runner invokes a skill.' },
    { surface: 'skill_output',     proxyType: 'Agent Skills', label: 'Skill output', description: 'Scan skill script output and template renders.' },

    { surface: 'a2a_inbound',      proxyType: 'A2A', label: 'A2A inbound',  description: 'Scan incoming A2A v1.0 message bodies (tasks.send / message.send) when an external agent invokes one of this tenant’s hosted Agent Cards. Block returns a v1.0 PolicyBlocked structured error.' },
    { surface: 'a2a_outbound',     proxyType: 'A2A', label: 'A2A outbound', description: 'Scan outgoing A2A delegations — the payload Brutor sends when one of its hosted agents calls a peer agent at another vendor or business unit. Block stops the request before it leaves the platform.' },
];

/** The rows of SURFACE_METADATA a check can be put on: every one but MCP registration. */
export const CHECK_SURFACE_METADATA = SURFACE_METADATA.filter(
    (entry): entry is SurfaceMetaEntry & { surface: CheckSurface } => entry.surface !== 'mcp_registration',
);

/**
 * Per-surface override map. A check's scalar `action`/`provider`/
 * `threshold` apply to every active surface unless overridden here.
 * Keys are surface tokens, values are partial config dicts. Only the
 * fields named here are valid; the Pydantic validator on the backend
 * rejects anything else.
 *
 *   pii / prompt_injection / jailbreak: {enabled?, action?, provider?}
 *   toxic_content:                      {enabled?, action?, provider?, threshold?}
 *   secrets:                            {enabled?, action?, threshold?}
 *   banned_words:                       {enabled?, action?, words?}
 *   banned_patterns:                    {enabled?, action?, patterns?}
 *
 * `words` / `patterns` REPLACE the global list per-surface (no merge).
 */
export type PerSurfaceOverride = Record<string, Record<string, any>>;

export interface GuardrailBuiltinConfig {
    pii: {
        enabled: boolean;
        action: GuardrailAction;
        provider?: PiiProvider;
        categories?: string[] | null;
        // Presidio score_threshold (0..1). Forwarded to the
        // analyzer; built-in regex provider ignores it. Null = use
        // Presidio's default (typically 0.5).
        threshold?: number | null;
        surfaces: CheckSurface[];
        per_surface?: PerSurfaceOverride | null;
    };
    prompt_injection: {
        enabled: boolean;
        action: GuardrailAction;
        sensitivity?: number;
        provider?: PromptInjectionProvider;
        surfaces: CheckSurface[];
        per_surface?: PerSurfaceOverride | null;
    };
    jailbreak: {
        enabled: boolean;
        action: GuardrailAction;
        provider?: JailbreakProvider;
        surfaces: CheckSurface[];
        per_surface?: PerSurfaceOverride | null;
    };
    toxic_content: {
        enabled: boolean;
        action: GuardrailAction;
        threshold?: number;
        provider?: ToxicContentProvider;
        category_thresholds?: Record<string, number> | null;
        surfaces: CheckSurface[];
        per_surface?: PerSurfaceOverride | null;
    };
    banned_words: string[];
    banned_patterns: string[];
    banned_words_match_mode?: BannedWordsMatchMode;
    banned_words_surfaces: CheckSurface[];
    banned_patterns_surfaces: CheckSurface[];
    banned_words_per_surface?: PerSurfaceOverride | null;
    banned_patterns_per_surface?: PerSurfaceOverride | null;
    // Secrets detection.
    secrets?: {
        enabled: boolean;
        action: GuardrailAction;
        categories: SecretsCategory[];
        surfaces: CheckSurface[];
        threshold?: number;
        per_surface?: PerSurfaceOverride | null;
    };
}

/**
 * Secret category wire tokens. Must match
 * the Rust `SecretsDetector::known_categories()` and the
 * `ALLOWED_SECRETS_CATEGORIES` constant in
 * `brutor-gateway-control/app/api/v1/models/admin/guardrail.py`.
 */
export type SecretsCategory =
    | 'aws_access_key' | 'aws_secret_key'
    | 'github_token' | 'openai_key'
    | 'gcp_service_account' | 'private_key_pem'
    | 'jwt' | 'generic_high_entropy';

export const ALL_SECRETS_CATEGORIES: SecretsCategory[] = [
    'aws_access_key', 'aws_secret_key',
    'github_token', 'openai_key',
    'gcp_service_account', 'private_key_pem',
    'jwt', 'generic_high_entropy',
];

/** Categories shipped on by default. */
export const DEFAULT_SECRETS_CATEGORIES: SecretsCategory[] = [
    'aws_access_key', 'aws_secret_key',
    'github_token', 'openai_key',
    'gcp_service_account', 'private_key_pem',
    'jwt',
];

/**
 * A provider of a configuration as the Control Plane answers with it. A
 * provider has no mode and no surfaces of its own: a standalone provider
 * runs on every surface the configuration enables, a per-check one where
 * its check runs. The secrets are never in the answer; `config_secrets_stored` says, for each secret of `config`
 * (`aws_secret_access_key`, `aws_session_token`), whether a value is stored:
 * a save that leaves one out keeps it, `null` removes it.
 */
export type GuardrailProviderConfig = components['schemas']['GuardrailProviderResponse'];

// ============================================================================
// Response Models (from API)
// ============================================================================

export interface GuardrailGroupInfo {
    id: string;
    name: string;
    display_name?: string;
    parent_group_id?: string;
    is_active?: boolean;
}

export interface GuardrailConfigResponse {
    id: string;
    tenant_id: string;
    name: string;
    description?: string;
    is_default: boolean;
    priority: number;
    // Per-call scope filters surfaced on responses
    // so the form preloads them and the list view can show
    // "scoped to: srv-a / tool: search" at a glance.
    scope_mcp_server_id?: string | null;
    scope_tool_name?: string | null;
    /** Streaming output guardrail posture. */
    streaming_output_mode?: 'auto' | 'sync' | 'async';
    group_ids: string[];
    groups: GuardrailGroupInfo[];
    enabled: boolean;
    surfaces: GuardrailSurfaceToggles;
    builtin: GuardrailBuiltinConfig;
    providers: GuardrailProviderConfig[];
    max_check_timeout_ms: number;
    fail_open: boolean;
    fail_open_overrides?: Record<string, boolean> | null;
    log_all_checks: boolean;
    created_at?: string;
    updated_at?: string;
}

export interface GuardrailConfigDetailResponse {
    config: GuardrailConfigResponse;
}

export interface GuardrailConfigListResponse {
    configs: GuardrailConfigResponse[];
    total: number;
}

// ============================================================================
// Request Models (to API)
// ============================================================================

export interface GuardrailSurfaceTogglesRequest {
    // Per-mode LLM surface gates.
    chat_input?: boolean;
    chat_output?: boolean;
    embeddings_input?: boolean;
    image_gen_input?: boolean;
    image_gen_output?: boolean;
    audio_tts_input?: boolean;
    audio_tts_output?: boolean;
    audio_stt_input?: boolean;
    audio_stt_output?: boolean;
    video_gen_input?: boolean;
    video_gen_output?: boolean;
    batch_input?: boolean;
    batch_output?: boolean;
    moderation_input?: boolean;
    mcp_input?: boolean;
    mcp_output?: boolean;
    mcp_registration?: boolean;
    skill_input?: boolean;
    skill_output?: boolean;
    a2a_inbound?: boolean;
    a2a_outbound?: boolean;
}

export interface GuardrailBuiltinConfigRequest {
    pii_enabled?: boolean;
    pii_action?: string;
    pii_provider?: PiiProvider;
    pii_categories?: string[] | null;
    /**
     * Presidio /analyze score_threshold. 0..1. Ignored by the
     * built-in regex provider. Null/undefined falls back to Presidio's
     * own default.
     */
    pii_threshold?: number | null;
    prompt_injection_enabled?: boolean;
    prompt_injection_action?: string;
    prompt_injection_sensitivity?: number;
    prompt_injection_provider?: PromptInjectionProvider;
    jailbreak_enabled?: boolean;
    jailbreak_action?: string;
    jailbreak_provider?: JailbreakProvider;
    toxic_content_enabled?: boolean;
    toxic_content_action?: string;
    toxic_content_threshold?: number;
    toxic_content_provider?: ToxicContentProvider;
    toxic_content_category_thresholds?: Record<string, number> | null;
    banned_words?: string[];
    banned_patterns?: string[];
    banned_words_match_mode?: BannedWordsMatchMode;
    // Per-check surfaces. Null/undefined means
    // "leave the row unchanged" so a PATCH that only flips an action
    // doesn't accidentally clobber the surface set.
    pii_surfaces?: CheckSurface[] | null;
    prompt_injection_surfaces?: CheckSurface[] | null;
    jailbreak_surfaces?: CheckSurface[] | null;
    toxic_content_surfaces?: CheckSurface[] | null;
    banned_words_surfaces?: CheckSurface[] | null;
    banned_patterns_surfaces?: CheckSurface[] | null;
    // Secrets detection.
    secrets_enabled?: boolean;
    secrets_action?: string;
    secrets_categories?: SecretsCategory[] | null;
    secrets_surfaces?: CheckSurface[] | null;
    secrets_threshold?: number;
    // Per-surface override maps. Null/undefined leaves the column
    // unchanged (PATCH-style); explicit `null` clears it.
    pii_per_surface?: PerSurfaceOverride | null;
    prompt_injection_per_surface?: PerSurfaceOverride | null;
    jailbreak_per_surface?: PerSurfaceOverride | null;
    toxic_content_per_surface?: PerSurfaceOverride | null;
    secrets_per_surface?: PerSurfaceOverride | null;
    banned_words_per_surface?: PerSurfaceOverride | null;
    banned_patterns_per_surface?: PerSurfaceOverride | null;
}

/**
 * A provider as a save names it: the contract's own type. It has no `mode`
 * and no `surfaces`; the Control Plane's model of a provider names neither.
 */
export type GuardrailProviderConfigRequest = components['schemas']['GuardrailProviderConfig'];

export interface GuardrailConfigUpdateRequest {
    name?: string;
    description?: string;
    is_default?: boolean;
    priority?: number;
    group_ids?: string[];
    // Per-call scope filters. Null/empty = applies to
    // all MCP calls. Set to narrow the config to a specific server
    // and/or tool. See trial_bundle/docs/reference/...
    scope_mcp_server_id?: string | null;
    scope_tool_name?: string | null;
    /** Streaming output guardrail posture. */
    streaming_output_mode?: 'auto' | 'sync' | 'async';
    enabled: boolean;
    surfaces?: GuardrailSurfaceTogglesRequest;
    builtin?: GuardrailBuiltinConfigRequest;
    providers?: GuardrailProviderConfigRequest[];
    max_check_timeout_ms?: number;
    fail_open?: boolean;
    fail_open_overrides?: Record<string, boolean> | null;
    log_all_checks?: boolean;
}

export interface GuardrailConfigCreateRequest {
    name: string;
    description?: string;
    is_default?: boolean;
    priority?: number;
    group_ids?: string[];
    // Per-call scope filters (see Update interface).
    scope_mcp_server_id?: string | null;
    scope_tool_name?: string | null;
    /** Streaming output guardrail posture. */
    streaming_output_mode?: 'auto' | 'sync' | 'async';
    enabled: boolean;
    surfaces?: GuardrailSurfaceTogglesRequest;
    builtin?: GuardrailBuiltinConfigRequest;
    providers?: GuardrailProviderConfigRequest[];
    max_check_timeout_ms?: number;
    fail_open?: boolean;
    fail_open_overrides?: Record<string, boolean> | null;
    log_all_checks?: boolean;
}

export interface GuardrailGroupAssociationRequest {
    group_ids: string[];
}

// ============================================================================
// Test Models
// ============================================================================

export interface GuardrailTestRequest {
    content: string;
    /**
     * Per-mode LLM tokens (chat_input, chat_output, embeddings_input, …)
     * encode direction in the name; non-LLM kinds ('mcp', 'skill') take
     * an explicit direction; a2a_inbound / a2a_outbound encode direction
     * in the name. The backend Pydantic validator (`ALLOWED_SURFACES`)
     * is the source of truth — kept as `string` here so the UI never
     * has to mirror the runtime allowlist.
     */
    surface: string;
    direction: GuardrailDirection;
    config_id?: string;
}

export interface GuardrailViolation {
    type: string;
    provider: string;
    confidence: number;
    action: GuardrailAction;
    detail?: string;
    config_id?: string;
    config_name?: string;
}

export interface GuardrailTestResponse {
    action: GuardrailAction;
    blocked: boolean;
    violations: GuardrailViolation[];
    duration_ms: number;
    surface: string;
    direction: string;
    configs_evaluated: number;
    config_id?: string;
    config_name?: string;
}

// ============================================================================
// Provider Info
// ============================================================================

export interface GuardrailProviderInfo {
    id: string;
    name: string;
    description: string;
    modes: string[];
    requires_api_key: boolean;
}

export interface GuardrailProviderListResponse {
    providers: GuardrailProviderInfo[];
}

// ============================================================================
// UI Helpers
// ============================================================================

export function getActionConfig(action: GuardrailAction) {
    return GUARDRAIL_ACTIONS.find(a => a.value === action) || GUARDRAIL_ACTIONS[3];
}

/**
 * Count the number of enabled checks in a config
 */
export function countEnabledChecks(config: GuardrailConfigResponse): number {
    let count = 0;
    if (config.builtin.pii.enabled) count++;
    if (config.builtin.prompt_injection.enabled) count++;
    if (config.builtin.jailbreak.enabled) count++;
    if (config.builtin.toxic_content.enabled) count++;
    if (config.builtin.banned_words.length > 0) count++;
    if (config.builtin.banned_patterns.length > 0) count++;
    count += config.providers.filter(p => p.enabled).length;
    return count;
}

/**
 * Count the number of active surfaces
 */
export function countActiveSurfaces(config: GuardrailConfigResponse): number {
    const s = config.surfaces;
    const llmActive = s.chat_input || s.chat_output
        || s.embeddings_input
        || s.image_gen_input || s.image_gen_output
        || s.audio_tts_input || s.audio_tts_output
        || s.audio_stt_input || s.audio_stt_output
        || s.video_gen_input || s.video_gen_output
        || s.batch_input || s.batch_output
        || s.moderation_input;
    let count = 0;
    if (llmActive) count++;
    if (s.mcp_input || s.mcp_output || s.mcp_registration) count++;
    if (s.skill_input || s.skill_output) count++;
    if (s.a2a_inbound) count++;
    if (s.a2a_outbound) count++;
    return count;
}


/**
 * Guardrail break-glass.
 *
 * A declared detector outage during which checks degrade to log-and-allow.
 * The runtime effect is identical to a hardcoded fail-open — what makes it
 * acceptable is that it is attributed, carries a reason, and expires. Render
 * all three; a break-glass banner without its expiry invites the operator to
 * forget it is on.
 */
export interface BreakGlassWindow {
    id: string;
    tenant_id: string;
    reason: string;
    activated_by: string;
    activated_at: string | null;
    expires_at: string | null;
    deactivated_at: string | null;
    deactivated_by: string | null;
    is_active: boolean;
    remaining_seconds: number;
}

export interface BreakGlassState {
    active: boolean;
    current: BreakGlassWindow | null;
    history: BreakGlassWindow[];
    max_hours: number;
    default_minutes: number;
}
