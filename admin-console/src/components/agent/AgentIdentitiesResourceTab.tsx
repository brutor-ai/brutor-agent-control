/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Agent Identities (Resources → Agents → Identities) — DEFINE agents as
 * governable principals: name, owner, IdP subject, source, default-deny
 * baseline, enforcement mode, and resource-group memberships. This is the
 * definition surface (like Agent Cards). Per-action GRANTS (the authz policy)
 * live under Governance → Agent Policies.
 *
 * A single "Edit" action opens one dialog (Agent-Cards form pattern) covering
 * the identity fields, its Implementation (declared name, approved
 * versions, require-release, release timeline) and its resource-group
 * memberships. `a2a-identities:<id>:implementation` opens that dialog on the
 * Implementation section — where the inbox's agent findings link.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import { Bot, Plus, ShieldOff, RefreshCw, X, Pencil, Loader2, ArrowRight } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import InfoBanner from '../common/InfoBanner';
import HierarchicalGroupSelector from '../common/HierarchicalGroupSelector';
import { GroupHierarchyTree } from '../../lib/groupTypes';
import { AgentIdentity, AgentImplementationDeclaration, AgentMembership, AgentSource } from '../../lib/agentIdentityTypes';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { declarationOf, implementationNameError, normaliseApprovedVersions } from '../../lib/agentRelease';
import { errorMessage } from '../../lib/narrow';
import { agentIdentityStanding, expiryFieldValue, expiryFromField } from '../../lib/agentIdentityStanding';
import AgentImplementationSection from './AgentImplementationSection';
import { useReadFailures } from '../../hooks/useRead';
import type { ReadFailure } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

const SOURCES: AgentSource[] = ['manual', 'idp', 'discovered'];

export interface AgentIdentitiesResourceTabProps {
    /** Deep link: open this identity's dialog once the list has loaded. */
    initialAgentId?: string;
    /** `implementation` scrolls the dialog to the Implementation section. */
    initialSection?: string;
}

const AgentIdentitiesResourceTab: React.FC<AgentIdentitiesResourceTabProps> = ({ initialAgentId, initialSection }) => {
    const { gatewayClient, fetchGroupHierarchyTree } = useGateway();
    const toast = useToast();

    const [agents, setAgents] = useState<AgentIdentity[]>([]);
    const [loading, setLoading] = useState(true);
    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);
    const [readFailure, reads] = useReadFailures<'groups'>();
    // The single dialog. `null` = closed; { agent: null } = create; else edit.
    const [dialog, setDialog] = useState<{ agent: AgentIdentity | null; focusImplementation?: boolean } | null>(null);
    // Deep link (the inbox's agent findings): the linked identity's dialog is
    // open until the operator closes it or opens another one — derived, not
    // copied into state by an effect.
    const [linkDismissed, setLinkDismissed] = useState(false);
    // The browser's clock when the list was last read: an identity's expiry
    // is judged against it, because nothing changes the stored status when
    // the expiry passes (`agentIdentityStanding`).
    const [readAt, setReadAt] = useState(() => Date.now());

    const load = useCallback(async () => {
        if (!gatewayClient) return;
        setLoading(true);
        try {
            const r = await gatewayClient.listAgentIdentities();
            setAgents(r.agents || []);
            setReadAt(Date.now());
        } catch (err) { toast.error('Failed to load agent identities', errorMessage(err, '') || undefined); }
        finally { setLoading(false); }
    }, [gatewayClient, toast]);

    const loadGroupTree = useCallback(async () => {
        try {
            const r = await fetchGroupHierarchyTree();
            setGroupTree(r.tree || []);
            reads.loaded('groups');
        } catch (err) {
            reads.failed('groups', 'Failed to load the resource groups', err);
        }
    }, [fetchGroupHierarchyTree, reads]);

    useEffect(() => { runBackground(load(), 'async work'); runBackground(loadGroupTree(), 'group tree'); }, [load, loadGroupTree]);

    const linked = !linkDismissed && initialAgentId ? agents.find((a) => a.id === initialAgentId) : undefined;
    const openDialog = dialog ?? (linked ? { agent: linked, focusImplementation: initialSection === 'implementation' } : null);
    const openFor = (next: { agent: AgentIdentity | null } | null) => {
        setLinkDismissed(true);
        setDialog(next);
    };

    const revoke = async (a: AgentIdentity) => {
        if (!gatewayClient) return;
        try { await gatewayClient.revokeAgentIdentity(a.id); toast.success('Agent revoked'); await load(); }
        catch (err) { toast.error('Revoke failed', errorMessage(err, '') || undefined); }
    };

    const toggleEnforcement = async (a: AgentIdentity) => {
        if (!gatewayClient) return;
        const next = a.enforcement_mode === 'dry_run' ? 'enforce' : 'dry_run';
        try { await gatewayClient.updateAgentIdentity(a.id, { enforcement_mode: next }); toast.success(`Now ${next}`); await load(); }
        catch (err) { toast.error('Update failed', errorMessage(err, '') || undefined); }
    };

    return (
        <div className="app-column h-full">
            <div className="flex-shrink-0 px-6 py-4 bg-surface border-b border-default flex items-center justify-between">
                <div>
                    <h1 className="text-xl font-bold text-default flex items-center gap-2">
                        <Bot className="w-5 h-5 text-primary-600" /> Agent Identities
                    </h1>
                    <p className="text-sm text-muted">
                        Define agents as governable principals. Their per-action grants live under <strong>Governance → Agent Policies</strong>.
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <button onClick={asyncHandler(load, 'click')} className="flex items-center gap-1.5 px-3 py-2 text-sm text-secondary bg-surface border border-default rounded-lg hover:bg-surface-alt">
                        <RefreshCw className="w-4 h-4" /> Refresh
                    </button>
                    <button onClick={() => openFor({ agent: null })} className="flex items-center gap-1.5 px-3 py-2 text-sm text-white bg-primary-500 hover:bg-primary-600 rounded-lg">
                        <Plus className="w-4 h-4" /> New Agent
                    </button>
                </div>
            </div>

            <div className="scroll-region p-6 space-y-4">
                <InfoBanner title="What an agent identity is">
                    <p>
                        An agent identity is the <strong>security principal</strong> Brutor governs at runtime — distinct from an{' '}
                        <strong>Agent Card</strong> (the A2A capability advertisement other agents discover and call). The
                        card says <em>"here's what I expose"</em>; the identity says <em>"here's who this agent is and what it
                        may do"</em>. Issuance stays with your IdP; Brutor decides, enforces, and proves against the identity.
                        Configure each identity's allowed actions in <strong>Governance → Agent Policies</strong>.
                    </p>
                </InfoBanner>

                {loading ? (
                    <div className="flex items-center justify-center h-40"><RefreshCw className="w-6 h-6 animate-spin text-primary-600" /></div>
                ) : agents.length === 0 ? (
                    <div className="text-center py-16 text-secondary">
                        <Bot className="w-12 h-12 text-disabled mx-auto mb-3" />
                        <p>No agent identities yet. Create one, then grant it actions under Agent Policies.</p>
                    </div>
                ) : (
                    <div className="bg-surface rounded-lg border border-default">
                        <div className={`${TABLE_TH_TYPE} hidden lg:flex items-center px-4 py-2.5 bg-surface-alt border-b border-default`}>
                            <div className="flex-1 min-h-0 min-w-0">Agent</div>
                            <div className="w-32">Source</div>
                            <div className="w-28">Default</div>
                            <div className="w-24">Mode</div>
                            <div className="w-24">Status</div>
                            <div className="w-40 text-right">Actions</div>
                        </div>
                        {agents.map(a => {
                            const standing = agentIdentityStanding(a, readAt);
                            return (
                            <div key={a.id} className="flex items-center px-4 py-3 border-b border-default last:border-0 hover:bg-surface-alt">
                                <button className="flex-1 text-left min-h-0 min-w-0" onClick={() => openFor({ agent: a })}>
                                    <div className="font-medium text-default">{a.name}</div>
                                    <div className="text-xs text-muted">{a.description || a.id}</div>
                                    {declarationOf(a).implementation_name && (
                                        <div className="text-xs text-muted" data-testid={`agent-impl-name-${a.id}`}>
                                            implementation <code className="font-mono">{declarationOf(a).implementation_name}</code>
                                        </div>
                                    )}
                                </button>
                                <div className="w-32 text-xs text-secondary">{a.source}</div>
                                <div className="w-28 text-xs">
                                    <span className={a.default_deny ? 'text-red-600' : 'text-muted'}>
                                        {a.default_deny ? 'deny' : 'allow-through'}
                                    </span>
                                </div>
                                <div className="w-24">
                                    <button
                                        onClick={asyncHandler(() => toggleEnforcement(a), 'click')}
                                        title="Click to toggle enforce/dry-run"
                                        className={`text-xs px-2 py-0.5 rounded font-semibold ${a.enforcement_mode === 'dry_run' ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'}`}
                                    >
                                        {a.enforcement_mode === 'dry_run' ? 'dry-run' : 'enforce'}
                                    </button>
                                </div>
                                <div className="w-24">
                                    <span
                                        data-testid={`agent-status-${a.id}`}
                                        title={standing.detail ?? undefined}
                                        className={`text-xs px-2 py-0.5 rounded-full ${
                                            standing.state === 'active'
                                                ? 'bg-green-100 text-green-700'
                                                : standing.state === 'expired'
                                                  ? 'bg-amber-100 text-amber-800'
                                                  : 'bg-red-100 text-red-700'
                                        }`}
                                    >
                                        {standing.label}
                                    </span>
                                </div>
                                <div className="w-40 flex items-center justify-end gap-2">
                                    <button onClick={() => openFor({ agent: a })} className="flex items-center gap-1 px-2 py-1 text-xs border border-strong rounded text-secondary hover:bg-surface-alt">
                                        <Pencil className="w-3.5 h-3.5" /> Edit
                                    </button>
                                    {a.enabled && a.status === 'active' && (
                                        <button onClick={asyncHandler(() => revoke(a), 'click')} title="Revoke" className="px-2 py-1 text-xs border border-strong rounded text-red-600 hover:bg-red-50 flex items-center gap-1">
                                            <ShieldOff className="w-3 h-3" /> Revoke
                                        </button>
                                    )}
                                </div>
                            </div>
                            );
                        })}
                    </div>
                )}
            </div>

            {openDialog && (
                <AgentEditDialog
                    key={openDialog.agent?.id ?? 'new'}
                    agent={openDialog.agent}
                    focusImplementation={openDialog.focusImplementation}
                    groupTree={groupTree}
                    groupsFailure={readFailure('groups')}
                    onRetryGroups={() => runBackground(loadGroupTree(), 'group tree')}
                    onClose={() => openFor(null)}
                    onSaved={asyncHandler(async () => { openFor(null); await load(); }, 'saved')}
                />
            )}
        </div>
    );
};

// ── Single create/edit dialog: identity fields + memberships ──────────────
const AgentEditDialog: React.FC<{
    agent: AgentIdentity | null;
    focusImplementation?: boolean;
    groupTree: GroupHierarchyTree[];
    groupsFailure: ReadFailure | null;
    onRetryGroups: () => void;
    onClose: () => void;
    onSaved: () => void;
}> = ({ agent, focusImplementation, groupTree, groupsFailure, onRetryGroups, onClose, onSaved }) => {
    const [readFailure, reads] = useReadFailures<'memberships'>();
    const [membershipsAsked, setMembershipsAsked] = useState(0);
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const isEdit = agent !== null;

    const [name, setName] = useState(agent?.name ?? '');
    const [description, setDescription] = useState(agent?.description ?? '');
    const [ownerLabel, setOwnerLabel] = useState(agent?.owner_label ?? '');
    const [source, setSource] = useState<AgentSource>(agent?.source ?? 'manual');
    const [idpSubject, setIdpSubject] = useState(agent?.idp_subject ?? '');
    const [defaultDeny, setDefaultDeny] = useState(agent?.default_deny ?? true);
    const [enforcementMode, setEnforcementMode] = useState<'enforce' | 'dry_run'>(agent?.enforcement_mode ?? 'enforce');
    // The expiry, as a date and time in the reader's own zone; '' for none.
    // `savedExpiry` is what the field opened on: the expiry is sent only
    // when the administrator changed it, so a save of something else does
    // not write the stored instant again.
    const [savedExpiry] = useState(() => expiryFieldValue(agent?.expires_at));
    const [expiry, setExpiry] = useState(savedExpiry);
    // The implementation declaration (part of every member
    // system's contract closure). `saved` is what the contracts sign today.
    const [savedImpl] = useState<AgentImplementationDeclaration>(() => declarationOf(agent));
    const [impl, setImpl] = useState<AgentImplementationDeclaration>(savedImpl);
    const implValid = implementationNameError(impl.implementation_name ?? '') === null;

    // Memberships are a field on this form. Seed from the agent (edit mode); the
    // map group_id → membership-id lets us remove dropped ones on save.
    const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>([]);
    const [membershipMap, setMembershipMap] = useState<Record<string, number>>({});
    const [membersLoading, setMembersLoading] = useState(isEdit);
    const [saving, setSaving] = useState(false);
    // Judged when the dialog opens, on the browser's clock.
    const [openedAt] = useState(() => Date.now());
    const standing = agent ? agentIdentityStanding(agent, openedAt) : null;
    // Whether the time in the field has passed, on the same clock.
    const expiryInstant = expiryFromField(expiry);
    const expiryPassed = expiryInstant !== null && Date.parse(expiryInstant) <= openedAt;

    useEffect(() => {
        if (!isEdit || !gatewayClient || !agent) { setMembersLoading(false); return; }
        let cancelled = false;
        runBackground((async () => {
            try {
                const m = await gatewayClient.listAgentMemberships(agent.id);
                if (cancelled) return;
                const rows: AgentMembership[] = m.memberships || [];
                setMembershipMap(Object.fromEntries(rows.map((r) => [r.group_id, r.id])));
                setSelectedGroupIds(rows.map((r) => r.group_id));
                reads.loaded('memberships');
            } catch (err) {
                if (!cancelled) reads.failed('memberships', 'Failed to load the memberships of this agent', err);
            }
            finally { if (!cancelled) setMembersLoading(false); }
        })(), 'async work');
        return () => { cancelled = true; };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [membershipsAsked]);

    const reconcileMemberships = async (agentId: string) => {
        if (!gatewayClient) return;
        const current = Object.keys(membershipMap);
        const toAdd = selectedGroupIds.filter((id) => !(id in membershipMap));
        const toRemove = current.filter((id) => !selectedGroupIds.includes(id));
        await Promise.all([
            ...toAdd.map((id) => gatewayClient.addAgentMembership(agentId, { group_id: id })),
            ...toRemove.map((id) => gatewayClient.removeAgentMembership(agentId, membershipMap[id])),
        ]);
    };

    const implementationBody = () => ({
        implementation_name: impl.implementation_name,
        approved_versions: normaliseApprovedVersions(impl.approved_versions),
        require_release: impl.require_release,
    });

    const save = async () => {
        if (!gatewayClient || !name.trim() || !implValid) return;
        setSaving(true);
        try {
            if (!isEdit) {
                const created = await gatewayClient.createAgentIdentity({
                    name: name.trim(),
                    description: description || undefined,
                    owner_label: ownerLabel || undefined,
                    source,
                    idp_subject: idpSubject || undefined,
                    default_deny: defaultDeny,
                    enforcement_mode: enforcementMode,
                    // Left out, the identity does not expire.
                    ...(expiryFromField(expiry) ? { expires_at: expiryFromField(expiry) } : {}),
                    ...implementationBody(),
                });
                if (created?.id && selectedGroupIds.length) {
                    await Promise.all(selectedGroupIds.map((id) =>
                        gatewayClient.addAgentMembership(created.id, { group_id: id })));
                }
                toast.success('Agent created');
            } else {
                await gatewayClient.updateAgentIdentity(agent.id, {
                    name: name.trim(),
                    description: description || null,
                    owner_label: ownerLabel || null,
                    idp_subject: idpSubject || null,
                    default_deny: defaultDeny,
                    enforcement_mode: enforcementMode,
                    // A later time renews the identity; null clears the expiry.
                    ...(expiry !== savedExpiry ? { expires_at: expiryFromField(expiry) } : {}),
                    ...implementationBody(),
                });
                await reconcileMemberships(agent.id);
                toast.success('Agent updated');
            }
            onSaved();
        } catch (e: unknown) {
            // A 422 carries the server's reason (an invalid version pattern).
            toast.error(errorMessage(e, isEdit ? 'Update failed' : 'Create failed (name may already exist)'));
        }
        finally { setSaving(false); }
    };

    const inputCls = 'mt-1 w-full border border-strong rounded-lg px-3 py-2 bg-surface text-sm';

    return (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default flex-shrink-0">
                    <h2 className="text-lg font-semibold text-default flex items-center gap-2">
                        <Bot className="w-5 h-5 text-primary-600" />
                        {isEdit ? `Edit Agent Identity — ${agent.name}` : 'New Agent Identity'}
                    </h2>
                    <button onClick={onClose} className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg"><X className="w-5 h-5" /></button>
                </div>

                {/* Body */}
                <div className="scroll-region px-6 py-5 space-y-5">
                    {/* The expiry, as the server holds it, and what it means once passed */}
                    {standing?.state === 'expired' && (
                        <p className="p-3 rounded-lg bg-amber-50 border border-amber-200 text-sm text-amber-900" data-testid="agent-expired-note">
                            {standing.detail}
                        </p>
                    )}
                    {standing && standing.state !== 'expired' && agent?.expires_at && (
                        <p className="text-xs text-muted" data-testid="agent-expiry">
                            Expires {agent.expires_at}. From then on the gateway refuses every request made with this identity.
                        </p>
                    )}

                    {/* Identity fields */}
                    <div className="space-y-3">
                        <label className="block text-sm">
                            <span className="text-secondary">Name</span>
                            <input value={name} onChange={e => setName(e.target.value)} className={inputCls} placeholder="billing-bot" />
                        </label>
                        <label className="block text-sm">
                            <span className="text-secondary">Description</span>
                            <input value={description ?? ''} onChange={e => setDescription(e.target.value)} className={inputCls} />
                        </label>
                        <label className="block text-sm">
                            <span className="text-secondary">Owner</span>
                            <input value={ownerLabel ?? ''} onChange={e => setOwnerLabel(e.target.value)} className={inputCls} placeholder="RevOps" />
                        </label>
                        <div className="grid grid-cols-2 gap-3">
                            <label className="block text-sm">
                                <span className="text-secondary">Source</span>
                                <select value={source} onChange={e => setSource(e.target.value as AgentSource)} className={inputCls}>
                                    {SOURCES.map(s => <option key={s} value={s}>{s}</option>)}
                                </select>
                            </label>
                            <label className="block text-sm">
                                <span className="text-secondary">Enforcement</span>
                                <select value={enforcementMode} onChange={e => setEnforcementMode(e.target.value as 'enforce' | 'dry_run')} className={inputCls}>
                                    <option value="enforce">enforce</option>
                                    <option value="dry_run">dry-run (shadow)</option>
                                </select>
                            </label>
                        </div>
                        <label className="block text-sm">
                            <span className="text-secondary">IdP subject (optional)</span>
                            <input value={idpSubject ?? ''} onChange={e => setIdpSubject(e.target.value)} className={`${inputCls} font-mono`} placeholder="spiffe://brutor/ns/agents/billing" />
                        </label>
                        <div className="text-sm">
                            <label htmlFor="agent-expires-at" className="text-secondary">Expires</label>
                            <div className="flex items-center gap-2">
                                <input
                                    id="agent-expires-at"
                                    type="datetime-local"
                                    value={expiry}
                                    onChange={e => setExpiry(e.target.value)}
                                    className={inputCls}
                                />
                                {expiry && (
                                    <button type="button" onClick={() => setExpiry('')} className="mt-1 px-3 py-2 text-sm border border-strong rounded-lg text-secondary whitespace-nowrap">
                                        No expiry
                                    </button>
                                )}
                            </div>
                            <p className="mt-1 text-xs text-muted" data-testid="agent-expiry-rule">
                                Leave empty for an identity that does not expire. The gateway refuses an identity that is past its expiry at the next request. To renew an expired identity, set a later time or clear the field.
                            </p>
                            {expiryPassed && (
                                <p className="mt-1 text-xs text-amber-700" data-testid="agent-expiry-passed">
                                    This time has passed. Saved like this, the gateway refuses this identity from its next request.
                                </p>
                            )}
                        </div>
                        <label className="flex items-center gap-2 text-sm text-secondary pt-1">
                            <input type="checkbox" checked={defaultDeny} onChange={e => setDefaultDeny(e.target.checked)} />
                            Default-deny (block any action without a matching grant — recommended)
                        </label>
                    </div>

                    {/* Implementation */}
                    <div className="pt-4 border-t border-default">
                        <AgentImplementationSection
                            agentId={agent?.id ?? null}
                            value={impl}
                            saved={savedImpl}
                            onChange={setImpl}
                            focus={focusImplementation}
                        />
                    </div>

                    {/* Memberships */}
                    <div className="pt-4 border-t border-default">
                        <h3 className="text-sm font-semibold text-default mb-1">Resource-group memberships</h3>
                        <p className="text-xs text-muted mb-3">
                            Membership applies a group's ACLs, limits, and approval rules to this agent.
                        </p>
                        {membersLoading ? (
                            <div className="flex items-center gap-2 text-sm text-muted py-3"><Loader2 className="w-4 h-4 animate-spin" /> Loading memberships…</div>
                        ) : readFailure('memberships') ? (
                            // Without the memberships the selector would show none and a save would add to what is there.
                            <ReadFailureNotice compact failure={readFailure('memberships')} onRetry={() => setMembershipsAsked((n) => n + 1)} />
                        ) : groupsFailure && groupTree.length === 0 ? (
                            <ReadFailureNotice compact failure={groupsFailure} onRetry={onRetryGroups} />
                        ) : groupTree.length === 0 ? (
                            <p className="text-xs text-muted py-2">No resource groups defined yet.</p>
                        ) : (
                            <HierarchicalGroupSelector
                                groupTree={groupTree}
                                selectedGroupIds={selectedGroupIds}
                                onSelectionChange={setSelectedGroupIds}
                            />
                        )}
                    </div>

                    {/* Grants pointer */}
                    <div className="bg-surface-alt border border-default rounded-lg p-3 text-xs text-muted flex items-center gap-2">
                        <ArrowRight className="w-3.5 h-3.5 flex-shrink-0" />
                        Configure this agent's allowed actions (grants) under <strong className="mx-1">Governance → Agent Policies</strong>.
                    </div>
                </div>

                {/* Footer */}
                <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-default flex-shrink-0">
                    <button onClick={onClose} disabled={saving} className="px-4 py-2 text-sm border border-strong rounded-lg text-secondary disabled:opacity-50">Cancel</button>
                    <button onClick={asyncHandler(save, 'click')} disabled={saving || !name.trim() || !implValid} className="flex items-center gap-2 px-4 py-2 text-sm text-white bg-primary-500 hover:bg-primary-600 rounded-lg disabled:opacity-50">
                        {saving && <Loader2 className="w-4 h-4 animate-spin" />}
                        {saving ? 'Saving…' : isEdit ? 'Save Changes' : 'Create'}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default AgentIdentitiesResourceTab;
