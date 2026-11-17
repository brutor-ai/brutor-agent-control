/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemLogsPane.
 *
 * Coverage:
 *   - Renders the curated module list with level radio buttons that
 *     reflect the API response.
 *   - Without `system:operate`, every level radio is disabled and
 *     advertises the required permission via tooltip.
 *   - With `system:operate`, clicking a level calls
 *     `setLogLevel({module, level, scope:'all'})` and triggers a
 *     refetch.
 *   - A failed level-set surfaces in an inline error banner without
 *     dropping the slider grid.
 *   - The Pause / Resume button flips the local paused flag AND
 *     sends a control op down the WebSocket.
 *   - When the WebSocket reports a `dropped_since_last`, the cumulative
 *     dropped-line counter renders.
 *   - The replica id from the `hello` frame is surfaced in the
 *     connection badge.
 *   - The Clear button drops the rendered tail without disconnecting.
 *   - A read-only operator without `system:read` sees the permission
 *     hint instead of the pane.
 *
 * Test doubles:
 *   - `useAuth` is mocked so `hasPermission` is a controllable spy.
 *   - `useGateway` is mocked so `getLogLevels`, `setLogLevel`, and
 *     `openLogsWebSocket` are spies. The WS spy returns a stub object
 *     that exposes `send`, `close`, and lifecycle hooks we drive
 *     manually from the test.
 */

import { act } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockHasPermission = vi.fn();
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: mockHasPermission }),
}));

const mockGetLogLevels = vi.fn();
const mockSetLogLevel = vi.fn();
const mockOpenLogsWebSocket = vi.fn();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            getLogLevels: mockGetLogLevels,
            setLogLevel: mockSetLogLevel,
            openLogsWebSocket: mockOpenLogsWebSocket,
        },
    }),
}));

import SystemLogsPane from '../SystemLogsPane';
import { LogLevelListResponse } from '../../../lib/systemOpsTypes';

const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

/** A WebSocket-shaped stub the test controls. */
function makeWsStub() {
    const stub: any = {
        send: vi.fn(),
        close: vi.fn(),
        readyState: 1, // OPEN
        onopen: undefined,
        onmessage: undefined,
        onerror: undefined,
        onclose: undefined,
    };
    return stub;
}

const sampleLevels: LogLevelListResponse = {
    items: [
        {
            module: 'guardrails-service',
            level: 'info',
            effective_level_int: 20,
            is_curated: true,
            override_level: null,
            override_at: null,
            override_by: null,
        },
        {
            module: 'mcp-server-client',
            level: 'debug',
            effective_level_int: 10,
            is_curated: true,
            override_level: 'debug',
            override_at: '2026-06-07T11:00:00Z',
            override_by: null,
        },
    ],
};

describe('SystemLogsPane', () => {
    beforeEach(() => {
        mockHasPermission.mockReset();
        mockGetLogLevels.mockReset();
        mockSetLogLevel.mockReset();
        mockOpenLogsWebSocket.mockReset();
    });

    it('renders permission hint when read is missing', async () => {
        grant([]); // no permissions
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        mockOpenLogsWebSocket.mockResolvedValue(makeWsStub());
        render(<SystemLogsPane />);
        expect(
            await screen.findByText(/system:read/i),
        ).toBeInTheDocument();
        // Should not have attempted to open the WebSocket at all.
        expect(mockOpenLogsWebSocket).not.toHaveBeenCalled();
    });

    it('renders levels and disables sliders without operate perm', async () => {
        grant(['system:read']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        const ws = makeWsStub();
        mockOpenLogsWebSocket.mockResolvedValue(ws);

        render(<SystemLogsPane />);

        // Wait for the table to populate.
        expect(
            await screen.findByText('guardrails-service'),
        ).toBeInTheDocument();
        expect(screen.getByText('mcp-server-client')).toBeInTheDocument();

        // Every level button should be disabled when canOperate=false.
        const btn = screen.getByTestId(
            'system-logs-level-guardrails-service-error',
        );
        expect(btn).toBeDisabled();
        expect(btn.getAttribute('title')).toMatch(/system:operate/i);

        // Drift badge wasn't triggered (override matches live for mcp).
        expect(screen.queryByText(/drift/i)).not.toBeInTheDocument();
    });

    it('calls setLogLevel and refetches when an operator clicks a level', async () => {
        grant(['system:read', 'system:operate']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        mockSetLogLevel.mockResolvedValue({});
        mockOpenLogsWebSocket.mockResolvedValue(makeWsStub());

        render(<SystemLogsPane />);

        const errorBtn = await screen.findByTestId(
            'system-logs-level-guardrails-service-error',
        );
        expect(errorBtn).not.toBeDisabled();

        fireEvent.click(errorBtn);

        await waitFor(() => {
            expect(mockSetLogLevel).toHaveBeenCalledWith({
                module: 'guardrails-service',
                level: 'error',
                scope: 'all',
            });
        });
        // Refetch was triggered.
        await waitFor(() => {
            expect(mockGetLogLevels).toHaveBeenCalledTimes(2);
        });
    });

    it('surfaces a setLogLevel error inline without dropping the grid', async () => {
        grant(['system:read', 'system:operate']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        mockSetLogLevel.mockRejectedValue(new Error('boom'));
        mockOpenLogsWebSocket.mockResolvedValue(makeWsStub());

        render(<SystemLogsPane />);
        const btn = await screen.findByTestId(
            'system-logs-level-guardrails-service-warn',
        );
        fireEvent.click(btn);

        expect(
            await screen.findByTestId('system-logs-levels-error'),
        ).toHaveTextContent('boom');
        // Grid is still there.
        expect(screen.getByText('guardrails-service')).toBeInTheDocument();
    });

    it('renders a hello frame and tail lines from the WebSocket', async () => {
        grant(['system:read']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        const ws = makeWsStub();
        mockOpenLogsWebSocket.mockResolvedValue(ws);

        render(<SystemLogsPane />);

        // Trigger onopen + hello frame.
        await waitFor(() => expect(ws.onmessage).toBeTypeOf('function'));
        act(() => {
            ws.onopen?.({} as any);
            ws.onmessage?.({
                data: JSON.stringify({
                    type: 'hello',
                    replica_id: 'gateway-control-7c9',
                    ring_size: 2000,
                    initial_replay: 200,
                }),
            } as any);
            ws.onmessage?.({
                data: JSON.stringify({
                    seq: 1,
                    ts: '2026-06-07T11:00:00Z',
                    level: 'info',
                    logger: 'guardrails-service',
                    message: 'first line',
                }),
            } as any);
            ws.onmessage?.({
                data: JSON.stringify({
                    seq: 2,
                    ts: '2026-06-07T11:00:01Z',
                    level: 'warn',
                    logger: 'mcp-server-client',
                    message: 'second line',
                    dropped_since_last: 3,
                }),
            } as any);
        });

        // Replica id from hello frame is visible.
        expect(
            await screen.findByTestId('system-logs-connection-badge'),
        ).toHaveTextContent('gateway-control-7c9');
        // Both tail lines rendered.
        expect(screen.getByText(/first line/)).toBeInTheDocument();
        expect(screen.getByText(/second line/)).toBeInTheDocument();
        // Dropped counter surfaces the cumulative number.
        expect(
            screen.getByTestId('system-logs-dropped'),
        ).toHaveTextContent('3');
    });

    it('pause button flips state and sends a pause control op', async () => {
        grant(['system:read']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        const ws = makeWsStub();
        mockOpenLogsWebSocket.mockResolvedValue(ws);

        render(<SystemLogsPane />);
        const btn = await screen.findByTestId('system-logs-pause-toggle');
        // Mark socket as OPEN before the toggle effect fires.
        act(() => {
            ws.onopen?.({} as any);
        });
        // Initial mount sends a 'resume' once because the effect runs
        // on first render. Reset send so we observe the pause cleanly.
        ws.send.mockReset();

        fireEvent.click(btn);

        await waitFor(() => {
            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({ op: 'pause' }),
            );
        });
        expect(btn).toHaveTextContent(/resume/i);
    });

    it('clear button empties the viewport without closing the socket', async () => {
        grant(['system:read']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        const ws = makeWsStub();
        mockOpenLogsWebSocket.mockResolvedValue(ws);

        render(<SystemLogsPane />);

        await waitFor(() => expect(ws.onmessage).toBeTypeOf('function'));
        act(() => {
            ws.onmessage?.({
                data: JSON.stringify({
                    seq: 1,
                    ts: '2026-06-07T11:00:00Z',
                    level: 'info',
                    logger: 'guardrails-service',
                    message: 'to be cleared',
                }),
            } as any);
        });

        expect(await screen.findByText(/to be cleared/)).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('system-logs-clear'));
        await waitFor(() =>
            expect(screen.queryByText(/to be cleared/)).not.toBeInTheDocument(),
        );
        // Close was NOT called by Clear.
        expect(ws.close).not.toHaveBeenCalled();
    });

    it.each([
        ['invalid or expired ticket', 'The log tail was refused: its ticket was spent or had expired. Reload the page to open it again.'],
        ['session ended', 'The log tail closed: the session it was opened in has ended. Sign in again to follow the log.'],
    ])('says what the close reason "%s" means', async (reason, sentence) => {
        grant(['system:read']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        const ws = makeWsStub();
        mockOpenLogsWebSocket.mockResolvedValue(ws);
        vi.spyOn(console, 'error').mockImplementation(() => undefined);

        render(<SystemLogsPane />);
        await waitFor(() => expect(ws.onclose).toBeTypeOf('function'));
        act(() => {
            ws.onclose?.({ code: 1008, reason });
        });

        expect(await screen.findByTestId('system-logs-tail-error')).toHaveTextContent(sentence);
    });

    it('says so when no ticket could be had', async () => {
        grant(['system:read']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        mockOpenLogsWebSocket.mockRejectedValue(new Error('Permission denied'));
        vi.spyOn(console, 'error').mockImplementation(() => undefined);

        render(<SystemLogsPane />);

        expect(await screen.findByTestId('system-logs-tail-error')).toHaveTextContent(
            'The log tail could not be opened: Permission denied',
        );
    });

    it('reports a socket error without the socket', async () => {
        grant(['system:read']);
        mockGetLogLevels.mockResolvedValue(sampleLevels);
        const ws = makeWsStub();
        ws.url = 'ws://localhost:5050/v1/admin/system/logs/tail?ticket=tk-41c9';
        mockOpenLogsWebSocket.mockResolvedValue(ws);
        const written = vi.spyOn(console, 'error').mockImplementation(() => undefined);

        render(<SystemLogsPane />);
        await waitFor(() => expect(ws.onerror).toBeTypeOf('function'));
        act(() => {
            ws.onerror?.({ type: 'error', target: ws, currentTarget: ws });
        });

        expect(written).toHaveBeenCalledWith('[system-logs] WS error');
        for (const call of written.mock.calls) {
            for (const value of call) expect(typeof value).not.toBe('object');
        }
        written.mockRestore();
    });
});
