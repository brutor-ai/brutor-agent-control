/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemCachesPane.
 *
 * Coverage:
 *   - Mount fetches `/v1/admin/system/caches` once and renders
 *     one card per returned layer
 *   - Each card surfaces hit/miss/hit-rate, item count when
 *     present, and the last-flushed timestamp
 *   - The per-card Flush button is disabled when SYSTEM_OPERATE
 *     is absent and the lock icon + tooltip is shown
 *   - Clicking Flush opens the scope-picker modal pre-populated
 *     with the first scope the backend advertised
 *   - Modal Confirm posts (layer, scope, id, reason) to
 *     `flushSystemCache` and refreshes the page
 *   - Modal Cancel closes without calling the API
 *   - Backend-published validation errors (UnknownChannelError,
 *     PayloadValidationError) surface inside the modal so the
 *     operator sees why their action no-op'd
 *
 * We mock both useGateway and useAuth at the module level so the
 * component never hits the real API or AuthProvider.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockListSystemCaches = vi.fn();
const mockFlushSystemCache = vi.fn();

// One client object across renders, as the auth provider gives the console
// (it is memoised there): the pane loads when it mounts and when the client
// is another one. `gateway.client` is replaced to stand for that.
const gateway = vi.hoisted((): { client: Record<string, unknown> } => ({ client: {} }));
const newClient = () => ({ listSystemCaches: mockListSystemCaches, flushSystemCache: mockFlushSystemCache });

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: gateway.client }),
}));

const mockHasPermission = vi.fn();
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: mockHasPermission }),
}));

import SystemCachesPane from '../SystemCachesPane';

const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

const FIXTURE = {
    tenant_id: 'tenant-acme',
    generated_at: '2026-06-01T12:00:00+00:00',
    layers: [
        {
            layer: 'semantic',
            hit_count: 800,
            miss_count: 200,
            hit_rate: 0.8,
            item_count_approx: null,
            last_flushed_at: '2026-05-31T10:00:00+00:00',
            scopes_available: ['tenant', 'group', 'model'],
            detail: null,
        },
        {
            layer: 'policy',
            hit_count: 0,
            miss_count: 0,
            hit_rate: 0,
            item_count_approx: null,
            last_flushed_at: null,
            scopes_available: ['tenant'],
            detail: 'Live counters require a gateway-core fan-out reply round-trip.',
        },
        {
            layer: 'group',
            hit_count: 0,
            miss_count: 0,
            hit_rate: 0,
            item_count_approx: null,
            last_flushed_at: null,
            scopes_available: ['tenant'],
            detail: null,
        },
        {
            layer: 'tool',
            hit_count: 0,
            miss_count: 0,
            hit_rate: 0,
            item_count_approx: null,
            last_flushed_at: null,
            scopes_available: ['mcp_server', 'tenant'],
            detail: null,
        },
        {
            layer: 'response',
            hit_count: 0,
            miss_count: 0,
            hit_rate: 0,
            item_count_approx: null,
            last_flushed_at: null,
            scopes_available: ['config', 'tenant'],
            detail: null,
        },
    ],
};

beforeEach(() => {
    gateway.client = newClient();
    mockListSystemCaches.mockReset();
    mockFlushSystemCache.mockReset();
    mockHasPermission.mockReset();
});

describe('SystemCachesPane', () => {
    it('renders one card per layer with hit/miss numbers and last-flushed', async () => {
        grant(['system:read', 'system:operate']);
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);

        render(<SystemCachesPane />);

        await waitFor(() => {
            expect(mockListSystemCaches).toHaveBeenCalledTimes(1);
        });

        // All 5 layer cards
        for (const layer of ['semantic', 'policy', 'group', 'tool', 'response']) {
            expect(
                await screen.findByTestId(`system-caches-card-${layer}`)
            ).toBeDefined();
        }

        // Semantic card surfaces both numeric totals
        const semantic = await screen.findByTestId('system-caches-card-semantic');
        expect(semantic.textContent).toContain('800');
        expect(semantic.textContent).toContain('200');
        // 80% hit-rate (the gauge bar fills proportionally — we just
        // assert that the bar element rendered, since we don't
        // measure pixel widths in unit tests).
        expect(
            screen.getByTestId('system-caches-gauge-semantic')
        ).toBeDefined();
    });

    it('surfaces the limitation detail when the backend supplies one', async () => {
        grant(['system:read']);
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);

        render(<SystemCachesPane />);

        const policyDetail = await screen.findByTestId(
            'system-caches-detail-policy'
        );
        expect(policyDetail.textContent).toMatch(/fan-out/i);
    });

    it('disables Flush when the operator lacks system:operate', async () => {
        grant(['system:read']); // read-only auditor
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);

        render(<SystemCachesPane />);

        const btn = (await screen.findByTestId(
            'system-caches-flush-semantic'
        ));
        expect(btn).toBeDisabled();
        // The tooltip names the missing permission so the operator
        // can ask their admin for exactly that perm.
        expect(btn.title).toMatch(/system:operate/);
    });

    it('opens the scope-picker modal pre-populated with the first scope', async () => {
        grant(['system:read', 'system:operate']);
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);

        render(<SystemCachesPane />);

        fireEvent.click(
            await screen.findByTestId('system-caches-flush-semantic')
        );

        await screen.findByTestId('system-caches-flush-modal');
        const scopeSelect = screen.getByTestId<HTMLSelectElement>(
            'system-caches-flush-scope'
        );
        // First scope from the fixture is 'tenant' for semantic.
        expect(scopeSelect.value).toBe('tenant');
    });

    it('confirm posts the request and refreshes the page', async () => {
        grant(['system:read', 'system:operate']);
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);
        mockFlushSystemCache.mockResolvedValueOnce({ status: 'queued' });
        // Re-fetch after a successful flush.
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);

        render(<SystemCachesPane />);

        fireEvent.click(
            await screen.findByTestId('system-caches-flush-semantic')
        );
        await screen.findByTestId('system-caches-flush-modal');

        const scope = screen.getByTestId('system-caches-flush-scope');
        fireEvent.change(scope, { target: { value: 'group' } });

        const idInput = screen.getByTestId('system-caches-flush-id');
        fireEvent.change(idInput, { target: { value: 'grp-default' } });

        const reason = screen.getByTestId('system-caches-flush-reason');
        fireEvent.change(reason, {
            target: { value: 'RAG content rebuild' },
        });

        fireEvent.click(screen.getByTestId('system-caches-flush-confirm'));

        await waitFor(() => {
            expect(mockFlushSystemCache).toHaveBeenCalledWith({
                layer: 'semantic',
                scope: 'group',
                id: 'grp-default',
                reason: 'RAG content rebuild',
            });
        });
        // Modal closes
        await waitFor(() => {
            expect(
                screen.queryByTestId('system-caches-flush-modal')
            ).toBeNull();
        });
        // Page refreshes
        expect(mockListSystemCaches).toHaveBeenCalledTimes(2);
    });

    it('surfaces a server-side flush error inside the modal', async () => {
        grant(['system:read', 'system:operate']);
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);
        mockFlushSystemCache.mockRejectedValueOnce(
            new Error("NOTIFY channel 'brutor:cache:invalid' is not in the registry")
        );

        render(<SystemCachesPane />);

        fireEvent.click(
            await screen.findByTestId('system-caches-flush-semantic')
        );
        await screen.findByTestId('system-caches-flush-modal');

        fireEvent.change(screen.getByTestId('system-caches-flush-id'), {
            target: { value: 'tenant-acme' },
        });
        fireEvent.click(screen.getByTestId('system-caches-flush-confirm'));

        const err = await screen.findByTestId('system-caches-flush-error');
        expect(err.textContent).toMatch(/registry/);
        // Modal stays open so the operator can correct + retry.
        expect(
            screen.queryByTestId('system-caches-flush-modal')
        ).not.toBeNull();
    });

    it('confirm is disabled until an id is entered', async () => {
        grant(['system:read', 'system:operate']);
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);

        render(<SystemCachesPane />);

        fireEvent.click(
            await screen.findByTestId('system-caches-flush-semantic')
        );
        await screen.findByTestId('system-caches-flush-modal');

        const confirm = screen.getByTestId(
            'system-caches-flush-confirm'
        );
        expect(confirm).toBeDisabled();

        fireEvent.change(screen.getByTestId('system-caches-flush-id'), {
            target: { value: 'grp-1' },
        });
        expect(confirm).not.toBeDisabled();
    });

    it('cancel closes the modal without calling the API', async () => {
        grant(['system:read', 'system:operate']);
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);

        render(<SystemCachesPane />);

        fireEvent.click(
            await screen.findByTestId('system-caches-flush-semantic')
        );
        await screen.findByTestId('system-caches-flush-modal');

        fireEvent.click(screen.getByTestId('system-caches-flush-cancel'));
        await waitFor(() => {
            expect(
                screen.queryByTestId('system-caches-flush-modal')
            ).toBeNull();
        });
        expect(mockFlushSystemCache).not.toHaveBeenCalled();
    });

    it('renders the load error pane with a retry button', async () => {
        grant(['system:read']);
        mockListSystemCaches.mockRejectedValueOnce(new Error('boom'));

        render(<SystemCachesPane />);

        const err = await screen.findByTestId('system-caches-error');
        expect(err.textContent).toMatch(/boom/);

        // Retry triggers a second fetch.
        mockListSystemCaches.mockResolvedValueOnce(FIXTURE);
        fireEvent.click(screen.getByText('Retry'));
        await waitFor(() => {
            expect(mockListSystemCaches).toHaveBeenCalledTimes(2);
        });
    });

    it('loads once for a client, and again when the client is another one', async () => {
        mockListSystemCaches.mockResolvedValue(FIXTURE);
        const { rerender } = render(<SystemCachesPane />);
        await waitFor(() => expect(mockListSystemCaches).toHaveBeenCalledTimes(1));

        // A render with the same client asks nothing.
        rerender(<SystemCachesPane />);
        rerender(<SystemCachesPane />);
        expect(mockListSystemCaches).toHaveBeenCalledTimes(1);

        // The client was replaced (another session): the stats are its to give.
        gateway.client = newClient();
        rerender(<SystemCachesPane />);
        await waitFor(() => expect(mockListSystemCaches).toHaveBeenCalledTimes(2));
    });
});
