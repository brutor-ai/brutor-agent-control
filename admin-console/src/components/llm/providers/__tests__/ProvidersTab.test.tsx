/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Providers list.
 *
 * Pinned: the delete gate (system rows and providers still in use cannot be
 * deleted), the toggle payload, that row actions do not also open the drill-down
 * on the row underneath, and that the list refreshes after every write.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const { toastMock } = vi.hoisted(() => ({
    toastMock: { success: vi.fn(), error: vi.fn() },
}));
vi.mock('react-hot-toast', () => ({ default: toastMock }));

const client = { updateProvider: vi.fn(), deleteProvider: vi.fn() };
const gateway = { gatewayClient: client };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

const providersHook = {
    providers: [] as Array<Record<string, unknown>>,
    loading: false,
    error: null as Error | null,
    refresh: vi.fn(),
};
vi.mock('../../../../hooks/useLlmProviders', () => ({ useLlmProviders: () => providersHook }));

vi.mock('../../config/ProviderIcon', () => ({ default: () => <span>icon</span> }));
vi.mock('../LLProviderForm', () => ({
    default: ({
        provider,
        onClose,
        onSaved,
    }: {
        provider: { display_name: string } | null;
        onClose: () => void;
        onSaved: (msg: string) => Promise<void>;
    }) => (
        <div>
            form for {provider?.display_name ?? 'a new provider'}
            <button onClick={() => { void onSaved('Saved it'); }}>form saved</button>
            <button onClick={onClose}>form close</button>
        </div>
    ),
}));
vi.mock('../ProviderModelsView', () => ({
    default: ({
        provider,
        onBack,
        onEditProvider,
    }: {
        provider: { display_name: string };
        onBack: () => void;
        onEditProvider: (p: unknown) => void;
    }) => (
        <div>
            models of {provider.display_name}
            <button onClick={onBack}>back</button>
            <button onClick={() => onEditProvider(provider)}>edit from drilldown</button>
        </div>
    ),
}));
vi.mock('../../../common/GenericDeleteConfirmationModal', () => ({
    default: ({
        itemName,
        onConfirm,
        onClose,
        errorMessage,
    }: {
        itemName: string;
        onConfirm: () => void;
        onClose: () => void;
        errorMessage?: string | null;
    }) => (
        <div>
            delete modal for {itemName}
            {errorMessage && <p>{errorMessage}</p>}
            <button onClick={onConfirm}>confirm delete</button>
            <button onClick={onClose}>cancel delete</button>
        </div>
    ),
}));

import ProvidersTab from '../ProvidersTab';

function provider(overrides: Record<string, unknown> = {}) {
    return {
        id: 'p1',
        key: 'acme',
        display_name: 'Acme',
        enabled: true,
        is_system: false,
        model_count: 0,
        ...overrides,
    };
}

function click(name: string) {
    fireEvent.click(screen.getByRole('button', { name }));
}

function rows(): string[] {
    return Array.from(document.querySelectorAll('tr[data-provider-id]')).map(
        r => r.getAttribute('data-provider-id') ?? ''
    );
}

describe('ProvidersTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        client.updateProvider.mockReset();
        client.deleteProvider.mockReset();
        client.updateProvider.mockResolvedValue({});
        client.deleteProvider.mockResolvedValue({});
        providersHook.providers = [];
        providersHook.loading = false;
        providersHook.error = null;
        providersHook.refresh.mockResolvedValue(undefined);
    });
    afterEach(cleanup);

    describe('list', () => {
        it('shows loading, then the empty state', () => {
            providersHook.loading = true;
            const { unmount } = render(<ProvidersTab />);
            expect(screen.getByText('Loading providers…')).toBeTruthy();
            unmount();

            providersHook.loading = false;
            render(<ProvidersTab />);
            expect(screen.getByText(/No providers configured yet/)).toBeTruthy();
        });

        it('toasts a load failure', () => {
            providersHook.error = new Error('down');
            render(<ProvidersTab />);
            expect(toastMock.error).toHaveBeenCalledWith('Failed to load providers: down');
        });

        it('sorts enabled first, then by name', () => {
            providersHook.providers = [
                provider({ id: 'off', display_name: 'Aardvark', enabled: false }),
                provider({ id: 'z', display_name: 'Zed' }),
                provider({ id: 'b', display_name: 'Beta' }),
            ];
            render(<ProvidersTab />);
            expect(rows()).toEqual(['b', 'z', 'off']);
        });

        it('searches key, name and category, and says when nothing matches', () => {
            providersHook.providers = [
                provider({ id: 'a', key: 'openai', display_name: 'One', category: 'chat' }),
                provider({ id: 'b', key: 'stable', display_name: 'Two', category: 'image' }),
                provider({ id: 'c', key: 'other', display_name: 'Special' }),
            ];
            render(<ProvidersTab />);
            const search = screen.getByPlaceholderText('Search providers (key, name, category)…');

            fireEvent.change(search, { target: { value: 'OPENAI' } });
            expect(rows()).toEqual(['a']);
            fireEvent.change(search, { target: { value: 'image' } });
            expect(rows()).toEqual(['b']);
            fireEvent.change(search, { target: { value: 'special' } });
            expect(rows()).toEqual(['c']);
            fireEvent.change(search, { target: { value: 'nope' } });
            expect(screen.getByText('No providers match your search.')).toBeTruthy();
        });

        it('labels category and auth, raw when unknown and a dash when unset', () => {
            providersHook.providers = [
                provider({
                    id: 'a',
                    category: 'self_hosted',
                    default_auth_type: 'aws_iam',
                    model_count: 7,
                    is_system: true,
                    developer_url: 'https://dev.test',
                }),
                provider({ id: 'b', display_name: 'B', category: 'odd', default_auth_type: 'weird' }),
                provider({ id: 'c', display_name: 'C', enabled: false }),
            ];
            render(<ProvidersTab />);
            expect(screen.getByText('Self-hosted / OSS')).toBeTruthy();
            expect(screen.getByText('AWS IAM')).toBeTruthy();
            expect(screen.getByText('7')).toBeTruthy();
            expect(screen.getByText('System')).toBeTruthy();
            expect(screen.getByText('odd')).toBeTruthy();
            expect(screen.getByText('weird')).toBeTruthy();
            expect(screen.getAllByText('—')).toHaveLength(2);
            expect(screen.getByText('Disabled')).toBeTruthy();
            expect(screen.getByRole('link', { name: 'Developer site' }).getAttribute('href')).toBe(
                'https://dev.test'
            );
        });

        it('refreshes on demand', () => {
            render(<ProvidersTab />);
            click('Refresh');
            expect(providersHook.refresh).toHaveBeenCalledTimes(1);
        });
    });

    describe('toggle', () => {
        it('disables an enabled provider, then refreshes', async () => {
            providersHook.providers = [provider()];
            render(<ProvidersTab />);
            fireEvent.click(screen.getByTitle('Disable'));
            await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith('Disabled Acme'));
            expect(client.updateProvider).toHaveBeenCalledWith('p1', { enabled: false });
            await waitFor(() => expect(providersHook.refresh).toHaveBeenCalledTimes(1));
            // The action cell must not also open the row's drill-down.
            expect(screen.queryByText('models of Acme')).toBeNull();
        });

        it('enables a disabled provider', async () => {
            providersHook.providers = [provider({ enabled: false })];
            render(<ProvidersTab />);
            fireEvent.click(screen.getByTitle('Enable'));
            await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith('Enabled Acme'));
            expect(client.updateProvider).toHaveBeenCalledWith('p1', { enabled: true });
        });

        it('reports a failure, without refreshing, and re-enables the control', async () => {
            providersHook.providers = [provider()];
            client.updateProvider.mockRejectedValue(new Error('locked'));
            render(<ProvidersTab />);
            const toggle = screen.getByTitle<HTMLButtonElement>('Disable');
            fireEvent.click(toggle);
            expect(toggle.disabled).toBe(true);
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to disable provider: locked')
            );
            expect(providersHook.refresh).not.toHaveBeenCalled();
            await waitFor(() => expect(toggle.disabled).toBe(false));
        });
    });

    describe('delete', () => {
        it('blocks a system provider and one still in use, and says why', () => {
            providersHook.providers = [
                provider({ id: 'sys', is_system: true }),
                provider({ id: 'used', display_name: 'Used', model_count: 3 }),
            ];
            render(<ProvidersTab />);
            expect(screen.getByTitle<HTMLButtonElement>('System providers cannot be deleted').disabled).toBe(true);
            expect(
                screen.getByTitle<HTMLButtonElement>('In use by 3 model(s) — reassign first').disabled
            ).toBe(true);
        });

        it('deletes only after confirming, then refreshes', async () => {
            providersHook.providers = [provider()];
            render(<ProvidersTab />);
            fireEvent.click(screen.getByTitle('Delete'));
            expect(screen.getByText('delete modal for Acme')).toBeTruthy();
            expect(client.deleteProvider).not.toHaveBeenCalled();

            click('confirm delete');
            await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith('Deleted provider Acme'));
            expect(client.deleteProvider).toHaveBeenCalledWith('p1');
            expect(screen.queryByText('delete modal for Acme')).toBeNull();
            await waitFor(() => expect(providersHook.refresh).toHaveBeenCalledTimes(1));
        });

        it('keeps the modal open with the server message, and clears it on reopen', async () => {
            providersHook.providers = [provider()];
            client.deleteProvider.mockRejectedValue(new Error('Referenced by 2 models'));
            render(<ProvidersTab />);
            fireEvent.click(screen.getByTitle('Delete'));
            click('confirm delete');
            expect(await screen.findByText('Referenced by 2 models')).toBeTruthy();
            expect(toastMock.success).not.toHaveBeenCalled();

            click('cancel delete');
            expect(screen.queryByText('delete modal for Acme')).toBeNull();
            fireEvent.click(screen.getByTitle('Delete'));
            expect(screen.queryByText('Referenced by 2 models')).toBeNull();
        });
    });

    describe('create and edit', () => {
        it('opens an empty form for New Provider, and on save toasts, closes and refreshes', async () => {
            render(<ProvidersTab />);
            click('New Provider');
            expect(screen.getByText('form for a new provider')).toBeTruthy();
            click('form saved');
            await waitFor(() => expect(providersHook.refresh).toHaveBeenCalledTimes(1));
            expect(toastMock.success).toHaveBeenCalledWith('Saved it');
            expect(screen.queryByText('form for a new provider')).toBeNull();
        });

        it('opens the form on the row, and closes without refreshing', () => {
            providersHook.providers = [provider()];
            render(<ProvidersTab />);
            fireEvent.click(screen.getByTitle('Edit'));
            expect(screen.getByText('form for Acme')).toBeTruthy();
            expect(screen.queryByText('models of Acme')).toBeNull();
            click('form close');
            expect(screen.queryByText('form for Acme')).toBeNull();
            expect(providersHook.refresh).not.toHaveBeenCalled();
        });

        it.todo(
            'BUG: an icon uploaded or removed in the form and then cancelled leaves the table on the old icon until a manual Refresh, because the form only tells the parent on save (ProvidersTab.tsx onClose)'
        );
    });

    describe('drill-down', () => {
        it('opens from the row and goes back', () => {
            providersHook.providers = [provider()];
            render(<ProvidersTab />);
            fireEvent.click(screen.getByText('Acme'));
            expect(screen.getByText('models of Acme')).toBeTruthy();
            click('back');
            expect(screen.queryByText('models of Acme')).toBeNull();
            expect(rows()).toEqual(['p1']);
        });

        it('edits from the drill-down and picks up the refreshed provider', async () => {
            providersHook.providers = [provider()];
            const { rerender } = render(<ProvidersTab />);
            fireEvent.click(screen.getByText('Acme'));
            click('edit from drilldown');
            expect(screen.getByText('form for Acme')).toBeTruthy();
            click('form close');
            expect(screen.queryByText('form for Acme')).toBeNull();

            providersHook.providers = [provider({ display_name: 'Acme Renamed' })];
            rerender(<ProvidersTab />);
            expect(await screen.findByText('models of Acme Renamed')).toBeTruthy();
        });
    });
});
