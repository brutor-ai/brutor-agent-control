/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The read-only list of argument policies bound to a group.
 *
 * Bindings are owned by the policy, not by the group, so this pane is a
 * mirror — and a mirror has to be honest about three things:
 *
 *   - it lists only the policies bound to THIS group;
 *   - a disabled policy reads as "inactive", not as the severity it would
 *     have applied, because an operator scanning for what is in force must
 *     not count a switched-off rule;
 *   - a failed load says so rather than rendering as "no policies apply",
 *     which is the reading that would let someone conclude a tool is
 *     ungoverned when it is not.
 *
 * The search box appears only once the list is long enough to need it.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockListArgumentPolicies = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({ listArgumentPolicies: mockListArgumentPolicies }),
}));

import GroupArgumentPoliciesTab from '../GroupArgumentPoliciesTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';
import type { ArgumentPolicy } from '../../../../lib/argumentPolicyTypes';

const GROUP = { id: 'rg-1', name: 'engineering' } as ResourceGroup;

function policy(overrides: Partial<ArgumentPolicy> = {}): ArgumentPolicy {
    return {
        id: 'ap-1',
        tenant_id: 't1',
        name: 'No prod schema writes',
        description: 'Refuses DDL against the production schema',
        surface: 'mcp_input',
        target: 'postgres.query',
        argument_key: 'sql',
        analyzer: 'sql',
        rules: [{ predicate: 'writes_to', value: 'public' }],
        enabled: true,
        severity: 'deny',
        group_ids: ['rg-1'],
        ...overrides,
    } as ArgumentPolicy;
}

const renderTab = () => render(<GroupArgumentPoliciesTab group={GROUP} />);

beforeEach(() => {
    vi.clearAllMocks();
    mockListArgumentPolicies.mockResolvedValue({ policies: [] });
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('GroupArgumentPoliciesTab — what it lists', () => {
    it('says it is loading before claiming anything about coverage', () => {
        mockListArgumentPolicies.mockReturnValue(new Promise(() => { /* never settles */ }));
        renderTab();
        expect(screen.getByText(/Loading argument policies/)).toBeInTheDocument();
        expect(screen.queryByText('No Argument Policies Apply')).toBeNull();
    });

    it('shows only the policies bound to this group', async () => {
        mockListArgumentPolicies.mockResolvedValue({
            policies: [policy(), policy({ id: 'ap-2', name: 'Other group rule', group_ids: ['rg-9'] })],
        });
        renderTab();
        expect(await screen.findByText('No prod schema writes')).toBeInTheDocument();
        expect(screen.queryByText('Other group rule')).toBeNull();
        expect(screen.getByText('1 policy apply to this group')).toBeInTheDocument();
    });

    it('a failed load says so instead of reading as "nothing governs this"', async () => {
        mockListArgumentPolicies.mockRejectedValue(new Error('policy service down'));
        renderTab();
        expect(await screen.findByText('Failed to load argument policies')).toBeInTheDocument();
        expect(screen.getByText('policy service down')).toBeInTheDocument();
        expect(screen.queryByText('No Argument Policies Apply')).toBeNull();
    });

    it('an empty binding set points at where bindings are made', async () => {
        renderTab();
        expect(await screen.findByText('No Argument Policies Apply')).toBeInTheDocument();
        expect(
            screen.getByText(/Bind a policy from Governance → Argument Policies → Access Control/),
        ).toBeInTheDocument();
    });
});

describe('GroupArgumentPoliciesTab — reading a row', () => {
    it('names the surface, target, argument, analyzer and rule count', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: [policy()] });
        renderTab();
        await screen.findByText('No prod schema writes');
        expect(screen.getByText('MCP inbound tool/call')).toBeInTheDocument();
        expect(screen.getByText('postgres.query')).toBeInTheDocument();
        expect(screen.getByText('sql', { selector: 'span.font-mono' })).toBeInTheDocument();
        expect(screen.getByText('SQL')).toBeInTheDocument();
        expect(screen.getByText('Refuses DDL against the production schema')).toBeInTheDocument();
    });

    it('a switched-off policy reads as inactive, never as the severity it would apply', async () => {
        mockListArgumentPolicies.mockResolvedValue({
            policies: [policy({ enabled: false, severity: 'deny' })],
        });
        renderTab();
        expect(await screen.findByText('inactive')).toBeInTheDocument();
        expect(screen.queryByText('deny')).toBeNull();
    });

    it('an enabled policy shows the severity it enforces', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: [policy({ severity: 'warn' })] });
        renderTab();
        expect(await screen.findByText('warn')).toBeInTheDocument();
    });
});

describe('GroupArgumentPoliciesTab — searching', () => {
    const many = [
        policy({ id: 'p1', name: 'SQL guard', target: 'postgres.query', argument_key: 'sql' }),
        policy({ id: 'p2', name: 'URL guard', target: 'http.fetch', argument_key: 'url' }),
        policy({ id: 'p3', name: 'Shell guard', target: 'shell.run', argument_key: 'cmd' }),
        policy({ id: 'p4', name: 'Path guard', target: 'fs.read', argument_key: 'path' }),
    ];

    it('offers no search box for a list short enough to read', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: [policy()] });
        renderTab();
        await screen.findByText('No prod schema writes');
        expect(screen.queryByPlaceholderText('Search...')).toBeNull();
    });

    it('offers one once the list gets long', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: many });
        renderTab();
        expect(await screen.findByPlaceholderText('Search...')).toBeInTheDocument();
    });

    it('filters on the policy name', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: many });
        renderTab();
        fireEvent.change(await screen.findByPlaceholderText('Search...'), { target: { value: 'shell' } });
        expect(screen.getByText('Shell guard')).toBeInTheDocument();
        expect(screen.queryByText('SQL guard')).toBeNull();
    });

    it('filters on the target and on the argument key too', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: many });
        renderTab();
        const box = await screen.findByPlaceholderText('Search...');

        fireEvent.change(box, { target: { value: 'http.fetch' } });
        expect(screen.getByText('URL guard')).toBeInTheDocument();

        fireEvent.change(box, { target: { value: 'path' } });
        expect(screen.getByText('Path guard')).toBeInTheDocument();
        expect(screen.queryByText('URL guard')).toBeNull();
    });

    it('a search with no hits says so, and keeps the total honest', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: many });
        renderTab();
        fireEvent.change(await screen.findByPlaceholderText('Search...'), { target: { value: 'zzz' } });
        expect(screen.getByText('No Matches')).toBeInTheDocument();
        expect(screen.getByText('No policies match "zzz"')).toBeInTheDocument();
        // The count still reports what is BOUND, not what survived the filter.
        expect(screen.getByText('4 policies apply to this group')).toBeInTheDocument();
    });

    it('clearing the search brings the whole list back', async () => {
        mockListArgumentPolicies.mockResolvedValue({ policies: many });
        renderTab();
        const box = await screen.findByPlaceholderText('Search...');
        fireEvent.change(box, { target: { value: 'shell' } });
        fireEvent.click(screen.getByRole('button'));
        await waitFor(() => expect(screen.getByText('SQL guard')).toBeInTheDocument());
        expect(box).toHaveValue('');
    });
});
