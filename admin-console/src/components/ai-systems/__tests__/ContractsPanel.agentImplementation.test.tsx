/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Each member agent's implementation terms are part
 * of the contract: "What this version covers" lists them, and the diff
 * names the agent whose approved versions were widened or narrowed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const mockContracts = vi.fn();
const mockVersion = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchAISystems: vi.fn().mockResolvedValue({ systems: [], total: 0 }),
    fetchAISystemLifecycle: vi.fn(),
    mintAISystemContract: vi.fn(),
    approveContract: vi.fn(),
    rejectContract: vi.fn(),
    promoteContract: vi.fn(),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemContracts: mockContracts,
        fetchContractVersion: mockVersion,
        fetchAISystems: stableGateway.fetchAISystems,
        fetchAISystemLifecycle: stableGateway.fetchAISystemLifecycle,
        mintAISystemContract: stableGateway.mintAISystemContract,
        approveContract: stableGateway.approveContract,
        rejectContract: stableGateway.rejectContract,
        promoteContract: stableGateway.promoteContract,
    }),
}));
vi.mock('../EnvelopePanel', () => ({ default: () => <div /> }));
vi.mock('../EvidencePanel', () => ({ default: () => <div /> }));
vi.mock('../../../hooks/useAdminUsername', () => ({ useAdminUsername: () => 'admin' }));

import ContractsPanel from '../ContractsPanel';
import type { AISystemContract, DiffEntry } from '../../../lib/aiSystemTypes';
import { callsWhileIdle } from '../../../test/idle';

function contract(widened: DiffEntry[] = [], narrowed: DiffEntry[] = []): AISystemContract {
    return {
        id: 'ctr-5',
        ai_system_id: 'sys-demo',
        version: 5,
        contract_hash: 'sha256:aa',
        short_hash: 'aa11bb22cc33',
        status: 'draft',
        diff_from_version: 4,
        diff_summary: {
            widened,
            narrowed,
            changed: [],
            widened_count: widened.length,
            narrowed_count: narrowed.length,
            changed_count: 0,
            has_widened_permissions: widened.length > 0,
        },
        is_active: false,
        is_approved: false,
    };
}

const listOf = (c: AISystemContract) => ({ contracts: [c], total: 1, active_version: 4, config_drifted: true });

// The control plane's `_diff_agent_implementation` entries.
const APPROVED_ADDED: DiffEntry = {
    kind: 'agent_implementation', agent: 'Screening', agent_id: 'agent-1', field: 'approved_versions',
    version: '1.5.0', detail: 'release 1.5.0 approved',
};
const APPROVED_REMOVED: DiffEntry = {
    kind: 'agent_implementation', agent: 'Screening', agent_id: 'agent-1', field: 'approved_versions',
    version: '1.3.*', detail: 'release 1.3.* no longer approved',
};

beforeEach(() => vi.clearAllMocks());

describe('contract diff — approved_versions', () => {
    it('adding a version is widened, removing one narrowed; each names the agent', async () => {
        mockContracts.mockResolvedValue(listOf(contract([APPROVED_ADDED], [APPROVED_REMOVED])));
        render(<ContractsPanel aiSystemId="sys-demo" />);
        const widened = await screen.findByTestId('widened');
        expect(within(widened).getByTestId('diff-agent-implementation')).toHaveTextContent(
            'Agent Screening: release 1.5.0 approved',
        );
        const narrowed = screen.getByTestId('narrowed');
        expect(within(narrowed).getByTestId('diff-agent-implementation')).toHaveTextContent(
            'Agent Screening: release 1.3.* no longer approved',
        );
    });
});

describe('What this version covers — agent implementation', () => {
    it('lists each member agent’s signed terms, and observe-only for one that declares none', async () => {
        const c = contract();
        mockContracts.mockResolvedValue(listOf(c));
        mockVersion.mockResolvedValue({
            ...c,
            resolved_closure: {
                ai_system: {},
                composition: {
                    agent_identities: [
                        { id: 'agent-1', name: 'Screening', implementation_name: 'screening-agent', approved_versions: ['1.4.*', '1.5.0'], require_release: true },
                        // Conditional keys: an identity that declares nothing carries none.
                        { id: 'agent-2', name: 'Fraud' },
                    ],
                },
            },
        });
        render(<ContractsPanel aiSystemId="sys-demo" />);
        fireEvent.click(await screen.findByTestId('closure-toggle-v5'));
        const block = await screen.findByTestId('closure-agent-implementation');
        const screening = within(block).getByTestId('closure-agent-implementation-agent-1');
        expect(screening).toHaveTextContent('Screening');
        expect(screening).toHaveTextContent('must declare implementation “screening-agent”');
        expect(screening).toHaveTextContent('approved releases: 1.4.*, 1.5.0');
        expect(screening).toHaveTextContent('runs without a declared release are a finding');
        expect(within(block).getByTestId('closure-agent-implementation-agent-2')).toHaveTextContent('observe only');
    });

    it('is absent when the system has no member agents', async () => {
        const c = contract();
        mockContracts.mockResolvedValue(listOf(c));
        mockVersion.mockResolvedValue({ ...c, resolved_closure: { ai_system: {}, composition: {} } });
        render(<ContractsPanel aiSystemId="sys-demo" />);
        fireEvent.click(await screen.findByTestId('closure-toggle-v5'));
        await screen.findByTestId('closure-ownership');
        expect(screen.queryByTestId('closure-agent-implementation')).toBeNull();
    });
});

describe('ContractsPanel — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        mockContracts.mockResolvedValue(listOf(contract([APPROVED_ADDED], [APPROVED_REMOVED])));
        render(<ContractsPanel aiSystemId="sys-demo" />);
        await screen.findByTestId('widened');
        expect(await callsWhileIdle([mockContracts])).toBe(0);
    });
});
