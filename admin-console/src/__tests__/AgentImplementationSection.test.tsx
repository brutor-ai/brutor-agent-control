/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Agents → Identities → identity → Implementation: the
 * declaration editor, its contract note, the release timeline, and the deep
 * link the inbox's agent findings use.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import AgentIdentitiesResourceTab from '../components/agent/AgentIdentitiesResourceTab';
import type { AgentRelease } from '../lib/agentIdentityTypes';

const _toast = { showToast: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('../components/common/Toast', () => ({ useToast: () => _toast }));

const mockList = vi.fn();
const mockUpdate = vi.fn();
const mockCreate = vi.fn();
const mockReleases = vi.fn();

const _client = {
    listAgentIdentities: mockList,
    createAgentIdentity: mockCreate,
    updateAgentIdentity: mockUpdate,
    revokeAgentIdentity: vi.fn(),
    listAgentMemberships: vi.fn().mockResolvedValue({ memberships: [], total: 0 }),
    addAgentMembership: vi.fn(),
    removeAgentMembership: vi.fn(),
    listAgentReleases: mockReleases,
};
const _fetchTree = vi.fn().mockResolvedValue({ tree: [] });
vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: _client, fetchGroupHierarchyTree: _fetchTree }),
}));

function agent(over = {}) {
    return {
        id: 'agent-1', tenant_id: 't', name: 'screening-bot', source: 'manual',
        enabled: true, status: 'active', default_deny: true, enforcement_mode: 'enforce',
        implementation_name: 'screening-agent', approved_versions: ['1.4.*'], require_release: false,
        ...over,
    };
}

function release(over: Partial<AgentRelease> = {}): AgentRelease {
    return {
        id: 'rel-1', agent_id: 'agent-1', name: 'screening-agent', version: '1.4.2', build: null,
        label: 'screening-agent@1.4.2', trust: 'declared',
        first_seen_at: '2026-09-20T10:00:00Z', last_seen_at: '2026-09-27T10:00:00Z',
        run_count: 42, fingerprints: ['sha256:aa'], fingerprint_count: 1,
        approved: true, name_matches: true, current: false,
        ...over,
    };
}

const RELEASES = [
    release({
        id: 'rel-2', version: '1.5.0', label: 'screening-agent@1.5.0', trust: 'verified',
        approved: false, current: true, fingerprints: ['sha256:aa', 'sha256:bb'], fingerprint_count: 2,
        build: `sha256:${'c'.repeat(64)}`,
    }),
    release(),
    release({ id: 'rel-3', name: 'other-agent', version: '0.1.0', label: 'other-agent@0.1.0', approved: false, name_matches: false }),
];

beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({ agents: [agent()], total: 1 });
    mockUpdate.mockResolvedValue(agent());
    mockCreate.mockResolvedValue(agent({ id: 'agent-2' }));
    mockReleases.mockResolvedValue({
        agent_id: 'agent-1', implementation_name: 'screening-agent', approved_versions: ['1.4.*'],
        require_release: false, current_release_id: 'rel-2', releases: RELEASES, total: 3,
    });
});

async function openEdit() {
    render(<AgentIdentitiesResourceTab />);
    await waitFor(() => expect(screen.getByText('screening-bot')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Edit'));
    return screen.findByTestId('agent-implementation');
}

describe('Implementation section — declaration', () => {
    it('shows the saved declaration: name, approved versions, require-release', async () => {
        await openEdit();
        expect(screen.getByTestId('impl-name-input')).toHaveValue('screening-agent');
        expect(screen.getByTestId('impl-version-1.4.*')).toBeInTheDocument();
        expect(screen.getByTestId('impl-require-release')).not.toBeChecked();
        // Nothing changed yet: no contract note.
        expect(screen.queryByTestId('impl-contract-note')).toBeNull();
    });

    it('refuses a pattern the server would refuse, before it is sent', async () => {
        await openEdit();
        const input = screen.getByTestId('impl-version-input');
        for (const bad of ['1.*.2', '1.4*', '*', 'a b']) {
            fireEvent.change(input, { target: { value: bad } });
            expect(screen.getByTestId('impl-version-error')).toBeInTheDocument();
            expect(screen.getByTestId('impl-version-add')).toBeDisabled();
        }
        fireEvent.change(input, { target: { value: '1.5.0' } });
        expect(screen.queryByTestId('impl-version-error')).toBeNull();
        expect(screen.getByTestId('impl-version-add')).toBeEnabled();
    });

    it('approving a version explains it changes the AI Systems’ contracts, and saves the normalised set', async () => {
        await openEdit();
        fireEvent.change(screen.getByTestId('impl-version-input'), { target: { value: ' 1.5.0 ' } });
        fireEvent.click(screen.getByTestId('impl-version-add'));
        expect(screen.getByTestId('impl-version-1.5.0')).toBeInTheDocument();
        expect(screen.getByTestId('impl-contract-note')).toHaveTextContent(/part of the contract of every AI System/);
        expect(screen.getByTestId('impl-contract-note')).toHaveTextContent(/new contract version is minted and approved/);
        fireEvent.click(screen.getByTestId('impl-require-release'));
        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() =>
            expect(mockUpdate).toHaveBeenCalledWith(
                'agent-1',
                expect.objectContaining({
                    implementation_name: 'screening-agent',
                    approved_versions: ['1.4.*', '1.5.0'],
                    require_release: true,
                }),
            ),
        );
    });

    it('removing the last pattern returns to observe only; clearing the name sends null', async () => {
        await openEdit();
        fireEvent.click(screen.getByTestId('impl-version-remove-1.4.*'));
        expect(screen.getByTestId('impl-observe-only')).toBeInTheDocument();
        fireEvent.change(screen.getByTestId('impl-name-input'), { target: { value: '' } });
        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() =>
            expect(mockUpdate).toHaveBeenCalledWith(
                'agent-1',
                expect.objectContaining({ implementation_name: null, approved_versions: [] }),
            ),
        );
    });

    it('an invalid implementation name blocks Save', async () => {
        await openEdit();
        fireEvent.change(screen.getByTestId('impl-name-input'), { target: { value: 'not valid!' } });
        expect(screen.getByTestId('impl-name-error')).toBeInTheDocument();
        expect(screen.getByText('Save Changes').closest('button')).toBeDisabled();
    });

    it('surfaces the server’s 422 reason', async () => {
        mockUpdate.mockRejectedValueOnce(new Error("approved version '1.x*' must be an exact version"));
        await openEdit();
        fireEvent.click(screen.getByTestId('impl-require-release'));
        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() => expect(_toast.error).toHaveBeenCalledWith(expect.stringContaining('must be an exact version')));
    });

    it('a new identity is created with its declaration', async () => {
        mockList.mockResolvedValue({ agents: [], total: 0 });
        render(<AgentIdentitiesResourceTab />);
        fireEvent.click(await screen.findByText('New Agent'));
        fireEvent.change(await screen.findByPlaceholderText('billing-bot'), { target: { value: 'new-bot' } });
        fireEvent.change(screen.getByTestId('impl-name-input'), { target: { value: 'New-Bot' } });
        fireEvent.change(screen.getByTestId('impl-version-input'), { target: { value: '2.*' } });
        fireEvent.click(screen.getByTestId('impl-version-add'));
        fireEvent.click(screen.getByText('Create'));
        await waitFor(() =>
            expect(mockCreate).toHaveBeenCalledWith(
                expect.objectContaining({ implementation_name: 'new-bot', approved_versions: ['2.*'], require_release: false }),
            ),
        );
        // No identity yet, so no releases to fetch.
        expect(mockReleases).not.toHaveBeenCalled();
    });
});

describe('Implementation section — release timeline', () => {
    it('lists each release with trust, approval, first/last seen, runs and fingerprints', async () => {
        await openEdit();
        const row = await screen.findByTestId('impl-release-rel-2');
        expect(row).toHaveTextContent('screening-agent@1.5.0');
        expect(within(row).getByTestId('trust-badge')).toHaveAttribute('data-trust', 'verified');
        expect(within(row).getByTestId('trust-badge').getAttribute('title')).toMatch(/verified workload token/);
        expect(within(row).getByTestId('approval-badge')).toHaveAttribute('data-approval', 'unapproved');
        expect(within(row).getByTestId('impl-release-current')).toBeInTheDocument();
        expect(row).toHaveTextContent(/first .*ago|first/);
        expect(row).toHaveTextContent('42');

        const old = screen.getByTestId('impl-release-rel-1');
        expect(within(old).getByTestId('trust-badge')).toHaveAttribute('data-trust', 'declared');
        expect(within(old).getByTestId('trust-badge').getAttribute('title')).toMatch(/reported its own name and version/);
        expect(within(old).getByTestId('approval-badge')).toHaveAttribute('data-approval', 'approved');
    });

    it('marks an undeclared change only on a release with more than one fingerprint', async () => {
        await openEdit();
        const changed = await screen.findByTestId('impl-release-rel-2');
        expect(within(changed).getByTestId('impl-release-undeclared-change')).toBeInTheDocument();
        expect(within(screen.getByTestId('impl-release-rel-1')).queryByTestId('impl-release-undeclared-change')).toBeNull();
    });

    it('marks a release whose name is not the declared implementation', async () => {
        await openEdit();
        const other = await screen.findByTestId('impl-release-rel-3');
        expect(within(other).getByTestId('impl-release-name-mismatch')).toBeInTheDocument();
        expect(within(screen.getByTestId('impl-release-rel-1')).queryByTestId('impl-release-name-mismatch')).toBeNull();
    });

    it('previews approval under the edited versions before saving', async () => {
        await openEdit();
        await screen.findByTestId('impl-release-rel-2');
        fireEvent.change(screen.getByTestId('impl-version-input'), { target: { value: '1.5.*' } });
        fireEvent.click(screen.getByTestId('impl-version-add'));
        expect(screen.getByTestId('impl-approval-preview')).toBeInTheDocument();
        expect(within(screen.getByTestId('impl-release-rel-2')).getByTestId('approval-badge')).toHaveAttribute(
            'data-approval',
            'approved',
        );
    });

    it('says so when the agent never declared a release', async () => {
        mockReleases.mockResolvedValue({
            agent_id: 'agent-1', implementation_name: null, approved_versions: [], require_release: false,
            current_release_id: null, releases: [], total: 0,
        });
        await openEdit();
        expect(await screen.findByTestId('impl-releases-empty')).toBeInTheDocument();
    });
});

describe('deep link a2a-identities:<id>:implementation', () => {
    it('opens that identity on its Implementation section', async () => {
        const scroll = vi.fn();
        Element.prototype.scrollIntoView = scroll;
        render(<AgentIdentitiesResourceTab initialAgentId="agent-1" initialSection="implementation" />);
        expect(await screen.findByTestId('agent-implementation')).toBeInTheDocument();
        expect(screen.getByText(/Edit Agent Identity — screening-bot/)).toBeInTheDocument();
        expect(scroll).toHaveBeenCalled();
        // Closing it does not reopen it.
        fireEvent.click(screen.getByText('Cancel'));
        await waitFor(() => expect(screen.queryByTestId('agent-implementation')).toBeNull());
    });

    it('opens nothing for an identity that no longer exists', async () => {
        render(<AgentIdentitiesResourceTab initialAgentId="gone" initialSection="implementation" />);
        await waitFor(() => expect(screen.getByText('screening-bot')).toBeInTheDocument());
        expect(screen.queryByTestId('agent-implementation')).toBeNull();
    });
});
