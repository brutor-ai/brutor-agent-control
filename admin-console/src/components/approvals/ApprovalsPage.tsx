/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control → Approvals: agent calls that policy parked for a person.
 *
 * An agent whose call (an MCP tool, another agent, a skill) needs approval stops and polls the gateway. This
 * queue is where an administrator answers it. The
 * decision itself is made by the gateway core — the control plane asks it —
 * so it lands in the run's audit trail and evidence like any other.
 *
 * Three rules the screen keeps:
 *  - what a request *is* comes from the server's `decision`, never from the
 *    raw status: a request past its deadline is still `pending` in the
 *    database and must not be offered for approval;
 *  - the one-time approval token in the approve response is the agent's; it
 *    is never shown or kept here;
 *  - after any attempt to decide — made, refused or failed — the queue is
 *    read again, so the screen shows what is, not what was asked for.
 */

import { Fragment, useCallback, useEffect, useState } from 'react';
import { CheckCircle, Clock, RefreshCw, UserCheck, X, XCircle } from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { TenantScoped } from '../common/TenantScoped';
import { useGateway } from '../../hooks/useGateway';
import { errorMessage } from '../../lib/narrow';
import {
    TOOL_APPROVAL_FILTERS,
    TOOL_APPROVAL_MANAGE,
    approvalDecision,
    approvalsByCall,
    approvalSubject,
    approvalSurface,
    heldMessage,
    skillHold,
    timeLeft,
    timeSince,
    type ToolApprovalDecision,
    type ToolApprovalDetail,
    type ToolApprovalFilter,
    type ToolApprovalSummary,
} from '../../lib/toolApprovalTypes';
import { HelpTip } from '../ui/HelpTip';
import { TABLE_CONTAINER, TABLE_ROW_HOVER, TABLE_TBODY, TABLE_TH, TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';

/** How often the queue is read again while it is on screen. */
export const APPROVALS_REFRESH_MS = 15_000;

const FILTER_LABELS: Record<ToolApprovalFilter, string> = {
    pending: 'Waiting',
    approved: 'Approved',
    rejected: 'Rejected',
    expired: 'Expired',
    all: 'All',
};

const DECISION_BADGE: Record<ToolApprovalDecision, string> = {
    pending: 'bg-blue-100 text-blue-800',
    approved: 'bg-green-100 text-green-800',
    rejected: 'bg-red-100 text-red-800',
    expired: 'bg-surface-raised text-secondary',
    unknown: 'bg-yellow-100 text-yellow-800',
};

const DECISION_LABEL: Record<ToolApprovalDecision, string> = {
    pending: 'Waiting',
    approved: 'Approved',
    rejected: 'Rejected',
    expired: 'Expired',
    unknown: 'Unknown',
};

/** "a", "a and b", "a, b and c". */
function inWords(items: string[]): string {
    if (items.length <= 1) return items.join('');
    return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

function DecisionBadge({ item }: { item: { decision?: string | null; token_used?: boolean | null } }) {
    const decision = approvalDecision(item);
    return (
        <span
            className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${DECISION_BADGE[decision]}`}
            data-testid={`approval-decision-${decision}`}
        >
            {DECISION_LABEL[decision]}
            {decision === 'approved' && item.token_used ? ' · used' : ''}
        </span>
    );
}

/**
 * The approvals of one tenant. An administrator of no tenant is asked which
 * tenant first: the control plane answers each of these routes for the
 * tenant the request is for.
 */
export default function ApprovalsPage() {
    return (
        <TenantScoped pageName="Approvals">
            <TenantApprovals />
        </TenantScoped>
    );
}

function TenantApprovals() {
    const { gatewayClient } = useGateway();
    const { hasPermission } = useAuth();
    const canDecide = hasPermission(TOOL_APPROVAL_MANAGE);

    const [filter, setFilter] = useState<ToolApprovalFilter>('pending');
    const [items, setItems] = useState<ToolApprovalSummary[]>([]);
    const [total, setTotal] = useState(0);
    const [waiting, setWaiting] = useState<number | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [now, setNow] = useState(() => Date.now());

    const [selected, setSelected] = useState<ToolApprovalDetail | null>(null);
    const [note, setNote] = useState('');
    const [busy, setBusy] = useState(false);
    const [actionError, setActionError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);

    // Bumped to read the queue again now: Refresh, and after every attempt
    // to decide.
    const [readAgain, setReadAgain] = useState(0);
    const reload = useCallback(() => setReadAgain((n) => n + 1), []);

    useEffect(() => {
        if (!gatewayClient) return;
        let cancelled = false;
        const read = async () => {
            try {
                const [list, count] = await Promise.all([
                    gatewayClient.listToolApprovals({ status_filter: filter, page_size: 100 }),
                    gatewayClient.getToolApprovalsPendingCount(),
                ]);
                if (cancelled) return;
                // Cleared by a read that worked, not before it: a failing
                // poll keeps saying so instead of flickering.
                setError(null);
                setItems(list.items);
                setTotal(list.total);
                setWaiting(count);
                setNow(Date.now());
            } catch (e) {
                if (!cancelled) setError(errorMessage(e, 'Failed to load approval requests'));
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        void read();
        const timer = setInterval(() => void read(), APPROVALS_REFRESH_MS);
        return () => {
            cancelled = true;
            clearInterval(timer);
        };
    }, [gatewayClient, filter, readAgain]);

    // The other holds raised by the call the open one came from.
    const [sameCall, setSameCall] = useState<ToolApprovalSummary[]>([]);
    const selectedRoot = selected?.originating_trace_root_id ?? null;
    const selectedId = selected?.id ?? null;
    useEffect(() => {
        setSameCall([]);
        if (!gatewayClient || selectedRoot === null) return;
        let cancelled = false;
        const read = async () => {
            try {
                const list = await gatewayClient.listToolApprovals({ status_filter: 'all', trace_root_id: selectedRoot, page_size: 100 });
                if (!cancelled) setSameCall(list.items.filter((hold) => hold.id !== selectedId));
            } catch {
                // The detail stands without the list; the queue says when reading fails.
            }
        };
        void read();
        return () => {
            cancelled = true;
        };
    }, [gatewayClient, selectedRoot, selectedId]);

    const open = useCallback(
        async (item: ToolApprovalSummary) => {
            if (!gatewayClient) return;
            setActionError(null);
            setNotice(null);
            setNote('');
            try {
                setSelected(await gatewayClient.getToolApproval(item.id));
            } catch (e) {
                setActionError(errorMessage(e, 'Failed to load the request'));
            }
        },
        [gatewayClient],
    );

    const decide = useCallback(
        async (verb: 'approve' | 'reject') => {
            if (!gatewayClient || !selected) return;
            setBusy(true);
            setActionError(null);
            try {
                if (verb === 'approve') {
                    await gatewayClient.approveToolApproval(selected.id, note.trim() || undefined);
                } else {
                    await gatewayClient.rejectToolApproval(selected.id, note.trim());
                }
                setNotice(
                    verb === 'approve'
                        ? `Approved ${selected.capability_name}. The agent picks the decision up on its next poll.`
                        : `Rejected ${selected.capability_name}. The agent's call is refused.`,
                );
                setSelected(null);
                setNote('');
            } catch (e) {
                // Refused (expired, already decided) or not answered (gateway
                // unavailable): say what the server said and show the request
                // as it now is.
                setActionError(errorMessage(e, `Failed to ${verb} the request`));
                try {
                    setSelected(await gatewayClient.getToolApproval(selected.id));
                } catch {
                    setSelected(null);
                }
            } finally {
                setBusy(false);
                reload();
            }
        },
        [gatewayClient, selected, note, reload],
    );

    const selectedDecision = selected ? approvalDecision(selected) : null;
    const selectedLeft = selected ? timeLeft(selected.expires_at, now) : null;
    const selectedSurface = selected ? approvalSurface(selected) : null;
    const selectedMessage = selected ? heldMessage(selected) : null;
    const selectedSkill = selected ? skillHold(selected) : null;

    return (
        <div className="p-6 space-y-4" data-testid="approvals-page">
            <div className="flex items-start gap-2">
                <UserCheck className="w-5 h-5 text-primary-600 mt-0.5" />
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                        <h1 className="text-xl font-semibold text-default">Approvals</h1>
                        <HelpTip text="A tool call, a call to another agent or a skill execution that policy marks as needing approval is parked: the agent stops and polls the gateway until a person decides or the deadline passes. Approving lets that one call through, once. Rejecting refuses it. A request nobody decides in time expires and the call is not made. Every outcome is recorded in the run's audit trail and evidence." />
                    </div>
                    <p className="text-sm text-secondary">
                        Agent calls waiting for a person.{' '}
                        {waiting !== null && (
                            <span data-testid="approvals-waiting-count">
                                {waiting === 0 ? 'Nothing is waiting.' : `${waiting} waiting now.`}
                            </span>
                        )}
                    </p>
                </div>
                <button
                    type="button"
                    onClick={reload}
                    className="px-3 py-1.5 border border-strong text-secondary rounded-md text-sm hover:bg-surface-alt flex items-center gap-1"
                    data-testid="approvals-refresh"
                >
                    <RefreshCw className="w-3.5 h-3.5" /> Refresh
                </button>
            </div>

            <div className="flex flex-wrap gap-2" role="tablist" aria-label="Filter approval requests">
                {TOOL_APPROVAL_FILTERS.map((f) => (
                    <button
                        key={f}
                        type="button"
                        role="tab"
                        aria-selected={filter === f}
                        onClick={() => {
                            setSelected(null);
                            setItems([]);
                            setLoading(true);
                            setFilter(f);
                        }}
                        className={`px-3 py-1 rounded-full text-sm border ${
                            filter === f
                                ? 'bg-primary-500 text-white border-primary-500'
                                : 'border-strong text-secondary hover:bg-surface-alt'
                        }`}
                        data-testid={`approvals-filter-${f}`}
                    >
                        {FILTER_LABELS[f]}
                        {f === 'pending' && waiting ? ` (${waiting})` : ''}
                    </button>
                ))}
            </div>

            {notice && (
                <div className="p-3 rounded-md bg-green-50 text-green-800 text-sm" data-testid="approvals-notice">
                    {notice}
                </div>
            )}
            {error && (
                <div className="p-3 rounded-md bg-red-50 text-red-800 text-sm" data-testid="approvals-error">
                    {error}
                </div>
            )}

            <div className={TABLE_CONTAINER}>
                <table className="w-full text-sm">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={TABLE_TH}>Held call</th>
                            <th className={TABLE_TH}>Requested by</th>
                            <th className={TABLE_TH}>Raised</th>
                            <th className={TABLE_TH}>Deadline</th>
                            <th className={TABLE_TH}>State</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {approvalsByCall(items).map((call) => (
                            <Fragment key={call.traceRoot ?? call.holds[0].id}>
                                {call.traceRoot !== null && call.holds.length > 1 && (
                                    <tr data-testid={`approval-call-${call.traceRoot}`} className="bg-surface-alt">
                                        <td colSpan={5} className="px-4 py-2 text-xs text-secondary">
                                            One call raised these {call.holds.length} holds
                                            <span className="ml-2 font-mono text-muted">{call.traceRoot}</span>
                                        </td>
                                    </tr>
                                )}
                                {call.holds.map((item) => {
                            const decision = approvalDecision(item);
                            const left = decision === 'pending' ? timeLeft(item.expires_at, now) : null;
                            return (
                                <tr
                                    key={item.id}
                                    className={`${TABLE_ROW_HOVER} cursor-pointer`}
                                    onClick={() => void open(item)}
                                    data-testid={`approval-row-${item.id}`}
                                >
                                    <td className="px-4 py-3">
                                        <div className="font-medium text-default">{item.capability_name}</div>
                                        <div className="text-xs text-muted">{approvalSubject(item)}</div>
                                    </td>
                                    <td className="px-4 py-3 text-secondary">
                                        {item.requester_display || item.requester_type}
                                        <div className="text-xs text-muted">{item.requester_group_id}</div>
                                    </td>
                                    <td className="px-4 py-3 text-secondary">{timeSince(item.created_at, now)}</td>
                                    <td className="px-4 py-3 text-secondary">
                                        {left ? (
                                            <span className="inline-flex items-center gap-1">
                                                <Clock className="w-3.5 h-3.5" /> {left} left
                                            </span>
                                        ) : (
                                            '—'
                                        )}
                                    </td>
                                    <td className="px-4 py-3">
                                        <DecisionBadge item={item} />
                                    </td>
                                </tr>
                            );
                                                        })}
                            </Fragment>
                        ))}
                        {!loading && items.length === 0 && (
                            <tr>
                                <td colSpan={5} className="px-4 py-8 text-center text-muted" data-testid="approvals-empty">
                                    {filter === 'pending'
                                        ? 'No agent is waiting for an approval.'
                                        : 'No requests match this filter.'}
                                </td>
                            </tr>
                        )}
                        {loading && items.length === 0 && (
                            <tr>
                                <td colSpan={5} className="px-4 py-8 text-center text-muted">
                                    Loading…
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
            {total > items.length && (
                <p className="text-xs text-muted" data-testid="approvals-truncated">
                    Showing the {items.length} most recent of {total}.
                </p>
            )}

            {actionError && !selected && (
                <div className="p-3 rounded-md bg-red-50 text-red-800 text-sm" data-testid="approvals-action-error">
                    {actionError}
                </div>
            )}

            {selected && (
                <div className="bg-surface rounded-lg border border-default p-4 space-y-3" data-testid="approval-detail">
                    <div className="flex items-start gap-2">
                        <div className="flex-1 min-w-0">
                            <h2 className="text-base font-semibold text-default">
                                {selected.capability_name}
                                {selectedSurface === 'mcp' && selected.server_name ? (
                                    <span className="font-normal text-secondary"> on {selected.server_name}</span>
                                ) : null}
                            </h2>
                            <p className="text-sm text-secondary" data-testid="approval-subject">
                                {approvalSubject(selected, 'MCP server ')}.
                                {selectedSurface === 'mcp' && !selected.server_name
                                    ? ' No MCP server was recorded with this request.'
                                    : ''}
                            </p>
                            {/* Only an MCP hold has a server configuration, and only when the
                                gateway matched the name; an A2A or a skill hold never has one. */}
                            {selectedSurface === 'mcp' && selected.server_name && !selected.server_config_id && (
                                <p className="text-sm text-secondary" data-testid="approval-server-unresolved">
                                    The gateway found no enabled MCP server configuration with this name when the
                                    request was raised.
                                </p>
                            )}
                            {sameCall.length > 0 && (
                                <div className="text-sm text-secondary" data-testid="approval-same-call">
                                    The same call raised {sameCall.length === 1 ? 'another hold' : `${sameCall.length} other holds`}:
                                    <ul className="mt-1 space-y-0.5">
                                        {sameCall.map((hold) => (
                                            <li key={hold.id} className="flex items-center gap-2">
                                                <span className="font-medium text-default">{hold.capability_name}</span>
                                                <span className="text-xs text-muted">{approvalSubject(hold)}</span>
                                                <DecisionBadge item={hold} />
                                            </li>
                                        ))}
                                    </ul>
                                </div>
                            )}
                            {selectedSkill && (
                                <p className="text-sm text-secondary" data-testid="approval-skill">
                                    Skill {selectedSkill.name ?? selectedSkill.id ?? selected.capability_name}
                                    {selectedSkill.name && selectedSkill.id ? ` (${selectedSkill.id})` : ''}, to run in Resource Group{' '}
                                    {selectedSkill.groupId}.
                                </p>
                            )}
                            {selectedSkill && selectedSkill.requiredBy.length > 0 && (
                                <p className="text-sm text-secondary" data-testid="approval-required-by">
                                    Approval is required by {inWords(selectedSkill.requiredBy)}.
                                </p>
                            )}
                            <p className="text-sm text-secondary">
                                Requested by {selected.requester_display || selected.requester_type} in{' '}
                                {selected.requester_group_id} · {timeSince(selected.created_at, now)}
                            </p>
                        </div>
                        <DecisionBadge item={selected} />
                        <button
                            type="button"
                            onClick={() => setSelected(null)}
                            className="p-1 text-muted hover:text-default"
                            aria-label="Close"
                            data-testid="approval-detail-close"
                        >
                            <X className="w-4 h-4" />
                        </button>
                    </div>

                    <div>
                        <div className={`${TABLE_TH_TYPE} mb-1`}>
                            {selectedSkill ? 'What the skill is to run with' : 'What the agent wants to call it with'}
                        </div>
                        {selectedMessage ? (
                            // A skill call that came over A2A is held with the message
                            // it carries: shown part by part, as a person reads it.
                            <div
                                className="text-xs bg-surface-alt rounded-md p-3 overflow-auto max-h-96 text-default space-y-3"
                                data-testid="approval-arguments"
                            >
                                <p className="text-secondary">The message the agent sent to the skill:</p>
                                {selectedMessage.parts.map((part, index) => (
                                    <div key={index} data-testid="approval-message-part">
                                        <div className="font-medium text-secondary">
                                            {[`Part ${index + 1}`, part.kind, ...part.labels].join(' · ')}
                                        </div>
                                        <pre className="whitespace-pre-wrap break-words mt-1">{part.body}</pre>
                                    </div>
                                ))}
                                {selectedMessage.parts.length === 0 && <p>The message has no parts.</p>}
                                {selectedMessage.metadata && (
                                    <div data-testid="approval-message-metadata">
                                        <div className="font-medium text-secondary">Message metadata</div>
                                        <pre className="whitespace-pre-wrap break-words mt-1">
                                            {JSON.stringify(selectedMessage.metadata, null, 2)}
                                        </pre>
                                    </div>
                                )}
                            </div>
                        ) : (
                            <pre
                                className="text-xs bg-surface-alt rounded-md p-3 overflow-auto max-h-64 text-default"
                                data-testid="approval-arguments"
                            >
                                {selected.call_arguments
                                    ? JSON.stringify(selected.call_arguments, null, 2)
                                    : 'The arguments were not recorded with this request.'}
                            </pre>
                        )}
                    </div>

                    {selectedDecision !== 'pending' && (
                        <p className="text-sm text-secondary" data-testid="approval-resolution">
                            {selectedDecision === 'expired'
                                ? 'Nobody decided before the deadline. The call was not made.'
                                : selectedDecision === 'unknown'
                                  ? 'This request is in a state this console does not know. It cannot be decided here.'
                                  : `${DECISION_LABEL[selectedDecision ?? 'unknown']}${
                                        selected.resolved_by ? ` by ${selected.resolved_by}` : ''
                                    }${selected.resolved_at ? ` at ${new Date(selected.resolved_at).toLocaleString()}` : ''}.`}
                            {selected.resolution_note ? ` Note: ${selected.resolution_note}` : ''}
                        </p>
                    )}

                    {actionError && (
                        <div className="p-3 rounded-md bg-red-50 text-red-800 text-sm" data-testid="approvals-action-error">
                            {actionError}
                        </div>
                    )}

                    {selectedDecision === 'pending' &&
                        (canDecide ? (
                            <div className="space-y-2">
                                <p className="text-sm text-secondary">
                                    {selectedLeft
                                        ? `${selectedLeft} left to decide.`
                                        : 'The deadline is passing; a decision may be refused.'}
                                </p>
                                <label className="block text-sm text-secondary" htmlFor="approval-note">
                                    Note (required to reject; recorded with the decision)
                                </label>
                                <textarea
                                    id="approval-note"
                                    value={note}
                                    onChange={(e) => setNote(e.target.value)}
                                    maxLength={1000}
                                    rows={2}
                                    className="w-full border border-strong rounded-md p-2 text-sm bg-surface text-default"
                                    data-testid="approval-note"
                                />
                                <div className="flex gap-2">
                                    <button
                                        type="button"
                                        disabled={busy}
                                        onClick={() => void decide('approve')}
                                        className="px-3 py-1.5 bg-primary-500 text-white rounded-md text-sm hover:bg-primary-600 disabled:opacity-50 flex items-center gap-1"
                                        data-testid="approval-approve"
                                    >
                                        <CheckCircle className="w-3.5 h-3.5" /> {selectedSkill ? 'Approve this execution' : 'Approve this call'}
                                    </button>
                                    <button
                                        type="button"
                                        disabled={busy || note.trim().length === 0}
                                        onClick={() => void decide('reject')}
                                        className="px-3 py-1.5 border border-strong text-secondary rounded-md text-sm hover:bg-surface-alt disabled:opacity-50 flex items-center gap-1"
                                        data-testid="approval-reject"
                                    >
                                        <XCircle className="w-3.5 h-3.5" /> Reject
                                    </button>
                                </div>
                            </div>
                        ) : (
                            <p className="text-sm text-secondary" data-testid="approval-read-only">
                                You can see this request but not decide it: that needs the{' '}
                                <code>{TOOL_APPROVAL_MANAGE}</code> permission.
                            </p>
                        ))}
                </div>
            )}
        </div>
    );
}
