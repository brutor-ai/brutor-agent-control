/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent Cards screen: list, detail, sign, revoke, delete, health, tenant key.
 *
 * Signing and revoking are the trust decisions on this screen, so the tests pin
 * that nothing is sent without confirmation and what is sent when it is given.
 * Three more things are held here: the group tree is not refetched in a loop when
 * it comes back empty, "Enabled only" keeps its checkbox when no card matches,
 * and no JSX string renders a literal backslash-u escape.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within, act } from '@testing-library/react';

const gatewayClient = {
    listA2AAgentCards: vi.fn(),
    getA2AAgentCard: vi.fn(),
    bulkHealthCheckA2AAgentCards: vi.fn(),
    healthCheckA2AAgentCard: vi.fn(),
    signA2AAgentCard: vi.fn(),
    revokeA2AAgentCard: vi.fn(),
    deleteA2AAgentCard: vi.fn(),
    getA2ATenantPublicKey: vi.fn(),
    fetchGroupHierarchyTree: vi.fn(),
};
const auth = { gatewayClient };
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
const confirm = vi.fn<(o: { title: string; message: string; confirmLabel: string; variant: string }) => Promise<boolean>>();

vi.mock('../../../hooks/useAuth', () => ({ useAuth: () => auth }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

vi.mock('../AgentCardEditDialog', () => ({
    default: ({ initial, onSaved, onClose }: { initial: { name: string }; onSaved: () => void; onClose: () => void }) => (
        <div>
            editing {initial.name}
            <button onClick={onSaved}>edit saved</button>
            <button onClick={onClose}>edit close</button>
        </div>
    ),
}));
vi.mock('../AgentCardCreateDialog', () => ({
    default: ({ onCreated, onClose }: { onCreated: (d: unknown) => void; onClose: () => void }) => (
        <div>
            create dialog
            <button onClick={() => onCreated(DETAIL)}>created</button>
            <button onClick={onClose}>create close</button>
        </div>
    ),
}));
vi.mock('../AgentCardImportModal', () => ({
    CardIcon: () => <span />,
    default: ({ onImported, onClose }: { onImported: () => void; onClose: () => void }) => (
        <div>
            import modal
            <button onClick={onImported}>imported</button>
            <button onClick={onClose}>import close</button>
        </div>
    ),
}));
vi.mock('../AgentCardEndpointModal', () => ({
    default: ({ cardName, tenantId, onClose }: { cardName: string; tenantId: string; onClose: () => void }) => (
        <div>
            endpoints for {cardName} in {tenantId}
            <button onClick={onClose}>endpoint close</button>
        </div>
    ),
}));

import AgentCardsTab from '../AgentCardsTab';

const UNSIGNED = {
    id: 'card-1',
    tenant_id: 't-1',
    name: 'Fraud Screener',
    version: '1.0',
    description: 'Screens fraud',
    enabled: true,
    trust_tier: 'unsigned',
    capability_count: 2,
    groups: [],
    health_status: 'unhealthy',
    health_check_error: 'connection refused',
    last_health_check_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
};
const SIGNED = {
    ...UNSIGNED,
    id: 'card-2',
    name: 'Scoring',
    description: null,
    enabled: false,
    trust_tier: 'tenant_signed',
    health_status: 'healthy',
    health_check_error: null,
    last_health_check_at: null,
};
const REVOKED = { ...UNSIGNED, id: 'card-3', name: 'Old', trust_tier: 'revoked', health_status: undefined, health_check_error: null, last_health_check_at: null };

const DETAIL = {
    ...UNSIGNED,
    signature_valid: true,
    signed_at: '2026-07-01T00:00:00Z',
    revoked_at: null,
    card_json: {
        supportedInterfaces: [
            { protocolBinding: 'HTTP+JSON', protocolVersion: '0.3', url: 'https://agent.test/a2a' },
            { url: 'https://agent.test/rpc' },
        ],
    },
    capabilities: [
        { id: 'c1', capability_name: 'screen', handler_type: 'skill', handler_ref: 'fraud-skill' },
        { id: 'c2', capability_name: 'ping', handler_type: null, handler_ref: null },
    ],
    groups: [{ id: 'g-1', name: 'risk', display_name: 'Risk Team' }, { id: 'g-2', name: 'ops', display_name: null }],
};

function rowOf(name: string): HTMLElement {
    const row = screen.getByText(name).closest('tr');
    if (!row) throw new Error(`no row for ${name}`);
    return row;
}

/** The menu is portalled to the end of the body, after the page's own buttons. */
function pickFromMenu(name: string, item: string) {
    fireEvent.click(within(rowOf(name)).getByRole('button', { name: 'Card actions' }));
    const matches = screen.getAllByRole('button', { name: item });
    fireEvent.click(matches[matches.length - 1]);
}

async function loadList() {
    render(<AgentCardsTab />);
    await screen.findByText('Fraud Screener');
}

async function openDetail() {
    await loadList();
    pickFromMenu('Fraud Screener', 'View');
    await screen.findByText('Back to list');
    await screen.findByText('Interfaces');
}

describe('AgentCardsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        confirm.mockResolvedValue(true);
        gatewayClient.listA2AAgentCards.mockResolvedValue({ cards: [UNSIGNED, SIGNED, REVOKED] });
        gatewayClient.getA2AAgentCard.mockResolvedValue(DETAIL);
        gatewayClient.fetchGroupHierarchyTree.mockResolvedValue({ tree: [{ id: 'g-1' }] });
        gatewayClient.signA2AAgentCard.mockResolvedValue({ ...DETAIL, trust_tier: 'tenant_signed' });
        gatewayClient.revokeA2AAgentCard.mockResolvedValue({ ...DETAIL, trust_tier: 'revoked', revoked_at: '2026-07-02T00:00:00Z', revoked_reason: 'leaked' });
        gatewayClient.deleteA2AAgentCard.mockResolvedValue({});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    describe('list', () => {
        it('shows counts and each card with its status and health', async () => {
            await loadList();
            expect(screen.getByText('3 cards, 2 enabled')).toBeTruthy();
            const row = within(rowOf('Fraud Screener'));
            expect(row.getByText('Enabled')).toBeTruthy();
            expect(row.getByText('Unhealthy').title).toContain('connection refused');
            expect(within(rowOf('Scoring')).getByText('Disabled')).toBeTruthy();
            expect(within(rowOf('Scoring')).getByText('Healthy').title).toBe('Health not yet probed');
            expect(within(rowOf('Old')).getByText('Unknown')).toBeTruthy();
        });

        it('shows no raw unicode escapes', async () => {
            await openDetail();
            expect(document.body.textContent).not.toMatch(/\\u20/);
        });

        it('shows the empty state', async () => {
            gatewayClient.listA2AAgentCards.mockResolvedValue({ cards: [] });
            render(<AgentCardsTab />);
            expect(await screen.findByText('No agent cards yet')).toBeTruthy();
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Health Check' }).disabled).toBe(true);
        });

        it('shows a load failure', async () => {
            gatewayClient.listA2AAgentCards.mockRejectedValue(new Error('gateway down'));
            render(<AgentCardsTab />);
            expect(await screen.findByText('gateway down')).toBeTruthy();
        });

        it('asks the server for enabled cards only', async () => {
            await loadList();
            expect(gatewayClient.listA2AAgentCards).toHaveBeenLastCalledWith({ enabled_only: undefined });

            fireEvent.click(screen.getByRole('checkbox', { name: 'Enabled only' }));
            await waitFor(() => expect(gatewayClient.listA2AAgentCards).toHaveBeenLastCalledWith({ enabled_only: true }));
            await screen.findByText('Fraud Screener');
        });

        it('keeps the filter reachable when no enabled card matches', async () => {
            await loadList();
            gatewayClient.listA2AAgentCards.mockResolvedValue({ cards: [] });
            fireEvent.click(screen.getByRole('checkbox', { name: 'Enabled only' }));

            const box = await screen.findByRole('checkbox', { name: 'Enabled only' });
            await waitFor(() => expect(screen.queryByText('Fraud Screener')).toBeNull());
            expect(screen.queryByText('No agent cards yet')).toBeNull();

            gatewayClient.listA2AAgentCards.mockResolvedValue({ cards: [UNSIGNED] });
            fireEvent.click(box);
            expect(await screen.findByText('Fraud Screener')).toBeTruthy();
        });

        it('copies a card id, and reports when the clipboard refuses', async () => {
            const writeText = vi.fn().mockResolvedValue(undefined);
            Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
            await loadList();

            fireEvent.click(within(rowOf('Fraud Screener')).getByTitle('Click to copy card ID'));
            await waitFor(() => expect(writeText).toHaveBeenCalledWith('card-1'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Card ID copied'));

            writeText.mockRejectedValue(new Error('denied'));
            fireEvent.click(within(rowOf('Fraud Screener')).getByTitle('Click to copy card ID'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Copy failed', 'Could not write to clipboard'));
        });

        it('offers Sign only on unsigned and Revoke only on signed, and disables Edit once revoked', async () => {
            await loadList();
            const open = (name: string) => fireEvent.click(within(rowOf(name)).getByRole('button', { name: 'Card actions' }));

            open('Fraud Screener');
            expect(screen.getByRole('button', { name: 'Sign' })).toBeTruthy();
            expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull();
            open('Fraud Screener');

            open('Scoring');
            expect(screen.queryByRole('button', { name: 'Sign' })).toBeNull();
            expect(screen.getByRole('button', { name: 'Revoke' })).toBeTruthy();
            open('Scoring');

            open('Old');
            expect(screen.queryByRole('button', { name: 'Sign' })).toBeNull();
            expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull();
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Edit' }).disabled).toBe(true);
        });

        it('closes the row menu on an outside click and on scroll', async () => {
            await loadList();
            const open = () => fireEvent.click(within(rowOf('Scoring')).getByRole('button', { name: 'Card actions' }));

            open();
            expect(screen.getByRole('button', { name: 'Endpoint Info' })).toBeTruthy();
            fireEvent.mouseDown(document.body);
            expect(screen.queryByRole('button', { name: 'Endpoint Info' })).toBeNull();

            open();
            act(() => { window.dispatchEvent(new Event('scroll')); });
            expect(screen.queryByRole('button', { name: 'Endpoint Info' })).toBeNull();
        });
    });

    describe('health checks', () => {
        function bulk() {
            fireEvent.click(screen.getByRole('button', { name: 'Health Check' }));
        }

        it('reports the bulk tally and reloads', async () => {
            gatewayClient.bulkHealthCheckA2AAgentCards.mockResolvedValue({ total: 3, healthy: 1, unhealthy: 1, unknown: 1 });
            await loadList();
            bulk();
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith('Health check complete', '1 healthy, 1 unhealthy, 1 unknown'),
            );
            expect(gatewayClient.listA2AAgentCards).toHaveBeenCalledTimes(2);
        });

        it('says when there was nothing to probe', async () => {
            gatewayClient.bulkHealthCheckA2AAgentCards.mockResolvedValue({ total: 0 });
            await loadList();
            bulk();
            await waitFor(() =>
                expect(toast.info).toHaveBeenCalledWith('No cards to check', 'There are no agent cards to probe.'),
            );
        });

        it('reports a bulk failure', async () => {
            gatewayClient.bulkHealthCheckA2AAgentCards.mockRejectedValue(new Error('probe failed'));
            await loadList();
            bulk();
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Health check failed', 'probe failed'));
        });

        it('probes one card and shows the error with the status', async () => {
            gatewayClient.healthCheckA2AAgentCard.mockResolvedValue({ status: 'unhealthy', error: 'timeout' });
            await loadList();
            pickFromMenu('Scoring', 'Health Check');
            await waitFor(() => expect(gatewayClient.healthCheckA2AAgentCard).toHaveBeenCalledWith('card-2'));
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith('Health check complete', 'Scoring: unhealthy — timeout'),
            );
        });

        it('shows a plain status for a healthy probe, and reports a failed one', async () => {
            gatewayClient.healthCheckA2AAgentCard.mockResolvedValue({ status: 'healthy' });
            await loadList();
            pickFromMenu('Scoring', 'Health Check');
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Health check complete', 'Scoring: healthy'));

            gatewayClient.healthCheckA2AAgentCard.mockRejectedValue(new Error('nope'));
            pickFromMenu('Scoring', 'Health Check');
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Health check failed', 'nope'));
        });
    });

    describe('sign', () => {
        it('names the card in the confirmation and signs only once confirmed', async () => {
            await loadList();
            pickFromMenu('Fraud Screener', 'Sign');

            await waitFor(() => expect(gatewayClient.signA2AAgentCard).toHaveBeenCalledWith('card-1'));
            expect(confirm.mock.calls[0][0].message).toContain('Fraud Screener@1.0');
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Card signed'));
            await waitFor(() => expect(gatewayClient.listA2AAgentCards).toHaveBeenCalledTimes(2));
        });

        it('does not sign when declined', async () => {
            confirm.mockResolvedValue(false);
            await loadList();
            pickFromMenu('Fraud Screener', 'Sign');
            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(gatewayClient.signA2AAgentCard).not.toHaveBeenCalled();
        });

        it('reports a failure', async () => {
            gatewayClient.signA2AAgentCard.mockRejectedValue(new Error('no signing key'));
            await loadList();
            pickFromMenu('Fraud Screener', 'Sign');
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Sign failed', 'no signing key'));
        });

        it('updates the open detail with the signed card', async () => {
            await openDetail();
            gatewayClient.signA2AAgentCard.mockResolvedValue({ ...DETAIL, signature_valid: false });
            fireEvent.click(screen.getByRole('button', { name: 'Sign' }));
            expect(await screen.findByText('Signature invalid')).toBeTruthy();
        });
    });

    describe('revoke', () => {
        function revoke() {
            pickFromMenu('Scoring', 'Revoke');
        }

        it('sends the trimmed reason after confirmation', async () => {
            vi.stubGlobal('prompt', vi.fn().mockReturnValue('  key leaked  '));
            await loadList();
            revoke();
            await waitFor(() =>
                expect(gatewayClient.revokeA2AAgentCard).toHaveBeenCalledWith('card-2', { reason: 'key leaked' }),
            );
            expect(confirm.mock.calls[0][0].variant).toBe('danger');
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Card revoked'));
        });

        it.each([[null], [''], ['   ']])('needs a reason: %j does not even ask', async (answer) => {
            vi.stubGlobal('prompt', vi.fn().mockReturnValue(answer));
            await loadList();
            revoke();
            await act(async () => { await Promise.resolve(); });
            expect(confirm).not.toHaveBeenCalled();
            expect(gatewayClient.revokeA2AAgentCard).not.toHaveBeenCalled();
        });

        it('does not revoke when declined', async () => {
            vi.stubGlobal('prompt', vi.fn().mockReturnValue('reason'));
            confirm.mockResolvedValue(false);
            await loadList();
            revoke();
            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(gatewayClient.revokeA2AAgentCard).not.toHaveBeenCalled();
        });

        it('reports a failure', async () => {
            vi.stubGlobal('prompt', vi.fn().mockReturnValue('reason'));
            gatewayClient.revokeA2AAgentCard.mockRejectedValue(new Error('already revoked'));
            await loadList();
            revoke();
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Revoke failed', 'already revoked'));
        });

        it('shows the revocation on the open detail and removes the actions it ends', async () => {
            vi.stubGlobal('prompt', vi.fn().mockReturnValue('leaked'));
            await openDetail();
            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

            expect(await screen.findByText(/Revoked .* — leaked/)).toBeTruthy();
            expect(screen.queryByRole('button', { name: 'Sign' })).toBeNull();
            expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull();
        });
    });

    describe('delete', () => {
        it('deletes once confirmed and reloads', async () => {
            await loadList();
            pickFromMenu('Scoring', 'Delete');
            await waitFor(() => expect(gatewayClient.deleteA2AAgentCard).toHaveBeenCalledWith('card-2'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Card deleted'));
            await waitFor(() => expect(gatewayClient.listA2AAgentCards).toHaveBeenCalledTimes(2));
        });

        it('does nothing when declined', async () => {
            confirm.mockResolvedValue(false);
            await loadList();
            pickFromMenu('Scoring', 'Delete');
            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(gatewayClient.deleteA2AAgentCard).not.toHaveBeenCalled();
        });

        it('reports a failure', async () => {
            gatewayClient.deleteA2AAgentCard.mockRejectedValue(new Error('in use'));
            await loadList();
            pickFromMenu('Scoring', 'Delete');
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Delete failed', 'in use'));
        });

        it('returns to the list when the open card is deleted', async () => {
            await openDetail();
            fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
            await waitFor(() => expect(gatewayClient.deleteA2AAgentCard).toHaveBeenCalledWith('card-1'));
            expect(await screen.findByText('Fraud Screener')).toBeTruthy();
            expect(screen.queryByText('Back to list')).toBeNull();
        });
    });

    describe('detail', () => {
        it('renders interfaces, handlers, signature state and groups', async () => {
            await openDetail();
            expect(gatewayClient.getA2AAgentCard).toHaveBeenCalledWith('card-1');
            expect(screen.getByText('Signature verifies')).toBeTruthy();
            expect(screen.getByText('https://agent.test/a2a')).toBeTruthy();
            expect(screen.getByText('HTTP+JSON / 0.3')).toBeTruthy();
            // Defaults for an interface that does not state its binding.
            expect(screen.getByText('JSONRPC / 1.0')).toBeTruthy();
            expect(screen.getByText('Capabilities (2)')).toBeTruthy();
            expect(screen.getByText('skill → fraud-skill')).toBeTruthy();
            expect(screen.getByText('noop', { selector: 'span' })).toBeTruthy();
            expect(screen.getByText('Risk Team')).toBeTruthy();
            expect(screen.getByText('ops')).toBeTruthy();
        });

        it('says when interfaces and capabilities are absent', async () => {
            gatewayClient.getA2AAgentCard.mockResolvedValue({ ...DETAIL, card_json: {}, capabilities: [], groups: [] });
            await openDetail();
            expect(screen.getByText('No supportedInterfaces declared.')).toBeTruthy();
            expect(screen.getByText('None declared.')).toBeTruthy();
            expect(screen.queryByText('Resource Groups')).toBeNull();
        });

        it('falls back to the list when the card cannot be loaded', async () => {
            gatewayClient.getA2AAgentCard.mockRejectedValue(new Error('gone'));
            await loadList();
            pickFromMenu('Fraud Screener', 'View');
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Load failed', 'gone'));
            expect(await screen.findByText('Fraud Screener')).toBeTruthy();
            expect(screen.queryByText('Back to list')).toBeNull();
        });

        it('goes back to the list', async () => {
            await openDetail();
            fireEvent.click(screen.getByText('Back to list'));
            expect(screen.getByText('Scoring')).toBeTruthy();
        });

        it('fetches the group tree once, even when the tenant has none', async () => {
            gatewayClient.fetchGroupHierarchyTree.mockResolvedValue({ tree: [] });
            await openDetail();
            await waitFor(() => expect(gatewayClient.fetchGroupHierarchyTree).toHaveBeenCalled());
            await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
            expect(gatewayClient.fetchGroupHierarchyTree).toHaveBeenCalledTimes(1);
        });

        it('does not spin on a failed group fetch, and tries again next open', async () => {
            gatewayClient.fetchGroupHierarchyTree.mockRejectedValue(new Error('down'));
            await openDetail();
            await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
            expect(gatewayClient.fetchGroupHierarchyTree).toHaveBeenCalledTimes(1);

            fireEvent.click(screen.getByText('Back to list'));
            pickFromMenu('Fraud Screener', 'View');
            await waitFor(() => expect(gatewayClient.fetchGroupHierarchyTree).toHaveBeenCalledTimes(2));
            await screen.findByText('Interfaces');
        });

        it('shows the endpoints for the open card', async () => {
            await openDetail();
            fireEvent.click(screen.getByRole('button', { name: 'Endpoint' }));
            expect(screen.getByText('endpoints for Fraud Screener in t-1')).toBeTruthy();
            fireEvent.click(screen.getByText('endpoint close'));
            expect(screen.queryByText(/endpoints for/)).toBeNull();
        });
    });

    describe('edit', () => {
        it('loads the detail, then reloads list and detail after saving', async () => {
            await openDetail();
            fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
            fireEvent.click(screen.getByText('edit saved'));

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Card updated'));
            expect(gatewayClient.listA2AAgentCards).toHaveBeenCalledTimes(2);
            expect(gatewayClient.getA2AAgentCard).toHaveBeenCalledTimes(2);
            expect(screen.queryByText('editing Fraud Screener')).toBeNull();
        });

        it('falls back to the list if the card vanished during the edit', async () => {
            await openDetail();
            fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
            gatewayClient.getA2AAgentCard.mockRejectedValue(new Error('gone'));
            fireEvent.click(screen.getByText('edit saved'));

            await waitFor(() => expect(screen.queryByText('Back to list')).toBeNull());
        });

        it('opens from the row after fetching the full card, and reports a failed fetch', async () => {
            await loadList();
            pickFromMenu('Fraud Screener', 'Edit');
            expect(await screen.findByText('editing Fraud Screener')).toBeTruthy();
            expect(gatewayClient.getA2AAgentCard).toHaveBeenCalledWith('card-1');
            fireEvent.click(screen.getByText('edit close'));
            expect(screen.queryByText('editing Fraud Screener')).toBeNull();

            gatewayClient.getA2AAgentCard.mockRejectedValue(new Error('gone'));
            pickFromMenu('Fraud Screener', 'Edit');
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Load failed', 'gone'));
        });
    });

    describe('create and import', () => {
        it('jumps to the new card after creating it', async () => {
            await loadList();
            fireEvent.click(screen.getByRole('button', { name: 'New Card' }));
            fireEvent.click(screen.getByText('created'));

            expect(await screen.findByText('Back to list')).toBeTruthy();
            expect(screen.queryByText('create dialog')).toBeNull();
            expect(screen.getByText('Capabilities (2)')).toBeTruthy();
            expect(gatewayClient.listA2AAgentCards).toHaveBeenCalledTimes(2);
        });

        it('cancels creating', async () => {
            await loadList();
            fireEvent.click(screen.getByRole('button', { name: 'New Card' }));
            fireEvent.click(screen.getByText('create close'));
            expect(screen.queryByText('create dialog')).toBeNull();
        });

        it('reloads after an import while the modal stays open', async () => {
            await loadList();
            fireEvent.click(screen.getByRole('button', { name: 'Import' }));
            fireEvent.click(screen.getByText('imported'));

            await waitFor(() => expect(gatewayClient.listA2AAgentCards).toHaveBeenCalledTimes(2));
            expect(screen.getByText('import modal')).toBeTruthy();
            fireEvent.click(screen.getByText('import close'));
            expect(screen.queryByText('import modal')).toBeNull();
        });
    });

    describe('tenant public key', () => {
        const KEY = {
            tenant_id: 't-1',
            key_algorithm: 'Ed25519',
            created_at: '2026-07-01T00:00:00Z',
            rotated_at: '2026-08-01T00:00:00Z',
            public_key: 'pubkey-b64',
        };

        function open() {
            fireEvent.click(screen.getByRole('button', { name: 'Tenant Public Key' }));
        }

        it('shows the key, copies it, and fetches only once', async () => {
            gatewayClient.getA2ATenantPublicKey.mockResolvedValue(KEY);
            const writeText = vi.fn().mockResolvedValue(undefined);
            Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
            await loadList();
            open();

            expect(await screen.findByDisplayValue('pubkey-b64')).toBeTruthy();
            expect(screen.getByText('Ed25519')).toBeTruthy();
            expect(screen.getByText('Rotated')).toBeTruthy();

            fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
            await waitFor(() => expect(writeText).toHaveBeenCalledWith('pubkey-b64'));
            expect(await screen.findByRole('button', { name: 'Copied' })).toBeTruthy();

            fireEvent.click(screen.getByRole('button', { name: 'Close' }));
            expect(screen.queryByDisplayValue('pubkey-b64')).toBeNull();
            open();
            expect(await screen.findByDisplayValue('pubkey-b64')).toBeTruthy();
            expect(gatewayClient.getA2ATenantPublicKey).toHaveBeenCalledTimes(1);
        });

        it('closes the modal and reports when the key cannot be loaded', async () => {
            gatewayClient.getA2ATenantPublicKey.mockRejectedValue(new Error('no key'));
            await loadList();
            open();
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Load failed', 'no key'));
            await waitFor(() => expect(screen.queryByText('Tenant A2A public key')).toBeNull());
        });
    });
});
