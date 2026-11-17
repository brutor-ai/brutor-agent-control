/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useEffect, useRef, useState } from 'react';
import { useGateway } from './useGateway';
import { HealthCheckConfig } from '../lib/healthTypes';
import { runBackground } from '../lib/asyncSafe';
import { debugLog } from '../lib/debugLog';
import toast from 'react-hot-toast';

import { errorKind, failureSentence } from '../lib/narrow';
import type { ReadFailure } from './useRead';

/**
 * Configuration for different health check types
 */
type HealthCheckSettings = {
    enabled: boolean;
    interval_seconds: number;
    /** Present on ai_settings / mcp_settings; dashboard has no such flag. */
    pause_when_inactive?: boolean;
};

/**
 * Who asked for the check. `background` = the periodic timer (or the
 * catch-up check on becoming visible); `manual` = an operator click.
 * Probe functions use it to pick a cost profile — e.g. the AI Models
 * tab asks for the free catalog probe in the background and keeps the
 * real end-to-end probe for an operator's explicit click.
 */
export type HealthCheckTrigger = 'background' | 'manual';

const isDocumentHidden = (): boolean =>
    typeof document !== 'undefined' && document.hidden === true;

type ConfigKey = 'ai_settings' | 'mcp_settings' | 'dashboard_settings';

interface UseHealthCheckBaseOptions {
    tabName: string;                              // For logging (e.g., "AI Models")
    configKey: ConfigKey;                         // Which config to use
    healthCheckFn: (trigger: HealthCheckTrigger) => Promise<void>; // The actual health check function
    paused?: boolean;                             // Optional pause flag
    defaultInterval?: number;                     // Default interval if config not available
}

/**
 * Base Health Check Hook
 *
 * Provides common health check functionality for all tabs:
 * - Config fetching from health check API
 * - First mount detection (no duplicate checks)
 * - 10-second cooldown period
 * - Periodic interval management
 * - Pause/resume support
 * - Pause while the browser tab is hidden when the section's
 *   `pause_when_inactive` setting is on (Page Visibility API), with one
 *   catch-up check on becoming visible if the last check is stale
 * - Manual health check function
 *
 * Used by: useAIModelHealthCheck, useMCPServerHealthCheck, useDashboardHealthCheck
 */
export const useHealthCheckBase = ({
                                       tabName,
                                       configKey,
                                       healthCheckFn,
                                       paused = false,
                                       defaultInterval = 300
                                   }: UseHealthCheckBaseOptions) => {
    const { gatewayClient } = useGateway();
    const intervalRef = useRef<NodeJS.Timeout | null>(null);
    const isCheckingRef = useRef(false);
    const [lastCheckTime, setLastCheckTime] = useState<Date | null>(null);
    const lastCheckTimeRef = useRef<Date | null>(null);  // For cooldown check
    const lastSweepRef = useRef<Date | null>(null);  // The last sweep that worked
    const [isChecking, setIsChecking] = useState(false);
    const [healthCheckConfig, setHealthCheckConfig] = useState<HealthCheckConfig | null>(null);
    // A timer's sweep that failed: the statuses on the page stand as of the
    // last sweep that worked. A configuration that could not be read: the
    // defaults are in use. The page shows both (`ReadFailureNotice`).
    const [checkFailure, setCheckFailure] = useState<ReadFailure | null>(null);
    const [configFailure, setConfigFailure] = useState<ReadFailure | null>(null);
    const configFailed = (error: unknown) =>
        setConfigFailure({ sentence: failureSentence('Failed to load the health check settings; the defaults are in use', error), loadedAt: null });

    // Extract settings from health check config
    const settings = healthCheckConfig?.[configKey] as HealthCheckSettings | undefined;
    const isEnabled = settings?.enabled ?? true;
    const intervalSeconds = settings?.interval_seconds ?? defaultInterval;
    // Strict: only an explicit `true` pauses. Before the config loads (or
    // for the dashboard section, which has no such flag) the poll is
    // always on.
    const pauseWhenInactive = settings?.pause_when_inactive === true;

    const [documentHidden, setDocumentHidden] = useState<boolean>(isDocumentHidden);
    // Set when the interval was torn down because the tab went hidden, so
    // the restart on becoming visible knows to consider a catch-up check.
    const pausedForHiddenRef = useRef(false);

    /**
     * Track page visibility — only while the setting asks us to, so a tab
     * with the setting off never re-renders on visibility flips.
     */
    useEffect(() => {
        if (!pauseWhenInactive || typeof document === 'undefined') return;
        const onVisibilityChange = () => setDocumentHidden(isDocumentHidden());
        onVisibilityChange();
        document.addEventListener('visibilitychange', onVisibilityChange);
        return () => document.removeEventListener('visibilitychange', onVisibilityChange);
    }, [pauseWhenInactive]);

    /**
     * Fetch health check config on mount
     */
    useEffect(() => {
        const fetchHealthConfig = async () => {
            if (!gatewayClient) return;

            try {
                const config = await gatewayClient.fetchHealthCheckConfig();
                setHealthCheckConfig(config);
                setConfigFailure(null);
                debugLog(`[${tabName}] Health check config loaded`);
            } catch (error) {
                configFailed(error);
            }
        };

        runBackground(fetchHealthConfig(), 'health config');
    }, [gatewayClient, tabName, configKey]);

    /**
     * Refresh health check config (for when settings change)
     */
    const refreshConfig = async () => {
        if (!gatewayClient) return;

        try {
            const config = await gatewayClient.fetchHealthCheckConfig();
            setHealthCheckConfig(config);
            setConfigFailure(null);
            debugLog(`[${tabName}] Health check config refreshed`);
        } catch (error) {
            configFailed(error);
        }
    };

    /**
     * Periodic health check with cooldown protection
     */
    useEffect(() => {
        const runHealthCheck = async () => {
            // Cooldown check: Don't run if checked within last 10 seconds
            if (lastCheckTimeRef.current) {
                const timeSinceLastCheck = Date.now() - lastCheckTimeRef.current.getTime();
                if (timeSinceLastCheck < 10000) {  // 10 seconds cooldown
                    debugLog(`[${tabName}] Cooldown active, skipping check (last check ${(timeSinceLastCheck / 1000).toFixed(1)}s ago)`);
                    return;
                }
            }

            // Check if already running
            if (isCheckingRef.current) {
                debugLog(`[${tabName}] Health check already in progress, skipping`);
                return;
            }

            isCheckingRef.current = true;
            setIsChecking(true);
            const startTime = Date.now();

            debugLog(`[${tabName}] Running health checks...`);

            try {
                await healthCheckFn('background');

                const duration = Date.now() - startTime;
                const now = new Date();
                setLastCheckTime(now);
                lastCheckTimeRef.current = now;  // Update ref for cooldown
                lastSweepRef.current = now;
                setCheckFailure(null);

                debugLog(`[${tabName}] Health checks completed (${duration}ms)`);
            } catch (error) {
                // Nobody asked for this sweep: no toast. The page says the
                // statuses stand as of the last sweep that worked.
                setCheckFailure({
                    sentence: failureSentence(`${tabName}: the health check failed`, error),
                    loadedAt: lastSweepRef.current?.getTime() ?? null,
                });
            } finally {
                isCheckingRef.current = false;
                setIsChecking(false);
            }
        };

        // Clear any existing interval
        if (intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
        }

        // Check if paused
        if (paused) {
            debugLog(`[${tabName}] Health checks paused (form/modal is open)`);
            return;
        }

        // Don't start if not enabled
        if (!isEnabled) {
            debugLog(`[${tabName}] Health checks disabled in configuration`);
            return;
        }

        // Pause while the tab is hidden (setting on). A background tab left
        // open overnight must not keep probing every resource.
        if (pauseWhenInactive && documentHidden) {
            debugLog(`[${tabName}] Health checks paused (tab hidden, pause_when_inactive)`);
            pausedForHiddenRef.current = true;
            return;
        }

        // Becoming visible again: run ONE catch-up check, but only if the
        // last check is older than the interval — flipping between tabs
        // must not turn into a probe per flip.
        if (pausedForHiddenRef.current) {
            pausedForHiddenRef.current = false;
            const last = lastCheckTimeRef.current;
            if (last && Date.now() - last.getTime() >= intervalSeconds * 1000) {
                debugLog(`[${tabName}] Tab visible again and last check is stale — running catch-up check`);
                runBackground(runHealthCheck(), 'health check');
            }
        }

        debugLog(`[${tabName}] Starting periodic health checks (interval: ${intervalSeconds}s)`);

        // Set cooldown ref to NOW to prevent immediate check,
        // but do NOT update lastCheckTime state (no real check ran yet).
        if (!lastCheckTimeRef.current) {
            lastCheckTimeRef.current = new Date();
        }

        // Set up periodic checks
        intervalRef.current = setInterval(() => runBackground(runHealthCheck(), 'health check'), intervalSeconds * 1000);

        // Cleanup on unmount
        return () => {
            if (intervalRef.current) {
                debugLog(`[${tabName}] Stopping periodic health checks`);
                clearInterval(intervalRef.current);
                intervalRef.current = null;
            }
        };
    }, [isEnabled, intervalSeconds, paused, pauseWhenInactive, documentHidden, healthCheckFn, tabName]);

    /**
     * Manual health check function (for button clicks)
     */
    const runManualHealthCheck = async () => {
        if (isCheckingRef.current) {
            debugLog(`[${tabName}] Health check already in progress, skipping manual request`);
            return;
        }

        isCheckingRef.current = true;
        setIsChecking(true);
        const startTime = Date.now();

        debugLog(`[${tabName}] Running manual health check...`);

        try {
            await healthCheckFn('manual');

            const duration = Date.now() - startTime;
            const now = new Date();
            setLastCheckTime(now);
            lastCheckTimeRef.current = now;
            lastSweepRef.current = now;
            setCheckFailure(null);

            debugLog(`[${tabName}] Manual health check completed (${duration}ms)`);
        } catch (error) {
            const duration = Date.now() - startTime;
            console.error(`[${tabName}] Manual health check failed (${duration}ms):`, errorKind(error));
            // Asked for by the user: told, with what the other side said.
            toast.error(failureSentence(`${tabName}: the health check failed`, error));
        } finally {
            isCheckingRef.current = false;
            setIsChecking(false);
        }
    };

    return {
        isEnabled,
        isPaused: paused,
        intervalSeconds,
        lastCheckTime,
        isChecking,
        runHealthCheck: runManualHealthCheck,
        healthCheckConfig,
        refreshConfig,
        checkFailure,
        configFailure,
    };
};

export default useHealthCheckBase;