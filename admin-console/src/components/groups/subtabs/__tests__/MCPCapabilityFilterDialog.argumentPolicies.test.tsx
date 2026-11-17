/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The argument policies that also gate a tool are read by the dialog, once,
 * and every row is given the answer: a dialog of N capabilities issues one
 * request for them, and a read that fails is said once, above the rows,
 * with a Retry, not on each row.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

import type { ArgumentPolicy } from '../../../../lib/argumentPolicyTypes';
import type { ServerConfigInfo } from '../../../../lib/groupTypes';
import { buildMCPServerConfig } from '../../../../test/mcp/mcpServerConfig';

const gateway = {
    getMCPCapabilityFilters: vi.fn(),
    updateMCPCapabilityFilters: vi.fn(),
    clearMCPCapabilityFilters: vi.fn(),
    listArgumentPolicies: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn() };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));

import MCPCapabilityFilterDialog from '../MCPCapabilityFilterDialog';

const SERVER: ServerConfigInfo = {
    id: 'srv-1',
    name: 'GitHub',
    description: '',
    enabled: true,
    icon_url: '',
    is_inherited: false,
    mcp_server: buildMCPServerConfig(),
};

const policy = (over: Partial<ArgumentPolicy> = {}): ArgumentPolicy => ({
    id: 'ap-1',
    tenant_id: 't',
    name: 'no-force-push',
    surface: 'mcp_input',
    target: 'delete_repo',
    argument_key: '*',
    analyzer: 'json',
    rules: [],
    enabled: true,
    severity: 'warn',
    group_ids: ['rg-1'],
    ...over,
});

const TOOLS = ['create_issue', 'delete_repo', 'merge_pr', 'list_repos'];

async function open() {
    render(<MCPCapabilityFilterDialog isOpen onClose={vi.fn()} groupId="rg-1" groupName="Eng" server={SERVER} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save Filters' })).toBeEnabled());
}

describe('MCPCapabilityFilterDialog — the argument policies that also gate a tool', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        gateway.getMCPCapabilityFilters.mockResolvedValue({
            has_filters: false,
            capabilities: [],
            discovered_capabilities: TOOLS.map((name) => ({ capability_type: 'tool', name })),
        });
        gateway.listArgumentPolicies.mockResolvedValue({ policies: [policy(), policy({ id: 'ap-2', name: 'any-tool', target: '*' })] });
    });

    afterEach(cleanup);

    it('are asked for once, whatever the number of rows, and each row shows its own', async () => {
        await open();
        expect(await screen.findByTestId('argument-policy-gate-delete_repo')).toHaveTextContent('2 argument policies also gate this');
        expect(screen.getByTestId('argument-policy-gate-create_issue')).toHaveTextContent('1 argument policy also gates this');
        expect(gateway.listArgumentPolicies).toHaveBeenCalledTimes(1);
        expect(gateway.listArgumentPolicies).toHaveBeenCalledWith({ surface: 'mcp_input' });
    });

    it('a read that fails is said once, above the rows, and Retry reads again', async () => {
        gateway.listArgumentPolicies.mockRejectedValueOnce(new Error('policy store down'));
        await open();
        const notices = await screen.findAllByText(/Failed to load the argument policies/);
        expect(notices).toHaveLength(1);
        expect(screen.getByTestId('argument-policies-failed')).toHaveTextContent(
            'Failed to load the argument policies that also gate these capabilities: policy store down',
        );
        expect(screen.queryByTestId('argument-policy-gate-delete_repo')).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        expect(await screen.findByTestId('argument-policy-gate-delete_repo')).toBeInTheDocument();
        expect(screen.queryByTestId('argument-policies-failed')).toBeNull();
        expect(gateway.listArgumentPolicies).toHaveBeenCalledTimes(2);
    });

    it('are not asked for while the dialog is closed', () => {
        render(<MCPCapabilityFilterDialog isOpen={false} onClose={vi.fn()} groupId="rg-1" groupName="Eng" server={SERVER} />);
        expect(gateway.listArgumentPolicies).not.toHaveBeenCalled();
    });
});
