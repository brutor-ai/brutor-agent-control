/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The core of `useGateway`: what every domain hook in this
 * directory needs — the options, the authenticated client, the error handler
 * and the auth-state effect. The facade (../useGateway.ts) calls it first and
 * passes its result to the domain hooks.
 */

import { useState, useEffect, useMemo } from 'react';
import { env } from '../../env';
import { useErrorHandler, type ErrorDetails } from '../useErrorHandler';
import { useAuth } from '../useAuth';

// Type guard for HTTP errors with status codes
export function isHttpError(error: unknown): error is { status: number; message?: string } {
    return (
        typeof error === 'object' &&
        error !== null &&
        'status' in error &&
        typeof (error as any).status === 'number'
    );
}
export interface UseGatewayOptions {
    enableAuth?: boolean;
    autoDismissMs?: number;
    maxErrors?: number;
    gatewayUrl?: string;
}
/**
 * The core of the gateway hook: options, the session's client, the error
 * handler and the auth-state effect. Every domain hook takes this.
 */
export function useGatewayCore(options: UseGatewayOptions = {}) {
    const {
        enableAuth = true,
        autoDismissMs = 8000,
        maxErrors = 25,
        gatewayUrl = env.BRUTOR_CONTROL_PLANE_PUBLIC_URL,
    } = options;

    // LLM Proxy URL is the runtime Rust proxy (gateway-core, default
    // :8100) — NOT the admin / control plane (gateway-control, :5050).
    // Building it off `gatewayUrl` here sent operators to the admin
    // host, where /v1/proxy/llm doesn't exist; OpenAI-compatible
    // clients then failed with 404 against what the modal advertised
    // as the "LLM Proxy Endpoint". The address is the Core Gateway's, as
    // the console is configured with it (`BRUTOR_GATEWAY_PUBLIC_URL`).
    const llmProxyUrl = `${env.BRUTOR_GATEWAY_PUBLIC_URL}/v1/proxy/llm`;

    // Get authentication state AND client from useAuth hook
    const { isAuthenticated, isLoading: authLoading, gatewayClient } = useAuth();

    const [initialLoading, setInitialLoading] = useState(true);

    // Error handling with stable configuration
    const errorConfig = useMemo(() => ({
        // The severity and the operation, which the console names itself:
        // the sentence of the failure is in the page, not on the browser's console.
        reportError: (error: ErrorDetails) => {
            console.error(`Gateway error (${error.severity}) in ${error.context?.operation ?? 'an operation'}`);
        },
        autoDismissMs,
        maxErrors,
        defaultRetryConfig: {
            maxAttempts: 3,
            backoffMs: 1000,
            backoffMultiplier: 1.5
        }
    }), [autoDismissMs, maxErrors]);

    const {
        errors,
        hasErrors,
        hasCriticalErrors,
        addError,
        dismissError,
        clearErrors,
        getErrorsBySeverity
    } = useErrorHandler(errorConfig);

    // Only handle auth state initialization - no data fetching
    useEffect(() => {
        if (authLoading) {
            return;
        }

        setInitialLoading(false);
    }, [enableAuth, isAuthenticated, authLoading]);

    return {
        addError,
        clearErrors,
        dismissError,
        enableAuth,
        errors,
        gatewayClient,
        gatewayUrl,
        getErrorsBySeverity,
        hasCriticalErrors,
        hasErrors,
        initialLoading,
        isAuthenticated,
        llmProxyUrl,
    };
}

export type GatewayCore = ReturnType<typeof useGatewayCore>;
