/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Approvals inbox.
 *
 * What is held here is what makes the screen safe to decide from: a request
 * is offered for a decision only when the server says it is pending and the
 * administrator may decide; the agent's one-time token never reaches the
 * page; and whatever happens to a decision — made, refused, unanswered —
 * the queue is read again and the server's words are shown.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ToolApprovalDetail, ToolApprovalSummary } from '../../../lib/toolApprovalTypes';

const NOW = Date.parse('2026-11-10T12:00:00Z');
const iso = (offsetMinutes: number) => new Date(NOW + offsetMinutes * 60_000).toISOString();

function summary(id: string, over: Partial<ToolApprovalSummary> = {}): ToolApprovalSummary {
    return {
        id,
        server_name: 'payments',
        capability_type: 'tool',
        capability_name: 'send_payment',
        requester_type: 'agent',
        requester_display: 'screening-agent',
        requester_group_id: 'rg-loans',
        status: 'pending',
        decision: 'pending',
        token_used: false,
        timeout_seconds: 600,
        expires_at: iso(8),
        created_at: iso(-2),
        ...over,
    };
}

function detail(id: string, over: Partial<ToolApprovalDetail> = {}): ToolApprovalDetail {
    return {
        ...summary(id),
        tenant_id: 't1',
        server_config_id: 'srv-1',
        call_arguments: { amount: 1200, currency: 'EUR' },
        ...over,
    };
}

const gatewayClient = {
    listToolApprovals: vi.fn(),
    getToolApprovalsPendingCount: vi.fn(),
    getToolApproval: vi.fn(),
    approveToolApproval: vi.fn(),
    rejectToolApproval: vi.fn(),
};
let permissions: string[] = [];

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient }),
}));
let principal: unknown = null;
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: (p: string) => permissions.includes(p), principal }),
}));

import ApprovalsPage from '../ApprovalsPage';

async function openFirst(id = 'apr-1') {
    render(<ApprovalsPage />);
    fireEvent.click(await screen.findByTestId(`approval-row-${id}`));
    await screen.findByTestId('approval-detail');
}

beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(Date, 'now').mockReturnValue(NOW);
    permissions = ['tool_approval:read', 'tool_approval:manage'];
    gatewayClient.listToolApprovals.mockResolvedValue({
        items: [summary('apr-1')],
        total: 1,
        page: 1,
        page_size: 100,
        status_filter: 'pending',
    });
    gatewayClient.getToolApprovalsPendingCount.mockResolvedValue(1);
    gatewayClient.getToolApproval.mockResolvedValue(detail('apr-1'));
});

describe('an administrator of no tenant', () => {
    afterEach(() => {
        principal = null;
    });

    it('is asked which tenant to show, and no approval is requested before it answers', () => {
        // The control plane answers such an administrator 400 unless the request names a tenant.
        principal = { id: 'system' };
        render(<ApprovalsPage />);
        expect(screen.getByTestId('tenant-needed')).toHaveTextContent('Approvals shows the data of one tenant.');
        expect(gatewayClient.listToolApprovals).not.toHaveBeenCalled();
        expect(gatewayClient.getToolApprovalsPendingCount).not.toHaveBeenCalled();
    });
});

describe('the queue', () => {
    it('opens on what is waiting, with who asked, for what, and how long is left', async () => {
        render(<ApprovalsPage />);
        const row = await screen.findByTestId('approval-row-apr-1');
        expect(gatewayClient.listToolApprovals).toHaveBeenCalledWith({ status_filter: 'pending', page_size: 100 });
        expect(row).toHaveTextContent('send_payment');
        expect(row).toHaveTextContent('tool on payments');
        expect(row).toHaveTextContent('screening-agent');
        expect(row).toHaveTextContent('rg-loans');
        expect(row).toHaveTextContent('2 min ago');
        expect(row).toHaveTextContent('8 min left');
        expect(screen.getByTestId('approvals-waiting-count')).toHaveTextContent('1 waiting now.');
    });

    it('says so when nothing is waiting', async () => {
        gatewayClient.listToolApprovals.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
        gatewayClient.getToolApprovalsPendingCount.mockResolvedValue(0);
        render(<ApprovalsPage />);
        expect(await screen.findByTestId('approvals-empty')).toHaveTextContent('No agent is waiting');
        expect(screen.getByTestId('approvals-waiting-count')).toHaveTextContent('Nothing is waiting.');
    });

    it('asks the server for each filter by its name', async () => {
        render(<ApprovalsPage />);
        await screen.findByTestId('approval-row-apr-1');
        fireEvent.click(screen.getByTestId('approvals-filter-expired'));
        await waitFor(() =>
            expect(gatewayClient.listToolApprovals).toHaveBeenLastCalledWith({
                status_filter: 'expired',
                page_size: 100,
            }),
        );
    });

    it('shows a failed read and keeps the page', async () => {
        gatewayClient.listToolApprovals.mockRejectedValue(new Error('control plane unavailable'));
        render(<ApprovalsPage />);
        expect(await screen.findByTestId('approvals-error')).toHaveTextContent('control plane unavailable');
        expect(screen.getByTestId('approvals-page')).toBeInTheDocument();
    });

    it('takes what a request is from the server, not from the raw status', async () => {
        // Past its deadline the row is still `pending` in the database.
        gatewayClient.listToolApprovals.mockResolvedValue({
            items: [summary('apr-late', { status: 'pending', decision: 'expired', expires_at: iso(-5) })],
            total: 1,
            page: 1,
            page_size: 100,
        });
        gatewayClient.getToolApproval.mockResolvedValue(
            detail('apr-late', { status: 'pending', decision: 'expired', expires_at: iso(-5) }),
        );
        await openFirst('apr-late');
        expect(screen.getAllByTestId('approval-decision-expired').length).toBeGreaterThan(0);
        expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument();
        expect(screen.queryByTestId('approval-reject')).not.toBeInTheDocument();
        expect(screen.getByTestId('approval-resolution')).toHaveTextContent('The call was not made');
    });

    it('does not offer a decision on a state it does not know', async () => {
        gatewayClient.getToolApproval.mockResolvedValue(detail('apr-1', { status: 'escalated', decision: 'escalated' }));
        await openFirst();
        expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument();
        expect(screen.getByTestId('approval-resolution')).toHaveTextContent('cannot be decided here');
    });
});

describe('what is held, when no server configuration is recorded (server_config_id is null)', () => {
    // The gateway core writes the row (agent_approval.rs): an MCP hold names
    // the server and, when it can match it, its configuration; an A2A hold
    // has the agent card's id where the server name is, a skill hold the
    // skill's id, and neither has a server configuration.
    function held(over: Partial<ToolApprovalDetail>) {
        gatewayClient.listToolApprovals.mockResolvedValue({
            items: [summary('apr-1', { server_name: over.server_name, capability_type: over.capability_type, capability_name: over.capability_name })],
            total: 1,
            page: 1,
            page_size: 100,
            status_filter: 'pending',
        });
        gatewayClient.getToolApproval.mockResolvedValue(detail('apr-1', { server_config_id: null, ...over }));
    }

    it('an MCP hold whose server was resolved names the server and says nothing more', async () => {
        await openFirst();
        expect(screen.getByTestId('approval-subject')).toHaveTextContent('tool on MCP server payments.');
        expect(screen.queryByTestId('approval-server-unresolved')).not.toBeInTheDocument();
    });

    it('an A2A hold is a call to an agent, not a tool on a server', async () => {
        held({ capability_type: 'a2a_call', capability_name: 'summarize', server_name: 'card-7' });
        await openFirst();
        expect(screen.getByTestId('approval-row-apr-1')).toHaveTextContent('Agent call to agent card-7');
        expect(screen.getByTestId('approval-row-apr-1')).not.toHaveTextContent('a2a_call');
        const shown = screen.getByTestId('approval-detail');
        expect(shown).toHaveTextContent('summarize');
        expect(screen.getByTestId('approval-subject')).toHaveTextContent('Agent call to agent card-7.');
        expect(shown).not.toHaveTextContent('MCP server');
        expect(shown).not.toHaveTextContent('null');
        expect(screen.queryByTestId('approval-server-unresolved')).not.toBeInTheDocument();
        expect(screen.getByTestId('approval-approve')).toBeEnabled();
    });

    it('a skill hold is the execution of a skill', async () => {
        // The gateway records the skill's id in both places.
        held({ capability_type: 'skill_exec', capability_name: 'sk-3', server_name: 'sk-3' });
        await openFirst();
        expect(screen.getByTestId('approval-row-apr-1')).toHaveTextContent('Skill execution of skill sk-3');
        const shown = screen.getByTestId('approval-detail');
        expect(screen.getByTestId('approval-subject')).toHaveTextContent('Skill execution of skill sk-3.');
        expect(shown).not.toHaveTextContent('MCP server');
        expect(shown).not.toHaveTextContent('null');
        expect(screen.queryByTestId('approval-server-unresolved')).not.toBeInTheDocument();
        expect(screen.getByTestId('approval-approve')).toBeEnabled();
    });

    it('a skill hold reads as a skill: which skill, in which Resource Group, and which settings ask for a person', async () => {
        held({
            capability_type: 'skill_exec',
            capability_name: 'sk-3',
            server_name: 'sk-3',
            request_metadata: { surface: 'skill', agent_id: 'agent-1', execution_mode: { required_by: ['group', 'skill'] } },
            call_arguments: { period: 'Q2' },
        });
        await openFirst();
        expect(screen.getByTestId('approval-skill')).toHaveTextContent('Skill sk-3, to run in Resource Group rg-loans.');
        expect(screen.getByTestId('approval-required-by')).toHaveTextContent(
            "Approval is required by the Resource Group's skill governance and the skill's own setting.",
        );
        const shown = screen.getByTestId('approval-detail');
        expect(shown).toHaveTextContent('What the skill is to run with');
        expect(shown).not.toHaveTextContent('What the agent wants to call it with');
        expect(screen.getByTestId('approval-arguments')).toHaveTextContent('"period": "Q2"');
        expect(screen.getByTestId('approval-approve')).toHaveTextContent('Approve this execution');
    });

    it('a skill hold that carries the skill\'s name shows the name and the id', async () => {
        held({ capability_type: 'skill_exec', capability_name: 'quarterly-report', server_name: 'sk-3' });
        await openFirst();
        expect(screen.getByTestId('approval-skill')).toHaveTextContent('Skill quarterly-report (sk-3), to run in Resource Group rg-loans.');
    });

    it('a skill hold whose metadata names no setting says nothing about one', async () => {
        held({ capability_type: 'skill_exec', capability_name: 'sk-3', server_name: 'sk-3', request_metadata: { surface: 'skill', agent_id: 'agent-1' } });
        await openFirst();
        expect(screen.getByTestId('approval-skill')).toBeInTheDocument();
        expect(screen.queryByTestId('approval-required-by')).not.toBeInTheDocument();
    });

    it('a hold that is not a skill says nothing of a skill, and is approved as a call', async () => {
        await openFirst();
        expect(screen.queryByTestId('approval-skill')).not.toBeInTheDocument();
        expect(screen.queryByTestId('approval-required-by')).not.toBeInTheDocument();
        expect(screen.getByTestId('approval-detail')).toHaveTextContent('What the agent wants to call it with');
        expect(screen.getByTestId('approval-approve')).toHaveTextContent('Approve this call');
    });

    it('the queue is headed for any kind of call, not only a tool', async () => {
        render(<ApprovalsPage />);
        await screen.findByTestId('approval-row-apr-1');
        expect(screen.getByRole('columnheader', { name: 'Held call' })).toBeInTheDocument();
        expect(screen.queryByRole('columnheader', { name: 'Tool' })).not.toBeInTheDocument();
    });

    it('a skill call that came over A2A shows the message it carries, part by part', async () => {
        // The gateway holds such a call with what the message asks for as its
        // arguments: every part of the message, and its metadata.
        held({
            capability_type: 'skill_exec',
            capability_name: 'pay-invoice',
            server_name: 'sk-3',
            call_arguments: {
                parts: [
                    { text: 'Pay invoice 12 today.' },
                    { data: { amount: 1200, currency: 'EUR' }, mediaType: 'application/json' },
                    { url: 'https://files.example.com/invoice-12.pdf', filename: 'invoice-12.pdf', mediaType: 'application/pdf' },
                    { raw: 'aGVsbG8gd29ybGQ=', filename: 'note.txt' },
                ],
                metadata: { capability: 'pay' },
            },
        });
        await openFirst();
        const shown = screen.getByTestId('approval-arguments');
        const parts = screen.getAllByTestId('approval-message-part');
        expect(parts).toHaveLength(4);
        expect(parts[0]).toHaveTextContent('Part 1 · Text');
        expect(parts[0]).toHaveTextContent('Pay invoice 12 today.');
        expect(parts[1]).toHaveTextContent('Part 2 · Data · application/json');
        expect(parts[1]).toHaveTextContent('"amount": 1200');
        expect(parts[2]).toHaveTextContent('Part 3 · File at an address · invoice-12.pdf · application/pdf');
        expect(parts[2]).toHaveTextContent('https://files.example.com/invoice-12.pdf');
        expect(parts[3]).toHaveTextContent('Part 4 · File content · note.txt');
        expect(parts[3]).toHaveTextContent('16 characters of base64, not shown');
        expect(parts[3]).not.toHaveTextContent('aGVsbG8gd29ybGQ=');
        expect(screen.getByTestId('approval-message-metadata')).toHaveTextContent('"capability": "pay"');
        // Read as a message, not as one block of JSON.
        expect(shown).not.toHaveTextContent('"parts"');
        expect(shown).toHaveTextContent('The message the agent sent to the skill');
    });

    it('a message with no metadata shows none, and a skill call with ordinary input stays as its input', async () => {
        held({ capability_type: 'skill_exec', capability_name: 'pay-invoice', server_name: 'sk-3', call_arguments: { parts: [{ text: 'Hi' }], metadata: null } });
        await openFirst();
        expect(screen.getAllByTestId('approval-message-part')).toHaveLength(1);
        expect(screen.queryByTestId('approval-message-metadata')).not.toBeInTheDocument();
    });

    it('arguments that only look like a message are shown as they are: an MCP tool\'s, or a skill input with other fields', async () => {
        held({ capability_type: 'tool', capability_name: 'send', server_name: 'mail', call_arguments: { parts: [{ text: 'Hi' }], metadata: null } });
        await openFirst();
        expect(screen.queryByTestId('approval-message-part')).not.toBeInTheDocument();
        expect(screen.getByTestId('approval-arguments')).toHaveTextContent('"parts"');
    });

    it('an MCP hold with no resolved server says so, and can still be decided', async () => {
        held({ capability_type: 'tool', capability_name: 'send_payment', server_name: 'payments' });
        await openFirst();
        expect(screen.getByTestId('approval-subject')).toHaveTextContent('tool on MCP server payments.');
        expect(screen.getByTestId('approval-server-unresolved')).toHaveTextContent(
            'The gateway found no enabled MCP server configuration with this name when the request was raised.',
        );
        expect(screen.getByTestId('approval-detail')).not.toHaveTextContent('null');
        gatewayClient.approveToolApproval.mockResolvedValue({ approval_id: 'apr-1', status: 'approved' });
        fireEvent.click(screen.getByTestId('approval-approve'));
        await waitFor(() => expect(gatewayClient.approveToolApproval).toHaveBeenCalledWith('apr-1', undefined));
    });

    it('an MCP hold with neither a server name nor a configuration shows no empty name', async () => {
        held({ capability_type: 'tool', capability_name: 'send_payment', server_name: null });
        await openFirst();
        expect(screen.getByTestId('approval-subject')).toHaveTextContent('tool. No MCP server was recorded with this request.');
        expect(screen.getByTestId('approval-detail')).not.toHaveTextContent('null');
    });
});

describe('deciding', () => {
    it('shows the arguments the agent wants to call the tool with', async () => {
        await openFirst();
        expect(screen.getByTestId('approval-arguments')).toHaveTextContent('"amount": 1200');
        expect(screen.getByTestId('approval-arguments')).toHaveTextContent('"currency": "EUR"');
    });

    it('approves with an optional note, never shows the token, and reads the queue again', async () => {
        gatewayClient.approveToolApproval.mockResolvedValue({
            id: 'apr-1',
            status: 'approved',
            resolved_by: 'admin:1',
            resolved_at: iso(0),
            approval_token: 'SECRET-ONE-TIME-TOKEN',
            message: 'ok',
        });
        await openFirst();
        const readsBefore = gatewayClient.listToolApprovals.mock.calls.length;
        fireEvent.click(screen.getByTestId('approval-approve'));
        expect(await screen.findByTestId('approvals-notice')).toHaveTextContent('Approved send_payment');
        expect(gatewayClient.approveToolApproval).toHaveBeenCalledWith('apr-1', undefined);
        expect(document.body.textContent).not.toContain('SECRET-ONE-TIME-TOKEN');
        expect(screen.queryByTestId('approval-detail')).not.toBeInTheDocument();
        await waitFor(() => expect(gatewayClient.listToolApprovals.mock.calls.length).toBeGreaterThan(readsBefore));
    });

    it('rejects only with a reason', async () => {
        gatewayClient.rejectToolApproval.mockResolvedValue({
            id: 'apr-1',
            status: 'rejected',
            resolved_by: 'admin:1',
            resolved_at: iso(0),
            message: 'ok',
        });
        await openFirst();
        expect(screen.getByTestId('approval-reject')).toBeDisabled();
        fireEvent.change(screen.getByTestId('approval-note'), { target: { value: '  amount is over the limit ' } });
        expect(screen.getByTestId('approval-reject')).toBeEnabled();
        fireEvent.click(screen.getByTestId('approval-reject'));
        expect(await screen.findByTestId('approvals-notice')).toHaveTextContent('Rejected send_payment');
        expect(gatewayClient.rejectToolApproval).toHaveBeenCalledWith('apr-1', 'amount is over the limit');
        expect(gatewayClient.approveToolApproval).not.toHaveBeenCalled();
    });

    it('shows a refusal in the server’s words and the request as it now is', async () => {
        gatewayClient.approveToolApproval.mockRejectedValue(new Error('Approval request has expired'));
        await openFirst();
        gatewayClient.getToolApproval.mockResolvedValue(detail('apr-1', { status: 'expired', decision: 'expired' }));
        const readsBefore = gatewayClient.listToolApprovals.mock.calls.length;
        fireEvent.click(screen.getByTestId('approval-approve'));
        expect(await screen.findByTestId('approvals-action-error')).toHaveTextContent('Approval request has expired');
        expect(screen.queryByTestId('approvals-notice')).not.toBeInTheDocument();
        await waitFor(() => expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument());
        expect(screen.getByTestId('approval-resolution')).toHaveTextContent('The call was not made');
        expect(gatewayClient.listToolApprovals.mock.calls.length).toBeGreaterThan(readsBefore);
    });

    it('a decision that was not answered is not reported as made', async () => {
        gatewayClient.rejectToolApproval.mockRejectedValue(
            new Error('The approval could not be decided: the gateway core is unavailable.'),
        );
        await openFirst();
        fireEvent.change(screen.getByTestId('approval-note'), { target: { value: 'no' } });
        fireEvent.click(screen.getByTestId('approval-reject'));
        expect(await screen.findByTestId('approvals-action-error')).toHaveTextContent('gateway core is unavailable');
        expect(screen.queryByTestId('approvals-notice')).not.toBeInTheDocument();
        // still pending on the server: still offered
        expect(await screen.findByTestId('approval-approve')).toBeInTheDocument();
    });

    it('an administrator who may only read sees the request and no decision', async () => {
        permissions = ['tool_approval:read'];
        await openFirst();
        expect(screen.getByTestId('approval-arguments')).toBeInTheDocument();
        expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument();
        expect(screen.queryByTestId('approval-reject')).not.toBeInTheDocument();
        expect(screen.getByTestId('approval-read-only')).toHaveTextContent('tool_approval:manage');
    });
});

describe('staying current', () => {
    it('reads the queue again on its own while it is on screen, and stops when it is not', async () => {
        vi.useFakeTimers();
        try {
            const { unmount } = render(<ApprovalsPage />);
            await act(async () => {
                await vi.advanceTimersByTimeAsync(0);
            });
            const first = gatewayClient.listToolApprovals.mock.calls.length;
            expect(first).toBeGreaterThan(0);
            await act(async () => {
                await vi.advanceTimersByTimeAsync(15_000);
            });
            expect(gatewayClient.listToolApprovals.mock.calls.length).toBe(first + 1);
            unmount();
            await act(async () => {
                await vi.advanceTimersByTimeAsync(60_000);
            });
            expect(gatewayClient.listToolApprovals.mock.calls.length).toBe(first + 1);
        } finally {
            vi.useRealTimers();
        }
    });
});

describe('holds raised by one call', () => {
    it('are shown together under the call, in the order the queue gives the first of them', async () => {
        gatewayClient.listToolApprovals.mockResolvedValue({
            items: [
                summary('apr-1', { originating_trace_root_id: 'root-a', originating_trace_root_kind: 'llm' }),
                summary('apr-2', { capability_name: 'lookup', originating_trace_root_id: null }),
                summary('apr-3', { capability_name: 'refund', originating_trace_root_id: 'root-a', originating_trace_root_kind: 'llm' }),
            ],
            total: 3,
            page: 1,
            page_size: 100,
            status_filter: 'pending',
        });
        render(<ApprovalsPage />);
        const call = await screen.findByTestId('approval-call-root-a');
        expect(call).toHaveTextContent('One call raised these 2 holds');
        const rows = screen.getAllByTestId(/^approval-row-/).map((row) => row.getAttribute('data-testid'));
        expect(rows).toEqual(['approval-row-apr-1', 'approval-row-apr-3', 'approval-row-apr-2']);
        expect(screen.queryByTestId('approval-call-null')).toBeNull();
    });

    it('the detail of a hold lists the other holds of its call, read by its trace root', async () => {
        gatewayClient.getToolApproval.mockResolvedValue(detail('apr-1', { originating_trace_root_id: 'root-a' }));
        gatewayClient.listToolApprovals.mockImplementation((params?: { trace_root_id?: string }) =>
            Promise.resolve({
                items: params?.trace_root_id
                    ? [summary('apr-1', { originating_trace_root_id: 'root-a' }), summary('apr-9', { capability_name: 'refund', decision: 'approved', originating_trace_root_id: 'root-a' })]
                    : [summary('apr-1', { originating_trace_root_id: 'root-a' })],
                total: 1,
                page: 1,
                page_size: 100,
                status_filter: 'pending',
            }),
        );
        await openFirst();
        expect(gatewayClient.listToolApprovals).toHaveBeenCalledWith({ status_filter: 'all', trace_root_id: 'root-a', page_size: 100 });
        const siblings = await screen.findByTestId('approval-same-call');
        expect(siblings).toHaveTextContent('refund');
        expect(siblings).not.toHaveTextContent('send_payment');
    });
});
