/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent Card editor with left-navbar tabs, matching the shape that
 * lands in `agent_cards` (DB model). Sections map one-for-one to the
 * denormalised v1.0 columns:
 *
 *   General          — name, version, description, source_url, enabled
 *   Interfaces       — supportedInterfaces[]  (card_json)
 *   Capabilities     — agent_card_capabilities (one-to-many)
 *   Extensions       — capabilities.extensions[]  (card_json)
 *   Signing          — trust_tier, signatures[] + v0.3 legacy fields
 *   Access Control   — resource-group bindings (resource_group_agent_cards)
 *   Advanced         — raw card_json editor (expert)
 *
 * Sign / revoke live on the `Signing` tab and are fire-and-forget —
 * they hit the admin API directly and re-load the card state after.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
    AlertCircle,
    Check,
    Copy,
    FileCode2,
    FileSignature,
    Layers,
    Loader,
    Network,
    Plug,
    Settings,
    Shield,
    ShieldCheck,
    Gauge,
    Users,
    X,
    XCircle,
    FileClock,
} from 'lucide-react';
import AuditInfoTab from '../common/AuditInfoTab';

import { useGateway } from '../../hooks/useGateway';
// Tenant-wide per-card Limits/Policies reuse the classic RG A2A forms (DRY).
import A2ALimitsForm from '../groups/subtabs/A2ALimitsForm';
import A2AGovernanceForm from '../groups/subtabs/A2AGovernanceForm';
import type {
    AgentCardCapabilityHandlerType,
    AgentCardCapabilityResponse,
    AgentCardDetailResponse,
    AgentCardTrustTier,
} from '../../lib/a2aTypes';
import type { A2AGovernance, GroupHierarchyTree } from '../../lib/groupTypes';
import type { GuardrailConfigResponse } from '../../lib/guardrailTypes';
import HierarchicalGroupSelector from '../common/HierarchicalGroupSelector';
import { TABLE_THEAD, TABLE_TH_TYPE, TABLE_TBODY } from '../ui/tableStyles';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler } from '../../lib/asyncSafe';
import ReadFailureNotice from '../common/ReadFailureNotice';
import type { ReadFailure } from '../../hooks/useRead';
import { governanceApprovalTimeoutProblem } from '../../lib/approvalTimeout';
import { withoutRefused } from '../../lib/refusedSettings';

// ─── Types + constants ──────────────────────────────────────────────────────

type TabId =
    | 'general'
    | 'audit'
    | 'interfaces'
    | 'capabilities'
    | 'extensions'
    | 'signing'
    | 'access'
    | 'limits'
    | 'policies'
    | 'guardrails'
    | 'advanced';

const TABS: { id: TabId; label: string; icon: React.ReactNode }[] = [
    { id: 'general', label: 'General', icon: <Settings className="w-4 h-4" /> },
    { id: 'audit', label: 'Audit Info', icon: <FileClock className="w-4 h-4" /> },
    { id: 'access', label: 'Access Control', icon: <Users className="w-4 h-4" /> },
    { id: 'interfaces', label: 'Interfaces', icon: <Network className="w-4 h-4" /> },
    { id: 'capabilities', label: 'Capabilities', icon: <Layers className="w-4 h-4" /> },
    { id: 'extensions', label: 'Extensions', icon: <Plug className="w-4 h-4" /> },
    { id: 'signing', label: 'Signing', icon: <ShieldCheck className="w-4 h-4" /> },
    { id: 'limits', label: 'Limits', icon: <Gauge className="w-4 h-4" /> },
    { id: 'policies', label: 'Policies', icon: <Shield className="w-4 h-4" /> },
    // Read-only inheritance hint surfacing the policies
    // that will fire on this card's inbound + outbound A2A traffic.
    { id: 'guardrails', label: 'Guardrails', icon: <Shield className="w-4 h-4" /> },
    { id: 'advanced', label: 'Advanced', icon: <FileCode2 className="w-4 h-4" /> },
];

/** Handler-type dropdown values — kept in lockstep with the Rust
 *  `HANDLER_TYPE_*` constants. Leaving as `''` preserves the current
 *  server-side default (noop) without writing one through. */
const HANDLER_TYPES: Array<{ value: AgentCardCapabilityHandlerType | ''; label: string }> = [
    { value: '', label: '— unchanged —' },
    { value: 'noop', label: 'noop — acknowledge without executing' },
    { value: 'skill', label: 'skill — dispatch to a skill' },
];

interface CapabilityDraft {
    name: string;
    description: string;
    handler_type: AgentCardCapabilityHandlerType | '';
    handler_ref: string;
    timeout_secs: number | '';
    rate_limit_per_hour: number | '';
    data_classification: string[];
}

interface InterfaceDraft {
    url: string;
    protocolBinding: string;
    protocolVersion: string;
}

interface ExtensionDraft {
    uri: string;
    description: string;
    required: boolean;
}

// ─── Component ──────────────────────────────────────────────────────────────

interface Props {
    initial: AgentCardDetailResponse | null;
    groupTree?: GroupHierarchyTree[];
    groupsLoading?: boolean;
    /** The read of the resource groups failed: the Access tab says so where it would say there are none. */
    groupsFailure?: ReadFailure | null;
    onRetryGroups?: () => void;
    onClose: () => void;
    onSaved: () => void;
}

export const AgentCardEditDialog: React.FC<Props> = ({
    initial,
    groupTree,
    groupsLoading,
    groupsFailure,
    onRetryGroups,
    onClose,
    onSaved,
}) => {
    const { gatewayClient } = useGateway();
    const [activeTab, setActiveTab] = useState<TabId>('general');
    const [saving, setSaving] = useState(false);
    const [signing, setSigning] = useState(false);
    const [revoking, setRevoking] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [actionMessage, setActionMessage] = useState<string | null>(null);
    const isEdit = !!initial;

    // General fields
    const [name, setName] = useState(initial?.name || '');
    const [version, setVersion] = useState(initial?.version || '1.0.0');
    const [description, setDescription] = useState(initial?.description || '');
    const [sourceUrl, setSourceUrl] = useState(initial?.source_url || '');
    const [enabled, setEnabled] = useState<boolean>(initial?.enabled ?? true);
    // Tenant-wide default Limits / Policies (min-wins with each Resource Group).
    const [tenantLimits, setTenantLimits] = useState<Record<string, any>>(
        (initial?.tenant_limits as Record<string, any>) || {},
    );
    const [tenantGovernance, setTenantGovernance] = useState<A2AGovernance>(initial?.tenant_governance ?? {});

    // Card JSON backing store — the dialog's "source of truth" for the
    // v1.0 fields we don't denormalise into dedicated inputs.
    const [cardJson, setCardJson] = useState<Record<string, unknown>>(
        initial?.card_json || { name: '', version: '1.0.0' },
    );
    const [rawJsonDraft, setRawJsonDraft] = useState(
        JSON.stringify(initial?.card_json || {}, null, 2),
    );
    const [rawJsonError, setRawJsonError] = useState<string | null>(null);

    // Capabilities (denormalised)
    const [capabilities, setCapabilities] = useState<CapabilityDraft[]>(() =>
        initialCapabilityDrafts(initial),
    );

    // v1.0 interfaces + extensions lifted from card_json
    const [interfaces, setInterfaces] = useState<InterfaceDraft[]>(() =>
        extractInterfaces(initial?.card_json),
    );
    const [extensions, setExtensions] = useState<ExtensionDraft[]>(() =>
        extractExtensions(initial?.card_json),
    );

    // Access-control — resource groups
    const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>(
        initial?.group_ids || [],
    );

    // Keep name/version in sync with card_json. Admins expect the
    // top-level inputs to drive what's stored on disk; otherwise two
    // sources of truth silently drift apart.
    useEffect(() => {
        setCardJson(prev => ({
            ...(prev || {}),
            name: name || (prev as any)?.name || '',
            version: version || (prev as any)?.version || '1.0.0',
            description: description || (prev as any)?.description || undefined,
        }));
    }, [name, version, description]);

    // Merge interfaces/extensions/capabilities back into card_json on
    // every relevant edit — the wire card emitted to the server must
    // include what the admin edited on dedicated tabs, not just what
    // they pasted on the Raw JSON tab.
    const composedCardJson = useMemo(
        () => composeCardJson(cardJson, interfaces, extensions, capabilities),
        [cardJson, interfaces, extensions, capabilities],
    );

    useEffect(() => {
        // Keep the raw editor hydrated with the composed view so users
        // opening Advanced after editing other tabs see the real payload.
        setRawJsonDraft(JSON.stringify(composedCardJson, null, 2));
    }, [composedCardJson]);

    // An approval timeout the Control Plane refuses is not sent: Save stays off while there is one.
    const timeoutProblem = governanceApprovalTimeoutProblem(tenantGovernance);

    // ── Save ────────────────────────────────────────────────────────────────
    const save = async () => {
        if (!gatewayClient) return;
        if (timeoutProblem !== null) {
            setError(timeoutProblem);
            setActiveTab('policies');
            return;
        }
        if (!name.trim()) {
            setError('Name is required.');
            setActiveTab('general');
            return;
        }

        // If the admin made changes on the Advanced tab, honour those
        // verbatim — otherwise use the composed view so tab edits win.
        let payloadCard = composedCardJson;
        if (rawJsonDraft && rawJsonDraft.trim().length > 0) {
            try {
                const parsed = JSON.parse(rawJsonDraft);
                if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                    payloadCard = parsed;
                } else {
                    setError('Raw JSON must be a JSON object.');
                    setActiveTab('advanced');
                    return;
                }
            } catch (e) {
                setError(`Raw JSON is invalid: ${errorMessage(e, 'parse error')}`);
                setActiveTab('advanced');
                return;
            }
        }

        setSaving(true);
        setError(null);
        try {
            let cardId: string;
            if (isEdit && initial) {
                await gatewayClient.updateA2AAgentCard(initial.id, {
                    name,
                    version,
                    description: description || null,
                    card_json: payloadCard,
                    enabled,
                    tenant_limits: withoutRefused('a2a_limits', tenantLimits),
                    tenant_governance: tenantGovernance,
                });
                cardId = initial.id;
            } else {
                const created = await gatewayClient.createA2AAgentCard({
                    name,
                    version,
                    description: description || null,
                    source_url: sourceUrl || null,
                    card_json: payloadCard,
                    // The create names it: a card made with Enabled off
                    // is stored disabled by the one request.
                    enabled,
                });
                cardId = created.id;
            }
            // Always rewrite the full set of group bindings — unsetting
            // works as "pass an empty array".
            if (groupTree && groupTree.length > 0) {
                await gatewayClient.setA2AAgentCardGroups(cardId, selectedGroupIds);
            }
            onSaved();
        } catch (e) {
            setError(errorMessage(e, 'Save failed.'));
        } finally {
            setSaving(false);
        }
    };

    // ── Sign / revoke (only available on existing cards) ────────────────────
    const handleSign = async () => {
        if (!gatewayClient || !initial) return;
        setSigning(true);
        setError(null);
        setActionMessage(null);
        try {
            await gatewayClient.signA2AAgentCard(initial.id, {});
            setActionMessage('Card signed with the tenant\'s Ed25519 key.');
            // Reload callers' view; the parent fetches fresh state on onSaved.
            onSaved();
        } catch (e) {
            setError(errorMessage(e, 'Sign failed.'));
        } finally {
            setSigning(false);
        }
    };

    const handleRevoke = async () => {
        if (!gatewayClient || !initial) return;
        const reason = window.prompt(
            'Enter a revocation reason (audited, cannot be undone):',
            '',
        );
        if (!reason || !reason.trim()) return;
        setRevoking(true);
        setError(null);
        setActionMessage(null);
        try {
            await gatewayClient.revokeA2AAgentCard(initial.id, {
                reason: reason.trim(),
            });
            setActionMessage('Card revoked.');
            onSaved();
        } catch (e) {
            setError(errorMessage(e, 'Revoke failed.'));
        } finally {
            setRevoking(false);
        }
    };

    // ── Render ──────────────────────────────────────────────────────────────
    const renderTab = () => {
        switch (activeTab) {
            case 'audit':
                return (
                    <AuditInfoTab
                        // The card's version is edited on General, where it
                        // sits next to the name because it is published in
                        // the card JSON clients read. Offering a second
                        // input for the same field here would let an
                        // operator change it in one place and save from the
                        // other.
                        versionHidden
                        versionDerivedFrom="General tab"
                        meta={{
                            created_by: initial?.created_by,
                            created_at: initial?.created_at,
                            updated_by: initial?.updated_by,
                            updated_at: initial?.updated_at,
                        }}
                        isNew={!initial}
                    />
                );
            case 'general':
                return (
                    <GeneralTab
                        name={name}
                        setName={setName}
                        version={version}
                        setVersion={setVersion}
                        description={description}
                        setDescription={setDescription}
                        sourceUrl={sourceUrl}
                        setSourceUrl={setSourceUrl}
                        enabled={enabled}
                        setEnabled={setEnabled}
                        initial={initial}
                    />
                );
            case 'interfaces':
                return (
                    <InterfacesTab interfaces={interfaces} setInterfaces={setInterfaces} />
                );
            case 'capabilities':
                return (
                    <CapabilitiesTab
                        capabilities={capabilities}
                        setCapabilities={setCapabilities}
                    />
                );
            case 'extensions':
                return (
                    <ExtensionsTab extensions={extensions} setExtensions={setExtensions} />
                );
            case 'signing':
                return (
                    <SigningTab
                        initial={initial}
                        onSign={asyncHandler(handleSign, 'sign')}
                        onRevoke={asyncHandler(handleRevoke, 'revoke')}
                        signing={signing}
                        revoking={revoking}
                    />
                );
            case 'access':
                return (
                    <AccessControlTab
                        groupTree={groupTree || []}
                        groupsLoading={!!groupsLoading}
                        groupsFailure={groupsFailure}
                        onRetryGroups={onRetryGroups}
                        selectedGroupIds={selectedGroupIds}
                        setSelectedGroupIds={setSelectedGroupIds}
                    />
                );
            case 'limits':
                return (
                    <div className="space-y-4">
                        <p className="text-sm text-secondary" data-testid="card-limits-help">
                            The limits of this card. The gateway enforces them on every call that
                            addresses the card, together with the limits of the caller&apos;s groups:
                            a call has to fit all of them.
                        </p>
                        <A2ALimitsForm value={tenantLimits} onChange={setTenantLimits} />
                    </div>
                );
            case 'policies':
                return (
                    <div className="space-y-4">
                        <p className="text-sm text-secondary" data-testid="card-policies-help">
                            The policy of this card. The gateway applies it to every call that
                            addresses the card, together with the policy of the caller&apos;s group and
                            of every group above it. Each is judged on its own and the strictest
                            verdict wins.
                        </p>
                        <A2AGovernanceForm value={tenantGovernance} onChange={setTenantGovernance} />
                    </div>
                );
            case 'guardrails':
                return <InheritedGuardrailsTab selectedGroupIds={selectedGroupIds} />;
            case 'advanced':
                return (
                    <AdvancedTab
                        rawJson={rawJsonDraft}
                        setRawJson={setRawJsonDraft}
                        parseError={rawJsonError}
                        setParseError={setRawJsonError}
                    />
                );
            default:
                return null;
        }
    };

    return (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-xl shadow-2xl w-full max-w-5xl max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div>
                        <h2 className="text-lg font-semibold text-default">
                            {isEdit && initial
                                ? `Edit Agent Card: ${initial.name}`
                                : 'New Agent Card'}
                        </h2>
                        <p className="text-xs text-muted mt-0.5">
                            Configure how Brutor publishes this agent card and dispatches
                            invocations to its capabilities.
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1 text-muted hover:text-secondary rounded"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="flex flex-1 min-h-0 min-w-0">
                    <nav className="w-52 border-r border-default py-3 overflow-y-auto flex-shrink-0">
                        {TABS.map(tab => (
                            <button
                                key={tab.id}
                                onClick={() => setActiveTab(tab.id)}
                                className={`w-full flex items-center gap-2 px-4 py-2 text-sm transition-colors ${
                                    activeTab === tab.id
                                        ? 'bg-primary-50 text-primary-700 font-medium border-r-2 border-primary-500'
                                        : 'text-secondary hover:bg-surface-alt hover:text-default'
                                }`}
                            >
                                {tab.icon}
                                {tab.label}
                            </button>
                        ))}
                    </nav>
                    <div className="scroll-region p-6">{renderTab()}</div>
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between px-6 py-4 border-t border-default">
                    <div className="flex-1 mr-4 min-h-0 min-w-0">
                        {error && (
                            <div className="flex items-start gap-2 text-sm text-red-700">
                                <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                                <p>{error}</p>
                            </div>
                        )}
                        {!error && actionMessage && (
                            <p className="text-sm text-green-700">{actionMessage}</p>
                        )}
                    </div>
                    <div className="flex items-center gap-3">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-sm text-secondary hover:text-default hover:bg-surface-raised rounded-lg transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(save, 'click')}
                            disabled={saving || timeoutProblem !== null}
                            title={timeoutProblem ?? undefined}
                            className="inline-flex items-center gap-1.5 px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-lg text-sm disabled:opacity-50"
                        >
                            {saving && <Loader className="w-3.5 h-3.5 animate-spin" />}
                            {isEdit ? 'Save Changes' : 'Create Card'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

// ─── Tabs ───────────────────────────────────────────────────────────────────

const GeneralTab: React.FC<{
    name: string;
    setName: (v: string) => void;
    version: string;
    setVersion: (v: string) => void;
    description: string;
    setDescription: (v: string) => void;
    sourceUrl: string;
    setSourceUrl: (v: string) => void;
    enabled: boolean;
    setEnabled: (v: boolean) => void;
    initial: AgentCardDetailResponse | null;
}> = ({
    name,
    setName,
    version,
    setVersion,
    description,
    setDescription,
    sourceUrl,
    setSourceUrl,
    enabled,
    setEnabled,
    initial,
}) => (
    <div className="space-y-4">
        <FormRow label="Name" required>
            <input
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                placeholder="billing-agent"
            />
        </FormRow>
        <FormRow label="Version" required>
            {/* Authored here rather than on Audit Info: this version is
                published in the agent card clients fetch, so it belongs
                next to the name it is published with. */}
            <input
                type="text"
                value={version}
                onChange={e => setVersion(e.target.value)}
                maxLength={64}
                className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                placeholder="1.0.0"
            />
        </FormRow>
        <FormRow label="Description">
            <textarea
                value={description}
                onChange={e => setDescription(e.target.value)}
                rows={3}
                className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                placeholder="Short description surfaced on the discovery endpoint"
            />
        </FormRow>
        <FormRow label="Source URL">
            {/* Set at creation: the update of a card names no source URL. */}
            <input
                type="text"
                value={sourceUrl}
                onChange={e => setSourceUrl(e.target.value)}
                readOnly={initial !== null}
                className={`w-full px-3 py-2 border border-strong rounded-md text-sm font-mono ${initial !== null ? 'bg-surface-alt text-muted' : 'bg-surface'}`}
                placeholder={initial !== null ? '' : 'https://source.example.com/agent-card.json'}
            />
            {initial !== null && (
                <p className="text-xs text-muted mt-1" data-testid="source-url-help">
                    Set when the card is created. An update does not change it.
                </p>
            )}
        </FormRow>
        <div className="flex items-start justify-between gap-4 p-3 bg-surface-alt rounded-lg">
            <div className="flex-1 min-w-0 min-h-0">
                <span className="text-sm font-medium text-secondary">Enabled</span>
                <p className="text-xs text-muted">
                    Disable to remove the card from discovery and block inbound /
                    outbound invocations without deleting it.
                </p>
            </div>
            <Toggle checked={enabled} onChange={setEnabled} label="Enabled" />
        </div>
        {initial && (
            <div className="mt-6 pt-4 border-t border-default space-y-2">
                <h4 className="text-xs font-semibold uppercase text-muted tracking-wider">
                    Metadata
                </h4>
                {/* Created / updated / created-by are on Audit Info,
                    which every resource form shares. Tenant is not shown:
                    an admin only ever sees one tenant's cards. */}
                <KeyValue
                    label="Card ID"
                    value={<CopyableId id={initial.id} label="Card ID" />}
                />
            </div>
        )}
    </div>
);

const InterfacesTab: React.FC<{
    interfaces: InterfaceDraft[];
    setInterfaces: (next: InterfaceDraft[]) => void;
}> = ({ interfaces, setInterfaces }) => {
    const update = (idx: number, patch: Partial<InterfaceDraft>) => {
        setInterfaces(interfaces.map((i, j) => (j === idx ? { ...i, ...patch } : i)));
    };
    const add = () =>
        setInterfaces([
            ...interfaces,
            { url: '', protocolBinding: 'JSONRPC', protocolVersion: '1.0' },
        ]);
    const remove = (idx: number) =>
        setInterfaces(interfaces.filter((_, j) => j !== idx));

    return (
        <div className="space-y-4">
            <p className="text-xs text-muted">
                A2A v1.0 <code>supportedInterfaces[]</code>. Order is the preferred
                order — the proxy picks the first binding it understands. Bindings:{' '}
                <code>JSONRPC</code>, <code>HTTP+JSON</code>, <code>GRPC</code>.
            </p>
            {interfaces.length === 0 && (
                <div className="p-4 border border-dashed border-default rounded text-xs text-muted">
                    No interfaces declared yet. Add at least one so external callers
                    can reach the agent.
                </div>
            )}
            {interfaces.map((iface, idx) => (
                <div
                    key={idx}
                    className="p-3 border border-default rounded-lg bg-surface-alt space-y-2"
                >
                    <div className="flex items-center justify-between">
                        <span className="text-xs font-medium text-secondary">
                            Interface {idx + 1}
                        </span>
                        <button
                            onClick={() => remove(idx)}
                            className="text-xs text-red-600 hover:text-red-800"
                        >
                            Remove
                        </button>
                    </div>
                    <input
                        type="text"
                        value={iface.url}
                        onChange={e => update(idx, { url: e.target.value })}
                        className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm font-mono"
                        placeholder="URL (https://agent.example.com/rpc)"
                    />
                    <div className="grid grid-cols-2 gap-2">
                        <select
                            value={iface.protocolBinding}
                            onChange={e =>
                                update(idx, { protocolBinding: e.target.value })
                            }
                            className="px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                        >
                            <option value="JSONRPC">JSONRPC</option>
                            <option value="HTTP+JSON">HTTP+JSON</option>
                            <option value="GRPC">GRPC</option>
                        </select>
                        <select
                            value={iface.protocolVersion}
                            onChange={e =>
                                update(idx, { protocolVersion: e.target.value })
                            }
                            className="px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                        >
                            <option value="1.0">1.0 (A2A v1.0)</option>
                            <option value="0.3">0.3 (A2A v0.3 legacy)</option>
                        </select>
                    </div>
                </div>
            ))}
            <button
                onClick={add}
                className="text-sm text-primary-600 hover:text-primary-800"
            >
                + Add interface
            </button>
        </div>
    );
};

const CapabilitiesTab: React.FC<{
    capabilities: CapabilityDraft[];
    setCapabilities: (next: CapabilityDraft[]) => void;
}> = ({ capabilities, setCapabilities }) => {
    const update = (idx: number, patch: Partial<CapabilityDraft>) => {
        setCapabilities(
            capabilities.map((c, j) => (j === idx ? { ...c, ...patch } : c)),
        );
    };
    const add = () =>
        setCapabilities([
            ...capabilities,
            {
                name: '',
                description: '',
                handler_type: 'noop',
                handler_ref: '',
                timeout_secs: '',
                rate_limit_per_hour: '',
                data_classification: [],
            },
        ]);
    const remove = (idx: number) =>
        setCapabilities(capabilities.filter((_, j) => j !== idx));

    return (
        <div className="space-y-4">
            <p className="text-xs text-muted">
                Each entry becomes a row in <code>agent_card_capabilities</code>.
                <strong> handler_type</strong> and <strong>handler_ref</strong> control
                how inbound invocations dispatch — pick <code>noop</code> for an agent
                card that isn't wired to anything yet.
            </p>
            <p className="text-xs text-muted" data-testid="capability-rate-help">
                Rate limit: the most calls of the capability in one hour. The gateway refuses a
                call above it. Empty: no rate limit of its own.
            </p>
            {capabilities.length === 0 && (
                <div className="p-4 border border-dashed border-default rounded text-xs text-muted">
                    No capabilities declared. Add one to enable inbound dispatch.
                </div>
            )}
            {capabilities.map((cap, idx) => (
                <div
                    key={idx}
                    className="p-3 border border-default rounded-lg bg-surface-alt space-y-2"
                >
                    <div className="flex items-center justify-between">
                        <span className="text-xs font-medium text-secondary">
                            Capability {idx + 1}
                        </span>
                        <button
                            onClick={() => remove(idx)}
                            className="text-xs text-red-600 hover:text-red-800"
                        >
                            Remove
                        </button>
                    </div>
                    <input
                        type="text"
                        value={cap.name}
                        onChange={e => update(idx, { name: e.target.value })}
                        className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm font-mono"
                        placeholder="Name (e.g. billing.refund)"
                    />
                    <input
                        type="text"
                        value={cap.description}
                        onChange={e => update(idx, { description: e.target.value })}
                        className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                        placeholder="Description"
                    />
                    <div className="grid grid-cols-2 gap-2">
                        <select
                            value={cap.handler_type}
                            onChange={e =>
                                update(idx, {
                                    handler_type: e.target.value as
                                        | AgentCardCapabilityHandlerType
                                        | '',
                                })
                            }
                            className="px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                        >
                            {HANDLER_TYPES.map(t => (
                                <option key={t.value} value={t.value}>
                                    {t.label}
                                </option>
                            ))}
                        </select>
                        <input
                            type="text"
                            value={cap.handler_ref}
                            onChange={e =>
                                update(idx, { handler_ref: e.target.value })
                            }
                            className="px-3 py-2 border border-strong rounded-md bg-surface text-sm font-mono"
                            placeholder="Handler ref (skill name or ULID)"
                            disabled={cap.handler_type !== 'skill'}
                        />
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                        <input
                            type="number"
                            min={0}
                            value={cap.timeout_secs}
                            onChange={e =>
                                update(idx, {
                                    timeout_secs:
                                        e.target.value === ''
                                            ? ''
                                            : Math.max(0, parseInt(e.target.value, 10)),
                                })
                            }
                            className="px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                            placeholder="Timeout (sec)"
                        />
                        <input
                            type="number"
                            min={0}
                            value={cap.rate_limit_per_hour}
                            onChange={e =>
                                update(idx, {
                                    rate_limit_per_hour:
                                        e.target.value === ''
                                            ? ''
                                            : Math.max(0, parseInt(e.target.value, 10)),
                                })
                            }
                            className="px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                            placeholder="Rate limit (req/hr)"
                        />
                    </div>
                    <TagEditor
                        value={cap.data_classification}
                        onChange={next => update(idx, { data_classification: next })}
                        placeholder="Data classification tag (e.g. pii, phi)"
                    />
                </div>
            ))}
            <button
                onClick={add}
                className="text-sm text-primary-600 hover:text-primary-800"
            >
                + Add capability
            </button>
        </div>
    );
};

const ExtensionsTab: React.FC<{
    extensions: ExtensionDraft[];
    setExtensions: (next: ExtensionDraft[]) => void;
}> = ({ extensions, setExtensions }) => {
    const update = (idx: number, patch: Partial<ExtensionDraft>) =>
        setExtensions(extensions.map((e, j) => (j === idx ? { ...e, ...patch } : e)));
    const add = () =>
        setExtensions([
            ...extensions,
            { uri: '', description: '', required: false },
        ]);
    const remove = (idx: number) =>
        setExtensions(extensions.filter((_, j) => j !== idx));

    return (
        <div className="space-y-4">
            <p className="text-xs text-muted">
                A2A v1.0 <code>capabilities.extensions[]</code>. Each entry declares a
                capability extension the agent implements. Mark as <em>required</em>{' '}
                to force compliant clients to refuse if they don't understand the URI.
            </p>
            {extensions.map((ext, idx) => (
                <div
                    key={idx}
                    className="p-3 border border-default rounded-lg bg-surface-alt space-y-2"
                >
                    <div className="flex items-center justify-between">
                        <span className="text-xs font-medium text-secondary">
                            Extension {idx + 1}
                        </span>
                        <button
                            onClick={() => remove(idx)}
                            className="text-xs text-red-600 hover:text-red-800"
                        >
                            Remove
                        </button>
                    </div>
                    <input
                        type="text"
                        value={ext.uri}
                        onChange={e => update(idx, { uri: e.target.value })}
                        className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm font-mono"
                        placeholder="URI (https://example.com/a2a/extensions/foo/v1)"
                    />
                    <input
                        type="text"
                        value={ext.description}
                        onChange={e => update(idx, { description: e.target.value })}
                        className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                        placeholder="Description"
                    />
                    <label className="flex items-center gap-2 text-sm text-secondary">
                        <input
                            type="checkbox"
                            checked={ext.required}
                            onChange={e =>
                                update(idx, { required: e.target.checked })
                            }
                            className="w-4 h-4"
                        />
                        Required — clients that don't understand this extension MUST
                        refuse the interaction.
                    </label>
                </div>
            ))}
            <button
                onClick={add}
                className="text-sm text-primary-600 hover:text-primary-800"
            >
                + Add extension
            </button>
        </div>
    );
};

const SigningTab: React.FC<{
    initial: AgentCardDetailResponse | null;
    onSign: () => void;
    onRevoke: () => void;
    signing: boolean;
    revoking: boolean;
}> = ({ initial, onSign, onRevoke, signing, revoking }) => {
    if (!initial) {
        return (
            <p className="text-sm text-muted">
                Signing is available after the card is created.
            </p>
        );
    }
    const tier = initial.trust_tier;
    const revoked = tier === 'revoked' || !!initial.revoked_at;
    return (
        <div className="space-y-5">
            <p className="text-xs text-muted" data-testid="trust-tier-help">
                The trust tier is informational: the gateway decides no call by it. A revoked
                card takes no call.
            </p>
            <div className="flex items-center gap-3">
                <TrustTierBadge tier={tier} />
                {initial.signature_valid === false && (
                    <span className="flex items-center gap-1 text-sm text-red-600">
                        <AlertCircle className="w-4 h-4" />
                        Signature invalid
                    </span>
                )}
            </div>
            <dl className="grid grid-cols-1 gap-2 text-sm">
                {initial.signed_by && (
                    <KeyValue label="Signed by" value={initial.signed_by} />
                )}
                {initial.signed_at && (
                    <KeyValue label="Signed at" value={formatDate(initial.signed_at)} />
                )}
                {initial.signature_algorithm && (
                    <KeyValue
                        label="Algorithm"
                        value={<code>{initial.signature_algorithm}</code>}
                    />
                )}
                {initial.revoked_at && (
                    <KeyValue
                        label="Revoked at"
                        value={
                            <span className="text-red-600">
                                {formatDate(initial.revoked_at)}
                            </span>
                        }
                    />
                )}
                {initial.revoked_reason && (
                    <KeyValue label="Revoked reason" value={initial.revoked_reason} />
                )}
            </dl>
            <div className="flex items-center gap-3 pt-4 border-t border-default">
                <button
                    onClick={onSign}
                    disabled={signing || revoked}
                    className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-lg hover:bg-primary-100 disabled:opacity-50"
                >
                    {signing ? (
                        <Loader className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                        <FileSignature className="w-4 h-4" />
                    )}
                    {tier === 'unsigned' ? 'Sign' : 'Re-sign'}
                </button>
                <button
                    onClick={onRevoke}
                    disabled={revoking || revoked}
                    className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-red-700 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 disabled:opacity-50"
                >
                    {revoking ? (
                        <Loader className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                        <XCircle className="w-4 h-4" />
                    )}
                    Revoke
                </button>
            </div>
            <p className="text-xs text-muted">
                Signing with this tenant's Ed25519 key moves the card to{' '}
                <code>tenant_signed</code>. External verifiers check the signature
                against the public key served at{' '}
                <code>/.well-known/a2a-jwks/&lt;tenant_id&gt;</code>. Revocation is
                terminal — create a new card to replace a revoked one.
            </p>
        </div>
    );
};

const AccessControlTab: React.FC<{
    groupTree: GroupHierarchyTree[];
    groupsLoading: boolean;
    groupsFailure?: ReadFailure | null;
    onRetryGroups?: () => void;
    selectedGroupIds: string[];
    setSelectedGroupIds: (next: string[]) => void;
}> = ({ groupTree, groupsLoading, groupsFailure, onRetryGroups, selectedGroupIds, setSelectedGroupIds }) => (
    <div className="space-y-4">
        <p className="text-xs text-muted">
            Bind this card to resource groups. Groups inherit bound cards down the
            hierarchy — an agent bound to the root is visible to every descendant
            group. Unbound cards are registry-only; no group can invoke them.
        </p>
        {groupsLoading ? (
            <div className="flex items-center gap-2 text-sm text-muted">
                <Loader className="w-4 h-4 animate-spin" />
                Loading resource groups…
            </div>
        ) : groupsFailure && groupTree.length === 0 ? (
            <ReadFailureNotice compact failure={groupsFailure} onRetry={onRetryGroups} />
        ) : groupTree.length === 0 ? (
            <p className="text-sm text-muted">No resource groups available.</p>
        ) : (
            <HierarchicalGroupSelector
                groupTree={groupTree}
                selectedGroupIds={selectedGroupIds}
                onSelectionChange={setSelectedGroupIds}
            />
        )}
    </div>
);

/**
 * Read-only summary of the guardrail policies that will
 * fire on this Agent Card's inbound + outbound A2A traffic. Inherited
 * from the user/group binding the operator picked on the Access
 * Control tab; edited on the Guardrails tab elsewhere in the admin
 * console (link below the table). Mirrors the "Inherited from
 * provider" hint pattern on the LLModel form so operators see, at
 * the card level, exactly what'll get scanned without having to
 * cross-reference the user/group page.
 */
const InheritedGuardrailsTab: React.FC<{ selectedGroupIds: string[] }> = ({
    selectedGroupIds,
}) => {
    const { gatewayClient } = useGateway();
    const [allConfigs, setAllConfigs] = useState<GuardrailConfigResponse[] | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            try {
                setLoading(true);
                const resp = await gatewayClient.listGuardrailConfigs();
                if (!cancelled) {
                    setAllConfigs(resp.configs);
                    setError(null);
                }
            } catch (e) {
                if (!cancelled) setError(errorMessage(e) || String(e));
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        void load();
        return () => {
            cancelled = true;
        };
    }, [gatewayClient]);

    // Effective set: tenant defaults + any policy bound to one of the
    // groups the card grants. Mirrors the proxy's GuardrailsCache
    // resolution so what the operator sees here matches what fires
    // at request time.
    const effective = useMemo(() => {
        if (!allConfigs) return [] as GuardrailConfigResponse[];
        const groupSet = new Set(selectedGroupIds);
        return allConfigs.filter(
            c =>
                c.enabled &&
                (c.is_default || (c.group_ids || []).some(g => groupSet.has(g))),
        );
    }, [allConfigs, selectedGroupIds]);

    const inboundCovered = effective.some(c => c.surfaces.a2a_inbound);
    const outboundCovered = effective.some(c => c.surfaces.a2a_outbound);

    return (
        <div className="space-y-4">
            <div className="text-xs text-muted">
                Read-only view of the guardrail policies that fire on this card's A2A
                traffic. Inheritance: <strong>tenant defaults</strong> plus any policy
                bound to a group on the Access Control tab. Edit policies on the
                Guardrails tab in the main admin console.
            </div>

            <div className="grid grid-cols-2 gap-3">
                <SurfaceCoverageCard
                    label="Inbound A2A"
                    description="Incoming tasks.send / message.send payloads"
                    covered={inboundCovered}
                />
                <SurfaceCoverageCard
                    label="Outbound A2A"
                    description="Outgoing delegations to peer agents"
                    covered={outboundCovered}
                />
            </div>

            {error && (
                <p className="text-xs text-red-600">
                    <AlertCircle className="inline w-3.5 h-3.5 mr-1" /> {error}
                </p>
            )}

            {loading ? (
                <div className="flex items-center gap-2 text-xs text-muted">
                    <Loader className="w-3.5 h-3.5 animate-spin" /> Loading policies…
                </div>
            ) : effective.length === 0 ? (
                <div className="rounded border border-dashed border-default px-4 py-6 text-center text-xs text-muted">
                    No guardrail policies will fire on this card. Bind a group on the
                    Access Control tab, or set a tenant-default policy with
                    <code className="bg-surface-raised px-1 rounded mx-1">a2a_inbound</code>
                    /<code className="bg-surface-raised px-1 rounded mx-1">a2a_outbound</code>
                    enabled.
                </div>
            ) : (
                <table className="w-full text-xs border border-default rounded">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Policy</th>
                            <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Inherited via</th>
                            <th className={`text-center px-3 py-2 ${TABLE_TH_TYPE}`}>A2A in</th>
                            <th className={`text-center px-3 py-2 ${TABLE_TH_TYPE}`}>A2A out</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {effective.map(c => (
                            <tr key={c.id} className="border-t border-default">
                                <td className="px-3 py-2 font-mono">{c.name}</td>
                                <td className="px-3 py-2 text-muted">
                                    {c.is_default ? (
                                        <span>tenant default</span>
                                    ) : (
                                        <span>
                                            group:{' '}
                                            {(c.group_ids || [])
                                                .filter(g => selectedGroupIds.includes(g))
                                                .join(', ')}
                                        </span>
                                    )}
                                </td>
                                <td className="px-3 py-2 text-center">
                                    {c.surfaces.a2a_inbound ? (
                                        <Check className="inline w-3.5 h-3.5 text-emerald-600" />
                                    ) : (
                                        <span className="text-disabled">—</span>
                                    )}
                                </td>
                                <td className="px-3 py-2 text-center">
                                    {c.surfaces.a2a_outbound ? (
                                        <Check className="inline w-3.5 h-3.5 text-emerald-600" />
                                    ) : (
                                        <span className="text-disabled">—</span>
                                    )}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </div>
    );
};

const SurfaceCoverageCard: React.FC<{
    label: string;
    description: string;
    covered: boolean;
}> = ({ label, description, covered }) => (
    <div
        className={
            'rounded border p-3 flex items-start gap-3 ' +
            (covered ? 'border-emerald-300 bg-emerald-50' : 'border-default bg-surface-alt')
        }
    >
        <Shield
            className={
                'w-5 h-5 flex-shrink-0 mt-0.5 ' +
                (covered ? 'text-emerald-600' : 'text-disabled')
            }
        />
        <div>
            <div className="text-sm font-semibold text-default">{label}</div>
            <div className="text-xs text-muted">{description}</div>
            <div
                className={
                    'text-xs font-medium mt-1 ' +
                    (covered ? 'text-emerald-700' : 'text-muted')
                }
            >
                {covered ? 'Covered by ≥1 active policy' : 'No active policy'}
            </div>
        </div>
    </div>
);

const AdvancedTab: React.FC<{
    rawJson: string;
    setRawJson: (v: string) => void;
    parseError: string | null;
    setParseError: (v: string | null) => void;
}> = ({ rawJson, setRawJson, parseError, setParseError }) => (
    <div className="space-y-3">
        <p className="text-xs text-muted">
            Raw card JSON. The Interfaces / Capabilities / Extensions tabs compose
            into this document on save; editing here takes priority. Use for
            advanced fields Brutor doesn't model natively.
        </p>
        <textarea
            value={rawJson}
            onChange={e => {
                setRawJson(e.target.value);
                try {
                    JSON.parse(e.target.value);
                    setParseError(null);
                } catch (err) {
                    setParseError(errorMessage(err, 'JSON parse error'));
                }
            }}
            rows={22}
            className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-xs font-mono"
            spellCheck={false}
        />
        {parseError && (
            <p className="text-xs text-red-600">
                <AlertCircle className="inline w-3.5 h-3.5 mr-1" /> {parseError}
            </p>
        )}
    </div>
);

// ─── Sub-components ─────────────────────────────────────────────────────────

const FormRow: React.FC<{
    label: string;
    required?: boolean;
    children: React.ReactNode;
}> = ({ label, required, children }) => (
    <div>
        <label className="block text-sm font-medium text-secondary mb-1">
            {label} {required && <span className="text-red-500">*</span>}
        </label>
        {children}
    </div>
);

const Toggle: React.FC<{ checked: boolean; onChange: (v: boolean) => void; label?: string }> = ({
    checked,
    onChange,
    label,
}) => (
    <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        onClick={() => onChange(!checked)}
        className={`relative inline-flex h-6 w-11 shrink-0 mt-0.5 items-center rounded-full transition-colors ${
            checked ? 'bg-primary-500' : 'bg-surface-inset'
        }`}
    >
        <span
            className={`inline-block h-4 w-4 transform rounded-full bg-surface transition-transform ${
                checked ? 'translate-x-6' : 'translate-x-1'
            }`}
        />
    </button>
);

const KeyValue: React.FC<{ label: string; value: React.ReactNode }> = ({
    label,
    value,
}) => (
    <div className="flex items-start gap-3 text-sm">
        <dt className="text-muted min-w-[100px]">{label}</dt>
        <dd className="text-default">{value}</dd>
    </div>
);

const CopyableId: React.FC<{ id: string; label: string }> = ({ id, label }) => {
    const [copied, setCopied] = useState(false);
    const onCopy = async () => {
        try {
            await navigator.clipboard.writeText(id);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch {
            // no toast plumbed into this file; fail silently — the user can still
            // select the text and copy manually.
        }
    };
    return (
        <button
            type="button"
            onClick={asyncHandler(onCopy, 'click')}
            title={`Click to copy ${label.toLowerCase()}`}
            className="inline-flex items-center gap-1.5 font-mono text-xs text-default hover:bg-surface-alt px-1.5 py-0.5 rounded border border-default transition-colors"
        >
            <span className="select-all">{id}</span>
            {copied ? (
                <Check className="w-3.5 h-3.5 text-green-600 flex-shrink-0" />
            ) : (
                <Copy className="w-3.5 h-3.5 text-muted flex-shrink-0" />
            )}
        </button>
    );
};

const TrustTierBadge: React.FC<{ tier: AgentCardTrustTier }> = ({ tier }) => {
    const style: Record<AgentCardTrustTier, string> = {
        unsigned: 'bg-gray-100 text-gray-700',
        tenant_signed: 'bg-green-100 text-green-700',
        partner_signed: 'bg-blue-100 text-blue-700',
        revoked: 'bg-red-100 text-red-700',
    };
    return (
        <span
            className={`px-2 py-1 text-xs font-medium rounded ${style[tier] || 'bg-gray-100 text-gray-700'}`}
        >
            {tier}
        </span>
    );
};

const TagEditor: React.FC<{
    value: string[];
    onChange: (next: string[]) => void;
    placeholder?: string;
}> = ({ value, onChange, placeholder }) => {
    const [draft, setDraft] = useState('');
    const add = () => {
        const v = draft.trim();
        if (!v) return;
        if (value.includes(v)) {
            setDraft('');
            return;
        }
        onChange([...value, v]);
        setDraft('');
    };
    return (
        <div>
            <div className="flex gap-2">
                <input
                    type="text"
                    value={draft}
                    onChange={e => setDraft(e.target.value)}
                    onKeyDown={e => {
                        if (e.key === 'Enter') {
                            e.preventDefault();
                            add();
                        }
                    }}
                    className="flex-1 px-3 py-2 border border-strong rounded-md bg-surface text-sm min-h-0 min-w-0"
                    placeholder={placeholder || 'Tag'}
                />
                <button
                    onClick={add}
                    className="px-3 py-1.5 border border-strong rounded-md text-sm text-secondary hover:bg-surface-alt"
                >
                    Add
                </button>
            </div>
            {value.length > 0 && (
                <ul className="mt-2 flex flex-wrap gap-1">
                    {value.map((tag, i) => (
                        <li
                            key={`${tag}-${i}`}
                            className="inline-flex items-center gap-1 text-xs bg-surface-alt rounded px-2 py-0.5"
                        >
                            <span>{tag}</span>
                            <button
                                onClick={() =>
                                    onChange(value.filter((_, j) => j !== i))
                                }
                                className="text-muted hover:text-red-600"
                                title="Remove"
                            >
                                ✕
                            </button>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
};

// ─── Helpers ────────────────────────────────────────────────────────────────

function initialCapabilityDrafts(
    card: AgentCardDetailResponse | null,
): CapabilityDraft[] {
    if (!card) return [];
    const dbCaps = card.capabilities || [];
    // Primary source: denormalised rows on the detail response. Fall
    // back to parsing card_json.capabilities[] if the server didn't
    // populate them.
    if (dbCaps.length > 0) {
        return dbCaps.map(c => ({
            name: c.capability_name,
            description: c.description || '',
            handler_type: (c.handler_type as AgentCardCapabilityHandlerType) || '',
            handler_ref: c.handler_ref || '',
            timeout_secs: c.timeout_secs ?? '',
            rate_limit_per_hour: c.rate_limit_per_hour ?? '',
            data_classification: c.data_classification || [],
        }));
    }
    const raw = (card.card_json as any)?.capabilities;
    if (!Array.isArray(raw)) return [];
    return raw
        .filter(c => c && typeof c === 'object')
        .map((c: any) => ({
            name: String(c.name ?? ''),
            description: String(c.description ?? ''),
            handler_type:
                c.handler_type === 'skill' || c.handler_type === 'noop'
                    ? c.handler_type
                    : '',
            handler_ref: typeof c.handler_ref === 'string' ? c.handler_ref : '',
            timeout_secs: typeof c.timeout_secs === 'number' ? c.timeout_secs : '',
            rate_limit_per_hour:
                typeof c.rate_limit_per_hour === 'number' ? c.rate_limit_per_hour : '',
            data_classification: Array.isArray(c.data_classification)
                ? c.data_classification.filter((t: any) => typeof t === 'string')
                : [],
        }));
}

function extractInterfaces(cardJson: Record<string, unknown> | undefined): InterfaceDraft[] {
    if (!cardJson) return [];
    const v1 = (cardJson as any).supportedInterfaces;
    if (Array.isArray(v1)) {
        return v1
            .filter(x => x && typeof x === 'object')
            .map((x: any) => ({
                url: String(x.url ?? ''),
                protocolBinding: String(x.protocolBinding ?? 'JSONRPC'),
                protocolVersion: String(x.protocolVersion ?? '1.0'),
            }));
    }
    // v0.3 fallback — surface the legacy `url` so admins can see what's
    // stored without switching to the Advanced tab.
    const v03Url = (cardJson as any).url;
    if (typeof v03Url === 'string' && v03Url.length > 0) {
        return [
            {
                url: v03Url,
                protocolBinding:
                    String((cardJson as any).preferredTransport || 'JSONRPC'),
                protocolVersion: String((cardJson as any).protocolVersion || '0.3'),
            },
        ];
    }
    return [];
}

function extractExtensions(cardJson: Record<string, unknown> | undefined): ExtensionDraft[] {
    if (!cardJson) return [];
    const caps = (cardJson as any).capabilities;
    if (!caps || typeof caps !== 'object' || Array.isArray(caps)) return [];
    const arr = (caps).extensions;
    if (!Array.isArray(arr)) return [];
    return arr
        .filter(x => x && typeof x === 'object')
        .map((x: any) => ({
            uri: String(x.uri ?? ''),
            description: String(x.description ?? ''),
            required: !!x.required,
        }));
}

function composeCardJson(
    base: Record<string, unknown>,
    interfaces: InterfaceDraft[],
    extensions: ExtensionDraft[],
    capabilities: CapabilityDraft[],
): Record<string, unknown> {
    const out: Record<string, unknown> = { ...(base || {}) };
    if (interfaces.length > 0) {
        out.supportedInterfaces = interfaces.filter(i => i.url).map(i => ({
            url: i.url,
            protocolBinding: i.protocolBinding,
            protocolVersion: i.protocolVersion,
        }));
        // Remove v0.3 legacy top-level fields now that we have the v1.0
        // form. Keeping both would confuse the v0.3→v1.0 translator.
        delete (out as any).url;
        delete (out as any).additionalInterfaces;
        delete (out as any).preferredTransport;
        delete (out as any).protocolVersion;
    }
    if (extensions.length > 0) {
        const caps = (out.capabilities && typeof out.capabilities === 'object' && !Array.isArray(out.capabilities)
            ? { ...(out.capabilities as Record<string, unknown>) }
            : {}) as Record<string, unknown>;
        caps.extensions = extensions.filter(e => e.uri).map(e => ({
            uri: e.uri,
            description: e.description || undefined,
            required: e.required || undefined,
        }));
        out.capabilities = caps;
    }
    // Capabilities — the proxy re-reads the one-to-many rows from the
    // denormalised table, but we still mirror into card_json so the
    // discovery endpoint reflects the full declaration.
    if (capabilities.length > 0) {
        const existing =
            (Array.isArray((out as any).capabilitiesList) && (out as any).capabilitiesList) || [];
        void existing;
        // Some v0.3 tenants kept capabilities as an array on the top
        // level. When we spot that, preserve the array shape; otherwise
        // attach a structured capability list under
        // `capabilitiesList` alongside the v1.0 `capabilities` object.
        const legacyCapsArray = Array.isArray((out as any).capabilities);
        const mapped = capabilities
            .filter(c => c.name.trim().length > 0)
            .map(c => {
                const entry: Record<string, unknown> = {
                    name: c.name,
                    description: c.description || undefined,
                    handler_type: c.handler_type || undefined,
                    handler_ref: c.handler_type === 'skill' ? c.handler_ref || undefined : undefined,
                    timeout_secs:
                        c.timeout_secs === '' ? undefined : c.timeout_secs,
                    rate_limit_per_hour:
                        c.rate_limit_per_hour === ''
                            ? undefined
                            : c.rate_limit_per_hour,
                    data_classification:
                        c.data_classification.length > 0
                            ? c.data_classification
                            : undefined,
                };
                // Strip undefined keys so the JSON stays tidy.
                Object.keys(entry).forEach(k => {
                    if (entry[k] === undefined) delete entry[k];
                });
                return entry;
            });
        if (legacyCapsArray) {
            (out as any).capabilities = mapped;
        } else {
            (out as any).capabilitiesList = mapped;
        }
    }
    return out;
}

function formatDate(raw?: string | null): string {
    if (!raw) return '';
    try {
        return new Date(raw).toLocaleString();
    } catch {
        return raw;
    }
}

export default AgentCardEditDialog;
// Silence unused-import guard for the shared type — keeps typechecker
// honest across edits where the denormalised rows are only referenced
// in tests or downstream imports.
export type { AgentCardCapabilityResponse };
