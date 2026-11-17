/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * DataScopePanel — run data scope and record reach.
 *
 * Pinned:
 *   - the stored config renders (mode, keys, rules, unscoped tools);
 *   - save PUTs the edited config and shows the API's warnings verbatim;
 *   - a 422 shows the control plane's `detail`, not a generic failure;
 *   - the activity table highlights verdicts `enforce` would refuse;
 *   - a reach preview shows over-limit groups and p95/p99 next to the limit;
 *   - a reach rule is created from the form, grouped by a scope key.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockFetch = vi.fn();
const mockUpdate = vi.fn();
const mockActivity = vi.fn();
const mockRules = vi.fn();
const mockCreateRule = vi.fn();
const mockUpdateRule = vi.fn();
const mockDeleteRule = vi.fn();
const mockPreview = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchDataScope: mockFetch,
        updateDataScope: mockUpdate,
        fetchDataScopeActivity: mockActivity,
        fetchReachRules: mockRules,
        createReachRule: mockCreateRule,
        updateReachRule: mockUpdateRule,
        deleteReachRule: mockDeleteRule,
        previewReachRule: mockPreview,
    }),
}));

import DataScopePanel from '../DataScopePanel';
import type { DataScopeConfig, ReachRule } from '../../../lib/aiSystemTypes';

function config(overrides: Partial<DataScopeConfig> = {}): DataScopeConfig {
    return {
        ai_system_id: 'sys-1',
        configured: true,
        mode: 'audit',
        on_unbound: 'deny',
        keys: [{ key: 'customer_id', source: 'declared' }],
        rules: [{ tool: 'get_customer', argument: 'id', key: 'customer_id' }],
        unscoped_tools: ['search_docs'],
        version: 3,
        updated_at: '2026-11-07T10:00:00Z',
        updated_by: 'admin',
        warnings: [],
        ...overrides,
    };
}

function rule(overrides: Partial<ReachRule> = {}): ReachRule {
    return {
        id: 'rrl-1',
        ai_system_id: 'sys-1',
        scope_key: 'customer_id',
        group_by: 'subject',
        window_hours: 24,
        max_distinct: 50,
        severity: 'high',
        enabled: true,
        ...overrides,
    };
}

describe('DataScopePanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetch.mockResolvedValue(config());
        mockActivity.mockResolvedValue({
            ai_system_id: 'sys-1',
            days: 7,
            rows: [
                { tool: 'get_customer', verdict: 'in_scope', count: 120 },
                { tool: 'get_customer', verdict: 'out_of_scope', count: 4 },
                { tool: 'search_docs', verdict: 'unscoped_tool', count: 30 },
            ],
        });
        mockRules.mockResolvedValue({ rules: [] });
    });

    it('renders the stored config', async () => {
        render(<DataScopePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('data-scope-status')).toHaveTextContent('Version 3');
        expect(screen.getByTestId('data-scope-mode')).toHaveValue('audit');
        expect(screen.getByTestId('data-scope-on-unbound')).toHaveValue('deny');
        expect(screen.getByTestId('data-scope-key-name')).toHaveValue('customer_id');
        expect(screen.getByTestId('data-scope-key-source')).toHaveValue('declared');
        expect(screen.getAllByTestId('data-scope-rule-row')).toHaveLength(1);
        expect(screen.getByLabelText('Rule 1 tool')).toHaveValue('get_customer');
        expect(screen.getByTestId('data-scope-unscoped')).toHaveValue('search_docs');
        expect(mockActivity).toHaveBeenCalledWith('sys-1', 7);
    });

    it('says plainly when nothing is configured', async () => {
        mockFetch.mockResolvedValue(
            config({ configured: false, mode: 'off', keys: [], rules: [], unscoped_tools: [], version: 0 }),
        );
        render(<DataScopePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('data-scope-status')).toHaveTextContent('Not configured');
        // A reach limit counts a key, so none can be added before one exists.
        expect(screen.getByTestId('reach-add')).toBeDisabled();
    });

    it('highlights the calls enforce would refuse', async () => {
        render(<DataScopePanel aiSystemId="sys-1" />);
        const row = await screen.findByTestId('data-scope-activity-get_customer');
        expect(within(row).getByTestId('data-scope-violation')).toHaveTextContent('4');
        expect(within(screen.getByTestId('data-scope-activity-search_docs')).queryByTestId('data-scope-violation')).toBeNull();
    });

    it('saves the edited config and shows the warnings verbatim', async () => {
        mockUpdate.mockResolvedValue(
            config({ mode: 'enforce', version: 4, warnings: ['1 reach rule(s) count a key this config no longer has.'] }),
        );
        render(<DataScopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('data-scope-status');
        fireEvent.change(screen.getByTestId('data-scope-mode'), { target: { value: 'enforce' } });
        fireEvent.click(screen.getByTestId('data-scope-add-rule'));
        fireEvent.change(screen.getByLabelText('Rule 2 tool'), { target: { value: 'update_customer' } });
        fireEvent.change(screen.getByLabelText('Rule 2 argument'), { target: { value: 'customer' } });
        fireEvent.change(screen.getByTestId('data-scope-unscoped'), { target: { value: 'search_docs, get_weather' } });
        fireEvent.click(screen.getByTestId('data-scope-save'));

        await waitFor(() =>
            expect(mockUpdate).toHaveBeenCalledWith('sys-1', {
                mode: 'enforce',
                on_unbound: 'deny',
                keys: [{ key: 'customer_id', source: 'declared' }],
                rules: [
                    { tool: 'get_customer', argument: 'id', key: 'customer_id' },
                    { tool: 'update_customer', argument: 'customer', key: 'customer_id' },
                ],
                unscoped_tools: ['search_docs', 'get_weather'],
            }),
        );
        expect(await screen.findByTestId('data-scope-warning')).toHaveTextContent(
            '1 reach rule(s) count a key this config no longer has.',
        );
        expect(screen.getByTestId('data-scope-status')).toHaveTextContent('Version 4');
    });

    it("shows the control plane's 422 detail on an invalid config", async () => {
        mockUpdate.mockRejectedValue(
            Object.assign(new Error("rule 1: key 'account_id' is not a configured key"), { status: 422 }),
        );
        render(<DataScopePanel aiSystemId="sys-1" />);
        await screen.findByTestId('data-scope-status');
        fireEvent.click(screen.getByTestId('data-scope-save'));
        expect(await screen.findByTestId('data-scope-save-error')).toHaveTextContent(
            "rule 1: key 'account_id' is not a configured key",
        );
        expect(screen.queryByTestId('data-scope-saved')).not.toBeInTheDocument();
    });

    it('previews a draft reach limit against the last 30 days', async () => {
        mockPreview.mockResolvedValue({
            days: 30,
            groups_seen: 12,
            groups_over_limit: 2,
            top: [
                { group: 'user-7', max_distinct_records: 180 },
                { group: 'user-2', max_distinct_records: 40 },
            ],
            p95: 95,
            p99: 170,
            partial: false,
        });
        render(<DataScopePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('reach-add'));
        fireEvent.change(screen.getByTestId('reach-max'), { target: { value: '100' } });
        fireEvent.click(screen.getByTestId('reach-preview'));

        const result = await screen.findByTestId('reach-preview-result');
        expect(mockPreview).toHaveBeenCalledWith('sys-1', {
            scope_key: 'customer_id',
            group_by: 'subject',
            window_hours: 24,
            max_distinct: 100,
            severity: 'high',
            enabled: true,
        });
        expect(within(result).getByTestId('reach-over-limit')).toHaveTextContent('2');
        expect(result).toHaveTextContent('of 12 groups');
        expect(within(result).getByTestId('reach-percentiles')).toHaveTextContent('p95 95 · p99 170 · limit 100');
        expect(within(result).getByTestId('reach-top')).toHaveTextContent('user-7');
        expect(within(result).queryByTestId('reach-partial')).toBeNull();
    });

    it('creates a reach rule grouped by a scope key', async () => {
        mockCreateRule.mockResolvedValue(rule({ group_by: 'scope:customer_id' }));
        render(<DataScopePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('reach-add'));
        fireEvent.change(screen.getByTestId('reach-group-by'), { target: { value: 'scope:customer_id' } });
        fireEvent.change(screen.getByTestId('reach-window'), { target: { value: '48' } });
        fireEvent.change(screen.getByTestId('reach-max'), { target: { value: '20' } });
        fireEvent.change(screen.getByTestId('reach-severity'), { target: { value: 'critical' } });
        mockRules.mockResolvedValue({ rules: [rule({ id: 'rrl-9', group_by: 'scope:customer_id' })] });
        fireEvent.click(screen.getByTestId('reach-create'));

        await waitFor(() =>
            expect(mockCreateRule).toHaveBeenCalledWith('sys-1', {
                scope_key: 'customer_id',
                group_by: 'scope:customer_id',
                window_hours: 48,
                max_distinct: 20,
                severity: 'critical',
                enabled: true,
            }),
        );
        expect(await screen.findByTestId('reach-rule-rrl-9')).toHaveTextContent('Per customer_id');
        expect(screen.queryByTestId('reach-form')).not.toBeInTheDocument();
    });

    it('toggles and deletes an existing reach rule', async () => {
        mockRules.mockResolvedValue({ rules: [rule()] });
        mockUpdateRule.mockResolvedValue(rule({ enabled: false }));
        mockDeleteRule.mockResolvedValue(undefined);
        render(<DataScopePanel aiSystemId="sys-1" />);
        const row = await screen.findByTestId('reach-rule-rrl-1');
        fireEvent.click(within(row).getByTestId('reach-rule-enabled'));
        await waitFor(() => expect(mockUpdateRule).toHaveBeenCalledWith('sys-1', 'rrl-1', { enabled: false }));
        fireEvent.click(within(await screen.findByTestId('reach-rule-rrl-1')).getByTestId('reach-rule-delete'));
        await waitFor(() => expect(mockDeleteRule).toHaveBeenCalledWith('sys-1', 'rrl-1'));
    });

    it('surfaces a load failure instead of an empty panel', async () => {
        mockFetch.mockRejectedValue(new Error('control plane unreachable'));
        render(<DataScopePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('data-scope-error')).toHaveTextContent('control plane unreachable');
    });
});
