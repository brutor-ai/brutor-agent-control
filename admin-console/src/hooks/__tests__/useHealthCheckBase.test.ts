/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useHealthCheckBase Hook Tests
 *
 * Tests the periodic health check hook. `lastCheckTime` state is set only
 * after a real check: setting it on mount or on a pause transition causes
 * React error #310, "too many re-renders".
 */

import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

// We need to mock useGateway before importing the hook
const mockFetchHealthCheckConfig = vi.fn()
const mockGatewayClient = {
    fetchHealthCheckConfig: mockFetchHealthCheckConfig,
}

vi.mock('../useGateway', () => ({
    useGateway: () => ({ gatewayClient: mockGatewayClient }),
}))

// Import after mocking
import { useHealthCheckBase, type HealthCheckTrigger } from '../useHealthCheckBase'

describe('useHealthCheckBase', () => {
    let mockHealthCheckFn: Mock<(trigger: HealthCheckTrigger) => Promise<void>>

    beforeEach(() => {
        // Only fake the interval primitives the hook itself uses.
        // The default `useFakeTimers()` also stubs the microtask
        // scheduler, which prevents the hook's async
        // `fetchHealthCheckConfig` Promise from resolving during
        // `renderHook` — every test then hangs waiting for the
        // mounted state to converge. Scoping `toFake` keeps
        // `vi.advanceTimersByTime` working for the pause tests
        // while letting real microtasks flush.
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
        mockHealthCheckFn = vi.fn<(trigger: HealthCheckTrigger) => Promise<void>>().mockResolvedValue(undefined)
        mockFetchHealthCheckConfig.mockResolvedValue({
            ai_settings: { enabled: true, interval_seconds: 300 },
        })
    })

    afterEach(() => {
        vi.useRealTimers()
        vi.clearAllMocks()
    })

    // =========================================================================
    // Core Behavior
    // =========================================================================

    describe('initialization', () => {
        it('does NOT call healthCheckFn immediately on mount', () => {
            renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
            }))

            expect(mockHealthCheckFn).not.toHaveBeenCalled()
        })

        it('lastCheckTime is null on mount (no spurious state update)', () => {
            const { result } = renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
            }))

            // This is the critical assertion — lastCheckTime must NOT be set on mount
            // because that causes the #310 error (it triggers model reload effects)
            expect(result.current.lastCheckTime).toBeNull()
        })

        it('fetches health check config on mount', async () => {
            vi.useRealTimers() // config fetch is async, needs real timers
            renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
            }))

            await waitFor(() => {
                expect(mockFetchHealthCheckConfig).toHaveBeenCalledTimes(1)
            })
            // afterEach restores real timers; don't re-fake at end of
            // test or pending hook microtasks hang the next afterEach.
        })
    })

    // =========================================================================
    // Manual Health Check
    // =========================================================================

    describe('manual health check', () => {
        it('runHealthCheck calls healthCheckFn and updates lastCheckTime', async () => {
            vi.useRealTimers()

            const { result } = renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
                defaultInterval: 9999, // long interval, won't auto-fire
            }))

            expect(result.current.lastCheckTime).toBeNull()

            // Trigger manual check
            await act(async () => {
                await result.current.runHealthCheck()
            })

            expect(mockHealthCheckFn).toHaveBeenCalledTimes(1)
            expect(result.current.lastCheckTime).toBeInstanceOf(Date)
            // afterEach restores real timers; don't re-fake here.
        })
    })

    // =========================================================================
    // Pause/Resume (Form Editing)
    // =========================================================================

    describe('pausing', () => {
        it('does NOT run health checks when paused', async () => {
            renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
                paused: true,
                defaultInterval: 5,
            }))

            await act(async () => {
                vi.advanceTimersByTime(10000)
            })

            expect(mockHealthCheckFn).not.toHaveBeenCalled()
        })

        it('does NOT update lastCheckTime when transitioning to paused', () => {
            const { result, rerender } = renderHook(
                ({ paused }) => useHealthCheckBase({
                    tabName: 'Test',
                    configKey: 'ai_settings',
                    healthCheckFn: mockHealthCheckFn,
                    paused,
                    defaultInterval: 300,
                }),
                { initialProps: { paused: false } }
            )

            const beforePause = result.current.lastCheckTime

            // Transition to paused (form opens)
            rerender({ paused: true })

            // lastCheckTime must NOT change on pause transition:
            // a change here is what causes the #310 error
            expect(result.current.lastCheckTime).toBe(beforePause)
        })
    })

    // =========================================================================
    // Pause when inactive (Page Visibility API)
    //
    // The settings modal saves `pause_when_inactive` and the hook honours it:
    // otherwise a backgrounded admin tab keeps probing every model all night.
    // =========================================================================

    describe('pause when inactive', () => {
        let hidden = false
        const INTERVAL_S = 60

        const setHidden = (value: boolean) => {
            hidden = value
            act(() => {
                document.dispatchEvent(new Event('visibilitychange'))
            })
        }

        /** Let the async config fetch resolve and its state update commit. */
        const flushConfig = async () => {
            await act(async () => {
                for (let i = 0; i < 5; i++) await Promise.resolve()
            })
        }

        /** One interval at a time so each async check settles before the next tick. */
        const advanceIntervals = async (n: number) => {
            for (let i = 0; i < n; i++) {
                await act(async () => {
                    await vi.advanceTimersByTimeAsync(INTERVAL_S * 1000)
                })
            }
        }

        beforeEach(() => {
            vi.useRealTimers()
            // Date is faked too so "is the last check older than the
            // interval" is driven by the same clock as the interval.
            vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
            hidden = false
            Object.defineProperty(document, 'hidden', {
                configurable: true,
                get: () => hidden,
            })
        })

        afterEach(() => {
            Reflect.deleteProperty(document, 'hidden')
        })

        const render = (configKey: 'ai_settings' | 'mcp_settings', pause: boolean) => {
            mockFetchHealthCheckConfig.mockResolvedValue({
                [configKey]: {
                    enabled: true,
                    interval_seconds: INTERVAL_S,
                    pause_when_inactive: pause,
                },
            })
            return renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey,
                healthCheckFn: mockHealthCheckFn,
            }))
        }

        it('stops probing while the document is hidden when the setting is on', async () => {
            render('ai_settings', true)
            await flushConfig()

            setHidden(true)
            await advanceIntervals(10)

            expect(mockHealthCheckFn).not.toHaveBeenCalled()
        })

        it('keeps probing in a hidden tab when the setting is off', async () => {
            render('ai_settings', false)
            await flushConfig()

            setHidden(true)
            await advanceIntervals(3)

            expect(mockHealthCheckFn).toHaveBeenCalledTimes(3)
        })

        it('runs one catch-up check on becoming visible when the last check is stale, then resumes the interval', async () => {
            render('ai_settings', true)
            await flushConfig()

            setHidden(true)
            await advanceIntervals(5)
            expect(mockHealthCheckFn).not.toHaveBeenCalled()

            setHidden(false)
            await flushConfig()
            expect(mockHealthCheckFn).toHaveBeenCalledTimes(1)
            expect(mockHealthCheckFn).toHaveBeenLastCalledWith('background')

            await advanceIntervals(1)
            expect(mockHealthCheckFn).toHaveBeenCalledTimes(2)
        })

        it('does NOT run a catch-up check on becoming visible when the last check is fresh', async () => {
            render('ai_settings', true)
            await flushConfig()

            setHidden(true)
            await act(async () => {
                await vi.advanceTimersByTimeAsync((INTERVAL_S * 1000) / 2)
            })
            setHidden(false)
            await flushConfig()

            expect(mockHealthCheckFn).not.toHaveBeenCalled()
        })

        it('honours mcp_settings.pause_when_inactive for the MCP tab', async () => {
            render('mcp_settings', true)
            await flushConfig()

            setHidden(true)
            await advanceIntervals(4)

            expect(mockHealthCheckFn).not.toHaveBeenCalled()
        })
    })

    // =========================================================================
    // Trigger tagging — the probe fn learns whether a timer or a person asked
    // =========================================================================

    describe('trigger tagging', () => {
        it('tags timer-driven checks "background" and button checks "manual"', async () => {
            vi.useRealTimers()
            vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
            const { result } = renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
                defaultInterval: 60,
            }))
            await act(async () => {
                for (let i = 0; i < 5; i++) await Promise.resolve()
            })
            mockFetchHealthCheckConfig.mockResolvedValue({
                ai_settings: { enabled: true, interval_seconds: 60 },
            })
            await act(async () => {
                await result.current.refreshConfig()
            })

            await act(async () => {
                await vi.advanceTimersByTimeAsync(60 * 1000)
            })
            expect(mockHealthCheckFn).toHaveBeenLastCalledWith('background')

            await act(async () => {
                await result.current.runHealthCheck()
            })
            expect(mockHealthCheckFn).toHaveBeenLastCalledWith('manual')
        })
    })

    // =========================================================================
    // A sweep or a configuration that did not arrive is said, not dropped
    // =========================================================================

    describe('failures', () => {
        it('a background sweep that fails is held as a failure the page can show, and the next good sweep clears it', async () => {
            vi.useRealTimers()
            vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
            mockFetchHealthCheckConfig.mockResolvedValue({
                ai_settings: { enabled: true, interval_seconds: 60 },
            })
            mockHealthCheckFn.mockRejectedValue(new Error('The gateway is not reachable'))
            const { result } = renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
                defaultInterval: 60,
            }))
            await act(async () => {
                for (let i = 0; i < 5; i++) await Promise.resolve()
            })
            expect(result.current.checkFailure).toBeNull()

            await act(async () => {
                await vi.advanceTimersByTimeAsync(60 * 1000)
            })
            expect(result.current.checkFailure).toEqual({
                sentence: 'Test: the health check failed: The gateway is not reachable',
                loadedAt: null,
            })

            mockHealthCheckFn.mockResolvedValue(undefined)
            await act(async () => {
                await vi.advanceTimersByTimeAsync(60 * 1000)
            })
            expect(result.current.checkFailure).toBeNull()
        })

        it('a sweep that fails after a good one says since when the statuses stand', async () => {
            vi.useRealTimers()
            vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
            mockFetchHealthCheckConfig.mockResolvedValue({
                ai_settings: { enabled: true, interval_seconds: 60 },
            })
            const { result } = renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
                defaultInterval: 60,
            }))
            await act(async () => {
                for (let i = 0; i < 5; i++) await Promise.resolve()
            })
            await act(async () => {
                await vi.advanceTimersByTimeAsync(60 * 1000)
            })
            const stoodAt = result.current.lastCheckTime?.getTime()
            expect(stoodAt).toBeTypeOf('number')

            mockHealthCheckFn.mockRejectedValue(new Error('timeout'))
            await act(async () => {
                await vi.advanceTimersByTimeAsync(60 * 1000)
            })
            expect(result.current.checkFailure?.sentence).toBe('Test: the health check failed: timeout')
            expect(result.current.checkFailure?.loadedAt).toBe(stoodAt)
        })

        it('a configuration that could not be read is held as a failure: the defaults are in use', async () => {
            vi.useRealTimers()
            mockFetchHealthCheckConfig.mockRejectedValue(new Error('refused'))
            const { result } = renderHook(() => useHealthCheckBase({
                tabName: 'Test',
                configKey: 'ai_settings',
                healthCheckFn: mockHealthCheckFn,
                defaultInterval: 9999,
            }))
            await waitFor(() => expect(result.current.configFailure).not.toBeNull())
            expect(result.current.configFailure?.sentence).toBe(
                'Failed to load the health check settings; the defaults are in use: refused',
            )

            mockFetchHealthCheckConfig.mockResolvedValue({ ai_settings: { enabled: true, interval_seconds: 300 } })
            await act(async () => {
                await result.current.refreshConfig()
            })
            expect(result.current.configFailure).toBeNull()
        })
    })

    // Note: Cooldown and concurrency guards are internal to the effect's
    // runHealthCheck closure and are validated indirectly through the
    // periodic execution and manual check tests above.
})
