/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Server config card: the OAuth vs Open classification an operator relies on,
 * and the icon URL, which must be stable: with a Math.random in it, every
 * re-render of the list (each search keystroke) would re-download every icon.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import type { ServerConfig } from '../../../../../../lib/types';

const gateway = { getServerIconUrl: vi.fn((id: string) => `/icons/${id}`) };

vi.mock('../../../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../../common/GroupTreeDisplay', () => ({
    default: ({ groups }: { groups: Array<{ name: string }> }) => <div>groups:{groups.map(g => g.name).join(',')}</div>,
}));

import ServerConfigRow from '../ServerConfigRow';

function config(overrides: Record<string, unknown> = {}): ServerConfig {
    return {
        id: 'c1',
        name: 'My Config',
        description: 'desc',
        mcp_server_id: 'srv',
        enabled: true,
        is_default: false,
        priority: 7,
        usage_count: 12,
        groups: [{ name: 'Sales' }],
        ...overrides,
    } as unknown as ServerConfig;
}

const onEdit = vi.fn();
const onDelete = vi.fn();
const row = (cfg: ServerConfig, extra: Record<string, string> = {}) => (
    <ServerConfigRow config={cfg} serverName="Backend" onEdit={onEdit} onDelete={onDelete} {...extra} />
);

describe('ServerConfigRow', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('shows the details and passes the right arguments to edit and delete', () => {
        const cfg = config({ is_default: true });
        render(row(cfg));
        expect(screen.getByText('My Config')).toBeTruthy();
        expect(screen.getByText('desc')).toBeTruthy();
        expect(screen.getByText('Backend')).toBeTruthy();
        expect(screen.getByText('Default')).toBeTruthy();
        expect(screen.getByText('Enabled')).toBeTruthy();
        expect(screen.getByText('Priority: 7')).toBeTruthy();
        expect(screen.getByText('12 uses')).toBeTruthy();
        expect(screen.getByText('groups:Sales')).toBeTruthy();

        fireEvent.click(screen.getByTitle('Edit'));
        expect(onEdit).toHaveBeenCalledWith(cfg);
        fireEvent.click(screen.getByTitle('Delete'));
        expect(onDelete).toHaveBeenCalledWith('c1', 'My Config');
    });

    it('with no OAuth anywhere it reads Open / None Required', () => {
        render(row(config({ enabled: false, groups: undefined })));
        expect(screen.getByText('Open')).toBeTruthy();
        expect(screen.getByText('None Required')).toBeTruthy();
        expect(screen.getByText('Disabled')).toBeTruthy();
        expect(screen.queryByText('OAuth Client')).toBeNull();
    });

    it.each([
        ['auth_type oauth2', { mcp_server: { auth_type: 'oauth2' } }],
        ['auth_type oauth', { mcp_server: { auth_type: 'oauth' } }],
        ['a linked client', { oauth_client_id: 'x' }],
    ])('%s reads as OAuth', (_label, overrides) => {
        render(row(config(overrides)));
        expect(screen.getByText('OAuth')).toBeTruthy();
        expect(screen.queryByText('Open')).toBeNull();
        expect(screen.queryByText('None Required')).toBeNull();
    });

    it('shows the OAuth client and app type when protected', () => {
        render(row(config({ oauth_client_id: 'x' }), { oauthClientName: 'Web Client', oauthClientAppType: 'webapp' }));
        expect(screen.getByText('Web Client')).toBeTruthy();
        expect(screen.getByText('webapp')).toBeTruthy();
    });

    it.todo('BUG: a VMCP-backed config has no mcp_server or oauth_client_id, so it reads "Open / None Required" although a VMCP delegates per-call auth to its upstreams (ServerConfigRow.tsx isOAuthProtected)');

    describe('icon', () => {
        const uploaded = {
            mcp_server: { icon: { url: '/x', uploaded_at: '2026-01-02T03:04:05Z' } },
        };

        it('uses the uploaded icon keyed on its upload time, stable across re-renders', () => {
            const cfg = config(uploaded);
            const view = render(row(cfg));
            const src = screen.getByAltText('Backend').getAttribute('src');
            expect(src).toBe(`/icons/srv?t=${new Date('2026-01-02T03:04:05Z').getTime()}`);
            view.rerender(row(cfg));
            expect(screen.getByAltText('Backend').getAttribute('src')).toBe(src);
        });

        it('an upload with no timestamp is still stable', () => {
            const cfg = config({ mcp_server: { icon: { url: '/x' } } });
            const view = render(row(cfg));
            expect(screen.getByAltText('Backend').getAttribute('src')).toBe('/icons/srv');
            view.rerender(row(cfg));
            expect(screen.getByAltText('Backend').getAttribute('src')).toBe('/icons/srv');
        });

        it('falls back to an external icon_url; a VMCP config never asks for an upload', () => {
            render(row(config({ mcp_server_id: null, icon_url: 'https://i.test/a.png', ...uploaded })));
            expect(screen.getByAltText('Backend').getAttribute('src')).toBe('https://i.test/a.png');
            expect(gateway.getServerIconUrl).not.toHaveBeenCalled();
        });

        it('hides the image until loaded, and a broken icon falls back', () => {
            render(row(config({ icon_url: 'https://i.test/a.png' })));
            const img = screen.getByAltText('Backend');
            expect(img.style.display).toBe('none');
            fireEvent.load(img);
            expect(screen.getByAltText('Backend').style.display).toBe('block');
            fireEvent.error(screen.getByAltText('Backend'));
            expect(screen.queryByAltText('Backend')).toBeNull();
        });
    });
});
