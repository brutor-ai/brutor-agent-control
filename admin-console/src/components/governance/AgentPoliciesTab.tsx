/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Agent Policies (Governance → Agent Policies) — the authz policy for each
 * agent identity: per-action GRANTS (allow / deny / approval_required) enforced
 * by the Rust runtime engine ("actions, not access", default-deny, strictest-
 * wins with the resource-group ACL). Identities themselves are defined under
 * Resources → Agents → Identities.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    Bot, Plus, Trash2, RefreshCw, ShieldOff, ArrowRight,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import InfoBanner from '../common/InfoBanner';
import {
    AgentIdentity, AgentGrant,
    GRANT_ACTION_TYPES, GRANT_EFFECTS, GrantActionType, GrantEffect,
} from '../../lib/agentIdentityTypes';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { errorMessage } from '../../lib/narrow';

const effectClass = (e: string) =>
    e === 'deny' ? 'text-red-600' : e === 'approval_required' ? 'text-yellow-600' : 'text-green-700';

const AgentPoliciesTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    const toast = useToast();

    const [agents, setAgents] = useState<AgentIdentity[]>([]);
    const [loading, setLoading] = useState(true);
    const [selected, setSelected] = useState<AgentIdentity | null>(null);
    const [grants, setGrants] = useState<AgentGrant[]>([]);
    const [grantsLoading, setGrantsLoading] = useState(false);

    // New-grant row state.
    const [actionType, setActionType] = useState<GrantActionType>('llm_call');
    const [target, setTarget] = useState('*');
    const [effect, setEffect] = useState<GrantEffect>('allow');

    const load = useCallback(async () => {
        if (!gatewayClient) return;
        setLoading(true);
        try {
            const r = await gatewayClient.listAgentIdentities();
            const list: AgentIdentity[] = r.agents || [];
            setAgents(list);
            // Keep the selection if still present, else select the first.
            setSelected((cur: AgentIdentity | null) => (cur && list.some((a) => a.id === cur.id))
                ? list.find((a) => a.id === cur.id) || null
                : list[0] || null);
        } catch (err) { toast.error('Failed to load agent identities', errorMessage(err, '') || undefined); }
        finally { setLoading(false); }
    }, [gatewayClient, toast]);

    const loadGrants = useCallback(async (agentId: string) => {
        if (!gatewayClient) return;
        setGrantsLoading(true);
        try {
            const g = await gatewayClient.listAgentGrants(agentId);
            setGrants(g.grants || []);
        } catch (err) { toast.error('Failed to load grants', errorMessage(err, '') || undefined); }
        finally { setGrantsLoading(false); }
    }, [gatewayClient, toast]);

    useEffect(() => { runBackground(load(), 'load'); }, [load]);
    useEffect(() => { if (selected) runBackground(loadGrants(selected.id), 'load grants'); else setGrants([]); }, [selected, loadGrants]);

    const addGrant = async () => {
        if (!gatewayClient || !selected) return;
        try {
            await gatewayClient.addAgentGrant(selected.id, { action_type: actionType, target: target || '*', effect });
            toast.success('Grant added');
            setTarget('*');
            await loadGrants(selected.id);
        } catch (err) { toast.error('Add grant failed', errorMessage(err, '') || undefined); }
    };

    const delGrant = async (gid: string) => {
        if (!gatewayClient || !selected) return;
        try { await gatewayClient.deleteAgentGrant(selected.id, gid); await loadGrants(selected.id); }
        catch (err) { toast.error('Delete failed', errorMessage(err, '') || undefined); }
    };

    const toggleEnforcement = async () => {
        if (!gatewayClient || !selected) return;
        const next = selected.enforcement_mode === 'dry_run' ? 'enforce' : 'dry_run';
        try {
            await gatewayClient.updateAgentIdentity(selected.id, { enforcement_mode: next });
            toast.success(`Now ${next}`);
            await load();
        } catch (err) { toast.error('Update failed', errorMessage(err, '') || undefined); }
    };

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="p-2.5 rounded-xl bg-primary-100">
                        <Bot className="w-7 h-7 text-primary-600" />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">Agent Policies</h1>
                        <p className="text-sm text-muted">
                            Per-action grants for each agent identity — enforced at runtime.
                        </p>
                    </div>
                </div>
                <button onClick={asyncHandler(load, 'click')} className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors" title="Refresh">
                    <RefreshCw className={`w-5 h-5 ${loading ? 'animate-spin' : ''}`} />
                </button>
            </div>

            {/* How it works */}
            <InfoBanner title="How agent policies work">
                <p>
                    A policy is a set of <strong>grants</strong> on an agent identity — "<em>actions, not
                    access</em>". Each grant says an <strong>action type</strong> (LLM call, MCP tool, A2A
                    call, skill exec) on a <strong>target</strong> (glob; <code>*</code> = any) is{' '}
                    <span className="text-green-700 font-medium">allowed</span>,{' '}
                    <span className="text-red-600 font-medium">denied</span>, or{' '}
                    <span className="text-yellow-600 font-medium">approval-required</span>. With{' '}
                    <strong>default-deny</strong> on the identity, anything without a matching grant is
                    refused; <em>deny</em> always beats <em>allow</em>, and the agent must also pass its
                    resource-group ACL (strictest-wins).
                </p>
                <p className="mt-2">
                    Roll out safely with the identity's <strong>dry-run</strong> mode (logs would-block
                    verdicts without blocking), then flip to <strong>enforce</strong>. Identities are
                    defined under <strong>Resources → Agents → Identities</strong>.
                </p>
            </InfoBanner>

            {/* Master / detail */}
            {loading ? (
                <div className="flex items-center justify-center py-16"><RefreshCw className="w-6 h-6 animate-spin text-primary-600" /></div>
            ) : agents.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    <Bot className="w-12 h-12 text-disabled mx-auto mb-3" />
                    <h3 className="text-lg font-semibold text-secondary mb-1">No agent identities</h3>
                    <p className="text-sm text-muted">
                        Define an agent under <strong>Resources → Agents → Identities</strong>, then grant it actions here.
                    </p>
                </div>
            ) : (
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                    {/* Agent list */}
                    <div className="bg-surface border border-default rounded-lg overflow-hidden">
                        <div className={`${TABLE_TH_TYPE} px-4 py-2.5 bg-surface-alt border-b border-default`}>
                            Agents ({agents.length})
                        </div>
                        <div className="divide-y divide-default max-h-[28rem] overflow-y-auto">
                            {agents.map((a) => {
                                const active = selected?.id === a.id;
                                return (
                                    <button
                                        key={a.id}
                                        onClick={() => setSelected(a)}
                                        className={`w-full text-left px-4 py-3 hover:bg-surface-alt transition-colors ${active ? 'bg-primary-50' : ''}`}
                                    >
                                        <div className="flex items-center justify-between">
                                            <span className="font-medium text-default truncate">{a.name}</span>
                                            <span className={`text-[11px] px-1.5 py-0.5 rounded font-semibold ${a.enforcement_mode === 'dry_run' ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'}`}>
                                                {a.enforcement_mode === 'dry_run' ? 'dry-run' : 'enforce'}
                                            </span>
                                        </div>
                                        <div className="text-xs text-muted mt-0.5">
                                            {a.default_deny ? 'default-deny' : 'allow-through'}
                                            {!(a.enabled && a.status === 'active') && ' · revoked'}
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {/* Grants for selected agent */}
                    <div className="lg:col-span-2 bg-surface border border-default rounded-lg">
                        {!selected ? (
                            <div className="p-8 text-center text-muted text-sm">Select an agent to manage its grants.</div>
                        ) : (
                            <div className="p-5 space-y-5">
                                <div className="flex items-center justify-between">
                                    <div>
                                        <h3 className="font-semibold text-default flex items-center gap-2">
                                            <Bot className="w-4 h-4 text-primary-600" /> {selected.name}
                                        </h3>
                                        <p className="text-xs text-muted">{selected.description || selected.id}</p>
                                    </div>
                                    <div className="flex items-center gap-2 text-xs">
                                        <span className={selected.default_deny ? 'text-red-600' : 'text-muted'}>
                                            {selected.default_deny ? 'default-deny' : 'allow-through'}
                                        </span>
                                        <button
                                            onClick={asyncHandler(toggleEnforcement, 'click')}
                                            title="Toggle enforce / dry-run"
                                            className={`px-2 py-0.5 rounded font-semibold ${selected.enforcement_mode === 'dry_run' ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'}`}
                                        >
                                            {selected.enforcement_mode === 'dry_run' ? 'dry-run' : 'enforce'}
                                        </button>
                                    </div>
                                </div>

                                {/* Add grant */}
                                <div className="flex flex-wrap items-end gap-2">
                                    <select value={actionType} onChange={e => setActionType(e.target.value as GrantActionType)} className="border border-strong rounded px-2 py-1.5 text-sm bg-surface">
                                        {GRANT_ACTION_TYPES.map(a => <option key={a} value={a}>{a}</option>)}
                                    </select>
                                    <input value={target} onChange={e => setTarget(e.target.value)} placeholder="target (glob, * = any)" className="border border-strong rounded px-2 py-1.5 text-sm bg-surface flex-1 min-w-[140px] min-h-0 min-w-0" />
                                    <select value={effect} onChange={e => setEffect(e.target.value as GrantEffect)} className="border border-strong rounded px-2 py-1.5 text-sm bg-surface">
                                        {GRANT_EFFECTS.map(e2 => <option key={e2} value={e2}>{e2}</option>)}
                                    </select>
                                    <button onClick={asyncHandler(addGrant, 'click')} className="px-3 py-1.5 text-sm text-white bg-primary-500 hover:bg-primary-600 rounded flex items-center gap-1"><Plus className="w-3 h-3" /> Add grant</button>
                                </div>

                                {/* Grant list */}
                                <div className="space-y-1">
                                    {grantsLoading ? (
                                        <div className="flex items-center justify-center py-6"><RefreshCw className="w-5 h-5 animate-spin text-primary-600" /></div>
                                    ) : grants.length === 0 ? (
                                        <p className="text-xs text-muted py-2">
                                            <ShieldOff className="w-3.5 h-3.5 inline mr-1" />
                                            No grants — with default-deny this agent can do nothing.
                                        </p>
                                    ) : grants.map(g => (
                                        <div key={g.id} className="flex items-center justify-between text-sm border-b border-default py-1.5">
                                            <span className="text-secondary">
                                                <code className="text-xs">{g.action_type}</code>{' '}
                                                <ArrowRight className="w-3 h-3 inline text-muted" />{' '}
                                                <code className="text-xs">{g.target}</code>
                                                {g.constraints && Object.keys(g.constraints).length > 0 && (
                                                    <span className="ml-2 text-[11px] text-muted font-mono">
                                                        {JSON.stringify(g.constraints)}
                                                    </span>
                                                )}
                                            </span>
                                            <span className="flex items-center gap-3">
                                                <span className={`text-xs font-medium ${effectClass(g.effect)}`}>{g.effect}</span>
                                                <button onClick={asyncHandler(() => delGrant(g.id), 'click')} className="text-red-500 hover:text-red-700"><Trash2 className="w-4 h-4" /></button>
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
};

export default AgentPoliciesTab;
