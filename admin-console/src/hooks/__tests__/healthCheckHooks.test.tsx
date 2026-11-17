/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useAIModelHealthCheck / useMCPServerHealthCheck / useDashboardHealthCheck
 *
 * These three are thin, and the thinness is exactly what makes them worth
 * pinning: each one's whole job is to bind the shared base hook to the right
 * settings key, the right default cadence, and the right probe. Get the key
 * wrong and a tab silently obeys another tab's schedule — including its
 * `enabled: false` — which looks like "health checks just stopped working"
 * with nothing in the logs to say why.
 *
 * So the assertions are about the wiring, not about re-testing the base hook:
 * which config section each tab reads, what cadence it falls back to when that
 * section is absent, and which client call its probe actually makes.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
    fetchHealthCheckConfig: vi.fn(),
    bulkHealthCheckLLMs: vi.fn(),
    triggerAllHealthChecks: vi.fn(),
    /** Swapped to null by the "no client yet" test. */
    clientAvailable: { value: true },
}));

vi.mock('../useGateway', () => ({
    useGateway: () => ({
        gatewayClient: mocks.clientAvailable.value
            ? {
                  fetchHealthCheckConfig: mocks.fetchHealthCheckConfig,
                  bulkHealthCheckLLMs: mocks.bulkHealthCheckLLMs,
              }
            : null,
        triggerAllHealthChecks: mocks.triggerAllHealthChecks,
    }),
}));

import { useAIModelHealthCheck } from '../useAIModelHealthCheck';
import { useMCPServerHealthCheck } from '../useMCPServerHealthCheck';

const FULL_CONFIG = {
    ai_settings: { enabled: true, interval_seconds: 120 },
    mcp_settings: { enabled: true, interval_seconds: 240 },
    dashboard_settings: { enabled: false, interval_seconds: 30 },
};

beforeEach(() => {
    vi.clearAllMocks();
    mocks.clientAvailable.value = true;
    mocks.fetchHealthCheckConfig.mockResolvedValue(FULL_CONFIG);
    mocks.bulkHealthCheckLLMs.mockResolvedValue(undefined);
    mocks.triggerAllHealthChecks.mockResolvedValue(undefined);
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
    vi.restoreAllMocks();
});

describe('useAIModelHealthCheck', () => {
    it('falls back to a five-minute cadence before the config arrives', () => {
        const { result } = renderHook(() => useAIModelHealthCheck());

        expect(result.current.intervalSeconds).toBe(300);
        expect(result.current.isEnabled).toBe(true);
    });

    it('adopts ai_settings — not another tab’s section — once loaded', async () => {
        const { result } = renderHook(() => useAIModelHealthCheck());

        await waitFor(() => expect(result.current.intervalSeconds).toBe(120));
        // 240 is mcp_settings and 30 is dashboard_settings; reading either
        // would put this tab on the wrong schedule.
        expect(result.current.intervalSeconds).not.toBe(240);
        expect(result.current.isEnabled).toBe(true);
    });

    it('probes models only — not the whole estate', async () => {
        const { result } = renderHook(() => useAIModelHealthCheck());
        await waitFor(() => expect(mocks.fetchHealthCheckConfig).toHaveBeenCalled());

        await act(async () => {
            await result.current.runHealthCheck();
        });

        expect(mocks.bulkHealthCheckLLMs).toHaveBeenCalledTimes(1);
        expect(mocks.triggerAllHealthChecks).not.toHaveBeenCalled();
        expect(result.current.lastCheckTime).toBeInstanceOf(Date);
    });

    it('asks for the real endpoint probe when an operator clicks', async () => {
        const { result } = renderHook(() => useAIModelHealthCheck());
        await waitFor(() => expect(mocks.fetchHealthCheckConfig).toHaveBeenCalled());

        await act(async () => {
            await result.current.runHealthCheck();
        });

        expect(mocks.bulkHealthCheckLLMs).toHaveBeenCalledWith({ probe: 'endpoint' });
    });

    it('asks for the free catalog probe on the background timer', async () => {
        // A timer-driven sweep that ran a REAL generation against every
        // chat/embedding/audio model every interval would cost money all
        // night. Background checks must request the catalog probe.
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
        try {
            renderHook(() => useAIModelHealthCheck());
            await act(async () => {
                for (let i = 0; i < 5; i++) await Promise.resolve();
            });

            await act(async () => {
                await vi.advanceTimersByTimeAsync(120 * 1000);
            });

            expect(mocks.bulkHealthCheckLLMs).toHaveBeenCalledTimes(1);
            expect(mocks.bulkHealthCheckLLMs).toHaveBeenCalledWith({ probe: 'catalog' });
        } finally {
            vi.useRealTimers();
        }
    });

    it('reports paused and never probes while a form is open', () => {
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
        try {
            const { result } = renderHook(() => useAIModelHealthCheck({ paused: true }));

            expect(result.current.isPaused).toBe(true);

            act(() => {
                vi.advanceTimersByTime(20 * 60 * 1000);
            });

            expect(mocks.bulkHealthCheckLLMs).not.toHaveBeenCalled();
        } finally {
            vi.useRealTimers();
        }
    });

    it('degrades quietly when no gateway client is mounted yet', async () => {
        mocks.clientAvailable.value = false;
        const { result } = renderHook(() => useAIModelHealthCheck());

        await act(async () => {
            await result.current.runHealthCheck();
        });

        expect(mocks.bulkHealthCheckLLMs).not.toHaveBeenCalled();
        expect(mocks.fetchHealthCheckConfig).not.toHaveBeenCalled();
        // No client is a startup race, not a failure — the tab must not
        // present it as a failed health check.
        expect(result.current.intervalSeconds).toBe(300);
    });

    it('survives a config fetch that fails, keeping the default cadence', async () => {
        mocks.fetchHealthCheckConfig.mockRejectedValue(new Error('control plane down'));

        const { result } = renderHook(() => useAIModelHealthCheck());

        await waitFor(() => expect(mocks.fetchHealthCheckConfig).toHaveBeenCalled());
        expect(result.current.healthCheckConfig).toBeNull();
        expect(result.current.intervalSeconds).toBe(300);
        expect(result.current.isEnabled).toBe(true);
    });
});

describe('useMCPServerHealthCheck', () => {
    it('falls back to a five-minute cadence before the config arrives', () => {
        const { result } = renderHook(() => useMCPServerHealthCheck());

        expect(result.current.intervalSeconds).toBe(300);
    });

    it('adopts mcp_settings once loaded', async () => {
        const { result } = renderHook(() => useMCPServerHealthCheck());

        await waitFor(() => expect(result.current.intervalSeconds).toBe(240));
        expect(result.current.isEnabled).toBe(true);
    });

    it('probes through the shared estate-wide trigger', async () => {
        const { result } = renderHook(() => useMCPServerHealthCheck());
        await waitFor(() => expect(mocks.fetchHealthCheckConfig).toHaveBeenCalled());

        await act(async () => {
            await result.current.runHealthCheck();
        });

        expect(mocks.triggerAllHealthChecks).toHaveBeenCalledTimes(1);
        expect(result.current.lastCheckTime).toBeInstanceOf(Date);
    });

    it('records no check time when the probe fails', async () => {
        mocks.triggerAllHealthChecks.mockRejectedValue(new Error('proxy unreachable'));
        const { result } = renderHook(() => useMCPServerHealthCheck());
        await waitFor(() => expect(mocks.fetchHealthCheckConfig).toHaveBeenCalled());

        await act(async () => {
            await result.current.runHealthCheck();
        });

        // A failed probe must not stamp a fresh "last checked" time — that is
        // what makes a stale estate look freshly verified.
        expect(result.current.lastCheckTime).toBeNull();
        expect(result.current.isChecking).toBe(false);
    });
});
