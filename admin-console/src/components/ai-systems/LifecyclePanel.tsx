/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Lifecycle tab — the system's stage, the gate in front of it, and the
 * operations that open the gate.
 *
 * The stage rail at the top is live: the current stage is marked, the gate
 * leaving it shows every requirement as met or unmet, and each unmet
 * requirement with a single fix carries the button that performs it. Mint
 * the contract here; set the owner or intended use inline; approving the
 * contract, setting liveness, attaching an impact assessment and running
 * replay hand off to the surface that owns them and come back with the
 * checklist refreshed.
 *
 * Below the rail sit the transitions themselves — every reachable stage with
 * its full checklist, the approver field when a human must be named, and
 * the succession question a deprecation asks. Replay renders beside this
 * panel on the tab, because a passing replay is what the approved → active
 * gate consumes.
 *
 * The gate shows its checklist before you click. A gate you can only
 * discover by failing it is a gate people resent and route around.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, ArrowRight, Check, RefreshCw, X } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import { AgentReleaseChip } from '../agent/AgentReleaseBadges';
import { agentImplementationLink, newReleaseOf } from '../../lib/agentRelease';
import { StageDiagram } from './ContractLifecycleDiagram';
import { useGateway } from '../../hooks/useGateway';
import { useAdminUsername } from '../../hooks/useAdminUsername';
import {
    AISystemContract,
    ContractListResponse,
    LifecycleEvent,
    LifecycleResponse,
    TransitionOption,
    SuccessorCandidate,
} from '../../lib/aiSystemTypes';
import { errorKind } from '../../lib/narrow';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

/** Where the Lifecycle tab sends an operator for a fix it does not own. */
export type LifecycleHandoff =
    | { section: 'signals' }
    | { section: 'contract'; sub: 'active' | 'envelope' | 'evidence' };

interface LifecyclePanelProps {
    aiSystemId: string;
    /** Open another tab on this system (liveness lives on Signals; evidence
     *  and the contract list on Contract). Optional so the panel renders
     *  standalone in tests. */
    onOpen?: (target: LifecycleHandoff) => void;
    /** Navigate elsewhere in the console (the group's Ownership fields). */
    setActiveTab?: (tab: string) => void;
}

/**
 * Gate requirements an operator can go and fix elsewhere, mapped to where
 * the field lives. An AI System *is* a resource group, so the
 * system id doubles as the group id and the link needs nothing else.
 */
const REQUIREMENT_FIX_LOCATION: Record<string, { section: string; label: string }> = {
    owner_set: { section: 'ownership', label: 'Open the ownership fields' },
    intended_use_set: { section: 'ownership', label: 'Open the ownership fields' },
};

/** Which contract an "Approve contract" gate action signs: the newest one
 *  still open (draft or active) that nobody has approved yet. */
function contractAwaitingApproval(list: ContractListResponse | null): AISystemContract | undefined {
    return list?.contracts.find((c) => !c.is_approved && c.status !== 'rejected' && c.status !== 'superseded');
}

export function LifecyclePanel({ aiSystemId, onOpen, setActiveTab }: LifecyclePanelProps) {
    const {
        fetchAISystemLifecycle,
        fetchAISystemContracts,
        mintAISystemContract,
        approveContract,
        transitionAISystemLifecycle,
        updateResourceGroup,
    } = useGateway();

    const currentAdmin = useAdminUsername();
    const [lifecycle, setLifecycle] = useState<LifecycleResponse | null>(null);
    const [contracts, setContracts] = useState<ContractListResponse | null>(null);
    const [readFailure, reads] = useReadFailures<'contracts'>();
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    // Inline editor for the two declarations the gate can ask for.
    const [editing, setEditing] = useState<'owner' | 'intended_use' | null>(null);
    const [draft, setDraft] = useState('');
    // The transition row a rail button pointed at: scrolled to, focused and
    // ringed for a moment, so "Name successor" visibly lands somewhere.
    const [focusStage, setFocusStage] = useState<string | null>(null);

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const [l, c] = await Promise.all([
                fetchAISystemLifecycle(aiSystemId),
                fetchAISystemContracts(aiSystemId).then(
                    (answer) => {
                        reads.loaded('contracts');
                        return answer;
                    },
                    (err: unknown) => {
                        reads.lost('contracts', 'Failed to load the contracts of this system', err);
                        return null;
                    },
                ),
            ]);
            setLifecycle(l);
            setContracts(c);
        } catch (err: unknown) {
            console.error('Failed to load lifecycle:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load lifecycle');
            setLifecycle(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchAISystemLifecycle, fetchAISystemContracts, reads]);

    useEffect(() => {
        void load();
    }, [load]);

    const run = async (key: string, fn: () => Promise<unknown>, after?: string) => {
        try {
            setBusy(key);
            setError('');
            setNotice('');
            await fn();
            if (after) setNotice(after);
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Operation failed');
        } finally {
            setBusy('');
        }
    };

    const transition = (stage: string, extra: Record<string, unknown> = {}) =>
        run(`stage:${stage}`, () =>
            transitionAISystemLifecycle(aiSystemId, { to_stage: stage, ...extra }),
        );

    /** Scroll a lower block into view — used when the fix is on this tab. */
    const reveal = (selector: string) => {
        const el = document.querySelector<HTMLElement>(selector);
        el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const focusable = el?.querySelector<HTMLElement>('input, select, button');
        focusable?.focus();
    };

    const onAction = (key: string, toStage: string) => {
        switch (key) {
            case '__back__':
                void transition(toStage);
                return;
            case 'contract_minted':
                void run(
                    'action:contract_minted',
                    async () => {
                        const result = await mintAISystemContract(aiSystemId);
                        if (!result.created) {
                            setNotice(
                                `Configuration already matches contract v${result.contract.version}; no new version was created.`,
                            );
                        }
                    },
                    'Contract minted from the current configuration.',
                );
                return;
            case 'owner_set':
                setEditing('owner');
                setDraft('');
                return;
            case 'intended_use_set':
                setEditing('intended_use');
                setDraft('');
                return;
            case 'approver_recorded': {
                const target = contractAwaitingApproval(contracts);
                if (!target) {
                    setNotice('Nothing to approve yet — mint a contract first.');
                    return;
                }
                void run(
                    'action:approver_recorded',
                    () => approveContract(target.id),
                    `Approval recorded on contract v${target.version}.`,
                );
                return;
            }
            case 'liveness_expectation_set':
                onOpen?.({ section: 'signals' });
                return;
            case 'impact_assessment_on_file':
                onOpen?.({ section: 'contract', sub: 'evidence' });
                return;
            case 'no_unapproved_widening':
                onOpen?.({ section: 'contract', sub: 'active' });
                return;
            case 'replay_passed':
                reveal('[data-testid="replay-panel"]');
                return;
            case 'successor_named':
            case 'human_approval':
                setFocusStage(toStage);
                return;
            default:
                return;
        }
    };

    const saveDeclaration = () =>
        run(
            `action:${editing}`,
            async () => {
                if (!editing) return;
                await updateResourceGroup(aiSystemId, editing === 'owner' ? { owner: draft.trim() } : { intended_use: draft.trim() });
                setEditing(null);
            },
            editing === 'owner' ? 'Owner recorded.' : 'Intended use recorded.',
        );

    return (
        <div className="space-y-4" data-testid="lifecycle-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Lifecycle</h3>
                    <HelpTip text="The system's governance stage — proposed, approved, active, deprecated, retired. Every arrow forward is a gate with named requirements; the diagram shows which are met and offers the operation that meets each one. Moving backwards is always allowed: that is how a mistake is corrected." />
                </div>
                <button
                    type="button"
                    onClick={() => void load()}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    Refresh
                </button>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}
            <ReadFailureNotice failure={readFailure('contracts')} onRetry={() => void load()} />

            {notice && !error && (
                <p className="text-sm text-muted" data-testid="lifecycle-notice">
                    {notice}
                </p>
            )}

            {lifecycle && !error && (
                <>
                    <div className="border border-default rounded-lg bg-surface-alt px-3 pt-2 pb-3">
                        <StageDiagram
                            currentStage={lifecycle.current_stage}
                            transitions={lifecycle.transitions}
                            riskTier={lifecycle.eu_ai_act_risk_tier}
                            onAction={onAction}
                            busy={busy}
                        />
                    </div>

                    {editing && (
                        <div
                            className="border border-default rounded-lg bg-surface p-3 space-y-2"
                            data-testid={`declare-${editing}`}
                        >
                            <label className="block text-xs font-medium text-default">
                                {editing === 'owner'
                                    ? 'Accountable owner'
                                    : 'Intended use'}
                            </label>
                            {editing === 'owner' ? (
                                <input
                                    type="text"
                                    value={draft}
                                    onChange={(e) => setDraft(e.target.value)}
                                    placeholder="A named person or team, e.g. claims-operations@nordica"
                                    aria-label="Accountable owner"
                                    className="w-full px-2 py-1.5 text-sm border border-strong rounded"
                                    data-testid="declare-input"
                                />
                            ) : (
                                <textarea
                                    value={draft}
                                    onChange={(e) => setDraft(e.target.value)}
                                    rows={3}
                                    placeholder="What this system is for, and for whom — the sentence an approver signs against"
                                    aria-label="Intended use"
                                    className="w-full px-2 py-1.5 text-sm border border-strong rounded"
                                    data-testid="declare-input"
                                />
                            )}
                            <div className="flex items-center gap-2">
                                <button
                                    type="button"
                                    disabled={!draft.trim() || busy === `action:${editing}`}
                                    onClick={() => void saveDeclaration()}
                                    className="px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                                    data-testid="declare-save"
                                >
                                    Save
                                </button>
                                <button
                                    type="button"
                                    onClick={() => setEditing(null)}
                                    className="px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover"
                                    data-testid="declare-cancel"
                                >
                                    Cancel
                                </button>
                                {setActiveTab && REQUIREMENT_FIX_LOCATION[`${editing}_set`] && (
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setActiveTab(
                                                `groups:${aiSystemId}:general:` +
                                                    REQUIREMENT_FIX_LOCATION[`${editing}_set`].section,
                                            )
                                        }
                                        className="text-xs text-primary-600 hover:underline"
                                        data-testid="declare-open-fields"
                                    >
                                        {REQUIREMENT_FIX_LOCATION[`${editing}_set`].label} &rarr;
                                    </button>
                                )}
                            </div>
                        </div>
                    )}

                    <TransitionList
                        lifecycle={lifecycle}
                        aiSystemId={aiSystemId}
                        setActiveTab={setActiveTab}
                        busy={busy}
                        focusStage={focusStage}
                        onFocused={() => setFocusStage(null)}
                        currentAdmin={currentAdmin}
                        onTransition={(stage, extra) => void transition(stage, extra)}
                    />

                    {(lifecycle.history?.length ?? 0) > 0 && (
                        <div
                            className="bg-surface border border-default rounded-lg p-4 space-y-2"
                            data-testid="lifecycle-history"
                        >
                            <p className="text-sm font-medium text-default">Transitions on record</p>
                            <ul className="space-y-1">
                                {lifecycle.history!.map((e) => (
                                    <li key={e.id} className="text-xs text-secondary" data-testid={`lifecycle-event-${e.id}`}>
                                        <span className="font-medium text-default">
                                            {e.from_stage ?? 'unset'} → {e.to_stage}
                                        </span>
                                        {' · approved by '}
                                        <span className="text-default">{e.approved_by ?? e.recorded_by}</span>
                                        {e.approved_by && e.approved_by !== e.recorded_by && (
                                            <span className="text-muted"> (recorded by {e.recorded_by})</span>
                                        )}
                                        {e.at && (
                                            <span className="text-muted"> · {new Date(e.at).toLocaleString()}</span>
                                        )}
                                        {e.successor_system_id && (
                                            <span className="text-muted"> · successor {e.successor_system_id}</span>
                                        )}
                                        {e.no_successor && <span className="text-muted"> · no successor</span>}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}

                    <AgentReleaseEvents events={lifecycle.events ?? []} setActiveTab={setActiveTab} />
                </>
            )}
        </div>
    );
}

/** `agent.new_release`: each release a member agent was
 *  first seen running, with its trust and approval as recorded. */
function AgentReleaseEvents({
    events,
    setActiveTab,
}: {
    events: LifecycleEvent[];
    setActiveTab?: (tab: string) => void;
}) {
    const releases = events.flatMap((e) => {
        const r = newReleaseOf(e);
        return r ? [{ event: e, ...r }] : [];
    });
    if (releases.length === 0) return null;
    return (
        <div className="bg-surface border border-default rounded-lg p-4 space-y-2" data-testid="lifecycle-agent-releases">
            <div className="flex items-center gap-2">
                <p className="text-sm font-medium text-default">Agent releases</p>
                <HelpTip
                    title="Agent releases"
                    text="Each time a member agent is first seen running a new release (name, version, build), it is recorded here. A release outside the agent's approved versions is also an Unapproved release finding in the inbox until it is approved or rolled back."
                />
            </div>
            <ul className="space-y-1">
                {releases.map(({ event, agentId, agentName, release }) => (
                    <li
                        key={event.id}
                        className="flex flex-wrap items-center gap-1.5 text-xs text-secondary"
                        data-testid={`lifecycle-release-${event.id}`}
                    >
                        <span>New release</span>
                        <AgentReleaseChip release={release} approved={release.approved} />
                        <span>of</span>
                        {agentId && setActiveTab ? (
                            <button
                                type="button"
                                className="text-primary-700 hover:underline"
                                onClick={() => setActiveTab(agentImplementationLink(agentId))}
                                data-testid={`lifecycle-release-agent-${event.id}`}
                            >
                                {agentName ?? agentId}
                            </button>
                        ) : (
                            <span className="text-default">{agentName ?? agentId ?? 'an agent'}</span>
                        )}
                        {event.at && <span className="text-muted">· {new Date(event.at).toLocaleString()}</span>}
                    </li>
                ))}
            </ul>
        </div>
    );
}

function TransitionList({
    lifecycle,
    busy,
    onTransition,
    aiSystemId,
    setActiveTab,
    focusStage,
    onFocused,
    currentAdmin,
}: {
    lifecycle: LifecycleResponse;
    busy: string;
    onTransition: (stage: string, extra: Record<string, unknown>) => void;
    aiSystemId: string;
    setActiveTab?: (tab: string) => void;
    focusStage?: string | null;
    onFocused?: () => void;
    currentAdmin?: string | null;
}) {
    return (
        <div className="bg-surface border border-default rounded-lg p-4 space-y-3" data-testid="lifecycle-gate">
            <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-default">Transitions</span>
                <span className="text-sm text-muted" data-testid="current-stage">
                    {lifecycle.current_stage ?? 'unset'}
                </span>
                {lifecycle.history?.[0] && (
                    <span className="text-xs text-muted" data-testid="last-transition">
                        · since {lifecycle.history[0].at ? new Date(lifecycle.history[0].at).toLocaleString() : '—'},
                        approved by {lifecycle.history[0].approved_by ?? lifecycle.history[0].recorded_by}
                    </span>
                )}
                {lifecycle.eu_ai_act_risk_tier === 'high' && (
                    <span
                        className="inline-flex px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-800"
                        data-testid="high-risk-badge"
                    >
                        High risk — every transition needs a named approver
                    </span>
                )}
            </div>

            {/* A deprecated system is the one state on this page that needs
                a later action and never asks for one. Say when, and say what
                replaced it, so nobody has to reconstruct either. */}
            {lifecycle.retirement && (
                <div
                    className="p-3 border border-default rounded bg-surface-alt text-sm"
                    data-testid="retirement-readiness"
                >
                    {lifecycle.retirement.retirable ? (
                        <p className="text-default">
                            <span className="font-medium">Ready to retire.</span>{' '}
                            {lifecycle.retirement.last_run_at
                                ? `Quiet since ${new Date(lifecycle.retirement.last_run_at).toLocaleDateString()}.`
                                : 'This system never ran.'}
                        </p>
                    ) : (
                        <p className="text-default">
                            <span className="font-medium">
                                Retirable in {lifecycle.retirement.days_remaining}{' '}
                                day{lifecycle.retirement.days_remaining === 1 ? '' : 's'}
                            </span>
                            {lifecycle.retirement.retirable_at
                                ? `, on ${new Date(lifecycle.retirement.retirable_at).toLocaleDateString()}`
                                : ''}
                            . It needs {lifecycle.retirement.quiet_days_required} days
                            without a run — that quiet is the evidence the migration
                            actually happened.
                        </p>
                    )}
                    {lifecycle.succession?.successor_system_id && (
                        <p className="mt-1 text-muted" data-testid="succeeded-by">
                            Superseded by{' '}
                            <span className="font-medium text-default">
                                {lifecycle.succession.successor_name
                                    ?? lifecycle.succession.successor_system_id}
                            </span>
                            .
                        </p>
                    )}
                    {lifecycle.succession?.no_successor_declared && (
                        <p className="mt-1 text-muted" data-testid="succeeded-by">
                            Recorded as withdrawn — no successor.
                        </p>
                    )}
                </div>
            )}

            {lifecycle.transitions.length === 0 && (
                <p className="text-sm text-muted">No transitions available.</p>
            )}

            {lifecycle.transitions.map((t) => (
                <TransitionRow
                    key={t.to_stage}
                    option={t}
                    candidates={lifecycle.candidate_successors ?? []}
                    busy={busy === `stage:${t.to_stage}`}
                    onGo={(extra) => onTransition(t.to_stage, extra)}
                    aiSystemId={aiSystemId}
                    setActiveTab={setActiveTab}
                    highlight={focusStage === t.to_stage}
                    onFocused={onFocused}
                    currentAdmin={currentAdmin}
                />
            ))}
        </div>
    );
}

function TransitionRow({
    option,
    candidates,
    busy,
    onGo,
    aiSystemId,
    setActiveTab,
    highlight,
    onFocused,
    currentAdmin,
}: {
    option: TransitionOption;
    candidates: SuccessorCandidate[];
    busy: boolean;
    onGo: (extra: Record<string, unknown>) => void;
    aiSystemId: string;
    setActiveTab?: (tab: string) => void;
    highlight?: boolean;
    onFocused?: () => void;
    currentAdmin?: string | null;
}) {
    const needsApprover = option.unmet.some((r) => r.key === 'human_approval');
    const needsSuccessor = option.unmet.some((r) => r.key === 'successor_named');
    // The admin performing the move IS the named human; the field is only
    // for approving on behalf of someone who is not logged in.
    const [approver, setApprover] = useState('');
    const [otherApprover, setOtherApprover] = useState(false);
    const effectiveApprover = otherApprover ? approver.trim() : (currentAdmin ?? approver.trim());
    const rowRef = useRef<HTMLDivElement>(null);
    const [ringed, setRinged] = useState(false);

    // A rail button pointed here: bring the row into view, put the cursor in
    // the first thing to answer, and ring the row long enough to be noticed.
    useEffect(() => {
        if (!highlight) return;
        rowRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const first = rowRef.current?.querySelector<HTMLElement>(
            'input[type="radio"], input[type="text"], select',
        );
        first?.focus();
        setRinged(true);
        const t = setTimeout(() => {
            setRinged(false);
            onFocused?.();
        }, 2500);
        return () => clearTimeout(t);
    }, [highlight, onFocused]);

    // Deprecation asks a real question — is the capability moving, or going
    // away? Nothing is assumed: the button stays disabled until someone
    // answers, and "withdrawn" is a choice rather than a default.
    const [succession, setSuccession] = useState<'' | 'superseded' | 'none'>('');
    const [successorId, setSuccessorId] = useState('');
    const successionAnswered =
        !needsSuccessor ||
        succession === 'none' ||
        (succession === 'superseded' && !!successorId);

    return (
        <div
            ref={rowRef}
            className={`border-t border-default pt-3 first:border-t-0 first:pt-0 rounded-lg transition-shadow ${
                ringed ? 'ring-2 ring-primary-300 px-2 -mx-2' : ''
            }`}
            data-testid={`transition-${option.to_stage}`}
            data-highlighted={ringed ? 'true' : 'false'}
        >
            <div className="flex items-center justify-between gap-3">
                <span className="text-sm text-default flex items-center gap-1.5">
                    <ArrowRight className="w-3.5 h-3.5 text-muted" />
                    {option.to_stage}
                </span>
                <div className="flex items-center gap-2">
                    {needsApprover && currentAdmin && !otherApprover && (
                        <span className="text-xs text-secondary" data-testid={`approver-self-${option.to_stage}`}>
                            approving as <span className="font-medium text-default">{currentAdmin}</span>
                            {' · '}
                            <button
                                type="button"
                                onClick={() => setOtherApprover(true)}
                                className="text-primary-600 hover:underline"
                                data-testid={`approver-other-${option.to_stage}`}
                            >
                                on behalf of someone else
                            </button>
                        </span>
                    )}
                    {needsApprover && (otherApprover || !currentAdmin) && (
                        <input
                            type="text"
                            value={approver}
                            onChange={(e) => setApprover(e.target.value)}
                            placeholder="Accountable approver"
                            aria-label={`Approver for ${option.to_stage}`}
                            className="px-2 py-1 text-xs border border-strong rounded"
                            data-testid={`approver-${option.to_stage}`}
                        />
                    )}
                    <button
                        type="button"
                        disabled={
                            busy
                            || (option.unmet.length > 0 && !needsApprover && !needsSuccessor)
                            || !successionAnswered
                            || (needsApprover && !effectiveApprover)
                        }
                        onClick={() =>
                            onGo({
                                ...(needsApprover && effectiveApprover ? { approved_by: effectiveApprover } : {}),
                                ...(needsSuccessor && succession === 'superseded'
                                    ? { successor_id: successorId }
                                    : {}),
                                ...(needsSuccessor && succession === 'none'
                                    ? { no_successor: true }
                                    : {}),
                            })
                        }
                        className="px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                        data-testid={`go-${option.to_stage}`}
                    >
                        Move to {option.to_stage}
                    </button>
                </div>
            </div>

            {needsSuccessor && (
                <div
                    className="mt-3 p-3 border border-default rounded bg-surface-alt space-y-2"
                    data-testid={`succession-${option.to_stage}`}
                >
                    <p className="text-xs font-medium text-default">
                        Is this system superseded?
                    </p>
                    <label className="flex items-center gap-2 text-xs text-secondary">
                        <input
                            type="radio"
                            name={`succession-${option.to_stage}`}
                            checked={succession === 'superseded'}
                            onChange={() => setSuccession('superseded')}
                            data-testid={`succession-superseded-${option.to_stage}`}
                        />
                        Yes — its work moves to another AI System
                    </label>
                    {succession === 'superseded' && (
                        <select
                            value={successorId}
                            onChange={(e) => setSuccessorId(e.target.value)}
                            aria-label="Successor system"
                            className="w-full px-2 py-1 text-xs border border-strong rounded"
                            data-testid={`successor-select-${option.to_stage}`}
                        >
                            <option value="">Select the system that supersedes it…</option>
                            {candidates.map((c) => (
                                <option key={c.id} value={c.id}>
                                    {c.name}
                                    {c.lifecycle_stage ? ` (${c.lifecycle_stage})` : ''}
                                </option>
                            ))}
                        </select>
                    )}
                    <label className="flex items-center gap-2 text-xs text-secondary">
                        <input
                            type="radio"
                            name={`succession-${option.to_stage}`}
                            checked={succession === 'none'}
                            onChange={() => setSuccession('none')}
                            data-testid={`succession-none-${option.to_stage}`}
                        />
                        No — the capability is being withdrawn
                    </label>
                    <p className="text-xs text-muted">
                        Recorded on the system, so anyone landing on it later can
                        see where the work went.
                    </p>
                </div>
            )}

            {/* The full checklist, satisfied items included — an operator
                needs to see progress, not only failure. */}
            <ul className="mt-2 space-y-1">
                {option.requirements.map((r) => (
                    <li
                        key={r.key}
                        className="flex items-start gap-1.5 text-xs"
                        data-testid={`req-${option.to_stage}-${r.key}`}
                    >
                        {r.satisfied ? (
                            <Check className="w-3.5 h-3.5 text-green-600 shrink-0 mt-0.5" />
                        ) : (
                            <X className="w-3.5 h-3.5 text-amber-600 shrink-0 mt-0.5" />
                        )}
                        <span className={r.satisfied ? 'text-muted' : 'text-default'}>
                            {r.label}
                            {!r.satisfied && r.detail && (
                                <span className="text-muted"> — {r.detail}</span>
                            )}
                            {!r.satisfied && setActiveTab && REQUIREMENT_FIX_LOCATION[r.key] && (
                                <>
                                    {' '}
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setActiveTab(
                                                `groups:${aiSystemId}:general:` +
                                                REQUIREMENT_FIX_LOCATION[r.key].section,
                                            )
                                        }
                                        className="text-primary-600 hover:underline font-medium"
                                        data-testid={`req-fix-${r.key}`}
                                    >
                                        {REQUIREMENT_FIX_LOCATION[r.key].label} &rarr;
                                    </button>
                                </>
                            )}
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}

export default LifecyclePanel;
