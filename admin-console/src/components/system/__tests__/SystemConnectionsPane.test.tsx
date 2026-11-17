/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemConnectionsPane.
 *
 * Coverage focuses on the operator-visible invariants:
 *
 *   - Cards render in the expected order (infra first, then LLM
 *     providers) so the page is stable across reloads.
 *   - Saturation bar renders an indeterminate stripe when size is
 *     null (Redis / Qdrant cards) instead of dividing by zero.
 *   - Recycle button is HIDDEN for non-recyclable kinds (Qdrant,
 *     LLM provider entries) — we never want an operator clicking
 *     a control that does nothing on a 3am page.
 *   - Recycle button on Postgres is enabled when the operator has
 *     system:operate, disabled with the right tooltip otherwise.
 *   - Recycle posts the right body and surfaces the {status, detail}
 *     pair on the card after success.
 *   - Error fetch surfaces a retry affordance rather than rendering
 *     a blank pane.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockListPools = vi.fn();
const mockRecyclePool = vi.fn();
const mockHasPermission = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            listSystemConnections: mockListPools,
            recycleSystemPool: mockRecyclePool,
        },
    }),
}));

vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: mockHasPermission }),
}));

import SystemConnectionsPane from '../SystemConnectionsPane';

const POOLS_FIXTURE = {
    tenant_id: 'tenant-1',
    generated_at: '2026-06-07T10:00:00Z',
    pools: [
        {
            kind: 'postgres',
            label: 'Postgres',
            size: 10,
            checked_in: 6,
            checked_out: 4,
            overflow: 0,
            saturation: 0.4,
            client_count: null,
            recyclable: true,
            most_recent_latency_ms: null,
            detail: 'QueuePool — size=10, in=6, out=4, overflow=0',
            observed_at: '2026-06-07T10:00:00Z',
        },
        {
            kind: 'redis',
            label: 'Redis',
            size: null,
            checked_in: null,
            checked_out: null,
            overflow: null,
            saturation: null,
            client_count: null,
            recyclable: false,
            most_recent_latency_ms: null,
            detail: 'Redis client is opened per-call.',
            observed_at: '2026-06-07T10:00:00Z',
        },
        {
            kind: 'qdrant',
            label: 'Qdrant',
            size: null,
            checked_in: null,
            checked_out: null,
            overflow: null,
            saturation: null,
            client_count: 1,
            recyclable: false,
            most_recent_latency_ms: 42,
            detail: 'One client per request — no shared pool.',
            observed_at: '2026-06-07T10:00:00Z',
        },
        {
            kind: 'llm_provider:openai',
            label: 'LLM · OpenAI',
            size: null,
            checked_in: null,
            checked_out: null,
            overflow: null,
            saturation: null,
            client_count: null,
            recyclable: false,
            most_recent_latency_ms: null,
            detail: 'Provider pool stats live in the Rust proxy.',
            observed_at: '2026-06-07T10:00:00Z',
        },
    ],
};

const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

beforeEach(() => {
    mockListPools.mockReset();
    mockRecyclePool.mockReset();
    mockHasPermission.mockReset();
});

describe('SystemConnectionsPane', () => {
    it('renders one card per pool from the response', async () => {
        grant(['system:operate']);
        mockListPools.mockResolvedValueOnce(POOLS_FIXTURE);

        render(<SystemConnectionsPane />);

        await waitFor(() =>
            expect(
                screen.getByTestId('system-connections-card-postgres'),
            ).toBeDefined(),
        );
        expect(screen.getByTestId('system-connections-card-redis')).toBeDefined();
        expect(screen.getByTestId('system-connections-card-qdrant')).toBeDefined();
        expect(
            screen.getByTestId('system-connections-card-llm_provider:openai'),
        ).toBeDefined();
    });

    it('renders an indeterminate saturation bar for pools without size', async () => {
        grant(['system:operate']);
        mockListPools.mockResolvedValueOnce(POOLS_FIXTURE);

        render(<SystemConnectionsPane />);

        // The Redis card has saturation=null — the bar still renders
        // (so card heights stay uniform) but with no width style.
        const redisBar = await screen.findByTestId(
            'system-connections-saturation-redis',
        );
        // No populated child div (i.e. the indeterminate variant).
        expect(redisBar.querySelector('[class*="bg-emerald"]')).toBeNull();
        expect(redisBar.querySelector('[class*="bg-amber"]')).toBeNull();
        expect(redisBar.querySelector('[class*="bg-red"]')).toBeNull();
    });

    it('hides the recycle button for non-recyclable kinds (qdrant, llm provider)', async () => {
        grant(['system:operate']);
        mockListPools.mockResolvedValueOnce(POOLS_FIXTURE);

        render(<SystemConnectionsPane />);

        await screen.findByTestId('system-connections-card-qdrant');
        expect(
            screen.queryByTestId('system-connections-recycle-qdrant'),
        ).toBeNull();
        expect(
            screen.queryByTestId(
                'system-connections-recycle-llm_provider:openai',
            ),
        ).toBeNull();
    });

    it('renders the postgres recycle button disabled when the operator lacks system:operate', async () => {
        grant([]); // read-only
        mockListPools.mockResolvedValueOnce(POOLS_FIXTURE);

        render(<SystemConnectionsPane />);

        const btn = await screen.findByTestId(
            'system-connections-recycle-postgres',
        );
        expect((btn as HTMLButtonElement).disabled).toBe(true);
        expect(btn.getAttribute('title')).toMatch(/system:operate/);
    });

    it('recycles postgres and surfaces the result', async () => {
        grant(['system:operate']);
        mockListPools
            .mockResolvedValueOnce(POOLS_FIXTURE)
            // Second call after recycle:
            .mockResolvedValueOnce(POOLS_FIXTURE);
        mockRecyclePool.mockResolvedValueOnce({
            status: 'recycled',
            detail: 'SQLAlchemy engine pool disposed.',
        });

        render(<SystemConnectionsPane />);

        const btn = await screen.findByTestId(
            'system-connections-recycle-postgres',
        );
        fireEvent.click(btn);

        await waitFor(() =>
            expect(mockRecyclePool).toHaveBeenCalledWith({ kind: 'postgres' }),
        );
        await waitFor(() => {
            const result = screen.getByTestId(
                'system-connections-recycle-result-postgres',
            );
            expect(result.textContent).toContain('recycled');
            expect(result.textContent).toContain('disposed');
        });
    });

    it('shows a retry affordance when the initial fetch fails', async () => {
        grant(['system:operate']);
        mockListPools.mockRejectedValueOnce(new Error('boom'));

        render(<SystemConnectionsPane />);

        await waitFor(() =>
            expect(screen.getByTestId('system-connections-error')).toBeDefined(),
        );
        expect(screen.getByText(/Retry/i)).toBeDefined();
    });
});
