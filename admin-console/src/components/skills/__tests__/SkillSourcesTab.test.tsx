/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Skill sources: GitHub repos the proxy syncs skills from.
 *
 * Auto-publish decides whether code from a repo goes live unreviewed, so the exact
 * create and update bodies are pinned. The sync poll is pinned too: its completion
 * callback must judge the list the fetch returned. The list from before the fetch
 * always holds the syncing row that started the poll, so a callback reading that
 * one could never fire.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within, act } from '@testing-library/react';

const gatewayClient = {
    listSkillSources: vi.fn(),
    createSkillSource: vi.fn(),
    updateSkillSource: vi.fn(),
    deleteSkillSource: vi.fn(),
    refreshSkillSource: vi.fn(),
};
const gateway = { gatewayClient };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import { SkillSourcesTab } from '../SkillSourcesTab';

const IDLE = {
    id: 's-1',
    name: 'Examples',
    repo_url: 'https://github.com/anthropics/skill-examples',
    git_ref: 'main',
    subpath: 'skills/',
    enabled: true,
    auto_publish: false,
    sync_interval_seconds: 7200,
    status: 'idle',
    sync_error: null,
    discovered_skill_count: 4,
    last_synced_at: '2026-07-01T00:00:00Z',
};
const FAILED = {
    ...IDLE,
    id: 's-2',
    name: 'Broken',
    repo_url: 'https://gitlab.test/broken',
    subpath: null,
    enabled: false,
    status: 'failed',
    sync_error: 'repository not found',
    discovered_skill_count: 0,
    last_synced_at: null,
};
const SYNCING = { ...IDLE, id: 's-3', name: 'Busy', status: 'syncing' };

const NAME = 'Anthropic example skills';
const REPO = 'https://github.com/anthropics/skill-examples';

function rowOf(name: string): HTMLElement {
    const row = screen.getByText(name).closest('tr');
    if (!row) throw new Error(`no row for ${name}`);
    return row;
}

async function load(sources: unknown[] = [IDLE, FAILED]) {
    gatewayClient.listSkillSources.mockResolvedValue({ sources });
    render(<SkillSourcesTab />);
    await screen.findByText('Skill Sources');
}

describe('SkillSourcesTab', () => {
    let pollTick: (() => void) | null;

    beforeEach(() => {
        vi.clearAllMocks();
        gatewayClient.createSkillSource.mockResolvedValue({});
        gatewayClient.updateSkillSource.mockResolvedValue({});
        gatewayClient.deleteSkillSource.mockResolvedValue({});
        gatewayClient.refreshSkillSource.mockResolvedValue({});

        pollTick = null;
        const realSetInterval = window.setInterval.bind(window);
        vi.spyOn(window, 'setInterval').mockImplementation(((fn: () => void, ms?: number) => {
            if (ms === 3000) {
                pollTick = fn;
                return 4242;
            }
            return realSetInterval(fn, ms);
        }) as unknown as typeof window.setInterval);
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    describe('list', () => {
        it('shows the repo, ref, status and count', async () => {
            await load();
            const row = within(rowOf('Examples'));
            expect(row.getByRole('link', { name: 'anthropics/skill-examples' }).getAttribute('href')).toBe(IDLE.repo_url);
            expect(row.getByText('@ main · skills/')).toBeTruthy();
            expect(row.getByText('Idle')).toBeTruthy();
            expect(row.getByText('4')).toBeTruthy();

            const broken = within(rowOf('Broken'));
            expect(broken.getByText('Disabled — paused by operator')).toBeTruthy();
            expect(broken.getByText('Failed')).toBeTruthy();
            expect(broken.getByText('repository not found')).toBeTruthy();
            expect(broken.getByText('never')).toBeTruthy();
            expect(broken.getByText('@ main')).toBeTruthy();
        });

        it('shows the empty state', async () => {
            await load([]);
            expect(screen.getByText(/No skill sources registered yet/)).toBeTruthy();
        });

        it('shows a load failure', async () => {
            gatewayClient.listSkillSources.mockRejectedValue(new Error('gateway down'));
            render(<SkillSourcesTab />);
            expect(await screen.findByText('gateway down')).toBeTruthy();
        });
    });

    describe('register', () => {
        function open() {
            fireEvent.click(screen.getByRole('button', { name: 'Register source' }));
        }

        function register(): HTMLButtonElement {
            return screen.getByRole<HTMLButtonElement>('button', { name: 'Register' });
        }

        it('needs a name and a URL', async () => {
            await load();
            open();
            expect(register().disabled).toBe(true);
            fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: 'New' } });
            expect(register().disabled).toBe(true);
            fireEvent.change(screen.getByPlaceholderText(REPO), { target: { value: 'https://github.com/a/b' } });
            expect(register().disabled).toBe(false);
        });

        it('creates with safe defaults: draft, enabled, hourly', async () => {
            await load();
            open();
            fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: 'New' } });
            fireEvent.change(screen.getByPlaceholderText(REPO), { target: { value: 'https://github.com/a/b' } });
            fireEvent.click(register());

            await waitFor(() =>
                expect(gatewayClient.createSkillSource).toHaveBeenCalledWith({
                    name: 'New',
                    repo_url: 'https://github.com/a/b',
                    git_ref: 'main',
                    subpath: null,
                    enabled: true,
                    sync_interval_seconds: 3600,
                    auto_publish: false,
                }),
            );
            await waitFor(() => expect(gatewayClient.listSkillSources).toHaveBeenCalledTimes(2));
            expect(screen.queryByText('Register skill source')).toBeNull();
        });

        it('sends everything the operator set', async () => {
            await load();
            open();
            fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: 'New' } });
            fireEvent.change(screen.getByPlaceholderText(REPO), { target: { value: 'https://github.com/a/b' } });
            fireEvent.change(screen.getByPlaceholderText('main'), { target: { value: 'v2' } });
            fireEvent.change(screen.getByPlaceholderText('skills/'), { target: { value: 'pkg/' } });
            fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '600' } });
            fireEvent.click(screen.getByRole('checkbox', { name: /^Enabled/ }));
            fireEvent.click(screen.getByRole('checkbox', { name: /^Auto-publish/ }));
            fireEvent.click(register());

            await waitFor(() =>
                expect(gatewayClient.createSkillSource).toHaveBeenCalledWith({
                    name: 'New',
                    repo_url: 'https://github.com/a/b',
                    git_ref: 'v2',
                    subpath: 'pkg/',
                    enabled: false,
                    sync_interval_seconds: 600,
                    auto_publish: true,
                }),
            );
        });

        it('falls back to the default when the interval is emptied', async () => {
            await load();
            open();
            const interval = screen.getByRole<HTMLInputElement>('spinbutton');
            fireEvent.change(interval, { target: { value: '' } });
            expect(interval.value).toBe('3600');
        });

        it('keeps the modal open with the reason on failure', async () => {
            gatewayClient.createSkillSource.mockRejectedValue(new Error('not a GitHub URL'));
            await load();
            open();
            fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: 'New' } });
            fireEvent.change(screen.getByPlaceholderText(REPO), { target: { value: 'x' } });
            fireEvent.click(register());

            expect(await screen.findByText('not a GitHub URL')).toBeTruthy();
            expect(screen.getByText('Register skill source')).toBeTruthy();
            expect(gatewayClient.listSkillSources).toHaveBeenCalledTimes(1);
        });

        it('cancels', async () => {
            await load();
            open();
            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
            expect(screen.queryByText('Register skill source')).toBeNull();
            expect(gatewayClient.createSkillSource).not.toHaveBeenCalled();
        });
    });

    describe('edit', () => {
        async function open() {
            await load();
            fireEvent.click(within(rowOf('Examples')).getByTitle('Edit'));
            await screen.findByText('Edit skill source');
        }

        it('locks the repo and ref, and never sends them', async () => {
            await open();
            expect(screen.getByPlaceholderText<HTMLInputElement>(REPO).disabled).toBe(true);
            expect(screen.getByPlaceholderText<HTMLInputElement>('main').disabled).toBe(true);
            expect(screen.getByText(/Repo URL is immutable after create/)).toBeTruthy();

            fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: 'Renamed' } });
            fireEvent.change(screen.getByPlaceholderText('skills/'), { target: { value: '' } });
            fireEvent.click(screen.getByRole('checkbox', { name: /^Auto-publish/ }));
            fireEvent.click(screen.getByRole('button', { name: 'Save' }));

            await waitFor(() =>
                expect(gatewayClient.updateSkillSource).toHaveBeenCalledWith('s-1', {
                    name: 'Renamed',
                    subpath: null,
                    enabled: true,
                    sync_interval_seconds: 7200,
                    auto_publish: true,
                }),
            );
            await waitFor(() => expect(gatewayClient.listSkillSources).toHaveBeenCalledTimes(2));
            expect(gatewayClient.createSkillSource).not.toHaveBeenCalled();
        });

        it('shows a failure', async () => {
            gatewayClient.updateSkillSource.mockRejectedValue(new Error('conflict'));
            await open();
            fireEvent.click(screen.getByRole('button', { name: 'Save' }));
            expect(await screen.findByText('conflict')).toBeTruthy();
        });
    });

    describe('refresh', () => {
        it('triggers a sync and reloads', async () => {
            await load();
            fireEvent.click(within(rowOf('Examples')).getByTitle('Trigger immediate sync'));
            await waitFor(() => expect(gatewayClient.refreshSkillSource).toHaveBeenCalledWith('s-1'));
            await waitFor(() => expect(gatewayClient.listSkillSources).toHaveBeenCalledTimes(2));
        });

        it('shows a failure', async () => {
            gatewayClient.refreshSkillSource.mockRejectedValue(new Error('rate limited'));
            await load();
            fireEvent.click(within(rowOf('Examples')).getByTitle('Trigger immediate sync'));
            expect(await screen.findByText('rate limited')).toBeTruthy();
        });

        it('cannot start a second sync while one runs', async () => {
            await load([SYNCING]);
            expect(within(rowOf('Busy')).getByTitle<HTMLButtonElement>('Trigger immediate sync').disabled).toBe(true);
        });
    });

    describe('delete', () => {
        it('says what happens to the discovered skills, and deletes once confirmed', async () => {
            const confirm = vi.fn().mockReturnValue(true);
            vi.stubGlobal('confirm', confirm);
            await load();
            fireEvent.click(within(rowOf('Examples')).getByTitle('Delete'));

            await waitFor(() => expect(gatewayClient.deleteSkillSource).toHaveBeenCalledWith('s-1'));
            expect(confirm.mock.calls[0][0]).toContain('The 4 discovered skill(s) become independent');
            await waitFor(() => expect(gatewayClient.listSkillSources).toHaveBeenCalledTimes(2));
        });

        it('does nothing when declined', async () => {
            vi.stubGlobal('confirm', vi.fn().mockReturnValue(false));
            await load();
            fireEvent.click(within(rowOf('Examples')).getByTitle('Delete'));
            await act(async () => { await Promise.resolve(); });
            expect(gatewayClient.deleteSkillSource).not.toHaveBeenCalled();
        });

        it('shows a failure', async () => {
            vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
            gatewayClient.deleteSkillSource.mockRejectedValue(new Error('in use'));
            await load();
            fireEvent.click(within(rowOf('Examples')).getByTitle('Delete'));
            expect(await screen.findByText('in use')).toBeTruthy();
        });
    });

    describe('sync polling', () => {
        async function tick() {
            const fn = pollTick;
            if (!fn) throw new Error('polling is not running');
            await act(async () => {
                fn();
                await new Promise((r) => setTimeout(r, 0));
            });
        }

        it('does not poll at rest', async () => {
            await load();
            expect(pollTick).toBeNull();
        });

        it('tells the parent once the sync has settled, and not before', async () => {
            const onSyncComplete = vi.fn();
            gatewayClient.listSkillSources.mockResolvedValue({ sources: [SYNCING] });
            render(<SkillSourcesTab onSyncComplete={onSyncComplete} />);
            await screen.findByText('Busy');
            await waitFor(() => expect(pollTick).not.toBeNull());

            await tick();
            expect(onSyncComplete).not.toHaveBeenCalled();

            gatewayClient.listSkillSources.mockResolvedValue({ sources: [{ ...SYNCING, status: 'idle' }] });
            await tick();
            expect(onSyncComplete).toHaveBeenCalledTimes(1);
            expect(within(rowOf('Busy')).getByText('Idle')).toBeTruthy();
        });

        it('does not claim completion when the poll itself failed', async () => {
            const onSyncComplete = vi.fn();
            gatewayClient.listSkillSources.mockResolvedValue({ sources: [SYNCING] });
            render(<SkillSourcesTab onSyncComplete={onSyncComplete} />);
            await screen.findByText('Busy');
            await waitFor(() => expect(pollTick).not.toBeNull());

            gatewayClient.listSkillSources.mockRejectedValue(new Error('down'));
            await tick();

            expect(await screen.findByText('down')).toBeTruthy();
            expect(onSyncComplete).not.toHaveBeenCalled();
        });
    });
});
