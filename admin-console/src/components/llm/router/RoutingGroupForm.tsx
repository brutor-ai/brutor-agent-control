/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef } from 'react';
import {
    Save,
    Info,
    HelpCircle,
    X,
    Zap,
    Shield,
    Clock,
    RefreshCw,
    Gauge,
    ChevronDown,
    FileClock,
    Share2,
} from 'lucide-react';
import {
    RoutingGroup,
    RoutingGroupCreate,
    RoutingGroupMode,
    RoutingStrategy,
    ComplexityTier,
    RoutingGroupTierAssignment,
} from '../../../lib/llmTypes';
import AuditInfoTab from '../../common/AuditInfoTab';
import ComplexityTierEditor from './ComplexityTierEditor';
import { asyncHandler } from '../../../lib/asyncSafe';

/**
 * Inbound surfaces a routing group can serve. Mirrors the
 * server-side `RoutingGroupMode` enum (which excludes the
 * synthetic `routing` value reserved for the virtual model
 * the control plane creates per group). Order is the order
 * shown in the picker — chat first because it's the most
 * common, embedding next as second-most common, then the
 * remaining text-style surfaces, then media surfaces.
 */
const ROUTING_GROUP_MODES: { value: RoutingGroupMode; label: string; subtitle: string }[] = [
    { value: 'chat',                  label: 'Chat completions',     subtitle: '/v1/chat/completions — multi-turn conversations' },
    { value: 'responses',             label: 'Responses',            subtitle: '/v1/responses — OpenAI Responses API' },
    { value: 'completion',            label: 'Text completion',      subtitle: '/v1/completions — single-prompt completion (legacy)' },
    { value: 'embedding',             label: 'Embeddings',           subtitle: '/v1/embeddings — vector embeddings for KB / cache' },
    { value: 'image_generation',      label: 'Image generation',     subtitle: '/v1/images/generations — text-to-image' },
    { value: 'audio_speech',          label: 'Audio (TTS)',          subtitle: '/v1/audio/speech — text-to-speech' },
    { value: 'audio_transcription',   label: 'Audio (Transcription)',subtitle: '/v1/audio/transcriptions — speech-to-text' },
    { value: 'video_generation',      label: 'Video generation',     subtitle: '/v1/videos/generations — text-to-video' },
    { value: 'rerank',                label: 'Rerank',               subtitle: '/v1/rerank — re-rank documents by relevance' },
    { value: 'moderation',            label: 'Moderation',           subtitle: '/v1/moderations — content moderation' },
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

// ─── Section definitions ─────────────────────────────────────────────────────

interface SectionDef {
    id: string;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
}

const SECTIONS: SectionDef[] = [
    { id: 'basic',      label: 'Basic Information',  icon: Share2 },
    { id: 'audit',      label: 'Audit Info',         icon: FileClock },
    { id: 'strategy',   label: 'Routing Strategy',   icon: Zap },
    { id: 'fallbacks',  label: 'Fallbacks',          icon: Shield },
    { id: 'cooldowns',  label: 'Failure Handling',   icon: Clock },
    { id: 'retries',    label: 'Retries & Timeouts', icon: RefreshCw },
    { id: 'ratelimits', label: 'Rate Limiting',      icon: Gauge },
    // Note — there is no `Response Caching` or `Cost Tracking`
    // section. Response caching is configured per tenant via the
    // Semantic Cache admin tab; spend caps are configured per
    // resource group via the Usage Limits surface. Both apply
    // uniformly to routing-group requests.
];

// ─── Contextual help content per section ─────────────────────────────────────

const HELP_CONTENT: Record<string, React.ReactNode> = {
    audit: (
        <div className="space-y-2 text-xs text-secondary">
            <p>
                <strong>Version</strong> is a label you choose — a handle for humans, not a
                content hash. Change it when you make a change worth announcing to the teams
                that route through this group.
            </p>
            <p>
                Created and updated are recorded by the platform from the signed-in account
                and cannot be edited. Rows the platform creates itself — boot seeds and
                provisioning — are attributed to <code>system</code>.
            </p>
        </div>
    ),
    basic: (
        <div className="space-y-2 text-xs text-secondary">
            <p>
                Routing groups act as virtual model endpoints. Clients send requests to the
                group ID and the router selects the best model transparently.
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded p-2 text-blue-800">
                <p className="font-medium mb-1">How to set up:</p>
                <ol className="list-decimal ml-4 space-y-0.5">
                    <li>Create a routing group with your desired strategy</li>
                    <li>Go to <strong>AI Models | Configuration</strong> tab and assign models to the group</li>
                    <li>Clients use the routing group ID as the model name in API calls</li>
                </ol>
            </div>
        </div>
    ),
    strategy: (
        <div className="space-y-2 text-xs text-secondary">
            <p>Choose how requests are distributed across models in this group.</p>
            <div className="space-y-1.5">
                <div className="p-1.5 bg-surface-alt rounded"><strong>Weighted Shuffle</strong> <code className="text-[10px] text-muted ml-1">simple-shuffle</code> — Distributes randomly or by assigned weights. Most performant with lowest overhead. <em className="text-muted">Best for high-traffic production with similar models.</em></div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Queue-Aware</strong> <code className="text-[10px] text-muted ml-1">least-busy</code> — Routes to deployment with fewest pending requests. Prevents bottlenecking. <em className="text-muted">Best for varying request lengths or slower self-hosted models.</em></div>
                <div className="p-1.5 bg-yellow-50 border border-yellow-200 rounded"><strong>TPM/RPM Aware (v1)</strong> <code className="text-[10px] text-muted ml-1">usage-based-routing</code> <span className="text-[10px] bg-yellow-100 px-1 py-0.5 rounded">Legacy</span> — Basic distribution by token/request usage. No Redis, less accurate distributed. <em className="text-muted">Best for local testing or single-instance gateways.</em></div>
                <div className="p-1.5 bg-green-50 border border-green-200 rounded"><strong>Rate-Limit Optimized</strong> <code className="text-[10px] text-muted ml-1">usage-based-routing-v2</code> <span className="text-[10px] bg-green-100 px-1 py-0.5 rounded">Recommended</span> — Uses Redis for real-time TPM/RPM tracking. Stays under provider limits while maximizing throughput. <em className="text-muted">Best for multiple API keys or providers with strict rate limits.</em></div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Fastest Response</strong> <code className="text-[10px] text-muted ml-1">latency-based-routing</code> — Monitors historical response times and routes to the best performer. <em className="text-muted">Best for latency-sensitive apps where 100ms matters.</em></div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Cheapest Healthy</strong> <code className="text-[10px] text-muted ml-1">cost-based-routing</code> — Always picks the cheapest healthy deployment by blended input+output token price, falling back only when it is down. Selection is deterministic, so across models of <em>different</em> capability this is a permanent pin on the cheapest one, not a per-request optimization. <em className="text-muted">Best for arbitrage between equivalent deployments (same model, two providers or regions) plus failover.</em></div>
                <div className="p-1.5 bg-amber-50 border border-amber-200 rounded"><strong>Prompt Complexity</strong> <code className="text-[10px] text-muted ml-1">complexity-based-routing</code> <span className="text-[10px] bg-amber-100 px-1 py-0.5 rounded">Experimental</span> — Scores each prompt locally and sends it to the model you mapped to that tier. No extra model call, sub-millisecond, deterministic, and the console shows which signals fired. <strong>Unlike every other strategy here it selects a different <em>capability</em>, not a different instance of an equivalent one</strong> — so it can change the answer, and we have not yet measured that effect. <em className="text-muted">Treat as experimental: verify on your own traffic before relying on it.</em></div>
            </div>
        </div>
    ),
    fallbacks: (
        <div className="space-y-2 text-xs text-secondary">
            <p>Define which alternative routing groups to try when primary models fail.</p>
            <div className="space-y-1.5">
                <div className="p-1.5 bg-surface-alt rounded"><strong>General</strong> — Tried on any failure (timeouts, rate limits, API errors).</div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Context Window</strong> — Tried when input exceeds model's context window.</div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Content Policy</strong> — Tried when model rejects content due to safety filters.</div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Default</strong> — Catch-all when no specific type matches.</div>
            </div>
            <div className="bg-yellow-50 border border-yellow-200 rounded p-2 text-yellow-800">
                <strong>Note:</strong> Values are <em>other routing group IDs</em>, not individual model IDs.
            </div>
        </div>
    ),
    cooldowns: (
        <div className="space-y-2 text-xs text-secondary">
            <p>When models fail, the router can temporarily exclude them to avoid repeatedly hitting a broken endpoint.</p>
            <div className="space-y-1.5">
                <div className="p-1.5 bg-surface-alt rounded"><strong>Cooldown Time</strong> — How long a model is excluded after it fails.</div>
            </div>
        </div>
    ),
    retries: (
        <div className="space-y-2 text-xs text-secondary">
            <div className="space-y-1.5">
                <div className="p-1.5 bg-surface-alt rounded"><strong>Retries</strong> — Times to retry on same model before trying next.</div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Retry After</strong> — Seconds to wait between retries (helps with rate limits).</div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Stream Timeout</strong> — Seconds a streamed call waits for the first chunk of the model it was routed to; a model silent past it is given up on and the call fails over to the next. Empty: no such wait of its own.</div>
            </div>
        </div>
    ),
    ratelimits: (
        <div className="space-y-2 text-xs text-secondary">
            <p>Limit request throughput at the routing group level to prevent overloading models or exceeding provider quotas.</p>
            <div className="space-y-1.5">
                <div className="p-1.5 bg-surface-alt rounded"><strong>RPM</strong> — Requests per minute across the group.</div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>TPM</strong> — Tokens per minute across the group.</div>
                <div className="p-1.5 bg-surface-alt rounded"><strong>Max Parallel</strong> — Maximum concurrent requests per deployment.</div>
            </div>
        </div>
    ),
};

// ─── Sub-components ──────────────────────────────────────────────────────────

const FieldHint: React.FC<{ text: string }> = ({ text }) => (
    <p className="text-xs text-muted mt-1 flex items-start space-x-1">
        <Info className="w-3 h-3 text-muted mt-0.5 flex-shrink-0" />
        <span>{text}</span>
    </p>
);

/** Multi-select picker for choosing fallback routing groups */
const FallbackGroupPicker: React.FC<{
    label: string;
    hint: string;
    selected: string[];
    onChange: (ids: string[]) => void;
    options: { id: string; name: string }[];
}> = ({ label, hint, selected, onChange, options }) => {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);

    // Close on outside click
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, []);

    const available = options.filter(g => !selected.includes(g.id));

    const addGroup = (id: string) => {
        onChange([...selected, id]);
    };
    const removeGroup = (id: string) => {
        onChange(selected.filter(s => s !== id));
    };
    const moveUp = (idx: number) => {
        if (idx === 0) return;
        const next = [...selected];
        [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
        onChange(next);
    };
    const moveDown = (idx: number) => {
        if (idx >= selected.length - 1) return;
        const next = [...selected];
        [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
        onChange(next);
    };

    const getGroupName = (id: string) => {
        const g = options.find(o => o.id === id);
        return g ? g.name : id;
    };

    return (
        <div ref={ref}>
            <label className="block text-sm font-medium text-secondary mb-1">{label}</label>

            {/* Selected tags with order controls */}
            {selected.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-2">
                    {selected.map((id, idx) => (
                        <span
                            key={id}
                            className="inline-flex items-center gap-1 px-2 py-1 bg-primary-50 text-primary-800 border border-primary-200 rounded-md text-xs"
                        >
                            <span className="text-[10px] text-primary-400 font-mono mr-0.5">{idx + 1}</span>
                            <span className="font-medium">{getGroupName(id)}</span>
                            <span className="text-[10px] text-primary-400 font-mono">({id})</span>
                            <button
                                type="button"
                                onClick={() => moveUp(idx)}
                                disabled={idx === 0}
                                className="text-primary-400 hover:text-primary-700 disabled:opacity-30 p-0"
                                title="Move up (higher priority)"
                            >↑</button>
                            <button
                                type="button"
                                onClick={() => moveDown(idx)}
                                disabled={idx === selected.length - 1}
                                className="text-primary-400 hover:text-primary-700 disabled:opacity-30 p-0"
                                title="Move down (lower priority)"
                            >↓</button>
                            <button
                                type="button"
                                onClick={() => removeGroup(id)}
                                className="text-primary-400 hover:text-red-600 p-0 ml-0.5"
                                title="Remove"
                            >
                                <X className="w-3 h-3" />
                            </button>
                        </span>
                    ))}
                </div>
            )}

            {/* Dropdown trigger */}
            <div className="relative">
                <button
                    type="button"
                    onClick={() => setOpen(!open)}
                    className="w-full flex items-center justify-between px-3 py-2 border border-strong rounded-lg bg-surface text-left hover:border-gray-400 focus:ring-2 focus:ring-primary-500 focus:border-transparent text-sm"
                >
                    <span className={available.length === 0 ? 'text-muted' : 'text-secondary'}>
                        {available.length === 0
                            ? (options.length === 0 ? 'No other routing groups available' : 'All groups added')
                            : `Add fallback group (${available.length} available)…`}
                    </span>
                    <ChevronDown className={`w-4 h-4 text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
                </button>

                {open && available.length > 0 && (
                    <div className="absolute z-50 mt-1 w-full bg-surface border border-default rounded-lg shadow-lg max-h-48 overflow-y-auto">
                        {available.map(g => (
                            <button
                                key={g.id}
                                type="button"
                                onClick={() => { addGroup(g.id); setOpen(false); }}
                                className="w-full flex items-center justify-between px-3 py-2 text-sm text-left hover:bg-primary-50 border-b border-gray-50 last:border-0"
                            >
                                <span className="font-medium text-default">{g.name}</span>
                                <span className="text-xs text-muted font-mono">{g.id}</span>
                            </button>
                        ))}
                    </div>
                )}
            </div>

            <FieldHint text={hint} />
        </div>
    );
};

// ─── Form state ──────────────────────────────────────────────────────────────

/**
 * The fields of a routing group's requests the gateway does not read:
 * alerting, the pre-call capability check and the router's own retry and
 * blocking tuning. The Control Plane refuses a value for one (`false` for
 * `alerting_enabled` aside) and no response shows them, so the form holds none.
 */
type RoutingGroupUnavailableField =
    | 'alerting_enabled'
    | 'alerting_threshold'
    | 'alerting_webhook_url'
    | 'alerting_config'
    | 'required_capabilities'
    | 'routing_max_retries'
    | 'routing_retry_delay'
    | 'ttl_for_blocked_models'
    | 'router_settings';

/**
 * What the form holds: the fields of the Control Plane's create request (an
 * update takes the same ones, without the id), and the four fallback lists
 * under their own names, which are sent under the contract's on submit.
 */
type RoutingGroupFormState = Omit<Partial<RoutingGroupCreate>, RoutingGroupUnavailableField> & {
    _fallback_models: string[];
    _context_window_fallbacks: string[];
    _content_policy_fallbacks: string[];
    _default_fallbacks: string[];
};

// ─── Props ───────────────────────────────────────────────────────────────────

interface RoutingGroupFormProps {
    routingGroup?: RoutingGroup | null;
    isEditing: boolean;
    /** The fields to send: every one a key of the Control Plane's create request; an edit sends no id. */
    onSubmit: (data: Partial<RoutingGroupCreate>) => Promise<void>;
    onCancel: () => void;
    /** All routing groups (for fallback picker). Current group is excluded automatically. */
    availableGroups?: { id: string; name: string }[];
    /** Members of THIS group — the only legal targets for a complexity tier. */
    groupModels?: { id: string; name: string }[];
    /** Existing tier map, when editing. */
    initialTiers?: RoutingGroupTierAssignment[];
    /** Fired when the tier map changes so the parent can persist it after save. */
    onTiersChange?: (tiers: RoutingGroupTierAssignment[]) => void;
}

// ─── Component ───────────────────────────────────────────────────────────────

const RoutingGroupForm: React.FC<RoutingGroupFormProps> = ({
                                                               groupModels = [],
                                                               initialTiers,
                                                               onTiersChange,
                                                               routingGroup,
                                                               isEditing,
                                                               onSubmit,
                                                               onCancel,
                                                               availableGroups = [],
                                                           }) => {
    // ── State ────────────────────────────────────────────────────────────
    const [activeSection, setActiveSection] = useState('basic');
    const [helpOpen, setHelpOpen] = useState(false);

    // Tier map lives outside `formData` because it is persisted by a separate
    // endpoint (PUT .../tiers) rather than as a column on the group.
    const [tiers, setTiers] = useState<RoutingGroupTierAssignment[]>(initialTiers ?? []);

    useEffect(() => {
        setTiers(initialTiers ?? []);
    }, [initialTiers]);

    const [formData, setFormData] = useState<RoutingGroupFormState>({
        id: '',
        name: '',
        description: '',
        version: '1.0',
        mode: 'chat' as RoutingGroupMode,
        routing_strategy: 'usage-based-routing-v2',
        cooldown_time: 60,
        _fallback_models: [] as string[],
        _context_window_fallbacks: [] as string[],
        _content_policy_fallbacks: [] as string[],
        _default_fallbacks: [] as string[],
        max_fallbacks: 5,
        num_retries: 3,
        retry_after: 5,
        rpm_limit: undefined,
        tpm_limit: undefined,
        max_parallel_requests: undefined,
        stream_timeout: undefined,
        enabled: true,
    });

    const [submitting, setSubmitting] = useState(false);
    const [errors, setErrors] = useState<Record<string, string>>({});

    // ── Populate on edit ─────────────────────────────────────────────────
    useEffect(() => {
        if (routingGroup && isEditing) {
            setFormData({
                name: routingGroup.name,
                description: routingGroup.description || '',
                version: routingGroup.version ?? '1.0',
                mode: (routingGroup.mode || 'chat'),
                routing_strategy: routingGroup.routing_strategy,
                complexity_floor_tier: (routingGroup as any).complexity_floor_tier ?? undefined,
                cooldown_time: routingGroup.cooldown_time,
                _fallback_models: routingGroup.fallback_models || [],
                _context_window_fallbacks: routingGroup.context_window_fallbacks || [],
                _content_policy_fallbacks: routingGroup.content_policy_fallbacks || [],
                _default_fallbacks: routingGroup.default_fallbacks || [],
                max_fallbacks: routingGroup.max_fallbacks ?? 5,
                num_retries: routingGroup.num_retries,
                retry_after: routingGroup.retry_after,
                rpm_limit: routingGroup.rpm_limit,
                tpm_limit: routingGroup.tpm_limit,
                max_parallel_requests: routingGroup.max_parallel_requests,
                stream_timeout: routingGroup.stream_timeout ?? undefined,
                enabled: routingGroup.enabled,
            });
        }
    }, [routingGroup, isEditing]);

    // ── Close help when switching sections ────────────────────────────────
    useEffect(() => {
        setHelpOpen(false);
    }, [activeSection]);

    // ── Strategies ───────────────────────────────────────────────────────
    const routingStrategies: { value: RoutingStrategy; label: string; subtitle: string; description: string; bestFor: string }[] = [
        { value: 'simple-shuffle', label: 'Weighted Shuffle', subtitle: 'simple-shuffle', description: 'Distributes requests randomly or based on assigned weights. Most performant strategy with the lowest overhead.', bestFor: 'High-traffic production where models have similar performance.' },
        { value: 'least-busy', label: 'Queue-Aware', subtitle: 'least-busy', description: 'Routes to the deployment with the fewest active, pending requests. Helps prevent individual endpoints from bottlenecking.', bestFor: 'Scenarios with varying request lengths or slower self-hosted models.' },
        { value: 'usage-based-routing', label: 'TPM/RPM Aware (v1)', subtitle: 'usage-based-routing — Legacy', description: 'Basic distribution based on current token/request usage. Does not use Redis and is generally less accurate for distributed setups.', bestFor: 'Simple local testing or single-instance gateways.' },
        { value: 'usage-based-routing-v2', label: 'Rate-Limit Optimized', subtitle: 'usage-based-routing-v2', description: 'Uses Redis to track real-time TPM/RPM. Intelligent selection ensures you stay under provider limits while maximizing throughput.', bestFor: 'Managing multiple API keys or providers with strict rate limits.' },
        { value: 'latency-based-routing', label: 'Fastest Response', subtitle: 'latency-based-routing', description: 'Continuously monitors historical response times and routes to the provider currently performing the best.', bestFor: 'Latency-sensitive apps where a 100ms difference matters (e.g., chatbots).' },
        { value: 'cost-based-routing', label: 'Cheapest Healthy', subtitle: 'cost-based-routing', description: 'Always picks the cheapest healthy deployment, ranked on blended input + output token price. Selection is deterministic: across models of different capability this is a permanent pin on the cheapest one, not a per-request optimization — it only changes model when that deployment is unhealthy.', bestFor: 'Arbitrage between equivalent deployments — the same model at two providers, regions, or reserved vs on-demand — plus failover.' },
        { value: 'complexity-based-routing', label: 'Prompt Complexity', subtitle: 'complexity-based-routing', description: 'EXPERIMENTAL. Scores each prompt locally — no extra model call, sub-millisecond, and the same prompt always scores the same — then sends it to the model you mapped to the resulting tier: Simple, Medium, Complex or Reasoning. Every other strategy in this list picks between deployments that are interchangeable for the task; this one deliberately picks a different capability, so it can change the answer as well as the price. That effect is not yet measured, and a tier map that is wrong for your traffic will quietly degrade quality. Set a Floor Tier to guarantee it never drops below a given model.', bestFor: 'Evaluation on traffic you can verify. Mixed workloads where difficulty genuinely varies — but confirm quality on your own prompts first.' },
    ];

    // ── Validation ───────────────────────────────────────────────────────
    const validate = (): Record<string, string> => {
        const newErrors: Record<string, string> = {};
        if (!isEditing && !formData.id?.trim()) newErrors.id = 'ID is required';
        if (!formData.name?.trim()) newErrors.name = 'Name is required';
        if ((formData.cooldown_time ?? 0) < 0) newErrors.cooldown_time = 'Must be 0 or greater';
        if ((formData.num_retries ?? 0) < 0) newErrors.num_retries = 'Must be 0 or greater';
        if (formData.stream_timeout != null && !(formData.stream_timeout > 0 && formData.stream_timeout <= 1800)) {
            newErrors.stream_timeout = 'Greater than 0 and at most 1800 seconds';
        }
        setErrors(newErrors);
        return newErrors;
    };

    // ── Submit ───────────────────────────────────────────────────────────
    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        const validationErrors = validate();
        if (Object.keys(validationErrors).length > 0) {
            // Navigate to the first section with an error
            const errorSections: Record<string, string> = {
                id: 'basic', name: 'basic',
                cooldown_time: 'cooldowns',
                num_retries: 'retries',
                stream_timeout: 'retries',
            };
            for (const key of Object.keys(validationErrors)) {
                if (errorSections[key]) { setActiveSection(errorSections[key]); break; }
            }
            return;
        }

        setSubmitting(true);
        try {
            const { _fallback_models, _context_window_fallbacks, _content_policy_fallbacks, _default_fallbacks, ...fields } = formData;
            // What the form left empty is not sent.
            const body = Object.fromEntries(
                Object.entries(fields).filter(([, value]) => value !== undefined && value !== null && value !== ''),
            ) as typeof fields;

            // On edit an absent key means "leave as is", so what the admin emptied
            // has to go explicitly or it silently stays on the server.
            if (isEditing || _fallback_models.length > 0) body.fallback_models = _fallback_models;
            if (isEditing || _context_window_fallbacks.length > 0) body.context_window_fallbacks = _context_window_fallbacks;
            if (isEditing || _content_policy_fallbacks.length > 0) body.content_policy_fallbacks = _content_policy_fallbacks;
            if (isEditing || _default_fallbacks.length > 0) body.default_fallbacks = _default_fallbacks;
            if (isEditing) {
                body.description = formData.description ?? '';
                // An emptied stream timeout is cleared, not left as it was.
                body.stream_timeout = formData.stream_timeout ?? null;
            }

            await onSubmit(body);
        } catch (error) {
            // Error handling in parent
        } finally {
            setSubmitting(false);
        }
    };

    // ── Field updater ────────────────────────────────────────────────────
    const updateField = (field: string, value: any) => {
        setFormData((prev: any) => ({ ...prev, [field]: value }));
        if (errors[field]) {
            setErrors(prev => { const next = { ...prev }; delete next[field]; return next; });
        }
    };

    // ── Badge logic for sidebar nav items ────────────────────────────────
    const getSectionBadge = (id: string): string | undefined => {
        switch (id) {
            case 'strategy': {
                const s = routingStrategies.find(s => s.value === formData.routing_strategy);
                return s ? s.label.split(' ')[0] : undefined;
            }
            case 'fallbacks':
                return (formData._fallback_models.length || formData._context_window_fallbacks.length ||
                    formData._content_policy_fallbacks.length || formData._default_fallbacks.length) ? '●' : undefined;
            case 'ratelimits':
                return (formData.rpm_limit || formData.tpm_limit || formData.max_parallel_requests) ? '●' : undefined;
            default:
                return undefined;
        }
    };

    /** Does a section have validation errors? */
    const sectionHasError = (id: string): boolean => {
        const mapping: Record<string, string[]> = {
            basic: ['id', 'name'],
            cooldowns: ['cooldown_time'],
            retries: ['num_retries', 'stream_timeout'],

        };
        return (mapping[id] || []).some(f => !!errors[f]);
    };

    // ── Section content renderer ─────────────────────────────────────────
    const renderSectionContent = () => {
        const inputClass = (field: string) =>
            `w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent ${
                errors[field] ? 'border-red-300' : 'border-strong'
            }`;

        switch (activeSection) {
            case 'audit':
                return (
                    <AuditInfoTab
                        headingHidden
                        meta={{
                            version: formData.version ?? '1.0',
                            created_by: routingGroup?.created_by,
                            created_at: routingGroup?.created_at,
                            updated_by: routingGroup?.updated_by,
                            updated_at: routingGroup?.updated_at,
                        }}
                        onVersionChange={(v) => setFormData({ ...formData, version: v })}
                        isNew={!isEditing}
                    />
                );

            case 'basic':
                return (
                    <div className="space-y-4">
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    ID * {isEditing && <span className="text-muted font-normal">(read-only)</span>}
                                </label>
                                {isEditing ? (
                                    <input type="text" value={routingGroup?.id || ''} disabled
                                           className="w-full px-3 py-2 border border-strong rounded-lg bg-surface-alt text-muted" />
                                ) : (
                                    <>
                                        <input type="text" value={formData.id}
                                               onChange={(e) => updateField('id', e.target.value)}
                                               className={inputClass('id')}
                                               placeholder="e.g., production-gpt-4" />
                                        {errors.id && <p className="text-sm text-red-600 mt-1">{errors.id}</p>}
                                        <FieldHint text="Lowercase with hyphens. Becomes the model name in API calls." />
                                    </>
                                )}
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">Name *</label>
                                <input type="text" value={formData.name}
                                       onChange={(e) => updateField('name', e.target.value)}
                                       className={inputClass('name')}
                                       placeholder="e.g., Production GPT-4 Pool" />
                                {errors.name && <p className="text-sm text-red-600 mt-1">{errors.name}</p>}
                            </div>
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">Description</label>
                            <input type="text" value={formData.description ?? ''}
                                   onChange={(e) => updateField('description', e.target.value)}
                                   className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                   placeholder="Brief description of this routing group" />
                        </div>
                        {(() => {
                            // Mode is required at creation, immutable once
                            // members are assigned. The server rejects a
                            // mismatched mode change with HTTP 400, but
                            // we lock the picker proactively here so the
                            // operator doesn't get surprised by the
                            // round-trip error.
                            const memberCount = isEditing ? (routingGroup?.num_models ?? 0) : 0;
                            const modeLocked = isEditing && memberCount > 0;
                            return (
                                <div>
                                    <label className="block text-sm font-medium text-secondary mb-1">
                                        Mode * {modeLocked && (
                                            <span className="text-muted font-normal">
                                                (locked — {memberCount} member{memberCount === 1 ? '' : 's'} assigned)
                                            </span>
                                        )}
                                    </label>
                                    <select
                                        value={formData.mode || 'chat'}
                                        onChange={(e) => updateField('mode', e.target.value)}
                                        disabled={modeLocked}
                                        className={`w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent ${
                                            modeLocked ? 'bg-surface-alt text-muted cursor-not-allowed' : ''
                                        }`}
                                    >
                                        {ROUTING_GROUP_MODES.map(m => (
                                            <option key={m.value} value={m.value}>
                                                {m.label} — {m.subtitle}
                                            </option>
                                        ))}
                                    </select>
                                    <FieldHint text={
                                        modeLocked
                                            ? 'Detach all member models on the LLM Models page before changing the mode of this group.'
                                            : 'The inbound surface this group serves. Only models with this mode can be assigned, and the Test Router dispatches to the matching upstream endpoint.'
                                    } />
                                </div>
                            );
                        })()}
                        <label className="flex items-center space-x-2">
                            <input type="checkbox" checked={formData.enabled}
                                   onChange={(e) => updateField('enabled', e.target.checked)}
                                   className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500" />
                            <span className="text-sm font-medium text-secondary">Enabled</span>
                        </label>
                        <FieldHint text="Disabled routing groups will not be used for request routing" />
                    </div>
                );

            case 'strategy':
                return (
                    <div className="space-y-3">
                        {routingStrategies.map((strategy) => (
                            <label key={strategy.value}
                                   className={`flex items-start space-x-3 p-3 border-2 rounded-lg cursor-pointer transition-colors ${
                                       formData.routing_strategy === strategy.value
                                           ? 'border-primary-500 bg-primary-50'
                                           : 'border-default hover:border-strong'
                                   }`}>
                                <input type="radio" name="routing_strategy" value={strategy.value}
                                       checked={formData.routing_strategy === strategy.value}
                                       onChange={(e) => updateField('routing_strategy', e.target.value)}
                                       className="mt-1 w-4 h-4 text-primary-600 border-strong focus:ring-primary-500" />
                                <div className="flex-1 min-w-0 min-h-0">
                                    <div className="flex items-center space-x-2 flex-wrap">
                                        <span className="font-medium text-default text-sm">{strategy.label}</span>
                                        {strategy.value === 'usage-based-routing-v2' && (
                                            <span className="px-2 py-0.5 text-xs font-medium bg-green-100 text-green-800 rounded">Recommended</span>
                                        )}
                                        {strategy.value === 'usage-based-routing' && (
                                            <span className="px-2 py-0.5 text-xs font-medium bg-yellow-100 text-yellow-800 rounded">Legacy</span>
                                        )}
                                    </div>
                                    <p className="text-xs text-secondary mt-0.5">{strategy.description}</p>
                                    <p className="text-xs text-muted mt-1">
                                        <span className="font-medium text-muted">Best for:</span> {strategy.bestFor}
                                    </p>
                                    <code className="text-[10px] text-muted mt-0.5 block">{strategy.value}</code>
                                </div>
                            </label>
                        ))}

                        {formData.routing_strategy === 'complexity-based-routing' && (
                            <div className="mt-4 pt-4 border-t border-default">
                                <ComplexityTierEditor
                                    groupModels={groupModels}
                                    value={tiers}
                                    onChange={(next) => {
                                        setTiers(next);
                                        onTiersChange?.(next);
                                    }}
                                    floorTier={
                                        (formData.complexity_floor_tier as ComplexityTier) ?? null
                                    }
                                    onFloorTierChange={(t) =>
                                        updateField('complexity_floor_tier', t ?? undefined)
                                    }
                                />
                            </div>
                        )}
                    </div>
                );

            case 'fallbacks': {
                // Exclude the current routing group from fallback options
                const currentId = routingGroup?.id || formData.id;
                const fallbackOptions = availableGroups.filter(g => g.id !== currentId);

                return (
                    <div className="space-y-5">
                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 text-xs text-blue-800">
                            Select routing groups to use as fallbacks. Order matters — first listed is tried first.
                            Use the arrow buttons to reorder.
                        </div>
                        <FallbackGroupPicker
                            label="General Fallbacks"
                            hint="Tried on any failure (timeouts, rate limits, API errors)"
                            selected={formData._fallback_models}
                            onChange={(ids) => updateField('_fallback_models', ids)}
                            options={fallbackOptions}
                        />
                        <FallbackGroupPicker
                            label="Context Window Fallbacks"
                            hint="Tried when input exceeds model's context window"
                            selected={formData._context_window_fallbacks}
                            onChange={(ids) => updateField('_context_window_fallbacks', ids)}
                            options={fallbackOptions}
                        />
                        <FallbackGroupPicker
                            label="Content Policy Fallbacks"
                            hint="Tried when model rejects content due to safety filters"
                            selected={formData._content_policy_fallbacks}
                            onChange={(ids) => updateField('_content_policy_fallbacks', ids)}
                            options={fallbackOptions}
                        />
                        <FallbackGroupPicker
                            label="Default Fallbacks"
                            hint="Catch-all fallbacks when no specific fallback type matches"
                            selected={formData._default_fallbacks}
                            onChange={(ids) => updateField('_default_fallbacks', ids)}
                            options={fallbackOptions}
                        />
                        <div className="w-48">
                            <label className="block text-sm font-medium text-secondary mb-1">Max Fallback Attempts</label>
                            <input type="number" min="1" max="20" value={formData.max_fallbacks ?? ''}
                                   onChange={(e) => updateField('max_fallbacks', parseInt(e.target.value) || 5)}
                                   className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent" />
                            <FieldHint text="Stop trying after this many fallback groups (default: 5)" />
                        </div>
                    </div>
                );
            }

            case 'cooldowns':
                return (
                    <div className="space-y-4">
                        <div className="grid grid-cols-3 gap-4">
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">Cooldown Time (sec)</label>
                                <input type="number" min="0" value={formData.cooldown_time}
                                       onChange={(e) => updateField('cooldown_time', parseInt(e.target.value) || 0)}
                                       className={inputClass('cooldown_time')} />
                                {errors.cooldown_time && <p className="text-sm text-red-600 mt-1">{errors.cooldown_time}</p>}
                                <FieldHint text="Duration model is excluded after failures" />
                            </div>
                        </div>
                    </div>
                );

            case 'retries':
                return (
                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">Retries per Model</label>
                            <input type="number" min="0" max="10" value={formData.num_retries}
                                   onChange={(e) => updateField('num_retries', parseInt(e.target.value) || 0)}
                                   className={inputClass('num_retries')} />
                            {errors.num_retries && <p className="text-sm text-red-600 mt-1">{errors.num_retries}</p>}
                            <FieldHint text="Retries on same model before trying next" />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">Retry After (sec)</label>
                            <input type="number" min="0" value={formData.retry_after}
                                   onChange={(e) => updateField('retry_after', parseInt(e.target.value) || 0)}
                                   className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent" />
                            <FieldHint text="Wait time between retries (helps with rate limits)" />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">Stream Timeout (sec)</label>
                            <input type="number" min="0" max="1800" step="any" value={formData.stream_timeout ?? ''}
                                   onChange={(e) => updateField('stream_timeout', e.target.value === '' ? undefined : parseFloat(e.target.value))}
                                   className={inputClass('stream_timeout')}
                                   placeholder="Not set" />
                            {errors.stream_timeout && <p className="text-sm text-red-600 mt-1">{errors.stream_timeout}</p>}
                            <FieldHint text="How long a streamed call waits for the first chunk before failing over to the next model" />
                        </div>
                    </div>
                );

            case 'ratelimits':
                return (
                    <div className="grid grid-cols-3 gap-4">
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">Requests / Minute (RPM)</label>
                            <input type="number" min="1" value={formData.rpm_limit || ''}
                                   onChange={(e) => updateField('rpm_limit', e.target.value ? parseInt(e.target.value) : undefined)}
                                   className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                   placeholder="No limit" />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">Tokens / Minute (TPM)</label>
                            <input type="number" min="1" value={formData.tpm_limit || ''}
                                   onChange={(e) => updateField('tpm_limit', e.target.value ? parseInt(e.target.value) : undefined)}
                                   className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                   placeholder="No limit" />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-secondary mb-1">Max Parallel Requests</label>
                            <input type="number" min="1" value={formData.max_parallel_requests || ''}
                                   onChange={(e) => updateField('max_parallel_requests', e.target.value ? parseInt(e.target.value) : undefined)}
                                   className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                   placeholder="No limit" />
                            <FieldHint text="Maximum concurrent requests per deployment" />
                        </div>
                    </div>
                );

            default:
                return null;
        }
    };

    // ═════════════════════════════════════════════════════════════════════
    // Render
    // ═════════════════════════════════════════════════════════════════════

    const activeDef = SECTIONS.find(s => s.id === activeSection)!;
    const ActiveIcon = activeDef.icon;

    return (
        <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="app-column h-full">
            {/* ── Body: sidebar + content ─────────────────────────────── */}
            <div className="flex flex-1 min-h-0 min-w-0">
                {/* ── Section sidebar ─────────────────────────────────── */}
                <nav className="w-48 flex-shrink-0 border-r border-default overflow-y-auto py-2">
                    {SECTIONS.map(section => {
                        const Icon = section.icon;
                        const active = activeSection === section.id;
                        const hasError = sectionHasError(section.id);
                        const badge = getSectionBadge(section.id);

                        return (
                            <button
                                key={section.id}
                                type="button"
                                onClick={() => setActiveSection(section.id)}
                                className={`
                                    w-full flex items-center px-3 py-2 text-left text-[13px] transition-colors
                                    ${active
                                    ? 'bg-primary-50 text-primary-700 font-medium border-l-3 border-primary-500'
                                    : 'text-secondary hover:bg-surface-alt hover:text-default border-l-3 border-transparent'
                                }
                                    ${hasError ? 'text-red-600' : ''}
                                `}
                                style={{
                                    borderLeftWidth: '3px',
                                    borderLeftColor: active ? undefined : 'transparent',
                                }}
                            >
                                <Icon className={`w-4 h-4 flex-shrink-0 mr-2 ${
                                    hasError ? 'text-red-500' : active ? 'text-primary-600' : 'text-muted'
                                }`} />
                                <span className="truncate flex-1 min-h-0 min-w-0">{section.label}</span>
                                {hasError && (
                                    <span className="w-2 h-2 rounded-full bg-red-500 flex-shrink-0 ml-1" />
                                )}
                                {!hasError && badge && (
                                    <span className="text-primary-500 text-[10px] flex-shrink-0 ml-1">{badge}</span>
                                )}
                            </button>
                        );
                    })}
                </nav>

                {/* ── Content area ────────────────────────────────────── */}
                <div className="scroll-region px-6 py-4 min-w-0">
                    {/* Section header + help toggle */}
                    <div className="flex items-center justify-between mb-4">
                        <div className="flex items-center space-x-2">
                            <ActiveIcon className="w-5 h-5 text-primary-600" />
                            <h3 className="text-lg font-semibold text-default">{activeDef.label}</h3>
                        </div>
                        <button
                            type="button"
                            onClick={() => setHelpOpen(h => !h)}
                            className={`flex items-center space-x-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                                helpOpen
                                    ? 'bg-primary-100 text-primary-700'
                                    : 'text-muted hover:text-secondary hover:bg-surface-raised'
                            }`}
                            title={helpOpen ? 'Hide help' : 'Show help for this section'}
                        >
                            {helpOpen ? <X className="w-3.5 h-3.5" /> : <HelpCircle className="w-3.5 h-3.5" />}
                            <span>{helpOpen ? 'Close' : 'Help'}</span>
                        </button>
                    </div>

                    {/* Inline help panel */}
                    {helpOpen && HELP_CONTENT[activeSection] && (
                        <div className="mb-5 p-4 bg-gradient-to-r from-primary-50 to-blue-50 border border-primary-200 rounded-lg">
                            {HELP_CONTENT[activeSection]}
                        </div>
                    )}

                    {/* Form fields */}
                    {renderSectionContent()}
                </div>
            </div>

            {/* ── Form actions (pinned bottom) ────────────────────────── */}
            <div className="flex items-center justify-end space-x-3 px-6 py-4 border-t border-default bg-surface-alt flex-shrink-0">
                <button type="button" onClick={onCancel}
                        className="px-5 py-2 border border-strong text-secondary rounded-lg hover:bg-surface-raised transition-colors"
                        disabled={submitting}>
                    Cancel
                </button>
                <button type="submit" disabled={submitting}
                        className="flex items-center space-x-2 px-5 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50">
                    <Save className="w-4 h-4" />
                    <span>{submitting ? 'Saving...' : isEditing ? 'Update Routing Group' : 'Create Routing Group'}</span>
                </button>
            </div>
        </form>
    );
};

export default RoutingGroupForm;