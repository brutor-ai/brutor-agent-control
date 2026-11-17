/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
    X,
    Brain,
    Globe,
    Key,
    Settings,
    CheckCircle2,
    Info,
    HelpCircle,
    Zap,
    Activity,
    Users,
    Sliders,
    FileCode,
    MessageSquare,
    Lightbulb,
    List,
    AlertCircle,
    DollarSign,
    Calendar,
    Gauge,
    ShieldCheck,
    ChevronDown,
    FileClock,
} from 'lucide-react';
import InfoBanner from '../../common/InfoBanner';
import type { LLModelConfig, LLModelUpdateRequest } from '../../../lib/llmTypes';
import toast from 'react-hot-toast';
import { useGateway } from '../../../hooks/useGateway';
import { useLlmProviders } from '../../../hooks/useLlmProviders';
import { notServedReasons, notServedText } from '../../../lib/modelServed';
import type { GatewayApiClient } from '../../../lib/gatewayApiClient';
import type { LlmProviderCatalogEntry } from '../../../lib/llmProviderTypes';
import ProviderIcon from './ProviderIcon';
import { ProviderLogo } from './providerLogos';
import LLModelFormHelp from './LLModelFormHelp';
import { type ValidationErrorsType as ValidationErrors, FieldError, ValidatedInput,
    ValidatedTextarea, ValidationSummary } from './FormValidation';
import { validateField, validateJSON, VALIDATION_RULES } from './formValidationRules';
import HierarchicalGroupSelector from '../../common/HierarchicalGroupSelector';
import { GroupHierarchyTree, LLMGlobalLimits, LLMGovernance } from '../../../lib/groupTypes';
// Tenant-wide per-model Limits/Policies reuse the classic (mode-aware) forms —
// the same components the Resource Group tabs render (DRY).
import LLMLimitsForm from '../../groups/subtabs/LLMLimitsForm';
import LLMGovernanceForm from '../../groups/subtabs/LLMGovernanceForm';
import MediaLimitsForm from '../../groups/subtabs/MediaLimitsForm';
import MediaGovernanceForm from '../../groups/subtabs/MediaGovernanceForm';
import type { MediaMode } from '../../groups/subtabs/MediaLimitsForm';
import AuditInfoTab from '../../common/AuditInfoTab';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import {
    EMPTY_BEDROCK_CREDENTIAL,
    apiUrlRequiredMessage,
    bedrockCredentialErrors,
    bedrockProviderConfig,
    isBedrockProvider,
    isVertexProvider,
    localAddressHint,
    modelSaveRefusalText,
    storedBedrockCredential,
    storedVertexCredential,
    cloudModelApiKey,
    vertexCredentialErrors,
    vertexProviderConfig,
    EMPTY_VERTEX_CREDENTIAL,
    type VertexCredentialInput,
    apiUrlRefusal,
    catalogRequiresApiUrl,
    type BedrockCredentialInput,
} from './providerConnection';
import { failureSentence, errorKind } from '../../../lib/narrow';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';
import { withoutRefused } from '../../../lib/refusedSettings';

/**
 * What the form hands to `onSave`: the fields of the Control Plane's model
 * update (the creation takes the same ones), and three more that only a
 * catalog entry has (shown when `variant === 'catalog'`), which the catalog
 * adapter in LLMCatalogAdminTab maps onto the catalog's own requests and a
 * model's save leaves out.
 */
export type LLModelFormData = LLModelUpdateRequest & {
    recommended?: boolean;
    deprecated?: boolean;
    icon_url?: string | null;
};

interface LLModelFormProps {
    model: LLModelConfig | null;
    onClose: () => void;
    onSave: (modelData: LLModelFormData) => Promise<void>;
    /**
     * Which surface is rendering this form. `'instance'` (default) is the
     * per-tenant configured-model editor. `'catalog'` reuses the same form
     * for the global LLM catalog: most tabs are hidden, a few catalog-only
     * fields (recommended/deprecated/icon_url) appear on the General tab,
     * and the values shown are the IMPORT DEFAULTS.
     */
    variant?: 'instance' | 'catalog';
}

/**
 * What is wrong with the circuit breaker as entered, or undefined: both set
 * (failures 1-100, seconds 1-3600) or both empty.
 */
function circuitBreakerProblem(fails: string, seconds: string): string | undefined {
    const f = fails.trim();
    const c = seconds.trim();
    if (!f && !c) return undefined;
    if (!f || !c) return 'Set both, or neither: the failures before the breaker opens and how long it stays open.';
    const failures = Number(f);
    const open = Number(c);
    if (!Number.isInteger(failures) || failures < 1 || failures > 100) return 'Failures before it opens: a whole number from 1 to 100.';
    if (!Number.isInteger(open) || open < 1 || open > 3600) return 'Seconds it stays open: a whole number from 1 to 3600.';
    return undefined;
}

type TabId = 'basic' | 'audit' | 'cost' | 'access' | 'media' | 'api' | 'generation' | 'tuning' | 'output' | 'tools' | 'params' | 'fallbacks' | 'routing' | 'prompt' | 'limits' | 'policies' | 'retirement';

/**
 * Orderable multi-select picker for a model's failover chain. Pick candidate
 * models from a dropdown; reorder with ↑/↓ (top = first fallback tried);
 * remove with ✕. Operates on an ordered `string[]` of model ids. Mirrors
 * `FallbackGroupPicker` in RoutingGroupForm but self-contained (no FieldHint).
 */
export const FallbackModelPicker: React.FC<{
    label: string;
    hint: string;
    selected: string[];
    onChange: (ids: string[]) => void;
    options: { id: string; name: string }[];
}> = ({ label, hint, selected, onChange, options }) => {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);

    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, []);

    const available = options.filter(o => !selected.includes(o.id));
    const nameOf = (id: string) => options.find(o => o.id === id)?.name ?? id;
    const add = (id: string) => onChange([...selected, id]);
    const remove = (id: string) => onChange(selected.filter(s => s !== id));
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

    return (
        <div ref={ref}>
            <label className="block text-sm font-medium text-secondary mb-1">{label}</label>
            {selected.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-2">
                    {selected.map((id, idx) => (
                        <span
                            key={id}
                            className="inline-flex items-center gap-1 px-2 py-1 bg-primary-50 text-primary-800 border border-primary-200 rounded-md text-xs"
                        >
                            <span className="text-[10px] text-primary-400 font-mono mr-0.5">{idx + 1}</span>
                            <span className="font-medium">{nameOf(id)}</span>
                            <button type="button" onClick={() => moveUp(idx)} disabled={idx === 0}
                                className="text-primary-400 hover:text-primary-700 disabled:opacity-30 p-0" title="Move up (higher priority)">↑</button>
                            <button type="button" onClick={() => moveDown(idx)} disabled={idx === selected.length - 1}
                                className="text-primary-400 hover:text-primary-700 disabled:opacity-30 p-0" title="Move down (lower priority)">↓</button>
                            <button type="button" onClick={() => remove(id)}
                                className="text-primary-400 hover:text-red-600 p-0 ml-0.5" title="Remove">
                                <X className="w-3 h-3" />
                            </button>
                        </span>
                    ))}
                </div>
            )}
            <div className="relative">
                <button type="button" onClick={() => setOpen(!open)}
                    className="w-full flex items-center justify-between px-3 py-2 border border-strong rounded-lg bg-surface text-left hover:border-gray-400 focus:ring-2 focus:ring-primary-500 focus:border-transparent text-sm">
                    <span className={available.length === 0 ? 'text-muted' : 'text-secondary'}>
                        {available.length === 0
                            ? (options.length === 0 ? 'No other models of this mode available' : 'All eligible models added')
                            : `Add fallback model (${available.length} available)…`}
                    </span>
                    <ChevronDown className={`w-4 h-4 text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
                </button>
                {open && available.length > 0 && (
                    <div className="absolute z-50 mt-1 w-full bg-surface border border-default rounded-lg shadow-lg max-h-48 overflow-y-auto">
                        {available.map(o => (
                            <button key={o.id} type="button" onClick={() => { add(o.id); setOpen(false); }}
                                className="w-full flex items-center justify-between px-3 py-2 text-sm text-left hover:bg-primary-50 border-b border-gray-50 last:border-0">
                                <span className="font-medium text-default">{o.name}</span>
                            </button>
                        ))}
                    </div>
                )}
            </div>
            <p className="mt-1 text-xs text-muted">{hint}</p>
        </div>
    );
};

// Modes whose settings live in the Media tab. Anything outside
// this set hides the tab entirely so chat/embedding/routing models
// don't carry a useless empty pane.
const MEDIA_MODES: ReadonlyArray<string> = [
    'image_generation',
    'audio_speech',
    'audio_transcription',
    'video_generation',
];

// Modes whose tenant-wide Limits/Policies use the media-style forms
// (MediaLimitsForm / MediaGovernanceForm) rather than the chat LLM forms.
// Embedding is included here — even though it is NOT in MEDIA_MODES (which
// gates the Media *tab* of image/audio/video generation params) — because
// embeddings have their own limit/governance fields (batch size, per-item
// tokens, dimension whitelist), exactly as rendered under RG → Governance.
const MEDIA_FORM_MODES: ReadonlyArray<string> = [...MEDIA_MODES, 'embedding'];

// Chat-style modes that share generation parameters (temperature,
// max_tokens, top_p, system prompt, response_format, etc.). Tabs
// covering those parameters (Generation / Tuning / Output / Param
// Control / Sys Prompt) only show for these modes.
const CHAT_LIKE_MODES: ReadonlyArray<string> = ['chat', 'completion', 'responses'];

// Modes that support tool / function calling. Drives the Tools tab.
const TOOL_CAPABLE_MODES: ReadonlyArray<string> = ['chat', 'responses'];

// Modes priced per input/output token. Embeddings are priced per
// input token (the vector itself is the output, not billed).
// Rerank / image / audio / video have their own pricing models and
// should not see the cost-per-input/output-token fields.
const TOKEN_PRICED_MODES: ReadonlyArray<string> = ['chat', 'completion', 'responses', 'embedding'];

/** Map a provider's `category` field (from the REST API) to the
 *  optgroup label the provider dropdown uses. Category names
 *  match `LlmProvider.category` in the backend. */
const CATEGORY_TO_TIER: Record<string, string> = {
    chat: 'Major Direct API Providers',
    embeddings: 'Specialized Services',
    audio: 'Specialized Services',
    image: 'Specialized Services',
    video: 'Specialized Services',
    self_hosted: 'Self-Hosted / Specialized',
    internal: 'Other',
};
const TIER_ORDER = [
    'Major Direct API Providers',
    'Inference Platforms',
    'Self-Hosted / Specialized',
    'Specialized Services',
    'Other',
];

/** Upper bound of the catalog page read for the "Model" field's
 *  suggestions; the endpoint caps `limit` at 500 and no provider has
 *  anywhere near that many catalog entries. */
const MODEL_SUGGESTION_LIMIT = 500;

/**
 * Inline icon preview + replace button shown beneath the provider
 * dropdown. The current icon (or initials fallback) renders next to
 * a "Replace icon" link that opens the file picker. The upload
 * happens immediately — no separate "save" step — because the
 * provider row is independent of the model row being edited.
 *
 * Failure handling: error toasts via the host's `react-hot-toast`
 * setup; the component itself just surfaces a small inline error
 * line so the operator sees what's wrong without leaving the form.
 */
const ProviderIconAndUpload: React.FC<{
    provider: Awaited<ReturnType<GatewayApiClient['listProviders']>>['providers'][number];
    api: GatewayApiClient | null;
}> = ({ provider, api }) => {
    const inputRef = useRef<HTMLInputElement>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    // We bump this on a successful upload so the icon URL's
    // ?v= query param changes and the browser drops its cached
    // response. The hook-cached provider list refreshes on the
    // next mount; here we just need the visual to update.
    const [versionHint, setVersionHint] = useState<string | null>(
        provider.icon_uploaded_at ?? null,
    );

    const onPick = useCallback(() => {
        inputRef.current?.click();
    }, []);

    const onFile = useCallback(
        async (e: React.ChangeEvent<HTMLInputElement>) => {
            const file = e.target.files?.[0];
            if (!file || !api) return;
            setBusy(true);
            setError(null);
            try {
                const updated = await api.uploadProviderIcon(provider.id, file);
                setVersionHint(updated.icon_uploaded_at ?? new Date().toISOString());
                toast.success(`Icon updated for ${updated.display_name}`);
            } catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                setError(msg);
                toast.error(`Icon upload failed: ${msg}`);
            } finally {
                setBusy(false);
                if (inputRef.current) inputRef.current.value = '';
            }
        },
        [api, provider.id],
    );

    return (
        <div className="mt-2 flex items-center gap-2 text-xs text-secondary">
            <ProviderIcon
                provider={{ ...provider, icon_uploaded_at: versionHint }}
                api={api}
                size={28}
            />
            <button
                type="button"
                onClick={onPick}
                disabled={busy}
                className="text-primary-600 hover:text-primary-700 underline-offset-2 hover:underline disabled:opacity-50"
            >
                {busy
                    ? 'Uploading…'
                    : provider.has_icon
                    ? 'Replace icon'
                    : 'Upload icon'}
            </button>
            <input
                ref={inputRef}
                type="file"
                accept="image/svg+xml,image/png,image/jpeg,image/gif,image/webp"
                style={{ display: 'none' }}
                onChange={asyncHandler(onFile, 'change')}
            />
            {error && (
                <span className="text-red-600" role="alert">
                    {error}
                </span>
            )}
        </div>
    );
};

/** Whether a secret of the stored credential is set. Says so; never shows it. */
const StoredMark: React.FC<{ testId: string; stored: boolean }> = ({ testId, stored }) => (
    <p data-testid={testId} className={`mb-1 text-xs ${stored ? 'text-green-700' : 'text-muted'}`}>
        {stored ? 'Stored' : 'Not stored'}
    </p>
);

const LLModelForm: React.FC<LLModelFormProps> = ({ model, onClose, onSave, variant = 'instance' }) => {
    const isCatalog = variant === 'catalog';
    const gateway = useGateway();
    // Providers come from `/v1/admin/providers`. The
    // hook caches across mounts so the dropdown is instant after
    // the first fetch.
    const { providers: fetchedProviders } = useLlmProviders(
        gateway.gatewayClient ?? null,
    );

    // The provider dropdown OPTIONS source the GLOBAL provider catalog
    // (`/v1/admin/provider-catalog`) so any provider is pickable even when
    // not yet configured on this tenant — the backend auto-imports the
    // chosen provider into `llm_providers` on model create. (The configured
    // `fetchedProviders` list above is still used to resolve a real
    // `llm_providers` id for the edit-flow icon upload control.)
    const [readFailure, reads] = useReadFailures<'providers' | 'suggestions' | 'routingGroups' | 'models'>();
    const [asked, setAsked] = useState(0);
    const askAgain = () => setAsked((n) => n + 1);
    const [providerCatalog, setProviderCatalog] = useState<
        LlmProviderCatalogEntry[]
    >([]);
    useEffect(() => {
        const client = gateway.gatewayClient;
        if (!client || typeof client.listProviderCatalog !== 'function') return;
        let cancelled = false;
        Promise.resolve()
            .then(() => client.listProviderCatalog())
            .then((resp) => {
                if (cancelled) return;
                setProviderCatalog(resp.providers);
                reads.loaded('providers');
            })
            .catch((err: unknown) => {
                // An empty catalog: the provider selected (if any) is
                // still rendered as a single option below, so the form
                // never loses its value, and the field says why it has
                // no other.
                if (cancelled) return;
                setProviderCatalog([]);
                reads.lost('providers', 'Failed to load the providers', err);
            });
        return () => {
            cancelled = true;
        };
    }, [gateway.gatewayClient, asked, reads]);

    const [activeTab, setActiveTab] = useState<TabId>('basic');
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [showHelp, setShowHelp] = useState(false);
    const [helpSection, setHelpSection] = useState<TabId>('basic');
    const [errors, setErrors] = useState<ValidationErrors>({});
    const [touched, setTouched] = useState<Set<string>>(new Set());
    const [serverError, setServerError] = useState<string | null>(null);

    // Basic Information
    const [name, setName] = useState(model?.name || '');
    const [description, setDescription] = useState(model?.description || '');
    // AI Resource identity block. Version is operator-authored;
    // authorship is system-stamped and never sent.
    const [version, setVersion] = useState(model?.version || '1.0');
    const [usage, setUsage] = useState(model?.usage || '');
    const [tags, setTags] = useState<string>(
        model?.tags ? model.tags.join(', ') : ''
    );
    const [provider, setProvider] = useState(model?.provider || 'openai');

    // "Model" field suggestions: the GLOBAL model catalog's entries for the
    // chosen provider (`/v1/admin/llm-catalog?provider_key=…`). The catalog
    // is the one place model ids are maintained, so nothing is compiled in
    // here — a static list would offer retired ids long after the catalog
    // has dropped them. The field stays free text: a failed or empty read just
    // means no suggestions. Not in catalog mode: there the operator is
    // writing a catalog entry, and an id the catalog already has is exactly
    // the one that cannot be added.
    const [modelSuggestions, setModelSuggestions] = useState<string[]>([]);
    useEffect(() => {
        const client = gateway.gatewayClient;
        if (isCatalog || !client || typeof client.listLLMCatalog !== 'function' || !provider) return;
        let cancelled = false;
        Promise.resolve()
            .then(() => client.listLLMCatalog({ provider_key: provider, limit: MODEL_SUGGESTION_LIMIT }))
            .then((resp) => {
                if (cancelled) return;
                setModelSuggestions(resp.catalog.map((entry) => entry.model_name));
                reads.loaded('suggestions');
            })
            .catch((err: unknown) => {
                if (cancelled) return;
                setModelSuggestions([]);
                reads.lost('suggestions', 'Failed to load the model names of this provider; the name can still be typed', err);
            });
        return () => {
            cancelled = true;
            // Never show one provider's ids under another while the next
            // read is in flight.
            setModelSuggestions([]);
        };
    }, [gateway.gatewayClient, provider, isCatalog, asked, reads]);
    const [modelName, setModelName] = useState(model?.model_name || '');
    const [mode, setMode] = useState(model?.mode || 'chat');

    // Does a model of this provider and mode need its own API URL? The
    // control plane says so on a catalog entry (`requires_api_url`, computed
    // from the entry's provider and mode alone), so one entry of the same
    // provider and mode answers for the model in this form. `undefined`
    // while the read is in flight, when the catalog has no such entry and
    // when it cannot be read: then the form cannot know, and the control
    // plane's refusal of the save is shown by the API URL field instead
    // (providerConnection.ts).
    const [catalogNeedsApiUrl, setCatalogNeedsApiUrl] = useState<boolean | undefined>(undefined);
    const [destinationAsked, setDestinationAsked] = useState(false);
    useEffect(() => {
        const client = gateway.gatewayClient;
        if (isCatalog || !client || typeof client.listLLMCatalog !== 'function' || !provider) return;
        let cancelled = false;
        Promise.resolve()
            .then(() => client.listLLMCatalog({ provider_key: provider, mode, limit: 1 }))
            .then((resp) => {
                if (cancelled) return;
                setCatalogNeedsApiUrl(catalogRequiresApiUrl(resp.catalog, provider, mode));
                setDestinationAsked(true);
            })
            .catch(() => {
                if (cancelled) return;
                setCatalogNeedsApiUrl(undefined);
                setDestinationAsked(true);
            });
        return () => {
            cancelled = true;
            // One provider's answer is never applied to the next.
            setCatalogNeedsApiUrl(undefined);
            setDestinationAsked(false);
        };
    }, [gateway.gatewayClient, provider, mode, isCatalog]);
    const [enabled, setEnabled] = useState(model?.enabled ?? true);
    // Tenant-wide default Limits / Policies for this model (min-wins with RG).
    // One opaque blob each; the form rendered depends on the model's mode.
    const [tenantLimits, setTenantLimits] = useState<Record<string, any>>(
        (model as any)?.tenant_limits || {},
    );
    const [tenantGovernance, setTenantGovernance] = useState<Record<string, any>>(
        (model as any)?.tenant_governance || {},
    );

    // Catalog-only fields (variant === 'catalog'). The catalog adapter feeds
    // these in via the synthesized `model` and reads them back off onSave.
    const [recommended, setRecommended] = useState<boolean>(
        (model as any)?.recommended ?? false
    );
    const [deprecated, setDeprecated] = useState<boolean>(
        (model as any)?.deprecated ?? false
    );
    const [iconUrl, setIconUrl] = useState<string>((model as any)?.icon_url || '');

    // Retirement metadata. Backend returns ISO strings (`YYYY-MM-DD`)
    // or null. We keep them as plain strings throughout the form
    // because the native <input type="date"> already speaks that
    // format — no `Date` instance gymnastics needed. The empty
    // string is the "unset" sentinel; submit converts back to null
    // (or omits the key) before sending.
    const [retireDate, setRetireDate] = useState<string>(
        (model as any)?.retire_date || ''
    );
    const [retirementAnnouncedAt, setRetirementAnnouncedAt] = useState<string>(
        (model as any)?.retirement_announced_at || ''
    );
    const [retirementReminderDate, setRetirementReminderDate] = useState<string>(
        (model as any)?.retirement_reminder_date || ''
    );
    const [retirementReplacementModelName, setRetirementReplacementModelName] = useState<string>(
        (model as any)?.retirement_replacement_model_name || ''
    );
    const [retirementReplacementProviderKey, setRetirementReplacementProviderKey] = useState<string>(
        (model as any)?.retirement_replacement_provider_key || ''
    );
    const [retirementNotes, setRetirementNotes] = useState<string>(
        (model as any)?.retirement_notes || ''
    );

    // Default-fill the reminder to `retire_date - 14d` whenever the
    // operator types a retire_date but hasn't touched the reminder
    // field. Two weeks is the same default the seed pipeline applies
    // server-side (see `_retirement_reminder_default` in
    // app/database/init_llms.py) — keeping the two in sync avoids
    // operator surprise on round-trip.
    const retireDateInputRef = retireDate;  // capture for effect dep
    useEffect(() => {
        if (!retireDate) return;
        if (retirementReminderDate) return;
        const d = new Date(retireDate + 'T00:00:00Z');
        if (Number.isNaN(d.getTime())) return;
        d.setUTCDate(d.getUTCDate() - 14);
        setRetirementReminderDate(d.toISOString().slice(0, 10));
        // Intentionally not depending on retirementReminderDate —
        // we only default-fill on retire_date change.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [retireDateInputRef]);
    const [parameters, setParameters] = useState(model?.parameters != null ? model.parameters.toString() : '');
    const [contextWindow, setContextWindow] = useState(
        model?.capabilities?.context_window != null ? String(model.capabilities.context_window) : ''
    );

    // API Configuration
    const [apiUrlAutoDiscovered, setApiUrlAutoDiscovered] = useState<boolean>(
        model?.api_url_auto_discovered !== undefined ? model.api_url_auto_discovered : true
    );
    const [apiUrl, setApiUrl] = useState(model?.api_url || '');
    const [authType, setAuthType] = useState(model?.auth_type || 'api_key');
    const [apiKey, setApiKey] = useState('');
    const [apiVersion, setApiVersion] = useState(model?.api_version || '');
    // The AWS credential of an Amazon Bedrock model. The secrets are
    // write-only: they start empty whatever the model carries. The control
    // plane says which of them are stored and the region, which is no secret
    // and is shown (`provider_config_keys`, `aws_region`).
    const awsStored = storedBedrockCredential(model);
    const [awsCredential, setAwsCredential] = useState<BedrockCredentialInput>({
        ...EMPTY_BEDROCK_CREDENTIAL,
        region: awsStored.region,
    });
    const [removeAwsSessionToken, setRemoveAwsSessionToken] = useState(false);
    const awsCredentialStored = awsStored.accessKeyId || awsStored.secretAccessKey;
    // The Google Cloud credential of a Vertex AI model. The service-account
    // key is write-only. The control plane says whether one is stored, and
    // the project and the location, which are no secrets and are shown
    // (`provider_config_keys`, `gcp_project_id`, `gcp_location`).
    const gcpStored = storedVertexCredential(model);
    const [gcpCredential, setGcpCredential] = useState<VertexCredentialInput>({
        ...EMPTY_VERTEX_CREDENTIAL,
        projectId: gcpStored.projectId,
        location: gcpStored.location,
    });
    const gcpCredentialStored = gcpStored.credentialsJson;
    const [extraHeaders, setExtraHeaders] = useState<string>(
        model?.extra_headers ? JSON.stringify(model.extra_headers, null, 2) : ''
    );

    // Generation Parameters
    const [temperature, setTemperature] = useState(model?.default_temperature?.toString() || '0.7');
    const [maxTokens, setMaxTokens] = useState(model?.default_max_tokens?.toString() || '4096');
    const [systemPrompt, setSystemPrompt] = useState(model?.system_prompt || '');
    const [topP, setTopP] = useState<number | undefined>(model?.top_p);
    const [n, setN] = useState<number>(model?.n || 1);
    const [stopSequences, setStopSequences] = useState<string[]>(model?.stop_sequences || []);
    const [seed, setSeed] = useState<number | undefined>(model?.seed);

    // Penalties
    const [presencePenalty, setPresencePenalty] = useState<number | undefined>(model?.presence_penalty);
    const [frequencyPenalty, setFrequencyPenalty] = useState<number | undefined>(model?.frequency_penalty);
    const [logitBias, setLogitBias] = useState<string>(
        model?.logit_bias ? JSON.stringify(model.logit_bias, null, 2) : ''
    );

    // Output
    const [responseFormat, setResponseFormat] = useState<string>(
        model?.response_format ? JSON.stringify(model.response_format, null, 2) : ''
    );
    const [logprobs, setLogprobs] = useState<boolean>(model?.logprobs || false);
    const [topLogprobs, setTopLogprobs] = useState<number | undefined>(model?.top_logprobs);
    const [streamOptions, setStreamOptions] = useState<string>(
        model?.stream_options ? JSON.stringify(model.stream_options, null, 2) : ''
    );

    // Tools
    const [tools, setTools] = useState<string>(
        model?.tools ? JSON.stringify(model.tools, null, 2) : ''
    );
    const [toolChoice, setToolChoice] = useState<string>(model?.tool_choice || 'auto');
    const [parallelToolCalls, setParallelToolCalls] = useState<boolean>(
        model?.parallel_tool_calls ?? true
    );

    // Monitoring
    const [userIdentifier, setUserIdentifier] = useState(model?.user_identifier || '');
    const [modelMetadata, setModelMetadata] = useState<string>(
        model?.model_metadata ? JSON.stringify(model.model_metadata, null, 2) : ''
    );
    // Whether the provider's dropped-parameters list applies to this model.
    // A model that states nothing gets it: that is how the gateway reads it.
    const [dropParams, setDropParams] = useState<boolean>(model?.drop_params ?? true);
    const [additionalDropParams, setAdditionalDropParams] = useState<string>(
        model?.additional_drop_params ? model.additional_drop_params.join(', ') : ''
    );
    const [thinkingEnabled, setThinkingEnabled] = useState<boolean>(model?.thinking_enabled ?? false);

    // Capabilities
    const [supportsStreaming, setSupportsStreaming] = useState(model?.supports_streaming ?? true);
    const [supportsFunctionCalling, setSupportsFunctionCalling] = useState(model?.supports_function_calling ?? true);
    const [supportsVision, setSupportsVision] = useState(model?.supports_vision ?? false);
    const [supportsBatch, setSupportsBatch] = useState(model?.supports_batch ?? false);

    // Auto-disable capabilities for non-chat modes
    const NON_CHAT_MODES = ['embedding', 'image_generation', 'audio_transcription', 'audio_speech', 'video_generation', 'rerank', 'moderation', 'routing'];
    const isNonChatMode = NON_CHAT_MODES.includes(mode);
    // Mode-aware tab gating. Chat-style modes share generation params,
    // tuning, output format, prompt, and tool-calling. Other modes get
    // a leaner set of tabs (Basic / Cost / Access / API / Routing,
    // plus Media for image/audio/video).
    const isChatLike = CHAT_LIKE_MODES.includes(mode);
    const isToolCapable = TOOL_CAPABLE_MODES.includes(mode);
    const isTokenPriced = TOKEN_PRICED_MODES.includes(mode);

    useEffect(() => {
        if (isNonChatMode) {
            setSupportsStreaming(false);
            setSupportsFunctionCalling(false);
            setSupportsVision(false);
        }
    }, [mode]);

    // If the user switches to a mode that hides the currently-active tab
    // (e.g. on Tools tab while editing a chat model, then switches mode
    // to embedding), snap back to the always-visible General tab so the
    // right pane never shows blank because of a hidden NavItem.
    // In catalog mode only a lean set of tabs is shown: General, Cost,
    // Media (mode-gated), Retirement. Everything instance-specific is hidden.
    const CATALOG_TABS: ReadonlyArray<TabId> = ['basic', 'audit', 'cost', 'media', 'retirement'];
    useEffect(() => {
        const chatOnlyTabs: TabId[] = ['generation', 'tuning', 'output', 'params', 'prompt'];
        const toolTab: TabId = 'tools';
        const mediaTab: TabId = 'media';
        const tabHiddenForCurrentMode =
            (chatOnlyTabs.includes(activeTab) && !isChatLike) ||
            (activeTab === toolTab && !isToolCapable) ||
            (activeTab === mediaTab && !MEDIA_MODES.includes(mode)) ||
            // Catalog mode hides every tab outside CATALOG_TABS.
            (isCatalog && !CATALOG_TABS.includes(activeTab));
        if (tabHiddenForCurrentMode) {
            setActiveTab('basic');
        }
        // CATALOG_TABS is a stable literal; isCatalog is render-stable.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [mode, activeTab, isChatLike, isToolCapable, isCatalog]);

    // Routing
    const [routingWeight, setRoutingWeight] = useState(model?.routing_weight?.toString() || '1');
    const [rateLimitRpm, setRateLimitRpm] = useState(model?.rate_limit_rpm?.toString() || '');
    const [rateLimitTpm, setRateLimitTpm] = useState(model?.rate_limit_tpm?.toString() || '');
    // Per-model circuit breaker. Empty string = inherit default.
    const [costPerInputToken, setCostPerInputToken] = useState(model?.cost_per_input_token?.toString() || '');
    const [costPerOutputToken, setCostPerOutputToken] = useState(model?.cost_per_output_token?.toString() || '');
    // Image-mode — only meaningful when mode === 'image_generation'.
    // Whitelists are stored as JSON arrays in the DB; the form
    // surfaces them as comma-separated strings for ergonomics.
    const [costPerImage, setCostPerImage] = useState(
        model?.cost_per_image?.toString() || ''
    );
    const [supportedImageSizes, setSupportedImageSizes] = useState<string>(
        Array.isArray(model?.supported_image_sizes)
            ? model.supported_image_sizes.join(', ')
            : ''
    );
    const [supportedImageQualities, setSupportedImageQualities] = useState<string>(
        Array.isArray(model?.supported_image_qualities)
            ? model.supported_image_qualities.join(', ')
            : ''
    );
    const [maxImagesPerRequest, setMaxImagesPerRequest] = useState<string>(
        model?.max_images_per_request?.toString() || '1'
    );
    // Audio TTS — only meaningful when mode === 'audio_speech'.
    const [costPerAudioChar, setCostPerAudioChar] = useState<string>(
        model?.cost_per_audio_char?.toString() || ''
    );
    const [supportedVoices, setSupportedVoices] = useState<string>(
        Array.isArray(model?.supported_voices)
            ? model.supported_voices.join(', ')
            : ''
    );
    const [supportedAudioFormats, setSupportedAudioFormats] = useState<string>(
        Array.isArray(model?.supported_audio_formats)
            ? model.supported_audio_formats.join(', ')
            : ''
    );
    // Audio STT — only meaningful when mode === 'audio_transcription'.
    const [costPerAudioMinute, setCostPerAudioMinute] = useState<string>(
        model?.cost_per_audio_minute?.toString() || ''
    );
    const [maxAudioDurationSeconds, setMaxAudioDurationSeconds] = useState<string>(
        model?.max_audio_duration_seconds?.toString() || ''
    );
    const [supportedInputFormats, setSupportedInputFormats] = useState<string>(
        Array.isArray(model?.supported_input_formats)
            ? model.supported_input_formats.join(', ')
            : ''
    );
    // Whisper-style upstream response formats. The Rust proxy's
    // `pick_upstream_format` reads this whitelist on every transcribe
    // request to decide whether to forward the user's requested
    // `response_format` upstream or coerce it to a model-supported
    // value. Empty/null means "use the proxy's per-model default".
    const [supportedResponseFormats, setSupportedResponseFormats] = useState<string[]>(
        Array.isArray(model?.supported_response_formats)
            ? model.supported_response_formats
            : []
    );
    // Video generation — only meaningful when mode === 'video_generation'.
    const [costPerVideoSecond, setCostPerVideoSecond] = useState<string>(
        model?.cost_per_video_second?.toString() || ''
    );
    const [supportedVideoResolutions, setSupportedVideoResolutions] = useState<string>(
        Array.isArray(model?.supported_video_resolutions)
            ? model.supported_video_resolutions.join(', ')
            : ''
    );
    const [supportedVideoDurations, setSupportedVideoDurations] = useState<string>(
        Array.isArray(model?.supported_video_durations)
            ? model.supported_video_durations.join(', ')
            : ''
    );
    const [maxVideoDurationSeconds, setMaxVideoDurationSeconds] = useState<string>(
        model?.max_video_duration_seconds?.toString() || ''
    );
    const [routingGroupId, setRoutingGroupId] = useState(model?.routing_group_id || '');
    const [routingGroups, setRoutingGroups] = useState<any[]>([]);
    const [loadingRoutingGroups, setLoadingRoutingGroups] = useState(false);
    // Model-level fallback chain (ordered model ids). Candidate models are all
    // OTHER models of the SAME mode as this one.
    const [fallbackModels, setFallbackModels] = useState<string[]>(model?.fallback_models || []);
    const [allModels, setAllModels] = useState<LLModelConfig[]>([]);

    // Connection
    const [timeout, setTimeout] = useState(model?.timeout?.toString() || '60');
    // Empty string → unbounded (NULL in the DB). The proxy's resilience
    // gate skips the semaphore acquisition entirely when this is null,
    // so the hot path cost is a single Option::is_none() check.
    const [maxConcurrentRequests, setMaxConcurrentRequests] = useState(
        model?.max_concurrent_requests?.toString() || '',
    );
    // The circuit breaker: both set, or both empty (the gateway's defaults apply: 5 failures, 30 seconds).
    const [allowedFails, setAllowedFails] = useState(model?.allowed_fails?.toString() ?? '');
    const [breakerCooldown, setBreakerCooldown] = useState(model?.cooldown_time?.toString() ?? '');

    // Access Control - Using hierarchical group selector
    // The Default group is the system group that every model must belong to.
    // Its real ID is a ULID (e.g. "group-01JXXXX..."). The frontend sends
    // the sentinel 'default' for new models; the backend resolves it.
    const DEFAULT_GROUP_SENTINEL = 'default';

    // Admin's group selection — respected as-is by the backend (no forced
    // Default injection, as for MCP server configs).
    // New models start with nothing selected; admin picks the target groups.
    const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>(
        () => model?.groups?.map((g) => g.id) ?? [],
    );
    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);
    const [groupsLoading, setGroupsLoading] = useState(false);

    // Load group hierarchy tree — no side-effects on selection.
    // The admin's picks in the Access Control tab are the source of truth.
    useEffect(() => {
        if (isCatalog) return;  // Catalog entries have no group bindings.
        const fetchGroups = async () => {
            if (!gateway.gatewayClient) return;

            setGroupsLoading(true);
            try {
                const response = await gateway.gatewayClient.fetchGroupHierarchyTree();
                setGroupTree(response.tree || []);
            } catch (error) {
                console.error('[LLModelForm] Failed to fetch group tree:', errorKind(error));
                toast.error(failureSentence('Failed to load groups', error));
            } finally {
                setGroupsLoading(false);
            }
        };

        runBackground(fetchGroups(), 'groups');
         
    }, [gateway.gatewayClient, isCatalog]);

    // Load routing groups for routing/load balancing
    useEffect(() => {
        if (isCatalog) return;  // Catalog entries don't participate in routing.
        const fetchRoutingGroups = async () => {
            if (!gateway.gatewayClient) return;

            setLoadingRoutingGroups(true);
            try {
                const response = await gateway.gatewayClient.listRoutingGroups({
                    enabled_only: false,
                });
                setRoutingGroups(response.routing_groups || []);
                reads.loaded('routingGroups');
            } catch (error) {
                // No toast: the field says the groups could not be loaded.
                reads.failed('routingGroups', 'Failed to load the routing groups', error);
            } finally {
                setLoadingRoutingGroups(false);
            }
        };

        runBackground(fetchRoutingGroups(), 'routing groups');
         
    }, [gateway.gatewayClient, isCatalog, asked, reads]);

    // Load all models to populate the Fallbacks picker. Candidates are filtered
    // to the SAME mode (a chat model fails over to chat models, an embedding
    // model to embedding models, …) and exclude the model being edited.
    useEffect(() => {
        if (isCatalog) return;
        const fetchModels = async () => {
            if (!gateway.gatewayClient) return;
            try {
                const response = await gateway.gatewayClient.listLLMs();
                setAllModels(Object.values(response.models || {}));
                reads.loaded('models');
            } catch (error) {
                reads.failed('models', 'Failed to load the models a fallback can be chosen from', error);
            }
        };
        runBackground(fetchModels(), 'models');
         
    }, [gateway.gatewayClient, isCatalog, asked, reads]);

    // ===== WHAT THE GATEWAY NEEDS OF THIS MODEL (providerConnection.ts) =====
    // An Amazon Bedrock model is signed with its own AWS credential; a
    // model the control plane says the gateway has no address for needs an
    // API URL, so its URL cannot be "auto-discovered".
    const isBedrock = !isCatalog && isBedrockProvider(provider);
    const needsApiUrl = !isCatalog && catalogNeedsApiUrl === true;
    // The catalog was asked and has no answer for this provider and mode.
    const destinationUnknown = !isCatalog && destinationAsked && catalogNeedsApiUrl === undefined;
    const autoDiscoverUrl = apiUrlAutoDiscovered && !needsApiUrl;
    // A Vertex AI model is called with its own service account, in its own
    // project: one way to authenticate, as for Bedrock, and never an API key.
    const isVertex = !isCatalog && isVertexProvider(provider);
    const effectiveAuthType = isBedrock ? 'aws_iam' : isVertex ? 'gcp_service_account' : authType;
    // The gateway also reads the region, the project and the location from
    // the model's metadata, as it is typed in the form now.
    let cloudMetadata: unknown;
    try {
        cloudMetadata = modelMetadata.trim() ? (JSON.parse(modelMetadata) as unknown) : undefined;
    } catch {
        cloudMetadata = undefined;
    }
    const awsErrors = isBedrock
        ? bedrockCredentialErrors(awsCredential, { stored: awsStored, enabled, apiUrl: autoDiscoverUrl ? '' : apiUrl, metadata: cloudMetadata })
        : {};
    const gcpErrors = isVertex
        ? vertexCredentialErrors(gcpCredential, {
              stored: gcpStored,
              enabled,
              metadata: cloudMetadata,
              // A Claude model with an API URL is sent to that address.
              addressGiven: !autoDiscoverUrl && apiUrl.trim() !== '' && modelName.toLowerCase().includes('claude'),
          })
        : {};
    const setGcpField = (field: keyof VertexCredentialInput, value: string) => {
        setGcpCredential(prev => ({ ...prev, [field]: value }));
        setErrors(prev => ({ ...prev, gcpProjectId: undefined, gcpLocation: undefined, gcpCredentialsJson: undefined }));
    };
    const apiUrlLocalHint = localAddressHint(apiUrl);
    const setAwsField = (field: keyof BedrockCredentialInput, value: string) => {
        setAwsCredential(prev => ({ ...prev, [field]: value }));
        // An error shown for the field is stale once it is typed in.
        setErrors(prev => ({ ...prev, awsAccessKeyId: undefined, awsSecretAccessKey: undefined, awsRegion: undefined }));
    };

    // ===== VALIDATION FUNCTIONS =====
    const validateFormField = (fieldName: string, value: any): string | undefined => {
        switch (fieldName) {
            case 'name':
                return validateField('name', value, VALIDATION_RULES);
            case 'model_name':
                return validateField('model_name', value, VALIDATION_RULES);
            case 'temperature':
                return validateField('temperature', value, VALIDATION_RULES);
            case 'maxTokens':
                return validateField('maxTokens', value, VALIDATION_RULES);
            case 'topP':
                return value !== undefined ? validateField('topP', value, VALIDATION_RULES) : undefined;
            case 'presencePenalty':
                return value !== undefined ? validateField('presencePenalty', value, VALIDATION_RULES) : undefined;
            case 'frequencyPenalty':
                return value !== undefined ? validateField('frequencyPenalty', value, VALIDATION_RULES) : undefined;
            case 'systemPrompt':
                return value ? validateField('systemPrompt', value, VALIDATION_RULES) : undefined;
            case 'responseFormat':
                return validateJSON(value, 'Response Format');
            case 'tools':
                return validateJSON(value, 'Tools');
            case 'apiUrl':
                if (needsApiUrl && !String(value ?? '').trim()) return apiUrlRequiredMessage(provider);
                return !autoDiscoverUrl && value ? validateField('apiUrl', value, VALIDATION_RULES) : undefined;
            default:
                return undefined;
        }
    };

    const handleFieldChange = (fieldName: string, value: any, setter: (val: any) => void) => {
        setter(value);
        setTouched(prev => new Set(prev).add(fieldName));
        const error = validateFormField(fieldName, value);
        setErrors(prev => ({ ...prev, [fieldName]: error }));
    };

    const handleFieldBlur = (fieldName: string, value: any) => {
        setTouched(prev => new Set(prev).add(fieldName));
        const error = validateFormField(fieldName, value);
        setErrors(prev => ({ ...prev, [fieldName]: error }));
    };

    const validateAllFields = (): boolean => {
        const newErrors: ValidationErrors = {};
        newErrors.name = validateFormField('name', name);
        newErrors.model_name = validateFormField('model_name', modelName);
        newErrors.temperature = validateFormField('temperature', temperature);
        newErrors.maxTokens = validateFormField('maxTokens', maxTokens);
        if (topP !== undefined) newErrors.topP = validateFormField('topP', topP);
        if (presencePenalty !== undefined) newErrors.presencePenalty = validateFormField('presencePenalty', presencePenalty);
        if (frequencyPenalty !== undefined) newErrors.frequencyPenalty = validateFormField('frequencyPenalty', frequencyPenalty);
        if (systemPrompt) newErrors.systemPrompt = validateFormField('systemPrompt', systemPrompt);
        if (responseFormat) newErrors.responseFormat = validateFormField('responseFormat', responseFormat);
        if (tools) newErrors.tools = validateFormField('tools', tools);
        if (needsApiUrl || (!autoDiscoverUrl && apiUrl)) newErrors.apiUrl = validateFormField('apiUrl', apiUrl);
        newErrors.awsAccessKeyId = awsErrors.accessKeyId;
        newErrors.awsSecretAccessKey = awsErrors.secretAccessKey;
        newErrors.awsRegion = awsErrors.region;
        newErrors.gcpProjectId = gcpErrors.projectId;
        newErrors.gcpLocation = gcpErrors.location;
        newErrors.gcpCredentialsJson = gcpErrors.credentialsJson;
        newErrors.circuitBreaker = circuitBreakerProblem(allowedFails, breakerCooldown);

        setErrors(newErrors);
        setTouched(new Set(Object.keys(newErrors)));

        const hasErrors = Object.values(newErrors).some(error => error !== undefined);
        return !hasErrors;
    };

    const parseJSON = (value: string): any => {
        if (!value.trim()) return undefined;
        try {
            return JSON.parse(value);
        } catch {
            return undefined;
        }
    };

    const getDefaultApiUrl = (providerValue: string): string => {
        const urls: Record<string, string> = {
            openai: 'https://api.openai.com/v1/chat/completions',
            anthropic: 'https://api.anthropic.com/v1/messages',
            gemini: 'https://generativelanguage.googleapis.com/v1beta/models',
            mistral: 'https://api.mistral.ai/v1',
            cohere: 'https://api.cohere.ai/v1',
            groq: 'https://api.groq.com/openai/v1',
            together_ai: 'https://api.together.xyz/v1',
            fireworks_ai: 'https://api.fireworks.ai/inference/v1',
            perplexity: 'https://api.perplexity.ai',
            deepseek: 'https://api.deepseek.com/v1',
            xai: 'https://api.x.ai/v1',
            // No address for a model server the administrator runs (Ollama,
            // LM Studio, vLLM): "localhost" would be the gateway's own
            // machine, and only the administrator knows the real one.
        };
        return urls[providerValue] || '';
    };

    const handleProviderChange = (newProvider: string) => {
        setProvider(newProvider);
        if (!model && apiUrlAutoDiscovered) {
            setApiUrl(getDefaultApiUrl(newProvider));
        }
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();

        // Validate all fields first
        const isValid = validateAllFields();
        if (!isValid) {
            toast.error('Please fix validation errors before submitting');
            const firstErrorField = Object.keys(errors).find(key => errors[key]);
            if (firstErrorField) {
                document.getElementById(firstErrorField)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
            return;
        }

        setIsSubmitting(true);
        setServerError(null);

        try {
            const modelData: LLModelFormData = {
                name: name.trim(),
                description: description?.trim(),
                version: version?.trim() || undefined,
                usage: usage?.trim() || undefined,
                tags: tags.trim() ? tags.split(',').map(s => s.trim()).filter(Boolean) : undefined,
                provider: provider,
                model_name: modelName.trim(),
                mode: mode,
                enabled: enabled,
                // Tenant-wide default Limits / Policies (min-wins with RG).
                tenant_limits: withoutRefused('llm_limits', tenantLimits),
                tenant_governance: withoutRefused('llm_governance', tenantGovernance),
                // Retirement metadata — empty string from the date
                // picker means "unset", which the backend expects
                // as null (`Optional[date]`). Send null explicitly
                // when the field was cleared so the patch round-
                // trips an unset correctly.
                retire_date: retireDate || null,
                retirement_announced_at: retirementAnnouncedAt || null,
                retirement_reminder_date: retirementReminderDate || null,
                retirement_replacement_model_name:
                    retirementReplacementModelName.trim() || null,
                retirement_replacement_provider_key:
                    retirementReplacementProviderKey.trim() || null,
                retirement_notes: retirementNotes.trim() || null,
                api_url_auto_discovered: autoDiscoverUrl,
                api_url: autoDiscoverUrl ? undefined : apiUrl.trim(),
                auth_type: effectiveAuthType,
                // A cloud model has no API key: none is sent, and the one a
                // stored model carries from another provider is cleared.
                api_key:
                    effectiveAuthType === 'api_key' && apiKey
                        ? apiKey.trim()
                        : isBedrock || isVertex
                            ? cloudModelApiKey(model?.provider, provider)
                            : undefined,
                // Absent keeps what is stored; what is sent is merged into
                // it by the control plane. An Amazon Bedrock model sends the
                // parts of its AWS credential that changed, a Vertex AI
                // model the Google Cloud fields that were typed, and a model
                // of any other provider nothing.
                provider_config: isBedrock
                    ? bedrockProviderConfig(awsCredential, awsStored, { removeSessionToken: removeAwsSessionToken })
                    : isVertex
                        ? vertexProviderConfig(gcpCredential, gcpStored)
                        : undefined,
                api_version: apiVersion.trim(),
                extra_headers: parseJSON(extraHeaders),
                default_temperature: parseFloat(temperature),
                default_max_tokens: parseInt(maxTokens),
                system_prompt: systemPrompt.trim(),  // Send empty string to clear, not undefined
                top_p: topP,
                n: n,
                stop_sequences: stopSequences.length > 0 ? stopSequences : undefined,
                seed: seed,
                presence_penalty: presencePenalty,
                frequency_penalty: frequencyPenalty,
                logit_bias: parseJSON(logitBias),
                response_format: parseJSON(responseFormat),
                logprobs: logprobs,
                top_logprobs: topLogprobs,
                stream_options: parseJSON(streamOptions),
                tools: parseJSON(tools),
                tool_choice: toolChoice !== 'auto' ? toolChoice : undefined,
                parallel_tool_calls: parallelToolCalls,
                user_identifier: userIdentifier.trim(),
                model_metadata: parseJSON(modelMetadata),
                drop_params: dropParams,
                additional_drop_params: additionalDropParams ? additionalDropParams.split(',').map(s => s.trim()).filter(Boolean) : undefined,
                thinking_enabled: thinkingEnabled,
                supports_streaming: supportsStreaming,
                supports_function_calling: supportsFunctionCalling,
                supports_vision: supportsVision,
                supports_batch: supportsBatch,
                routing_weight: parseFloat(routingWeight),
                // Empty string means "No routing group (standalone)"
                // — send null so the backend sees an explicit detach
                // intent, not an absent field. With `undefined` here
                // the field would be stripped from the JSON payload
                // and the server would treat it as "no change",
                // making detach impossible on an already-attached
                // model.
                routing_group_id: routingGroupId === '' ? null : routingGroupId,
                // Model-level failover chain (ordered model ids). Send [] to
                // clear so an operator can remove all fallbacks on an existing model.
                fallback_models: fallbackModels,
                rate_limit_rpm: rateLimitRpm ? parseInt(rateLimitRpm) : undefined,
                rate_limit_tpm: rateLimitTpm ? parseInt(rateLimitTpm) : undefined,
                parameters: parameters.trim() ? parseFloat(parameters.trim()) : undefined,
                capabilities: (() => {
                    // Preserve existing capabilities and override context_window if user set one
                    const base = (model?.capabilities && typeof model.capabilities === 'object')
                        ? { ...model.capabilities }
                        : {};
                    if (contextWindow.trim()) {
                        base.context_window = parseInt(contextWindow.trim(), 10);
                    } else {
                        delete base.context_window;
                    }
                    return Object.keys(base).length > 0 ? base : undefined;
                })(),
                cost_per_input_token: costPerInputToken ? parseFloat(costPerInputToken) : undefined,
                cost_per_output_token: costPerOutputToken ? parseFloat(costPerOutputToken) : undefined,
                // Image-mode columns. Sent only when image-mode;
                // sending them on chat models is harmless but a tidy
                // payload survives review better.
                ...(mode === 'image_generation'
                    ? {
                          cost_per_image: costPerImage ? parseFloat(costPerImage) : undefined,
                          supported_image_sizes: supportedImageSizes
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                          supported_image_qualities: supportedImageQualities
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                          max_images_per_request: maxImagesPerRequest
                              ? parseInt(maxImagesPerRequest, 10)
                              : 1,
                      }
                    : {}),
                ...(mode === 'audio_speech'
                    ? {
                          cost_per_audio_char: costPerAudioChar
                              ? parseFloat(costPerAudioChar)
                              : undefined,
                          supported_voices: supportedVoices
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                          supported_audio_formats: supportedAudioFormats
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                      }
                    : {}),
                ...(mode === 'audio_transcription'
                    ? {
                          cost_per_audio_minute: costPerAudioMinute
                              ? parseFloat(costPerAudioMinute)
                              : undefined,
                          max_audio_duration_seconds: maxAudioDurationSeconds
                              ? parseInt(maxAudioDurationSeconds, 10)
                              : undefined,
                          supported_input_formats: supportedInputFormats
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                          // Empty list → null on the wire so the
                          // proxy falls back to its per-model default.
                          supported_response_formats:
                              supportedResponseFormats.length > 0
                                  ? supportedResponseFormats
                                  : null,
                      }
                    : {}),
                ...(mode === 'video_generation'
                    ? {
                          cost_per_video_second: costPerVideoSecond
                              ? parseFloat(costPerVideoSecond)
                              : undefined,
                          supported_video_resolutions: supportedVideoResolutions
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                          supported_video_durations: supportedVideoDurations
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean)
                              .map((s) => parseInt(s, 10))
                              .filter((n) => Number.isFinite(n)),
                          max_video_duration_seconds: maxVideoDurationSeconds
                              ? parseInt(maxVideoDurationSeconds, 10)
                              : undefined,
                      }
                    : {}),
                timeout: parseFloat(timeout),
                max_concurrent_requests: maxConcurrentRequests.trim()
                    ? parseInt(maxConcurrentRequests)
                    : null,
                // Both or neither; both emptied clears the breaker.
                allowed_fails: allowedFails.trim() ? parseInt(allowedFails, 10) : null,
                cooldown_time: breakerCooldown.trim() ? parseInt(breakerCooldown, 10) : null,
                // Send the admin's selection as-is — the backend does not
                // force-inject Default (matches MCP server-config behavior).
                // Sentinel stays supported for callers that rely on it.
                group_ids: selectedGroupIds,
                // Catalog-only fields — surfaced on the General tab in catalog
                // mode and read back by the catalog adapter. Harmless on the
                // instance path (the instance onSave ignores them).
                ...(isCatalog
                    ? {
                          recommended,
                          deprecated,
                          icon_url: iconUrl.trim() || null,
                      }
                    : {}),
            };

            await onSave(modelData);
            toast.success(model ? 'Model updated' : 'Model created');
            onClose();
        } catch (error: any) {
            // The control plane refused the model for its missing API URL:
            // its sentence goes next to the field, which is shown for it.
            const noDestination = apiUrlRefusal(error);
            // A cloud model the gateway cannot serve, or a credential the
            // control plane cannot use or store: in plain words.
            const refused = modelSaveRefusalText(error);
            if (refused) {
                setServerError(refused);
            } else if (noDestination) {
                setApiUrlAutoDiscovered(false);
                setActiveTab('api');
                setTouched(prev => new Set(prev).add('apiUrl'));
                setErrors(prev => ({ ...prev, apiUrl: noDestination }));
            } else
            // Display server validation errors in the form
            if (error.validationErrors && Array.isArray(error.validationErrors)) {
                // Format 422 validation errors from FastAPI
                const lines = error.validationErrors.map((e: any) => {
                    const field = e.loc?.slice(1).join('.') || 'unknown';
                    return `${field}: ${e.msg}`;
                });
                setServerError(lines.join('\n'));
            } else if (error.message) {
                setServerError(error.message);
            } else {
                setServerError('Failed to save model');
            }
        } finally {
            setIsSubmitting(false);
        }
    };

    const openHelp = (section: TabId) => {
        setHelpSection(section);
        setShowHelp(true);
    };

    const NavItem: React.FC<{ id: TabId; label: string; icon: React.ReactNode }> = ({ id, label, icon }) => (
        <button
            type="button"
            onClick={() => setActiveTab(id)}
            className={`flex items-center space-x-2.5 w-full px-3 py-2.5 text-sm font-medium rounded-lg transition-colors text-left ${
                activeTab === id
                    ? 'bg-primary-50 text-primary-700 border-l-3 border-primary-500'
                    : 'text-secondary hover:text-default hover:bg-surface-alt'
            }`}
        >
            {icon}
            <span>{label}</span>
        </button>
    );

    return (
        <>
            <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                <div className="bg-surface rounded-lg shadow-xl w-full max-w-5xl max-h-[90vh] flex flex-col">
                    {/* Header */}
                    <div className="flex items-center justify-between px-6 py-4 border-b border-default flex-shrink-0">
                        <div className="flex items-center space-x-3">
                            <Brain className="w-6 h-6 text-primary-600" />
                            <h2 className="text-xl font-semibold text-default">
                                {isCatalog
                                    ? (model ? `Edit catalog entry — ${model.name}` : 'Add catalog entry')
                                    : (model ? `Edit LLM Model — ${model.name} (${model.model_name})` : 'Add New LLM Model')}
                            </h2>
                        </div>
                        <div className="flex items-center space-x-2">
                            <button
                                type="button"
                                onClick={() => openHelp(activeTab)}
                                className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                                title="Help"
                            >
                                <HelpCircle className="w-5 h-5" />
                            </button>
                            <button
                                onClick={onClose}
                                className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                    </div>

                    {/* Sidebar + Content Layout */}
                    <div className="flex-1 flex overflow-hidden min-h-0 min-w-0">
                        {/* Left Sidebar Navigation — mode-aware. Tabs that don't apply to the
                            current model's mode are hidden so an operator editing e.g. an image
                            model isn't faced with empty Generation / Tuning / Tools panes. */}
                        <nav className="w-48 flex-shrink-0 border-r border-default bg-surface-alt py-3 px-2 space-y-0.5 overflow-y-auto">
                            {/* Always-shown core tabs. */}
                            <NavItem id="basic" label="General" icon={<Brain className="w-4 h-4" />} />
                            <NavItem id="audit" label="Audit Info" icon={<FileClock className="w-4 h-4" />} />
                            <NavItem id="cost" label="Cost" icon={<DollarSign className="w-4 h-4" />} />
                            {/* Access Control is instance-only — catalog entries have
                                no group bindings. */}
                            {!isCatalog && (
                                <NavItem id="access" label="Access Control" icon={<Users className="w-4 h-4" />} />
                            )}
                            {/* Media tab — only for image / audio / video. */}
                            {MEDIA_MODES.includes(mode) && (
                                <NavItem id="media" label="Media" icon={<FileCode className="w-4 h-4" />} />
                            )}
                            {/* API / generation / tuning / output / tools / param-control /
                                routing / sys-prompt are all instance-only — hidden for
                                catalog entries (they carry import defaults, not a live
                                connection). */}
                            {!isCatalog && (
                                <NavItem id="api" label="API" icon={<Globe className="w-4 h-4" />} />
                            )}
                            {/* Chat-style generation parameters — only meaningful for
                                chat / completion / responses. Hidden for embedding /
                                rerank / image / audio / video / moderation / routing. */}
                            {!isCatalog && isChatLike && (
                                <>
                                    <NavItem id="generation" label="Generation" icon={<Zap className="w-4 h-4" />} />
                                    <NavItem id="tuning" label="Tuning" icon={<Sliders className="w-4 h-4" />} />
                                    <NavItem id="output" label="Output" icon={<FileCode className="w-4 h-4" />} />
                                </>
                            )}
                            {/* Tools / function calling — only chat & responses. */}
                            {!isCatalog && isToolCapable && (
                                <NavItem id="tools" label="Tools" icon={<Settings className="w-4 h-4" />} />
                            )}
                            {/* Param Control — drop_params & friends, chat-style only. */}
                            {!isCatalog && isChatLike && (
                                <NavItem id="params" label="Param Control" icon={<List className="w-4 h-4" />} />
                            )}
                            {/* Fallbacks — model→model failover chain. Applies to ANY
                                mode (chat, embedding, image, audio…): a model fails over
                                to other models of the same mode. Keep visible for all
                                instance modes, like Routing. */}
                            {!isCatalog && (
                                <NavItem id="fallbacks" label="Fallbacks" icon={<ShieldCheck className="w-4 h-4" />} />
                            )}
                            {/* Routing applies to any model that participates in load
                                balancing — keep visible for all instance modes. */}
                            {!isCatalog && (
                                <NavItem id="routing" label="Routing" icon={<Activity className="w-4 h-4" />} />
                            )}
                            {/* System prompt — chat-style only. */}
                            {!isCatalog && isChatLike && (
                                <NavItem id="prompt" label="Sys Prompt" icon={<MessageSquare className="w-4 h-4" />} />
                            )}
                            {/* Tenant-wide default Limits / Policies for this
                                model (min-wins with each Resource Group). Not
                                shown for the global catalog. */}
                            {!isCatalog && (
                                <NavItem id="limits" label="Limits" icon={<Gauge className="w-4 h-4" />} />
                            )}
                            {!isCatalog && (
                                <NavItem id="policies" label="Policies" icon={<ShieldCheck className="w-4 h-4" />} />
                            )}
                            {/* Retirement metadata — always visible. Most models
                                have no retirement set; the tab renders an empty
                                state with a clear call-to-action when so. */}
                            <NavItem id="retirement" label="Retirement" icon={<Calendar className="w-4 h-4" />} />
                        </nav>

                        {/* Right Content Area */}
                        <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="scroll-region">
                            <div className="p-6">
                                {/* Server Error Banner */}
                                {serverError && (
                                    <div className="mb-6 bg-red-50 border border-red-300 rounded-lg p-4 flex items-start space-x-3">
                                        <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                                        <div className="flex-1 min-h-0 min-w-0">
                                            <p className="text-sm font-medium text-red-800 mb-1">
                                                {model ? 'Failed to update model' : 'Failed to create model'}
                                            </p>
                                            <pre className="text-sm text-red-700 whitespace-pre-wrap font-mono">{serverError}</pre>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => setServerError(null)}
                                            className="text-red-400 hover:text-red-600"
                                        >
                                            <X className="w-4 h-4" />
                                        </button>
                                    </div>
                                )}

                                {/* A model that reads as enabled and takes no call:
                                    past its retire date, or of a disabled provider. */}
                                {model && !isCatalog && notServedReasons({ retire_date: retireDate, provider, mode }, fetchedProviders).length > 0 && (
                                    <div
                                        className="mb-6 bg-red-50 border border-red-300 rounded-lg p-4 flex items-start space-x-3"
                                        data-testid="model-not-served-banner"
                                    >
                                        <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                                        <p className="text-sm text-red-800">
                                            {notServedText(notServedReasons({ retire_date: retireDate, provider, mode }, fetchedProviders))}
                                        </p>
                                    </div>
                                )}

                                {/* Validation Summary */}
                                {Object.values(errors).some(e => e) && (
                                    <div className="mb-6">
                                        <ValidationSummary errors={errors} />
                                    </div>
                                )}

                                {/* TAB 1: BASIC INFO */}
                                {activeTab === 'basic' && (
                                    <div className="space-y-4">
                                        {/* Info Box - Skyblue. Instance-only: the catalog form
                                            has no API tab to point at. */}
                                        {!isCatalog && (
                                            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                                <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <p className="text-sm text-blue-900">
                                                        <strong>Most providers use auto-discovery</strong> - check the API Config tab to enable automatic API URL detection for standard providers like OpenAI, Anthropic, Google, etc.
                                                    </p>
                                                </div>
                                                <button
                                                    type="button"
                                                    onClick={() => openHelp('basic')}
                                                    className="text-blue-600 hover:text-blue-700"
                                                >
                                                    <HelpCircle className="w-5 h-5" />
                                                </button>
                                            </div>
                                        )}

                                        {/* Name + Usage */}
                                        <div className="grid grid-cols-2 gap-4">
                                            <div>
                                                <ValidatedInput
                                                    id="name"
                                                    label="Name"
                                                    value={name}
                                                    onChange={(value) => handleFieldChange('name', value, setName)}
                                                    onBlur={() => handleFieldBlur('name', name)}
                                                    error={touched.has('name') ? errors.name : undefined}
                                                    showSuccess={touched.has('name') && !errors.name}
                                                    required
                                                    placeholder="e.g., Production GPT-4o"
                                                    helpText="A friendly display name for this model"
                                                />
                                            </div>
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Usage
                                                </label>
                                                <input
                                                    type="text"
                                                    value={usage}
                                                    onChange={(e) => setUsage(e.target.value)}
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    placeholder="e.g., Customer support chatbot"
                                                />
                                            </div>
                                        </div>

                                        {/* Description + Tags */}
                                        <div className="grid grid-cols-2 gap-4">
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Description
                                                </label>
                                                <textarea
                                                    value={description}
                                                    onChange={(e) => setDescription(e.target.value)}
                                                    rows={2}
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    placeholder="Optional description"
                                                />
                                            </div>
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Tags
                                                </label>
                                                <textarea
                                                    value={tags}
                                                    onChange={(e) => setTags(e.target.value)}
                                                    rows={2}
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    placeholder="coding, fast, multilingual"
                                                />
                                                <p className="text-xs text-muted mt-1">Comma-separated</p>
                                            </div>
                                        </div>

                                        <div className="grid grid-cols-3 gap-4">
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Provider *
                                                </label>
                                                {isCatalog && model ? (
                                                    // Provider is immutable on a catalog update.
                                                    <p className="px-3 py-2 text-sm text-default bg-surface-alt rounded-lg border border-default font-mono truncate">
                                                        {provider}
                                                    </p>
                                                ) : (
                                                <>
                                                <select
                                                    value={provider}
                                                    onChange={(e) => handleProviderChange(e.target.value)}
                                                    required
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                >
                                                    {(() => {
                                                        // Options come from the GLOBAL provider catalog so
                                                        // every provider is selectable even if not yet
                                                        // configured on this tenant — the backend
                                                        // auto-imports the chosen provider into
                                                        // `llm_providers` on model create. Grouped by the
                                                        // catalog entry's category → tier. No per-tenant
                                                        // enabled state here, so no "(disabled)" suffix.
                                                        const grouped: Record<string, LlmProviderCatalogEntry[]> = {};
                                                        for (const p of providerCatalog) {
                                                            const tier = CATEGORY_TO_TIER[p.category ?? ''] ?? 'Other';
                                                            (grouped[tier] ||= []).push(p);
                                                        }
                                                        const orderedTiers = TIER_ORDER.filter(t => grouped[t]?.length);
                                                        // Guard: if the catalog hasn't loaded yet (or the
                                                        // current provider isn't in it), still render the
                                                        // selected value so the form never loses it.
                                                        const known = providerCatalog.some(p => p.key === provider);
                                                        return (
                                                            <>
                                                                {!known && provider && (
                                                                    <option value={provider}>{provider}</option>
                                                                )}
                                                                {orderedTiers.map(tier => (
                                                                    <optgroup key={tier} label={tier}>
                                                                        {(grouped[tier] || [])
                                                                            .slice()
                                                                            .sort((a, b) =>
                                                                                a.display_name.localeCompare(b.display_name),
                                                                            )
                                                                            .map(p => (
                                                                                <option key={p.id} value={p.key}>
                                                                                    {p.display_name}
                                                                                </option>
                                                                            ))}
                                                                    </optgroup>
                                                                ))}
                                                            </>
                                                        );
                                                    })()}
                                                </select>
                                                {(() => {
                                                    // Icon preview. The upload control only makes sense
                                                    // when the selected provider already exists as a
                                                    // configured `llm_providers` row (there's a real id to
                                                    // POST `/v1/admin/providers/{id}/icon` against). That's
                                                    // the edit flow. For the ADD flow — where the provider
                                                    // is a catalog entry not yet imported — there's no id
                                                    // to upload against, so we render the slug-based brand
                                                    // mark via <ProviderLogo> instead. The backend
                                                    // auto-imports the provider (with its catalog icon) on
                                                    // model create.
                                                    if (!provider) return null;
                                                    const configured = fetchedProviders.find(
                                                        p => p.key === provider,
                                                    );
                                                    if (model && configured) {
                                                        return (
                                                            <ProviderIconAndUpload
                                                                provider={configured}
                                                                api={gateway.gatewayClient ?? null}
                                                            />
                                                        );
                                                    }
                                                    return (
                                                        <div className="mt-2 flex items-center gap-2 text-xs text-secondary">
                                                            <ProviderLogo provider={provider} size="lg" />
                                                            <span className="text-muted">
                                                                Brand icon imported automatically when the model is saved.
                                                            </span>
                                                        </div>
                                                    );
                                                })()}
                                                </>
                                                )}
                                            </div>

                                            {isCatalog && model ? (
                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Model
                                                    </label>
                                                    {/* Model name is immutable on a catalog update. */}
                                                    <p className="px-3 py-2 text-sm text-default bg-surface-alt rounded-lg border border-default font-mono truncate">
                                                        {modelName}
                                                    </p>
                                                </div>
                                            ) : (
                                            <ValidatedInput
                                                id="model_name"
                                                label="Model"
                                                value={modelName}
                                                onChange={(value) => handleFieldChange('model_name', value, setModelName)}
                                                onBlur={() => handleFieldBlur('model_name', modelName)}
                                                error={touched.has('model_name') ? errors.model_name : undefined}
                                                showSuccess={touched.has('model_name') && !errors.model_name}
                                                required
                                                placeholder="e.g., gpt-5.2"
                                                helpText="The specific model identifier"
                                                list="model-suggestions"
                                            />
                                            )}

                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Mode *
                                                </label>
                                                <select
                                                    value={mode}
                                                    onChange={(e) => setMode(e.target.value)}
                                                    required
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                >
                                                    <optgroup label="Core">
                                                        <option value="chat">Chat</option>
                                                        <option value="responses">Responses (Structured Output)</option>
                                                        <option value="completion">Completion (Legacy)</option>
                                                    </optgroup>
                                                    <optgroup label="Specialised">
                                                        <option value="embedding">Embedding</option>
                                                        <option value="image_generation">Image Generation</option>
                                                        <option value="audio_transcription">Audio Transcription</option>
                                                        <option value="audio_speech">Audio Speech (TTS)</option>
                                                        <option value="video_generation">Video Generation</option>
                                                        <option value="rerank">Rerank</option>
                                                        <option value="moderation">Moderation</option>
                                                    </optgroup>
                                                    <optgroup label="Internal">
                                                        <option value="routing">Routing (Virtual Model)</option>
                                                    </optgroup>
                                                </select>
                                            </div>

                                            {/* Parameters + Context Window — chat-flavoured fields,
                                                hidden for image / audio / video / rerank / moderation
                                                where they don't apply. State is preserved on mode flip. */}
                                            {!isNonChatMode && (
                                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Parameters (Billions)
                                                        </label>
                                                        <input
                                                            type="number"
                                                            value={parameters}
                                                            onChange={(e) => setParameters(e.target.value)}
                                                            placeholder="e.g. 8, 70, 405"
                                                            min={0}
                                                            step="0.1"
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Model size in billions of parameters. Displayed as a badge (e.g. 8 → 8B, 0.5 → 500M).
                                                        </p>
                                                    </div>
                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Context Window (tokens)
                                                        </label>
                                                        <input
                                                            type="number"
                                                            value={contextWindow}
                                                            onChange={(e) => setContextWindow(e.target.value)}
                                                            placeholder="e.g. 200000, 1000000"
                                                            min={0}
                                                            step={1024}
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Maximum input tokens the model accepts. Displayed as a badge (e.g. 200000 → 200K, 1000000 → 1M).
                                                        </p>
                                                    </div>
                                                </div>
                                            )}

                                            <ReadFailureNotice compact failure={readFailure('providers')} onRetry={askAgain} />
                                            <ReadFailureNotice compact failure={readFailure('suggestions')} onRetry={askAgain} />
                                            {/* Datalist for model suggestions */}
                                            {modelSuggestions.length > 0 && (
                                                <datalist id="model-suggestions">
                                                    {modelSuggestions.map(m => (
                                                        <option key={m} value={m} />
                                                    ))}
                                                </datalist>
                                            )}
                                        </div>

                                        {/* Capabilities — Streaming / Function Calling / Vision are
                                            chat-only and hidden for non-chat modes. Batch Processing
                                            applies to any mode and stays visible. State is preserved
                                            on mode flip so toggling chat → image → chat doesn't lose
                                            previously-set capability flags. */}
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-3">
                                                Capabilities
                                            </label>
                                            <div className="grid grid-cols-3 gap-3">
                                                {!isNonChatMode && (
                                                    <>
                                                        <label className="flex items-center space-x-2">
                                                            <input
                                                                type="checkbox"
                                                                checked={supportsStreaming}
                                                                onChange={(e) => setSupportsStreaming(e.target.checked)}
                                                                className="w-4 h-4 text-primary-600 rounded"
                                                            />
                                                            <span className="text-sm">Streaming</span>
                                                        </label>
                                                        <label className="flex items-center space-x-2">
                                                            <input
                                                                type="checkbox"
                                                                checked={supportsFunctionCalling}
                                                                onChange={(e) => setSupportsFunctionCalling(e.target.checked)}
                                                                className="w-4 h-4 text-primary-600 rounded"
                                                            />
                                                            <span className="text-sm">Function Calling</span>
                                                        </label>
                                                        <label className="flex items-center space-x-2">
                                                            <input
                                                                type="checkbox"
                                                                checked={supportsVision}
                                                                onChange={(e) => setSupportsVision(e.target.checked)}
                                                                className="w-4 h-4 text-primary-600 rounded"
                                                            />
                                                            <span className="text-sm">Vision</span>
                                                        </label>
                                                    </>
                                                )}
                                                <label className="flex items-center space-x-2">
                                                    <input
                                                        type="checkbox"
                                                        checked={supportsBatch}
                                                        onChange={(e) => setSupportsBatch(e.target.checked)}
                                                        className="w-4 h-4 text-primary-600 rounded"
                                                    />
                                                    <span className="text-sm">Batch Processing</span>
                                                </label>
                                            </div>
                                        </div>

                                        {/* `enabled` is an instance concern — catalog entries
                                            have no enabled flag. */}
                                        {!isCatalog && (
                                            <div>
                                                <label className="flex items-center space-x-2">
                                                    <input
                                                        type="checkbox"
                                                        checked={enabled}
                                                        onChange={(e) => setEnabled(e.target.checked)}
                                                        className="w-4 h-4 text-primary-600 rounded"
                                                    />
                                                    <span className="text-sm font-medium text-secondary">Enable this model</span>
                                                </label>
                                            </div>
                                        )}

                                        {/* Catalog-only fields. recommended / deprecated flag the
                                            entry in the browse list; icon_url overrides the provider
                                            logo; thinking_enabled normally lives on the hidden
                                            Generation tab so we surface it here as an import default. */}
                                        {isCatalog && (
                                            <div className="space-y-4 border-t border-default pt-4">
                                                <div className="flex flex-wrap items-center gap-6">
                                                    <label className="inline-flex items-center gap-2 text-sm text-default">
                                                        <input
                                                            type="checkbox"
                                                            checked={recommended}
                                                            onChange={(e) => setRecommended(e.target.checked)}
                                                            className="w-4 h-4 text-primary-600 rounded"
                                                        />
                                                        Recommended
                                                    </label>
                                                    <label className="inline-flex items-center gap-2 text-sm text-default">
                                                        <input
                                                            type="checkbox"
                                                            checked={deprecated}
                                                            onChange={(e) => setDeprecated(e.target.checked)}
                                                            className="w-4 h-4 text-primary-600 rounded"
                                                        />
                                                        Deprecated
                                                    </label>
                                                    <label className="inline-flex items-center gap-2 text-sm text-default">
                                                        <input
                                                            type="checkbox"
                                                            checked={thinkingEnabled}
                                                            onChange={(e) => setThinkingEnabled(e.target.checked)}
                                                            className="w-4 h-4 text-primary-600 rounded"
                                                        />
                                                        Thinking enabled
                                                    </label>
                                                </div>
                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Icon URL
                                                    </label>
                                                    <input
                                                        type="text"
                                                        value={iconUrl}
                                                        onChange={(e) => setIconUrl(e.target.value)}
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="https://…/icon.svg"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        Falls back to the provider logo when empty.
                                                    </p>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* TAB: COST */}
                                {activeTab === 'audit' && (
                                    <AuditInfoTab
                                        meta={{
                                            version,
                                            created_by: model?.created_by,
                                            created_at: model?.created_at,
                                            updated_by: model?.updated_by,
                                            updated_at: model?.updated_at,
                                        }}
                                        onVersionChange={setVersion}
                                        isNew={!model}
                                    />
                                )}

                                {activeTab === 'cost' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Token costs</strong> are used for usage tracking and cost center calculations in the dashboard.
                                                    Set the per-token cost for this model as charged by the provider (USD per 1M tokens).
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('cost')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        {(provider === 'huggingface' || apiUrl.includes('huggingface.co')) && (
                                            <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 flex items-start space-x-3">
                                                <AlertCircle className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
                                                <p className="text-sm text-amber-900">
                                                    HuggingFace does not publish per-token pricing in their API.
                                                    Check your <strong>HuggingFace billing dashboard</strong> for actual rates and enter them here manually.
                                                </p>
                                            </div>
                                        )}

                                        {/* Token-based pricing fields — shown for chat / completion /
                                            responses / embedding. For media modes (image / audio /
                                            video) the per-image / per-second / per-char fields live
                                            on the Media tab. For rerank / moderation / routing,
                                            per-call pricing isn't currently exposed in the UI. */}
                                        {isTokenPriced && (
                                            <div className="grid grid-cols-2 gap-4">
                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Input Cost (USD per 1M tokens)
                                                    </label>
                                                    <input
                                                        type="number"
                                                        value={costPerInputToken}
                                                        onChange={(e) => setCostPerInputToken(e.target.value)}
                                                        step="0.01"
                                                        min="0"
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="e.g. 0.15"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        Cost per 1 million input (prompt) tokens
                                                    </p>
                                                </div>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Output Cost (USD per 1M tokens)
                                                    </label>
                                                    <input
                                                        type="number"
                                                        value={costPerOutputToken}
                                                        onChange={(e) => setCostPerOutputToken(e.target.value)}
                                                        step="0.01"
                                                        min="0"
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="e.g. 0.60"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        {mode === 'embedding'
                                                            ? 'Embedding outputs are vectors, not tokens — usually leave at 0.'
                                                            : 'Cost per 1 million output (completion) tokens'}
                                                    </p>
                                                </div>
                                            </div>
                                        )}

                                        {MEDIA_MODES.includes(mode) && (
                                            <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 flex items-start space-x-3">
                                                <Info className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <p className="text-sm text-amber-900">
                                                        This model is in <strong>{mode}</strong> mode. Per-image / per-character / per-second
                                                        cost and the size/voice/format whitelists live on the
                                                        <strong> Media </strong> tab.
                                                    </p>
                                                </div>
                                            </div>
                                        )}

                                        {!isTokenPriced && !MEDIA_MODES.includes(mode) && (
                                            <div className="bg-surface-alt border border-default rounded-lg p-4 flex items-start space-x-3">
                                                <Info className="w-5 h-5 text-secondary flex-shrink-0 mt-0.5" />
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <p className="text-sm text-secondary">
                                                        Per-call cost configuration for <strong>{mode}</strong> mode
                                                        isn't currently exposed in the UI. Set
                                                        <code className="mx-1 px-1 py-0.5 bg-surface-raised rounded text-xs">cost_per_input_token</code>
                                                        on the model row directly via the API if you need to track
                                                        usage costs for this mode.
                                                    </p>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* TAB: ACCESS CONTROL */}
                                {activeTab === 'access' && (
                                    <div className="space-y-4">
                                        {/* Info Box */}
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Control which Resource Groups can access this model.</strong>{' '}
                                                    Pick the groups that should see and be billed for this model. Leave
                                                    empty for no group binding. Groups enable per-group cost tracking,
                                                    usage monitoring, alerts, and access policies.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('access')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        {/* Group Selector — user picks any combination, including Default.
                                            No group is auto-assigned; the admin's selection is what gets persisted. */}
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Resource Groups
                                            </label>

                                            {groupsLoading ? (
                                                <div className="text-sm text-muted py-4 text-center">
                                                    Loading groups...
                                                </div>
                                            ) : groupTree.length === 0 ? (
                                                <div className="text-sm text-muted py-4 px-3 bg-surface-alt rounded-lg border border-default">
                                                    <Info className="w-4 h-4 inline mr-1" />
                                                    No Resource Groups defined yet. Create groups in the Groups
                                                    section to track costs, usage, and alerts per assistant, app,
                                                    or agent.
                                                </div>
                                            ) : (
                                                <>
                                                    <HierarchicalGroupSelector
                                                        groupTree={groupTree}
                                                        selectedGroupIds={selectedGroupIds.filter(
                                                            (id) => id !== DEFAULT_GROUP_SENTINEL,
                                                        )}
                                                        onSelectionChange={(ids) => setSelectedGroupIds(ids)}
                                                    />

                                                    <div className="mt-2 text-sm text-secondary flex items-center">
                                                        <Users className="w-4 h-4 mr-1" />
                                                        <span>
                                                            {selectedGroupIds.length === 0
                                                                ? 'No groups selected — model will not be visible to any user until assigned'
                                                                : `${selectedGroupIds.length} group${selectedGroupIds.length === 1 ? '' : 's'} selected`}
                                                        </span>
                                                    </div>

                                                    <div className="mt-2 text-xs text-muted">
                                                        <Info className="w-3 h-3 inline mr-1" />
                                                        Resource Groups control cost tracking, usage monitoring,
                                                        alerts, and access policies. Seeded models are bound to
                                                        the Default group for convenience; you can uncheck it
                                                        and assign the model to specific groups instead.
                                                    </div>
                                                </>
                                            )}
                                        </div>
                                    </div>
                                )}

                                {/* MEDIA — image / audio / video parameters. */}
                                {/* Only rendered when the current mode is in MEDIA_MODES; the */}
                                {/* sidebar entry is hidden for chat / embedding / routing models. */}
                                {activeTab === 'media' && MEDIA_MODES.includes(mode) && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Media-mode settings.</strong> The proxy enforces the
                                                    whitelists below at the edge — a request with a size, voice,
                                                    or format outside the lists is rejected with HTTP 400 before
                                                    any provider call is made.
                                                </p>
                                            </div>
                                        </div>

                                        {/* IMAGE GENERATION */}
                                        {mode === 'image_generation' && (
                                            <div className="space-y-3 border border-default rounded-lg p-4">
                                                <p className="text-sm font-semibold text-secondary">
                                                    Image generation
                                                </p>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Cost per image (USD)
                                                    </label>
                                                    <input
                                                        type="number"
                                                        value={costPerImage}
                                                        onChange={(e) => setCostPerImage(e.target.value)}
                                                        step="0.001"
                                                        min="0"
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="e.g. 0.04"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        Per-generated-image cost. Distinct from token costs so
                                                        per-image and per-token spend roll up cleanly in dashboards.
                                                    </p>
                                                </div>

                                                <div className="grid grid-cols-2 gap-3">
                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Supported sizes
                                                        </label>
                                                        <input
                                                            type="text"
                                                            value={supportedImageSizes}
                                                            onChange={(e) => setSupportedImageSizes(e.target.value)}
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="1024x1024, 1024x1792, 1792x1024"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Comma-separated. Stored as a JSON whitelist.
                                                        </p>
                                                    </div>

                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Supported qualities
                                                        </label>
                                                        <input
                                                            type="text"
                                                            value={supportedImageQualities}
                                                            onChange={(e) => setSupportedImageQualities(e.target.value)}
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="standard, hd"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Comma-separated. e.g. {`["standard","hd"]`}.
                                                        </p>
                                                    </div>
                                                </div>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Max images per request (n)
                                                    </label>
                                                    <input
                                                        type="number"
                                                        value={maxImagesPerRequest}
                                                        onChange={(e) => setMaxImagesPerRequest(e.target.value)}
                                                        min="1"
                                                        max="10"
                                                        className="w-32 px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        DALL-E 3 hard-caps at 1, DALL-E 2 supports up to 10.
                                                    </p>
                                                </div>
                                            </div>
                                        )}

                                        {/* AUDIO TTS (audio_speech) */}
                                        {mode === 'audio_speech' && (
                                            <div className="space-y-3 border border-default rounded-lg p-4">
                                                <p className="text-sm font-semibold text-secondary">
                                                    Audio TTS (text-to-speech)
                                                </p>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Cost per character (USD)
                                                    </label>
                                                    <input
                                                        type="number"
                                                        value={costPerAudioChar}
                                                        onChange={(e) => setCostPerAudioChar(e.target.value)}
                                                        step="0.0000001"
                                                        min="0"
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="e.g. 0.000015"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        tts-1 ≈ 0.000015, eleven_flash_v2_5 ≈ 0.00004.
                                                    </p>
                                                </div>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Supported voices
                                                    </label>
                                                    <input
                                                        type="text"
                                                        value={supportedVoices}
                                                        onChange={(e) => setSupportedVoices(e.target.value)}
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="alloy, echo, fable, onyx, nova, shimmer"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        Comma-separated voice ids. The proxy validates incoming
                                                        requests against this list.
                                                    </p>
                                                </div>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Supported audio formats
                                                    </label>
                                                    <input
                                                        type="text"
                                                        value={supportedAudioFormats}
                                                        onChange={(e) => setSupportedAudioFormats(e.target.value)}
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="mp3, opus, aac, flac, wav, pcm"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        Comma-separated. Determines the Content-Type the proxy
                                                        will accept on outgoing TTS requests.
                                                    </p>
                                                </div>
                                            </div>
                                        )}

                                        {/* AUDIO TRANSCRIPTION */}
                                        {mode === 'audio_transcription' && (
                                            <div className="space-y-3 border border-default rounded-lg p-4">
                                                <p className="text-sm font-semibold text-secondary">
                                                    Audio transcription (speech-to-text)
                                                </p>

                                                <div className="grid grid-cols-2 gap-3">
                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Cost per minute (USD)
                                                        </label>
                                                        <input
                                                            type="number"
                                                            value={costPerAudioMinute}
                                                            onChange={(e) =>
                                                                setCostPerAudioMinute(e.target.value)
                                                            }
                                                            step="0.0001"
                                                            min="0"
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="e.g. 0.006"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            gpt-transcribe ≈ 0.0045/minute.
                                                        </p>
                                                    </div>

                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Max duration (seconds)
                                                        </label>
                                                        <input
                                                            type="number"
                                                            value={maxAudioDurationSeconds}
                                                            onChange={(e) =>
                                                                setMaxAudioDurationSeconds(e.target.value)
                                                            }
                                                            min="1"
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="e.g. 1800"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Hard cap. Whisper file-size limit ≈ 30 min audio.
                                                        </p>
                                                    </div>
                                                </div>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Supported input formats
                                                    </label>
                                                    <input
                                                        type="text"
                                                        value={supportedInputFormats}
                                                        onChange={(e) =>
                                                            setSupportedInputFormats(e.target.value)
                                                        }
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                        placeholder="mp3, mp4, wav, m4a, webm"
                                                    />
                                                    <p className="text-xs text-muted mt-1">
                                                        Comma-separated. Matched against filename
                                                        extension and Content-Type at upload time.
                                                    </p>
                                                </div>

                                                <div>
                                                    <label className="block text-sm font-medium text-secondary mb-2">
                                                        Supported response formats
                                                    </label>
                                                    <div className="grid grid-cols-3 gap-2">
                                                        {(['json', 'text', 'verbose_json', 'srt', 'vtt'] as const).map(
                                                            (fmt) => {
                                                                const checked = supportedResponseFormats.includes(fmt);
                                                                return (
                                                                    <label
                                                                        key={fmt}
                                                                        className="flex items-center gap-2 px-3 py-2 border border-strong rounded-lg cursor-pointer hover:bg-tertiary"
                                                                    >
                                                                        <input
                                                                            type="checkbox"
                                                                            checked={checked}
                                                                            onChange={(e) => {
                                                                                if (e.target.checked) {
                                                                                    setSupportedResponseFormats([
                                                                                        ...supportedResponseFormats,
                                                                                        fmt,
                                                                                    ]);
                                                                                } else {
                                                                                    setSupportedResponseFormats(
                                                                                        supportedResponseFormats.filter(
                                                                                            (f) => f !== fmt
                                                                                        )
                                                                                    );
                                                                                }
                                                                            }}
                                                                            className="rounded"
                                                                        />
                                                                        <span className="text-sm font-mono">{fmt}</span>
                                                                    </label>
                                                                );
                                                            }
                                                        )}
                                                    </div>
                                                    <p className="text-xs text-muted mt-1">
                                                        Whisper-style upstream response formats this
                                                        model accepts. Leave all unchecked to use the
                                                        proxy's per-model default.
                                                        <code className="ml-1">gpt-transcribe</code>
                                                        documents only <code>json</code>.
                                                    </p>
                                                </div>
                                            </div>
                                        )}

                                        {/* VIDEO GENERATION */}
                                        {mode === 'video_generation' && (
                                            <div className="space-y-3 border border-default rounded-lg p-4">
                                                <p className="text-sm font-semibold text-secondary">
                                                    Video generation
                                                </p>

                                                <div className="grid grid-cols-2 gap-3">
                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Cost per second (USD)
                                                        </label>
                                                        <input
                                                            type="number"
                                                            value={costPerVideoSecond}
                                                            onChange={(e) =>
                                                                setCostPerVideoSecond(e.target.value)
                                                            }
                                                            step="0.001"
                                                            min="0"
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="e.g. 0.07"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Luma Dream Machine ≈ 0.07/sec.
                                                        </p>
                                                    </div>

                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Max duration (seconds)
                                                        </label>
                                                        <input
                                                            type="number"
                                                            value={maxVideoDurationSeconds}
                                                            onChange={(e) =>
                                                                setMaxVideoDurationSeconds(e.target.value)
                                                            }
                                                            min="1"
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="e.g. 10"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Hard ceiling per clip. Luma = 10s, Runway = 16s.
                                                        </p>
                                                    </div>
                                                </div>

                                                <div className="grid grid-cols-2 gap-3">
                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Supported resolutions
                                                        </label>
                                                        <input
                                                            type="text"
                                                            value={supportedVideoResolutions}
                                                            onChange={(e) =>
                                                                setSupportedVideoResolutions(e.target.value)
                                                            }
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="720p, 1080p"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Comma-separated resolutions or pixel pairs.
                                                        </p>
                                                    </div>

                                                    <div>
                                                        <label className="block text-sm font-medium text-secondary mb-2">
                                                            Supported durations (sec)
                                                        </label>
                                                        <input
                                                            type="text"
                                                            value={supportedVideoDurations}
                                                            onChange={(e) =>
                                                                setSupportedVideoDurations(e.target.value)
                                                            }
                                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                            placeholder="5, 10"
                                                        />
                                                        <p className="text-xs text-muted mt-1">
                                                            Comma-separated integer seconds.
                                                        </p>
                                                    </div>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* TAB 2: API CONFIGURATION */}
                                {activeTab === 'api' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Auto-discovery is recommended</strong> for standard providers. The gateway automatically determines the correct API endpoint.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('api')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        <div>
                                            <label className="flex items-start space-x-3 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={autoDiscoverUrl}
                                                    disabled={needsApiUrl}
                                                    onChange={(e) => setApiUrlAutoDiscovered(e.target.checked)}
                                                    className="mt-1 w-4 h-4 text-primary-600 rounded"
                                                />
                                                <div>
                                                <span className="text-sm font-medium text-default">
                                                    Auto-discover API URL
                                                </span>
                                                    <p className="text-xs text-secondary mt-1">
                                                        {needsApiUrl
                                                            ? `Not available for a ${mode} model of provider "${provider}": the gateway knows no address for it, so the model needs its own API URL.`
                                                            : destinationUnknown
                                                              ? `Uses the gateway's own address for provider "${provider}". The model catalog has no ${mode} entry of this provider, so this form cannot tell whether the gateway has an address for it: if it has none, the save is refused and the API URL is asked for here.`
                                                              : 'Automatically use the correct API endpoint for this provider'}
                                                    </p>
                                                </div>
                                            </label>
                                        </div>

                                        {!autoDiscoverUrl && (
                                            <div>
                                                <label htmlFor="apiUrl" className="block text-sm font-medium text-secondary mb-2">
                                                    API URL *
                                                </label>
                                                <input
                                                    id="apiUrl"
                                                    type="url"
                                                    value={apiUrl}
                                                    onChange={(e) => handleFieldChange('apiUrl', e.target.value, setApiUrl)}
                                                    required
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                    placeholder={
                                                        provider?.includes('azure') ? 'https://your-resource.openai.azure.com' :
                                                        provider === 'bedrock' || provider === 'bedrock_converse' ? 'https://bedrock-runtime.<your AWS region>.amazonaws.com' :
                                                        provider === 'vertex_ai' ? 'https://<your location>-aiplatform.googleapis.com' :
                                                        provider === 'sagemaker' ? 'https://runtime.sagemaker.<your AWS region>.amazonaws.com' :
                                                        provider === 'ollama' ? 'http://host.docker.internal:11434' :
                                                        provider === 'vllm' ? 'http://host.docker.internal:8000' :
                                                        provider === 'lm_studio' ? 'http://host.docker.internal:1234' :
                                                        'https://api.provider.com/v1'
                                                    }
                                                />
                                                <FieldError error={errors.apiUrl} />
                                                {apiUrlLocalHint && (
                                                    <p data-testid="local-address-hint" className="mt-1 text-xs text-amber-700">{apiUrlLocalHint}</p>
                                                )}
                                            </div>
                                        )}

                                        {/* An Amazon Bedrock model and a Vertex AI model have
                                            one way to authenticate each, so there is nothing
                                            to choose. */}
                                        {!isBedrock && !isVertex && (
                                        <div>
                                            <label htmlFor="authType" className="block text-sm font-medium text-secondary mb-2">
                                                Authentication Type
                                            </label>
                                            <select
                                                id="authType"
                                                value={authType}
                                                onChange={(e) => setAuthType(e.target.value)}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                            >
                                                <option value="api_key">API Key</option>
                                                <option value="oauth">OAuth</option>
                                                {/* Offered to no model; shown only so a model
                                                    stored with it is not shown as something else. */}
                                                {authType === 'aws_iam' && <option value="aws_iam">AWS IAM</option>}
                                                {authType === 'azure_ad' && <option value="azure_ad">Azure AD</option>}
                                                {authType === 'gcp_service_account' && <option value="gcp_service_account">GCP Service Account</option>}
                                                <option value="none">None</option>
                                            </select>
                                        </div>
                                        )}

                                        {effectiveAuthType === 'api_key' && (
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2 flex items-center">
                                                    <Key className="w-4 h-4 mr-1" />
                                                    API Key
                                                </label>
                                                <input
                                                    type="password"
                                                    value={apiKey}
                                                    onChange={(e) => setApiKey(e.target.value)}
                                                    maxLength={500}
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                    placeholder="sk-..."
                                                />
                                            </div>
                                        )}

                                        {/* Amazon Bedrock: the model's own AWS credential.
                                            Write-only, like the API key: never filled in from
                                            the server, sent only when typed. */}
                                        {isBedrock && (
                                            <div className="space-y-3" data-testid="bedrock-credential">
                                                <div className="p-3 bg-sky-500/10 border border-sky-500/20 rounded-lg text-xs text-secondary">
                                                    <strong>Amazon Bedrock</strong> — the gateway signs every request for this model with the AWS credential entered here, and uses no credential of its own environment. It is stored encrypted and is never shown again. The IAM user or role needs <code>bedrock:InvokeModel</code> (and <code>bedrock:InvokeModelWithResponseStream</code> for streaming).
                                                </div>
                                                {awsCredentialStored ? (
                                                    <p className="text-xs text-secondary">
                                                        An AWS credential is stored for this model. Leave a field empty to keep what is stored: only what you change is saved.
                                                    </p>
                                                ) : model ? (
                                                    <p className="text-xs text-amber-700">
                                                        No AWS credential is stored for this model. It can be saved disabled without one; to enable it, enter the credential and the region.
                                                    </p>
                                                ) : null}
                                                <div>
                                                    <label htmlFor="awsAccessKeyId" className="block text-sm font-medium text-secondary mb-1">AWS access key ID{!awsStored.accessKeyId && ' *'}</label>
                                                    {model && <StoredMark testId="aws-stored-accessKeyId" stored={awsStored.accessKeyId} />}
                                                    <input id="awsAccessKeyId" type="password" autoComplete="off" value={awsCredential.accessKeyId} onChange={(e) => setAwsField('accessKeyId', e.target.value)} className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm" placeholder={awsStored.accessKeyId ? 'Stored. Leave empty to keep it.' : 'AKIA...'} />
                                                    <FieldError error={errors.awsAccessKeyId} />
                                                </div>
                                                <div>
                                                    <label htmlFor="awsSecretAccessKey" className="block text-sm font-medium text-secondary mb-1">AWS secret access key{!awsStored.secretAccessKey && ' *'}</label>
                                                    {model && <StoredMark testId="aws-stored-secretAccessKey" stored={awsStored.secretAccessKey} />}
                                                    <input id="awsSecretAccessKey" type="password" autoComplete="new-password" value={awsCredential.secretAccessKey} onChange={(e) => setAwsField('secretAccessKey', e.target.value)} className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm" placeholder={awsStored.secretAccessKey ? 'Stored. Leave empty to keep it.' : ''} />
                                                    <FieldError error={errors.awsSecretAccessKey} />
                                                </div>
                                                <div>
                                                    <label htmlFor="awsSessionToken" className="block text-sm font-medium text-secondary mb-1">AWS session token <span className="text-muted font-normal">(optional, for temporary credentials)</span></label>
                                                    {model && <StoredMark testId="aws-stored-sessionToken" stored={awsStored.sessionToken} />}
                                                    <input id="awsSessionToken" type="password" autoComplete="new-password" value={awsCredential.sessionToken} onChange={(e) => setAwsField('sessionToken', e.target.value)} className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm" placeholder={awsStored.sessionToken ? 'Stored. Leave empty to keep it.' : ''} />
                                                    {awsStored.sessionToken && (
                                                        <label className="mt-1 inline-flex items-center gap-2 text-xs text-secondary">
                                                            <input type="checkbox" checked={removeAwsSessionToken} onChange={(e) => setRemoveAwsSessionToken(e.target.checked)} className="w-4 h-4 rounded" />
                                                            Remove the stored session token (the access key is a long-lived one)
                                                        </label>
                                                    )}
                                                </div>
                                                <div>
                                                    <label htmlFor="awsRegion" className="block text-sm font-medium text-secondary mb-1">AWS region{enabled && ' *'}</label>
                                                    <input id="awsRegion" type="text" autoComplete="off" value={awsCredential.region} onChange={(e) => setAwsField('region', e.target.value)} className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm" />
                                                    <p className="mt-1 text-xs text-muted">The AWS region the model is used in. The gateway has no default region.</p>
                                                    <FieldError error={errors.awsRegion} />
                                                </div>
                                            </div>
                                        )}

                                        {/* A model of another provider stored with AWS IAM:
                                            the gateway has no signed AWS request for it. */}
                                        {!isBedrock && authType === 'aws_iam' && (
                                            <div className="p-3 bg-amber-500/10 border border-amber-500/20 rounded-lg text-xs text-secondary">
                                                The gateway signs requests with an AWS credential for Amazon Bedrock only. Choose another authentication type for provider &quot;{provider}&quot;.
                                            </div>
                                        )}

                                        {/* A model stored with Azure AD: the gateway has no
                                            sign-in with client credentials. */}
                                        {effectiveAuthType === 'azure_ad' && (
                                            <div className="p-3 bg-amber-500/10 border border-amber-500/20 rounded-lg text-xs text-secondary">
                                                The gateway does not sign in to Azure with client credentials (Azure AD). Choose API Key and enter the API key of your Azure OpenAI resource.
                                            </div>
                                        )}

                                        {/* A model of another provider stored with a GCP
                                            service account: the gateway uses one for Vertex AI only. */}
                                        {!isVertex && authType === 'gcp_service_account' && (
                                            <div className="p-3 bg-amber-500/10 border border-amber-500/20 rounded-lg text-xs text-secondary">
                                                The gateway signs in with a Google Cloud service account for Vertex AI only. Choose another authentication type for provider &quot;{provider}&quot;.
                                            </div>
                                        )}

                                        {/* Vertex AI: the model's own service account, in its
                                            own project. Write-only: never filled in from the
                                            server, sent only when typed. */}
                                        {isVertex && (
                                            <div className="space-y-3" data-testid="vertex-credential">
                                                <div className="p-3 bg-sky-500/10 border border-sky-500/20 rounded-lg text-xs text-secondary">
                                                    <strong>Vertex AI</strong> — the gateway calls this model with the service account entered here, in the project entered here, and uses no Google identity of its own. The key is stored encrypted and is never shown again. It is the JSON file from the Google Cloud console (IAM &amp; Admin → Service Accounts → Keys); the service account needs the <code>Vertex AI User</code> role.
                                                </div>
                                                {gcpCredentialStored ? (
                                                    <p className="text-xs text-secondary">
                                                        A service account key is stored for this model. Leave the key field empty to keep it: only what you change is saved.
                                                    </p>
                                                ) : model ? (
                                                    <p className="text-xs text-amber-700">
                                                        No service account key is stored for this model. It can be saved disabled without one; to enable it, enter the key, the project and the location.
                                                    </p>
                                                ) : null}
                                                <div>
                                                    <label htmlFor="gcpProjectId" className="block text-sm font-medium text-secondary mb-1">GCP project ID{enabled && ' *'}</label>
                                                    <input id="gcpProjectId" type="text" autoComplete="off" value={gcpCredential.projectId} onChange={(e) => setGcpField('projectId', e.target.value)} className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 text-sm" />
                                                    <p className="mt-1 text-xs text-muted">The Google Cloud project the model is used in. The gateway has no default project.</p>
                                                    <FieldError error={errors.gcpProjectId} />
                                                </div>
                                                <div>
                                                    <label htmlFor="gcpLocation" className="block text-sm font-medium text-secondary mb-1">GCP location{enabled && ' *'}</label>
                                                    <input id="gcpLocation" type="text" autoComplete="off" value={gcpCredential.location} onChange={(e) => setGcpField('location', e.target.value)} className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 text-sm" />
                                                    <p className="mt-1 text-xs text-muted">A region such as europe-west4, or global, us or eu. The gateway has no default location.</p>
                                                    <FieldError error={errors.gcpLocation} />
                                                </div>
                                                <div>
                                                    <label htmlFor="gcpCredentialsJson" className="block text-sm font-medium text-secondary mb-1">Service account JSON key{enabled && !gcpCredentialStored && ' *'}</label>
                                                    {model && <StoredMark testId="gcp-stored-credentialsJson" stored={gcpCredentialStored} />}
                                                    <textarea id="gcpCredentialsJson" autoComplete="off" spellCheck={false} value={gcpCredential.credentialsJson} onChange={(e) => setGcpField('credentialsJson', e.target.value)} rows={6} className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-xs" placeholder={gcpCredentialStored ? 'Stored. Leave empty to keep it.' : '{"type": "service_account", "project_id": "...", ...}'} />
                                                    <FieldError error={errors.gcpCredentialsJson} />
                                                </div>
                                            </div>
                                        )}

                                        {effectiveAuthType === 'none' && (
                                            <div className="p-3 bg-sky-500/10 border border-sky-500/20 rounded-lg text-xs text-secondary">
                                                <strong>No authentication</strong> — for a model server you run yourself (Ollama, Docker Model Runner, vLLM, LM Studio). The gateway sends requests without a credential. The address must be one the gateway can reach: &quot;localhost&quot; is the gateway&apos;s own machine.
                                            </div>
                                        )}

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                API Version (optional)
                                            </label>
                                            <input
                                                type="text"
                                                value={apiVersion}
                                                onChange={(e) => setApiVersion(e.target.value)}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                placeholder="e.g., 2024-02-15-preview"
                                            />
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Extra Headers to Send With Requests (optional)
                                            </label>
                                            <textarea
                                                value={extraHeaders}
                                                onChange={(e) => setExtraHeaders(e.target.value)}
                                                rows={3}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                placeholder={'{\n  "X-Custom-Header": "value"\n}'}
                                            />
                                            <p className="text-xs text-muted mt-1">JSON object of header name/value pairs sent with every API request</p>
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Timeout (seconds)
                                            </label>
                                            <input
                                                type="number"
                                                value={timeout}
                                                onChange={(e) => setTimeout(e.target.value)}
                                                min="1"
                                                required
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                            />
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Max Concurrent Requests
                                            </label>
                                            <input
                                                type="number"
                                                value={maxConcurrentRequests}
                                                onChange={(e) => setMaxConcurrentRequests(e.target.value)}
                                                min="1"
                                                max="1024"
                                                placeholder="Unbounded"
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                            />
                                            <p className="text-xs text-muted mt-1">
                                                Cap on simultaneous in-flight requests to this model, enforced by a per-model semaphore in the proxy. Leave empty for unbounded — prefer the rate-limit rpm/tpm knobs for quota control.
                                            </p>
                                        </div>

                                        {/* Rate limits — RPM / TPM. Empty = unbounded (NULL). */}
                                        <p className="text-xs text-muted" data-testid="model-rate-limit-help">
                                            The gateway applies both limits to every call this model takes: a direct call, a call through a
                                            routing group, and a call that falls back to it. A call above a limit is refused. Empty: no limit.
                                        </p>
                                        <div className="grid grid-cols-2 gap-4">
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Rate limit (req/min)
                                                </label>
                                                <input
                                                    type="number"
                                                    value={rateLimitRpm}
                                                    onChange={(e) => setRateLimitRpm(e.target.value)}
                                                    min="1"
                                                    placeholder="Unbounded"
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    Requests-per-minute ceiling for this model. Leave empty for unbounded.
                                                </p>
                                            </div>
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Rate limit (tokens/min)
                                                </label>
                                                <input
                                                    type="number"
                                                    value={rateLimitTpm}
                                                    onChange={(e) => setRateLimitTpm(e.target.value)}
                                                    min="1"
                                                    placeholder="Unbounded"
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    Tokens-per-minute ceiling for this model. Leave empty for unbounded.
                                                </p>
                                            </div>
                                        </div>

                                        {/* End-user tracking for provider-side abuse monitoring. */}
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                User Identifier (abuse monitoring)
                                            </label>
                                            <input
                                                type="text"
                                                value={userIdentifier}
                                                onChange={(e) => setUserIdentifier(e.target.value)}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                placeholder="e.g., tenant-prod"
                                            />
                                            <p className="text-xs text-muted mt-1">
                                                Forwarded to the provider as the end-user identifier for abuse monitoring and rate-limit attribution.
                                            </p>
                                        </div>

                                        {/* Arbitrary metadata logged with each request. JSON object. */}
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Model Metadata (JSON)
                                            </label>
                                            <textarea
                                                value={modelMetadata}
                                                onChange={(e) => setModelMetadata(e.target.value)}
                                                rows={3}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                placeholder={'{\n  "team": "platform"\n}'}
                                            />
                                            <p className="text-xs text-muted mt-1">JSON object of custom metadata attached to logs for this model.</p>
                                        </div>
                                    </div>
                                )}

                                {/* TAB 3: GENERATION */}
                                {activeTab === 'generation' && (
                                    <div className="space-y-4">
                                        {/* Per-call parameter precedence — explains how the
                                            values on this tab interact with what the API
                                            client / agent actually sends in the request body.
                                            See gateway-core: services/llm_governance.rs
                                            (RG-cap clamping) + services/providers.rs
                                            (model-default backfill). */}
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3 dark:bg-blue-950/40 dark:border-blue-900">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5 dark:text-blue-300" />
                                            <div className="flex-1 space-y-2 text-sm text-blue-900 dark:text-blue-200 min-h-0 min-w-0">
                                                <p>
                                                    <strong>These are model-level defaults — caller values take precedence.</strong>{' '}
                                                    Lower temperature = more focused, higher = more creative; tune Max Tokens to the longest reply
                                                    you want the model to be allowed to produce.
                                                </p>
                                                <p>
                                                    <strong>Per-call precedence at the proxy:</strong>
                                                </p>
                                                <ol className="list-decimal ml-5 space-y-0.5">
                                                    <li>
                                                        If the API caller (chat client, agent, your code) sends an explicit{' '}
                                                        <code className="font-mono text-[11px] bg-blue-100 dark:bg-blue-900 px-1 rounded">temperature</code> /{' '}
                                                        <code className="font-mono text-[11px] bg-blue-100 dark:bg-blue-900 px-1 rounded">max_tokens</code>{' '}
                                                        in the request body, the proxy uses that — the value on this form is ignored for that call.
                                                    </li>
                                                    <li>
                                                        If the caller omits the field, the gateway fills it in from the defaults below before forwarding upstream.
                                                    </li>
                                                    <li>
                                                        Resource-group governance caps (Governance → Usage Limits) clamp the caller value when it's above the cap — either silently
                                                        (default <code className="font-mono text-[11px] bg-blue-100 dark:bg-blue-900 px-1 rounded">enforce=clamp</code>)
                                                        or with a 403 (<code className="font-mono text-[11px] bg-blue-100 dark:bg-blue-900 px-1 rounded">enforce=reject</code>).
                                                        Caps are the only knob that can override caller intent.
                                                    </li>
                                                </ol>
                                                <p className="text-xs text-blue-800/80 dark:text-blue-300/80">
                                                    Agent-builders: send whatever values your workflow needs — your code is in charge. Use this form to set
                                                    sane defaults for callers (chat UIs, ad-hoc scripts) that don't set parameters themselves.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('generation')}
                                                className="text-blue-600 hover:text-blue-700 dark:text-blue-300 dark:hover:text-blue-200"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        {/* Thinking / reasoning mode — provider-specific knob
                                            translated by the proxy adapter at request time.
                                            When on, Anthropic forces temperature=1.0 and OpenAI
                                            o-series rejects temperature outright, so we mirror
                                            that constraint in the UI by disabling the
                                            temperature input below. */}
                                        <div className="bg-surface border border-strong rounded-lg p-4">
                                            <label className="flex items-start space-x-3 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={thinkingEnabled}
                                                    onChange={(e) => setThinkingEnabled(e.target.checked)}
                                                    className="mt-0.5 h-4 w-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                                                />
                                                <div className="flex-1 min-h-0 min-w-0">
                                                    <span className="text-sm font-medium text-default">
                                                        Enable extended thinking / reasoning
                                                    </span>
                                                    <p className="text-xs text-muted mt-1">
                                                        Generic switch translated per-provider by the proxy:
                                                        Anthropic injects <code className="font-mono text-[11px]">thinking</code>{' '}
                                                        and forces temperature to 1.0; OpenAI o-series sets{' '}
                                                        <code className="font-mono text-[11px]">reasoning_effort</code> and strips temperature/top_p;
                                                        Gemini 2.5 adds <code className="font-mono text-[11px]">thinking_config</code>.
                                                        Only meaningful for reasoning-capable models — flipping it on for a
                                                        plain chat model is a no-op.
                                                    </p>
                                                    {thinkingEnabled && (
                                                        <p className="text-xs text-amber-700 mt-2 font-medium">
                                                            Temperature input is disabled — the proxy will coerce the
                                                            request to provider-specific defaults at dispatch time.
                                                        </p>
                                                    )}
                                                </div>
                                            </label>
                                        </div>

                                        <div className="grid grid-cols-2 gap-4">
                                            <ValidatedInput
                                                id="temperature"
                                                label="Default temperature"
                                                type="number"
                                                value={temperature}
                                                onChange={(value) => handleFieldChange('temperature', value, setTemperature)}
                                                onBlur={() => handleFieldBlur('temperature', temperature)}
                                                error={touched.has('temperature') ? errors.temperature : undefined}
                                                showSuccess={touched.has('temperature') && !errors.temperature}
                                                required
                                                step="0.1"
                                                min="0"
                                                max="2"
                                                disabled={thinkingEnabled}
                                                helpText={
                                                    thinkingEnabled
                                                        ? 'Locked while thinking is enabled — proxy applies provider defaults.'
                                                        : 'Applied only when the caller omits `temperature`. Range 0–2; lower = more focused.'
                                                }
                                            />

                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Default max tokens *
                                                </label>
                                                <input
                                                    type="number"
                                                    value={maxTokens}
                                                    onChange={(e) => setMaxTokens(e.target.value)}
                                                    min="1"
                                                    required
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    Applied only when the caller omits <code className="font-mono text-[11px]">max_tokens</code>. Resource-group caps still clamp caller-supplied values above the limit.
                                                </p>
                                            </div>
                                        </div>

                                        <div className="grid grid-cols-2 gap-4">
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Top P (0.0 - 1.0)
                                                </label>
                                                <input
                                                    type="number"
                                                    value={topP || ''}
                                                    onChange={(e) => setTopP(e.target.value ? parseFloat(e.target.value) : undefined)}
                                                    step="0.01"
                                                    min="0"
                                                    max="1"
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    placeholder="Optional"
                                                />
                                            </div>

                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    N (completions)
                                                </label>
                                                <input
                                                    type="number"
                                                    value={n}
                                                    onChange={(e) => setN(parseInt(e.target.value) || 1)}
                                                    min="1"
                                                    max="10"
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                />
                                            </div>
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Stop Sequences (comma-separated)
                                            </label>
                                            <input
                                                type="text"
                                                value={stopSequences.join(', ')}
                                                onChange={(e) => setStopSequences(e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                placeholder="e.g., ###, END"
                                            />
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Seed (for reproducibility)
                                            </label>
                                            <input
                                                type="number"
                                                value={seed || ''}
                                                onChange={(e) => setSeed(e.target.value ? parseInt(e.target.value) : undefined)}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                placeholder="Optional"
                                            />
                                        </div>
                                    </div>
                                )}

                                {/* TAB 4: TUNING */}
                                {activeTab === 'tuning' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Advanced settings</strong> for fine-tuning model behavior. Most use cases work well with defaults (all at 0).
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('tuning')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        <div className="grid grid-cols-2 gap-4">
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Presence Penalty (-2.0 to 2.0)
                                                </label>
                                                <input
                                                    type="number"
                                                    value={presencePenalty || ''}
                                                    onChange={(e) => setPresencePenalty(e.target.value ? parseFloat(e.target.value) : undefined)}
                                                    step="0.1"
                                                    min="-2"
                                                    max="2"
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    placeholder="0"
                                                />
                                            </div>

                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Frequency Penalty (-2.0 to 2.0)
                                                </label>
                                                <input
                                                    type="number"
                                                    value={frequencyPenalty || ''}
                                                    onChange={(e) => setFrequencyPenalty(e.target.value ? parseFloat(e.target.value) : undefined)}
                                                    step="0.1"
                                                    min="-2"
                                                    max="2"
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    placeholder="0"
                                                />
                                            </div>
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Logit Bias (JSON)
                                            </label>
                                            <textarea
                                                value={logitBias}
                                                onChange={(e) => setLogitBias(e.target.value)}
                                                rows={6}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                placeholder='{"50256": -100}'
                                            />
                                        </div>
                                    </div>
                                )}

                                {/* TAB 5: OUTPUT */}
                                {activeTab === 'output' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Use JSON mode</strong> for structured data extraction and programmatic parsing of responses.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('output')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Response Format (JSON)
                                            </label>
                                            <textarea
                                                value={responseFormat}
                                                onChange={(e) => setResponseFormat(e.target.value)}
                                                rows={4}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                placeholder='{"type": "json_object"}'
                                            />
                                        </div>

                                        <div>
                                            <label className="flex items-center space-x-2">
                                                <input
                                                    type="checkbox"
                                                    checked={logprobs}
                                                    onChange={(e) => setLogprobs(e.target.checked)}
                                                    className="w-4 h-4 text-primary-600 rounded"
                                                />
                                                <span className="text-sm font-medium text-secondary">Return log probabilities</span>
                                            </label>
                                        </div>

                                        {logprobs && (
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-2">
                                                    Top Logprobs (0-5)
                                                </label>
                                                <input
                                                    type="number"
                                                    value={topLogprobs || ''}
                                                    onChange={(e) => setTopLogprobs(e.target.value ? parseInt(e.target.value) : undefined)}
                                                    min="0"
                                                    max="5"
                                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                />
                                            </div>
                                        )}

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Stream Options (JSON)
                                            </label>
                                            <textarea
                                                value={streamOptions}
                                                onChange={(e) => setStreamOptions(e.target.value)}
                                                rows={4}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                placeholder='{"include_usage": true}'
                                            />
                                        </div>
                                    </div>
                                )}

                                {/* TAB 6: TOOLS */}
                                {activeTab === 'tools' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Function calling must be enabled</strong> in Basic Info tab for MCP tool integration.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('tools')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        {!supportsFunctionCalling && (
                                            <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
                                                <p className="text-sm text-yellow-900">
                                                    ⚠️ Function calling is disabled. Enable it in the Basic Info tab to use tools.
                                                </p>
                                            </div>
                                        )}

                                        <div>
                                            <label className="flex items-center space-x-2">
                                                <input
                                                    type="checkbox"
                                                    checked={parallelToolCalls}
                                                    onChange={(e) => setParallelToolCalls(e.target.checked)}
                                                    disabled={!supportsFunctionCalling}
                                                    className="w-4 h-4 text-primary-600 rounded"
                                                />
                                                <span className="text-sm font-medium text-secondary">Allow parallel tool calls</span>
                                            </label>
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Tool Choice Strategy
                                            </label>
                                            <select
                                                value={toolChoice}
                                                onChange={(e) => setToolChoice(e.target.value)}
                                                disabled={!supportsFunctionCalling}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                            >
                                                <option value="auto">Auto (recommended)</option>
                                                <option value="none">None</option>
                                                <option value="required">Required</option>
                                            </select>
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Predefined Tools (JSON)
                                            </label>
                                            <textarea
                                                value={tools}
                                                onChange={(e) => setTools(e.target.value)}
                                                rows={12}
                                                disabled={!supportsFunctionCalling}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                placeholder='[{"type": "function", "function": {...}}]'
                                            />
                                        </div>
                                    </div>
                                )}

                                {/* TAB 7: PARAM MANAGEMENT */}
                                {activeTab === 'params' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Control which parameters are passed to the LLM</strong> - some models don't support all OpenAI parameters.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('params')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        {/* Drop Params Toggle */}
                                        <div>
                                            <label className="flex items-center space-x-2">
                                                <input
                                                    type="checkbox"
                                                    checked={dropParams}
                                                    onChange={(e) => setDropParams(e.target.checked)}
                                                    className="w-4 h-4 text-primary-600 rounded"
                                                />
                                                <span className="text-sm font-medium text-secondary">Apply the provider&apos;s dropped-parameters list</span>
                                            </label>
                                            <p className="text-xs text-muted mt-1 ml-6" data-testid="drop-params-help">
                                                On: the gateway removes the parameters on the provider&apos;s list from every request to this model.
                                                Off: the provider&apos;s list is not applied to this model. The model&apos;s own list below always
                                                applies, whatever this switch says.
                                            </p>
                                        </div>

                                        {/* Additional Drop Params */}
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                This model's own dropped parameters
                                            </label>
                                            {/* Surface the provider-wide drop list the
                                             *  proxy will inherit at runtime, so operators editing
                                             *  this model see exactly what's already being stripped
                                             *  upstream of their own additions. Read-only here —
                                             *  edited on the provider admin page. */}
                                            {(() => {
                                                const inherited = fetchedProviders
                                                    .find(p => p.key === provider)
                                                    ?.additional_drop_params ?? [];
                                                if (inherited.length === 0) return null;
                                                return (
                                                    <div className="mb-2 p-3 bg-surface-alt border border-default rounded-lg">
                                                        <p className="text-xs font-medium text-secondary mb-1.5">
                                                            The list of provider <code className="bg-surface-raised px-1 rounded">{provider}</code>:
                                                        </p>
                                                        <div className="flex flex-wrap gap-1.5">
                                                            {inherited.map((param) => (
                                                                <span
                                                                    key={param}
                                                                    className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-surface-raised border border-default text-secondary"
                                                                >
                                                                    {param}
                                                                </span>
                                                            ))}
                                                        </div>
                                                        <p className="text-xs text-muted mt-1.5" data-testid="provider-drop-list-state">
                                                            {dropParams
                                                                ? 'The gateway removes these from every request to this model. Change the list on the provider.'
                                                                : 'The switch above is off, so these are not removed from requests to this model.'}
                                                        </p>
                                                    </div>
                                                );
                                            })()}
                                            <input
                                                type="text"
                                                value={additionalDropParams}
                                                onChange={(e) => setAdditionalDropParams(e.target.value)}
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                                                placeholder="temperature, top_p, presence_penalty, frequency_penalty"
                                            />
                                            <p className="text-xs text-muted mt-1">
                                                Parameters the gateway removes from every request to this model, comma-separated. The list
                                                always applies. Supports nested fields like{' '}
                                                <code className="bg-surface-raised px-1 rounded">tools[*].input_examples</code>.
                                            </p>
                                        </div>

                                        {/* Examples */}
                                        <div className="bg-surface-alt border border-default rounded-lg p-4">
                                            <p className="text-sm font-medium text-secondary mb-2">Common Examples:</p>
                                            <div className="space-y-2 text-sm text-secondary">
                                                <div>
                                                    <strong className="text-secondary">OpenAI O1/O3 models:</strong>
                                                    <code className="block mt-1 text-xs bg-surface p-2 rounded border border-default">
                                                        temperature, top_p, presence_penalty, frequency_penalty, logit_bias, logprobs, top_logprobs, n, parallel_tool_calls
                                                    </code>
                                                </div>
                                                <div>
                                                    <strong className="text-secondary">OpenAI GPT-5 models:</strong>
                                                    <code className="block mt-1 text-xs bg-surface p-2 rounded border border-default">
                                                        top_p, presence_penalty, frequency_penalty
                                                    </code>
                                                </div>
                                                <div>
                                                    <strong className="text-secondary">Ollama local models:</strong>
                                                    <code className="block mt-1 text-xs bg-surface p-2 rounded border border-default">
                                                        logprobs, top_logprobs, logit_bias, response_format, parallel_tool_calls
                                                    </code>
                                                </div>
                                                <div>
                                                    <strong className="text-secondary">Nested field example (Claude Code + Bedrock):</strong>
                                                    <code className="block mt-1 text-xs bg-surface p-2 rounded border border-default">
                                                        tools[*].input_examples
                                                    </code>
                                                    <p className="text-xs text-muted mt-1">
                                                        Removes input_examples from all tools. Click help (?) for more nested field examples.
                                                    </p>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                )}

                                {/* TAB 7b: FALLBACKS — model→model failover chain (all modes) */}
                                {activeTab === 'fallbacks' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3 dark:bg-blue-950/40 dark:border-blue-900">
                                            <ShieldCheck className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
                                            <div className="text-sm text-blue-800 dark:text-blue-200">
                                                <p className="font-medium mb-1">Model-level failover</p>
                                                <p>If a call to <strong>this model</strong> fails with a retryable error (rate&nbsp;limit, timeout, 5xx, connection error), the gateway automatically retries the models below, <strong>in order</strong>, until one succeeds — the caller never sees the outage. Only models of the same mode (<code>{mode}</code>) are eligible, and both legs are audited.</p>
                                            </div>
                                        </div>
                                        <ReadFailureNotice compact failure={readFailure('models')} onRetry={askAgain} />
                                        <div className="border border-default rounded-lg p-4 space-y-3" data-testid="model-circuit-breaker">
                                            <div>
                                                <p className="text-sm font-medium text-default">Circuit breaker</p>
                                                <p className="text-xs text-muted mt-1">
                                                    After this many consecutive failed calls the gateway stops sending to the model and answers at once
                                                    with a Retry-After, for the seconds below; it then lets one call through and closes on its success.
                                                    Set both, or leave both empty for the gateway&apos;s defaults (5 failures, 30 seconds).
                                                </p>
                                            </div>
                                            <div className="grid grid-cols-2 gap-4">
                                                <div>
                                                    <label htmlFor="allowed_fails" className="block text-sm font-medium text-secondary mb-2">
                                                        Consecutive failures before it opens
                                                    </label>
                                                    <input
                                                        id="allowed_fails"
                                                        type="number"
                                                        min="1"
                                                        max="100"
                                                        value={allowedFails}
                                                        onChange={(e) => setAllowedFails(e.target.value)}
                                                        placeholder="Not set"
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    />
                                                </div>
                                                <div>
                                                    <label htmlFor="breaker_cooldown_time" className="block text-sm font-medium text-secondary mb-2">
                                                        Seconds it stays open
                                                    </label>
                                                    <input
                                                        id="breaker_cooldown_time"
                                                        type="number"
                                                        min="1"
                                                        max="3600"
                                                        value={breakerCooldown}
                                                        onChange={(e) => setBreakerCooldown(e.target.value)}
                                                        placeholder="Not set"
                                                        className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                                    />
                                                </div>
                                            </div>
                                            {errors.circuitBreaker && <p className="text-sm text-red-600">{errors.circuitBreaker}</p>}
                                        </div>
                                        <FallbackModelPicker
                                            label="Fallback models (in priority order)"
                                            hint="Tried top-to-bottom when this model is unavailable. This is the industry-standard model→model failover chain — independent of routing-group load-balancing."
                                            selected={fallbackModels}
                                            onChange={setFallbackModels}
                                            options={allModels
                                                .filter(m => m.id !== model?.id && (m.mode || 'chat') === mode)
                                                .map(m => ({ id: m.id, name: m.name }))}
                                        />
                                    </div>
                                )}

                                {/* TAB 8: ROUTING */}
                                {activeTab === 'routing' && (
                                    <div className="space-y-4">
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3 dark:bg-blue-950/40 dark:border-blue-900">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5 dark:text-blue-300" />
                                            <div className="flex-1 text-sm text-blue-900 dark:text-blue-200 min-h-0 min-w-0">
                                                <p>
                                                    <strong>Assign this model to a routing group</strong> to make it
                                                    one of several upstream targets the router can pick from. The
                                                    routing group's <strong>Routing Strategy</strong> (Weighted
                                                    Shuffle, Queue-Aware, Rate-Limit Optimized, Fastest Response,
                                                    Cheapest Healthy) decides <em>how</em> the picker behaves. The
                                                    <strong> Routing Weight</strong> below is only consulted for
                                                    selection by Weighted Shuffle — see the inline note on that
                                                    field for which strategies fire on it.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('routing')}
                                                className="text-blue-600 hover:text-blue-700 dark:text-blue-300 dark:hover:text-blue-200"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        {/* Routing Group Selection */}
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Routing Group (Optional)
                                            </label>
                                            {loadingRoutingGroups ? (
                                                <div className="w-full px-3 py-2 border border-strong rounded-lg bg-surface-alt text-muted">
                                                    Loading routing groups...
                                                </div>
                                            ) : readFailure('routingGroups') && routingGroups.length === 0 ? (
                                                <ReadFailureNotice compact failure={readFailure('routingGroups')} onRetry={askAgain} />
                                            ) : routingGroups.length === 0 ? (
                                                <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
                                                    <div className="flex items-start space-x-3">
                                                        <Info className="w-5 h-5 text-yellow-600 flex-shrink-0 mt-0.5" />
                                                        <div className="flex-1 text-sm min-h-0 min-w-0">
                                                            <p className="text-yellow-900 font-medium mb-2">
                                                                No routing groups available
                                                            </p>
                                                            <p className="text-yellow-800 mb-3">
                                                                Routing groups enable load balancing and intelligent traffic distribution across multiple models.
                                                            </p>
                                                            <p className="text-yellow-800">
                                                                To assign this model to a routing group:
                                                            </p>
                                                            <ol className="list-decimal list-inside text-yellow-800 mt-2 space-y-1">
                                                                <li>Navigate to <strong>AI Models | Routing</strong> page</li>
                                                                <li>Click <strong>Create Routing Group</strong></li>
                                                                <li>Return here to assign this model</li>
                                                            </ol>
                                                        </div>
                                                    </div>
                                                </div>
                                            ) : (() => {
                                                // Partition groups by mode-match: same-mode
                                                // groups are selectable; other-mode groups
                                                // are still rendered but disabled, so the
                                                // operator sees *why* a group they expected
                                                // to see is missing rather than just an
                                                // empty list. The server-side
                                                // `assert_model_mode_matches_group`
                                                // enforces the same rule (HTTP 400 on
                                                // mismatch); this is the proactive UX
                                                // half of that contract.
                                                const matchingGroups = routingGroups.filter(
                                                    (g) => (g.mode || 'chat') === mode
                                                );
                                                const mismatchedGroups = routingGroups.filter(
                                                    (g) => (g.mode || 'chat') !== mode
                                                );
                                                const currentSelectionMismatched =
                                                    routingGroupId &&
                                                    routingGroups.some(
                                                        (g) => g.id === routingGroupId && (g.mode || 'chat') !== mode
                                                    );
                                                return (
                                                    <>
                                                        <select
                                                            value={routingGroupId}
                                                            onChange={(e) => setRoutingGroupId(e.target.value)}
                                                            className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary-500 ${
                                                                currentSelectionMismatched ? 'border-red-300' : 'border-strong'
                                                            }`}
                                                        >
                                                            <option value="">No routing group (standalone model)</option>
                                                            {matchingGroups.length > 0 && (
                                                                <optgroup label={`Same mode (${mode})`}>
                                                                    {matchingGroups.map((group) => (
                                                                        <option key={group.id} value={group.id}>
                                                                            {group.name} ({group.routing_strategy})
                                                                            {!group.enabled && ' — Disabled'}
                                                                        </option>
                                                                    ))}
                                                                </optgroup>
                                                            )}
                                                            {mismatchedGroups.length > 0 && (
                                                                <optgroup label="Different mode (not selectable)">
                                                                    {mismatchedGroups.map((group) => (
                                                                        <option
                                                                            key={group.id}
                                                                            value={group.id}
                                                                            disabled={group.id !== routingGroupId}
                                                                        >
                                                                            {group.name} ({group.mode || 'chat'} · {group.routing_strategy})
                                                                            {!group.enabled && ' — Disabled'}
                                                                        </option>
                                                                    ))}
                                                                </optgroup>
                                                            )}
                                                        </select>
                                                        {currentSelectionMismatched ? (
                                                            <p className="text-xs text-red-600 mt-1">
                                                                Current selection has a different mode than this model. Pick a same-mode group or change this model's mode on the General tab — the server will reject mismatched assignments.
                                                            </p>
                                                        ) : matchingGroups.length === 0 ? (
                                                            <p className="text-xs text-muted mt-1">
                                                                No routing groups exist with mode <code>{mode}</code>. Create one on the <strong>AI Models | Routing</strong> page, or change this model's mode on the General tab.
                                                            </p>
                                                        ) : (
                                                            <p className="text-xs text-muted mt-1">
                                                                Only routing groups whose mode matches this model's mode (<code>{mode}</code>) can be selected. Leave empty for standalone usage.
                                                            </p>
                                                        )}
                                                    </>
                                                );
                                            })()}
                                        </div>

                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-2">
                                                Routing Weight
                                            </label>
                                            <input
                                                type="number"
                                                value={routingWeight}
                                                onChange={(e) => setRoutingWeight(e.target.value)}
                                                step="1"
                                                min="0"
                                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                            />
                                            <div className="text-xs text-muted mt-1 space-y-1">
                                                <p>
                                                    Integer weight. Higher = more traffic. <strong>Only consumed for selection by the <code className="font-mono text-[11px]">Weighted Shuffle</code> strategy</strong>{' '}
                                                    (a.k.a. <code className="font-mono text-[11px]">simple-shuffle</code>) — other strategies
                                                    (<code className="font-mono text-[11px]">Queue-Aware</code>, <code className="font-mono text-[11px]">Rate-Limit Optimized</code>, <code className="font-mono text-[11px]">Fastest Response</code>, <code className="font-mono text-[11px]">Cheapest Healthy</code>)
                                                    pick by their own signal and only use weight as a tiebreaker / sort order.
                                                </p>
                                                <p>
                                                    Common patterns under Weighted Shuffle: <code className="font-mono text-[11px]">7</code> / <code className="font-mono text-[11px]">3</code> for a ~70/30 traffic split between healthy peers;{' '}
                                                    <code className="font-mono text-[11px]">1</code> / <code className="font-mono text-[11px]">0</code> for a primary-with-fallback pattern (the zero-weight peer only serves when the weight-1 peer is in cooldown).
                                                </p>
                                                <p>
                                                    Check the routing group's selected <strong>Routing Strategy</strong> to confirm this knob will fire on your traffic.
                                                </p>
                                            </div>
                                        </div>
                                    </div>
                                )}

                                {/* TAB 9: SYSTEM PROMPT */}
                                {activeTab === 'prompt' && (
                                    <div className="space-y-4">
                                        {/* Info Box */}
                                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start space-x-3">
                                            <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <p className="text-sm text-blue-900">
                                                    <strong>Configure a default system prompt</strong> that will be automatically prepended to all completion requests using this model. This prompt establishes the AI's role, behavior, and guidelines.
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => openHelp('prompt')}
                                                className="text-blue-600 hover:text-blue-700"
                                            >
                                                <HelpCircle className="w-5 h-5" />
                                            </button>
                                        </div>

                                        <ValidatedTextarea
                                            id="systemPrompt"
                                            label="System Prompt"
                                            value={systemPrompt}
                                            onChange={(value) => handleFieldChange('systemPrompt', value, setSystemPrompt)}
                                            onBlur={() => handleFieldBlur('systemPrompt', systemPrompt)}
                                            error={touched.has('systemPrompt') ? errors.systemPrompt : undefined}
                                            showSuccess={touched.has('systemPrompt') && !errors.systemPrompt}
                                            rows={20}
                                            maxLength={100000}
                                            showCharCount
                                            placeholder="You are a helpful AI assistant..."
                                            helpText="This prompt will be automatically added as the first system message for all completions. Leave empty to not use a system prompt."
                                        />

                                        {/* Examples */}
                                        <div className="border border-default rounded-lg p-4">
                                            <h4 className="text-sm font-semibold text-default mb-3 flex items-center">
                                                <Lightbulb className="w-4 h-4 mr-2 text-yellow-500" />
                                                Example System Prompts
                                            </h4>
                                            <div className="space-y-3">
                                                <div>
                                                    <p className="text-xs font-medium text-secondary mb-1">Customer Support:</p>
                                                    <code className="block text-xs bg-surface-alt p-2 rounded border border-default">
                                                        You are a friendly and professional customer support assistant for TechCorp. Always be polite, empathetic, and solution-focused. If you don't know the answer, offer to escalate to a human agent.
                                                    </code>
                                                </div>
                                                <div>
                                                    <p className="text-xs font-medium text-secondary mb-1">Code Assistant:</p>
                                                    <code className="block text-xs bg-surface-alt p-2 rounded border border-default">
                                                        You are an expert programming assistant. Provide clean, well-documented code with explanations. Follow best practices and industry standards. Always consider security and performance.
                                                    </code>
                                                </div>
                                                <div>
                                                    <p className="text-xs font-medium text-secondary mb-1">Content Writer:</p>
                                                    <code className="block text-xs bg-surface-alt p-2 rounded border border-default">
                                                        You are a professional content writer specializing in blog posts and articles. Write in a clear, engaging style appropriate for a general audience. Use active voice and concrete examples.
                                                    </code>
                                                </div>
                                            </div>
                                        </div>

                                        {/* Clear Button */}
                                        {systemPrompt && (
                                            <div className="flex justify-end">
                                                <button
                                                    type="button"
                                                    onClick={() => setSystemPrompt('')}
                                                    className="px-3 py-1.5 text-sm text-red-600 hover:text-red-700 hover:bg-red-50 rounded-lg transition-colors"
                                                >
                                                    Clear System Prompt
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* ====== LIMITS TAB (tenant-wide, mode-aware) ====== */}
                                {activeTab === 'limits' && (
                                    <div className="space-y-4">
                                        <InfoBanner title="Tenant-wide default limits">
                                            These usage limits apply to this model across the whole
                                            tenant. Each Resource Group can only tighten them — the
                                            stricter value wins (min-wins). The fields shown match
                                            this model&apos;s mode.
                                        </InfoBanner>
                                        {MEDIA_FORM_MODES.includes(mode) ? (
                                            <MediaLimitsForm
                                                mode={mode as MediaMode}
                                                value={tenantLimits}
                                                onChange={setTenantLimits}
                                            />
                                        ) : (
                                            <LLMLimitsForm
                                                limits={tenantLimits as LLMGlobalLimits}
                                                onChange={(l) => setTenantLimits(l)}
                                                mode="group"
                                            />
                                        )}
                                    </div>
                                )}

                                {/* ====== POLICIES TAB (tenant-wide, mode-aware) ====== */}
                                {activeTab === 'policies' && (
                                    <div className="space-y-4">
                                        <InfoBanner title="Tenant-wide default policies">
                                            Governance/policy defaults for this model across the
                                            tenant. Resource Groups can only tighten them (min-wins).
                                            The fields shown match this model&apos;s mode.
                                        </InfoBanner>
                                        {MEDIA_FORM_MODES.includes(mode) ? (
                                            <MediaGovernanceForm
                                                mode={mode as MediaMode}
                                                value={tenantGovernance}
                                                onChange={setTenantGovernance}
                                            />
                                        ) : (
                                            <LLMGovernanceForm
                                                value={tenantGovernance as LLMGovernance}
                                                onChange={(g) => setTenantGovernance(g)}
                                            />
                                        )}
                                    </div>
                                )}

                                {/* ====== RETIREMENT TAB ====== */}
                                {activeTab === 'retirement' && (
                                    <div className="space-y-4">
                                        {(() => {
                                            // Live status banner. Three states:
                                            //   1. No date set → empty-state info.
                                            //   2. Retire date is in the future →
                                            //      warning banner with countdown.
                                            //   3. Retire date is in the past →
                                            //      error banner ("model retired").
                                            if (!retireDate) {
                                                return (
                                                    <InfoBanner title="No retirement scheduled">
                                                        <p>
                                                            This model has no retire date. Set one when the provider
                                                            publishes an end-of-life: the platform then warns
                                                            administrators (and raises a system alert) ahead of it, and
                                                            from the retire date on the gateway does not serve the model.
                                                        </p>
                                                    </InfoBanner>
                                                );
                                            }
                                            // Both anchors must be in the same timezone or
                                            // the millisecond delta isn't a clean multiple of
                                            // a day. Mixing local-time `today` with
                                            // UTC-anchored `retire` produces an off-by-one
                                            // — `Math.ceil(-0.9167) === 0` reading
                                            // "retired 0 day(s) ago" for a yesterday date in
                                            // a non-UTC timezone. So: anchor both at UTC
                                            // midnight and use Math.round (the delta is
                                            // exactly N×dayMs in UTC, but FP can drift on
                                            // DST-adjacent dates — round, don't ceil).
                                            const nowLocal = new Date();
                                            const todayUtcMidnight = Date.UTC(
                                                nowLocal.getUTCFullYear(),
                                                nowLocal.getUTCMonth(),
                                                nowLocal.getUTCDate(),
                                            );
                                            const retireUtcMidnight = new Date(
                                                retireDate + 'T00:00:00Z'
                                            ).getTime();
                                            const dayMs = 24 * 60 * 60 * 1000;
                                            const diffDays = Math.round(
                                                (retireUtcMidnight - todayUtcMidnight) / dayMs
                                            );
                                            if (diffDays === 0) {
                                                // Retiring today is the boundary case.
                                                // Render as error (model is effectively
                                                // gone any moment now) but with a
                                                // less-doom-y title than "retired N days
                                                // ago" — operators reading at 09:00 today
                                                // shouldn't see the same banner they'd see
                                                // a week from now.
                                                return (
                                                    <InfoBanner
                                                        variant="error"
                                                        title="Model retired today"
                                                    >
                                                        <p data-testid="retirement-banner-text">
                                                            The retire date is <strong>today</strong>. From its retire
                                                            date on, the gateway does not serve a model: a call to it
                                                            is refused as a call to a disabled model is. Move users to
                                                            the replacement, or change the date.
                                                        </p>
                                                    </InfoBanner>
                                                );
                                            }
                                            if (diffDays < 0) {
                                                const daysAgo = -diffDays;
                                                return (
                                                    <InfoBanner
                                                        variant="error"
                                                        title={`Model retired ${daysAgo} day(s) ago`}
                                                    >
                                                        <p data-testid="retirement-banner-text">
                                                            The retire date has passed, so the gateway does not serve
                                                            this model: a call to it is refused as a call to a disabled
                                                            model is. Move users to the replacement, or change the date.
                                                        </p>
                                                    </InfoBanner>
                                                );
                                            }
                                            return (
                                                <InfoBanner
                                                    variant="warning"
                                                    title={`Retirement scheduled in ${diffDays} day(s)`}
                                                >
                                                    <p data-testid="retirement-banner-text">
                                                        Operator alerts will start firing on{' '}
                                                        <strong>
                                                            {retirementReminderDate || '(not set)'}
                                                        </strong>.
                                                        Configure a replacement and notify downstream users before
                                                        the retirement date. From the retire date on, the gateway
                                                        does not serve the model.
                                                    </p>
                                                </InfoBanner>
                                            );
                                        })()}

                                        <div className={`grid grid-cols-1 gap-4 ${isCatalog ? '' : 'md:grid-cols-3'}`}>
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-1">
                                                    Retire date
                                                </label>
                                                <input
                                                    type="date"
                                                    value={retireDate}
                                                    onChange={(e) => setRetireDate(e.target.value)}
                                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    Provider's announced end-of-life. From this date on (UTC), the
                                                    gateway does not serve the model.
                                                </p>
                                            </div>
                                            {!isCatalog && (
                                            <>
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-1">
                                                    Announced on
                                                </label>
                                                <input
                                                    type="date"
                                                    value={retirementAnnouncedAt}
                                                    onChange={(e) => setRetirementAnnouncedAt(e.target.value)}
                                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    When the provider published the retirement notice.
                                                </p>
                                            </div>
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-1">
                                                    Alert from
                                                </label>
                                                <input
                                                    type="date"
                                                    value={retirementReminderDate}
                                                    onChange={(e) => setRetirementReminderDate(e.target.value)}
                                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    System alerts start firing on this date. Defaults to
                                                    14 days before retirement.
                                                </p>
                                            </div>
                                            </>
                                            )}
                                        </div>

                                        {!isCatalog && (
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-1">
                                                    Replacement model
                                                </label>
                                                <input
                                                    type="text"
                                                    value={retirementReplacementModelName}
                                                    onChange={(e) => setRetirementReplacementModelName(e.target.value)}
                                                    placeholder="gpt-image-2.6-flare"
                                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    Provider-side name of the in-family successor.
                                                </p>
                                            </div>
                                            <div>
                                                <label className="block text-sm font-medium text-secondary mb-1">
                                                    Replacement provider
                                                </label>
                                                <input
                                                    type="text"
                                                    value={retirementReplacementProviderKey}
                                                    onChange={(e) => setRetirementReplacementProviderKey(e.target.value)}
                                                    placeholder="openai"
                                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                                />
                                                <p className="text-xs text-muted mt-1">
                                                    Provider key. Leave blank when the replacement is on
                                                    the same provider as this model.
                                                </p>
                                            </div>
                                        </div>
                                        )}

                                        {!isCatalog && (
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                Retirement notes (markdown)
                                            </label>
                                            <textarea
                                                value={retirementNotes}
                                                onChange={(e) => setRetirementNotes(e.target.value)}
                                                rows={6}
                                                placeholder={
                                                    "Provider announcement URL, breaking-change summary, " +
                                                    "migration notes ..."
                                                }
                                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm font-mono"
                                            />
                                            <p className="text-xs text-muted mt-1">
                                                Surfaced verbatim in the operator alert + admin UI.
                                                Markdown supported.
                                            </p>
                                        </div>
                                        )}

                                        {retireDate && (
                                            <div className="flex justify-end">
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setRetireDate('');
                                                        setRetirementAnnouncedAt('');
                                                        setRetirementReminderDate('');
                                                        setRetirementReplacementModelName('');
                                                        setRetirementReplacementProviderKey('');
                                                        setRetirementNotes('');
                                                    }}
                                                    className="px-3 py-1.5 text-sm text-red-600 hover:text-red-700 hover:bg-red-50 rounded-lg transition-colors"
                                                >
                                                    Clear retirement data
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        </form>
                    </div>

                    {/* Footer */}
                    <div className="px-6 py-4 border-t border-default flex items-center justify-end flex-shrink-0">
                        <div className="flex space-x-3">
                            <button
                                type="button"
                                onClick={onClose}
                                className="px-4 py-2 border border-strong text-secondary rounded-lg hover:bg-surface-alt"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={asyncHandler(handleSubmit, 'click')}
                                disabled={isSubmitting}
                                className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 flex items-center space-x-2"
                            >
                                {isSubmitting ? (
                                    <>
                                        <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                                        <span>Saving...</span>
                                    </>
                                ) : (
                                    <>
                                        <CheckCircle2 className="w-4 h-4" />
                                        <span>Save Model</span>
                                    </>
                                )}
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            {/* Help Modal */}
            {showHelp && (
                <LLModelFormHelp
                    isOpen={showHelp}
                    onClose={() => setShowHelp(false)}
                    initialSection={
                        // Limits/Policies/Fallbacks have no dedicated help pane,
                        // and Audit Info explains itself in the tab — fall back
                        // to General so the type stays a HelpSection.
                        helpSection === 'limits' || helpSection === 'policies'
                            || helpSection === 'fallbacks' || helpSection === 'audit'
                            ? 'basic'
                            : helpSection
                    }
                />
            )}
        </>
    );
};

export default LLModelForm;