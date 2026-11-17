/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Contracts of a system with dependencies — dependencies are contract terms:
 *
 * * a followed service promoting a new contract shows in the caller's diff
 *   as "dependency X widened (v1 → v2): …", and the service's own diff is
 *   one click away inside the entry;
 * * a dependency contract change that neither widens nor narrows (a
 *   re-attestation) is still named, not folded into a count;
 * * the sealed closure lists the dependency edges with the contract each
 *   shared service ran against (following vs pinned), names resolved.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const mockContracts = vi.fn();
const mockVersion = vi.fn();
const mockFetchAISystems = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
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
        fetchAISystems: mockFetchAISystems,
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
import { CLOSURE_DEPENDENCIES, DEPENDENCY_REATTESTED, DEPENDENCY_WIDENED } from './fixtures/dependencyHealthPayloads';
import { callsWhileIdle } from '../../../test/idle';

function contract(diff: { widened?: DiffEntry[]; narrowed?: DiffEntry[]; changed?: DiffEntry[] }): AISystemContract {
    const widened = diff.widened ?? [];
    const narrowed = diff.narrowed ?? [];
    const changed = diff.changed ?? [];
    return {
        id: 'ctr-demo-4',
        ai_system_id: 'sys-demo',
        version: 4,
        contract_hash: 'sha256:aa',
        short_hash: 'aa11bb22cc33',
        status: 'draft',
        diff_from_version: 3,
        diff_summary: {
            widened, narrowed, changed,
            widened_count: widened.length,
            narrowed_count: narrowed.length,
            changed_count: changed.length,
            has_widened_permissions: widened.length > 0,
        },
        is_active: false,
        is_approved: false,
    };
}

function listOf(c: AISystemContract) {
    return { contracts: [c], total: 1, active_version: 3, config_drifted: true };
}

beforeEach(() => {
    vi.clearAllMocks();
    mockFetchAISystems.mockResolvedValue({
        systems: [
            { id: 'sys-address', name: 'address', display_name: 'Address Validator' },
            { id: 'sys-scoring', name: 'scoring', display_name: 'Scoring Service' },
        ],
        total: 2,
    });
});

describe('dependency contract changes in the diff', () => {
    it('leads with the widened dependency and nests the service’s own diff', async () => {
        mockContracts.mockResolvedValue(listOf(contract({ widened: [DEPENDENCY_WIDENED] })));
        render(<ContractsPanel aiSystemId="sys-demo" />);
        const widened = await screen.findByTestId('widened');
        const entry = within(widened).getByTestId('diff-dependency-entry');
        expect(entry).toHaveTextContent('dependency Scoring Service widened (v1 → v2): model gpt-5 added');
        expect(within(entry).queryByTestId('diff-dependency-nested')).toBeNull();
        fireEvent.click(within(entry).getByTestId('diff-dependency-toggle'));
        const nested = within(entry).getByTestId('diff-dependency-nested');
        expect(within(nested).getByTestId('diff-dependency-nested-widened')).toHaveTextContent('model gpt-5 added');
        expect(within(nested).getByTestId('diff-dependency-nested-narrowed')).toHaveTextContent(
            'MCP server legacy-core removed',
        );
        expect(within(nested).queryByTestId('diff-dependency-nested-changed')).toBeNull();
    });

    it('names a re-attested dependency instead of hiding it in the count', async () => {
        mockContracts.mockResolvedValue(listOf(contract({ changed: [DEPENDENCY_REATTESTED] })));
        render(<ContractsPanel aiSystemId="sys-demo" />);
        const changed = await screen.findByTestId('changed-dependencies');
        expect(changed).toHaveTextContent('Address Validator re-attested (v2 → v3); its terms did not change');
        // No nested diff to open: the service's terms did not change.
        expect(within(changed).queryByTestId('diff-dependency-toggle')).toBeNull();
        expect(screen.getByTestId('no-widening')).toBeInTheDocument();
    });

    it('treats an unresolvable service diff as plain text', async () => {
        const unresolved: DiffEntry = { ...DEPENDENCY_WIDENED, dependency_diff: null, detail: 'Scoring Service changed contract (v1 → v2) — its diff could not be resolved' };
        mockContracts.mockResolvedValue(listOf(contract({ widened: [unresolved] })));
        render(<ContractsPanel aiSystemId="sys-demo" />);
        const entry = within(await screen.findByTestId('widened')).getByTestId('diff-dependency-entry');
        expect(entry).toHaveTextContent('could not be resolved');
        expect(within(entry).queryByTestId('diff-dependency-toggle')).toBeNull();
    });
});

describe('the sealed dependencies section', () => {
    it('lists each edge with the contract it ran against', async () => {
        const c = contract({});
        mockContracts.mockResolvedValue(listOf(c));
        mockVersion.mockResolvedValue({ ...c, resolved_closure: { ai_system: {}, composition: {}, dependencies: CLOSURE_DEPENDENCIES } });
        render(<ContractsPanel aiSystemId="sys-demo" />);
        fireEvent.click(await screen.findByTestId('closure-toggle-v4'));
        const section = await screen.findByTestId('closure-dependencies');
        const items = within(section).getAllByTestId('closure-dependency-item');
        expect(items).toHaveLength(3);
        expect(items[0]).toHaveTextContent('agent card card-bureau');
        expect(items[0]).toHaveTextContent('no contract (external)');
        expect(items[0]).toHaveTextContent('Assumed, not tested: Expected availability 99.5%');
        // Who supplies it is a signed term; only where set.
        expect(within(items[0]).getByTestId('closure-dependency-supplier')).toHaveTextContent('Supplier: Nordic Credit Bureau AB');
        expect(within(items[1]).queryByTestId('closure-dependency-supplier')).toBeNull();
        expect(items[0].className).toContain('border-dashed');
        expect(await within(items[1]).findByText('Address Validator')).toBeInTheDocument();
        expect(within(items[1]).getByTestId('closure-dependency-contract')).toHaveTextContent('pinned v2');
        expect(items[1]).toHaveTextContent('Separability: Formats addresses');
        expect(within(items[2]).getByTestId('closure-dependency-contract')).toHaveTextContent('follows v2');
    });

    it('names that could not be loaded: the edges are listed by id and the section says why', async () => {
        mockFetchAISystems.mockRejectedValue(new Error('systems down'));
        const c = contract({});
        mockContracts.mockResolvedValue(listOf(c));
        mockVersion.mockResolvedValue({ ...c, resolved_closure: { ai_system: {}, composition: {}, dependencies: CLOSURE_DEPENDENCIES } });
        render(<ContractsPanel aiSystemId="sys-demo" />);
        fireEvent.click(await screen.findByTestId('closure-toggle-v4'));
        const section = await screen.findByTestId('closure-dependencies');
        expect(await within(section).findByTestId('ai-system-names-failed')).toHaveTextContent(
            'Failed to load the names of the AI Systems; ids are shown in their place: systems down',
        );
        expect(within(section).queryByText('Address Validator')).toBeNull();
    });

    it('is absent from the closure of a system that declares no dependencies', async () => {
        const c = contract({});
        mockContracts.mockResolvedValue(listOf(c));
        mockVersion.mockResolvedValue({ ...c, resolved_closure: { ai_system: {}, composition: {} } });
        render(<ContractsPanel aiSystemId="sys-demo" />);
        fireEvent.click(await screen.findByTestId('closure-toggle-v4'));
        await screen.findByTestId('closure-ownership');
        expect(screen.queryByTestId('closure-dependencies')).toBeNull();
        expect(mockFetchAISystems).not.toHaveBeenCalled();
    });
});

describe('ContractsPanel — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        mockContracts.mockResolvedValue(listOf(contract({ widened: [DEPENDENCY_WIDENED] })));
        render(<ContractsPanel aiSystemId="sys-demo" />);
        await screen.findByTestId('widened');
        expect(await callsWhileIdle([mockContracts])).toBe(0);
    });
});
