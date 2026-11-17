/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useErrorHandler / useErrorBoundary / errorUtils
 *
 * The interesting behaviour here is *classification*, not storage. Severity
 * and retryability decide whether a banner shouts, whether a retry button
 * appears, and whether the console schedules background work — so a wrong
 * classification is either an ignored outage or an infinite retry loop against
 * a 400 that will never succeed.
 *
 * Two specific behaviours are pinned below:
 *
 *   - `addError({ message, code, severity, context })` — the single-object
 *     call form used across the app — must land `context` and `severity` on
 *     the recorded error. Left stranded on the input object, banners
 *     filtering on `context.resource` would match nothing.
 *   - the auto-dismiss and retry timers must be torn down on unmount, or a
 *     tab switch leaves timers firing setState against a dead tree.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

import { useErrorHandler, useErrorBoundary, errorUtils, type ErrorDetails } from '../useErrorHandler';

/** Auto-dismiss off by default: most assertions are about what is *kept*. */
const NO_AUTODISMISS = { autoDismissMs: 0 };

describe('useErrorHandler — recording', () => {
    beforeEach(() => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('starts empty', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        expect(result.current.errors).toEqual([]);
        expect(result.current.hasErrors).toBe(false);
        expect(result.current.hasCriticalErrors).toBe(false);
    });

    it('records the message, code and context of a failure', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(
                { message: 'boom', status: 503 },
                { operation: 'listServers', resource: 'mcp_server' }
            );
        });

        const [err] = result.current.errors;
        expect(err.message).toBe('boom');
        expect(err.code).toBe('503');
        expect(err.context).toEqual({ operation: 'listServers', resource: 'mcp_server' });
        expect(err.timestamp).toBeInstanceOf(Date);
        expect(result.current.hasErrors).toBe(true);
    });

    it('returns a distinct id per error', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        let first = '';
        let second = '';
        act(() => {
            first = result.current.addError(new Error('a'));
            second = result.current.addError(new Error('b'));
        });

        expect(first).not.toBe(second);
        expect(result.current.errors.map((e) => e.id)).toEqual([second, first]);
    });

    it('lists newest first', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(new Error('older'));
            result.current.addError(new Error('newer'));
        });

        expect(result.current.errors.map((e) => e.message)).toEqual(['newer', 'older']);
    });

    it('caps the list at maxErrors, discarding the oldest', () => {
        const { result } = renderHook(() =>
            useErrorHandler({ ...NO_AUTODISMISS, maxErrors: 2 })
        );

        act(() => {
            result.current.addError(new Error('one'));
            result.current.addError(new Error('two'));
            result.current.addError(new Error('three'));
        });

        expect(result.current.errors.map((e) => e.message)).toEqual(['three', 'two']);
    });

    it('falls back to a readable message when there is no error object at all', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(null);
            result.current.addError(undefined);
        });

        expect(result.current.errors.map((e) => e.message)).toEqual([
            'Unknown error occurred',
            'Unknown error occurred',
        ]);
    });

    it('keeps a rejected string as the message rather than stringifying an object', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError('plain string rejection');
        });

        expect(result.current.errors[0].message).toBe('plain string rejection');
    });

    it('lifts context and severity off the single-object call form', () => {
        // Callers that pass one object rather than three positional args
        // must not lose either field.
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError({
                message: 'search failed',
                code: 'REGISTRY_SEARCH',
                severity: 'high',
                context: { resource: 'registry', operation: 'search' },
            });
        });

        const [err] = result.current.errors;
        expect(err.severity).toBe('high');
        expect(err.context).toEqual({ resource: 'registry', operation: 'search' });
    });

    it('prefers an explicit severity argument over the one on the error', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError({ message: 'x', severity: 'low' }, undefined, 'critical');
        });

        expect(result.current.errors[0].severity).toBe('critical');
    });

    it('reports each error to the reporter, and survives a reporter that throws', () => {
        const reportError = vi.fn((_error: ErrorDetails) => {
            throw new Error('sentry is down');
        });
        const { result } = renderHook(() =>
            useErrorHandler({ ...NO_AUTODISMISS, reportError })
        );

        act(() => {
            expect(() => result.current.addError(new Error('boom'))).not.toThrow();
        });

        expect(reportError).toHaveBeenCalledTimes(1);
        expect(reportError.mock.calls[0][0]).toMatchObject({ message: 'boom' });
        // The error is still recorded even though reporting blew up.
        expect(result.current.errors).toHaveLength(1);
    });
});

describe('useErrorHandler — classification', () => {
    it.each([
        ['a 500', { status: 500 }, 'critical'],
        ['a 502', { status: 502 }, 'critical'],
        ['a network failure', { code: 'NETWORK_ERROR' }, 'critical'],
        ['a 401', { status: 401 }, 'high'],
        ['a 403', { status: 403 }, 'high'],
        ['a 404', { status: 404 }, 'medium'],
        ['a 429', { status: 429 }, 'medium'],
        ['an untyped error', {}, 'medium'],
    ])('classifies %s as %s', (_label, error, expected) => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(error);
        });

        expect(result.current.errors[0].severity).toBe(expected);
    });

    it('treats discovery and test operations as low severity', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError({}, { operation: 'tool discovery' });
            result.current.addError({}, { operation: 'test connection' });
        });

        expect(result.current.errors.map((e) => e.severity)).toEqual(['low', 'low']);
    });

    it.each([
        ['a 500', { status: 500 }],
        ['a network failure', { code: 'NETWORK_ERROR' }],
        ['a timeout', { code: 'TIMEOUT' }],
        ['a rate limit', { status: 429 }],
    ])('marks %s retryable', (_label, error) => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(error);
        });

        expect(result.current.errors[0].retryable).toBe(true);
    });

    it.each([
        ['a 400', { status: 400 }],
        ['a 401', { status: 401 }],
        ['a 404', { status: 404 }],
        ['an untyped error', {}],
    ])('does not mark %s retryable', (_label, error) => {
        // Retrying a client error just burns quota against a request that
        // cannot succeed.
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(error);
        });

        expect(result.current.errors[0].retryable).toBe(false);
    });
});

describe('useErrorHandler — queries', () => {
    it('filters by severity and by retryability, and flags critical errors', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError({ message: 'server', status: 500 });
            result.current.addError({ message: 'forbidden', status: 403 });
            result.current.addError({ message: 'not found', status: 404 });
        });

        expect(result.current.getErrorsBySeverity('critical').map((e) => e.message)).toEqual([
            'server',
        ]);
        expect(result.current.getErrorsBySeverity('high').map((e) => e.message)).toEqual([
            'forbidden',
        ]);
        expect(result.current.getRetryableErrors().map((e) => e.message)).toEqual(['server']);
        expect(result.current.hasCriticalErrors).toBe(true);
    });

    it('does not flag critical when only recoverable errors are present', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError({ status: 404 });
        });

        expect(result.current.hasCriticalErrors).toBe(false);
    });
});

describe('useErrorHandler — dismissal', () => {
    it('dismisses one error and leaves the rest', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        let keep = '';
        let drop = '';
        act(() => {
            keep = result.current.addError(new Error('keep'));
            drop = result.current.addError(new Error('drop'));
        });

        act(() => result.current.dismissError(drop));

        expect(result.current.errors.map((e) => e.id)).toEqual([keep]);
    });

    it('ignores a dismissal for an unknown id', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(new Error('keep'));
        });

        act(() => result.current.dismissError('error_does_not_exist'));

        expect(result.current.errors).toHaveLength(1);
    });

    it('clears every error at once', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        act(() => {
            result.current.addError(new Error('a'));
            result.current.addError(new Error('b'));
        });

        act(() => result.current.clearErrors());

        expect(result.current.errors).toEqual([]);
        expect(result.current.hasErrors).toBe(false);
    });
});

describe('useErrorHandler — auto-dismiss', () => {
    beforeEach(() => {
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('removes an error after the auto-dismiss window', () => {
        const { result } = renderHook(() => useErrorHandler({ autoDismissMs: 5000 }));

        act(() => {
            result.current.addError(new Error('transient'));
        });
        expect(result.current.errors).toHaveLength(1);

        act(() => {
            vi.advanceTimersByTime(4999);
        });
        expect(result.current.errors).toHaveLength(1);

        act(() => {
            vi.advanceTimersByTime(1);
        });
        expect(result.current.errors).toHaveLength(0);
    });

    it('keeps errors indefinitely when auto-dismiss is disabled', () => {
        const { result } = renderHook(() => useErrorHandler({ autoDismissMs: 0 }));

        act(() => {
            result.current.addError(new Error('sticky'));
        });

        act(() => {
            vi.advanceTimersByTime(600_000);
        });

        expect(result.current.errors).toHaveLength(1);
    });

    it('does not fire timers after unmount', () => {
        const { result, unmount } = renderHook(() => useErrorHandler({ autoDismissMs: 5000 }));

        act(() => {
            result.current.addError(new Error('transient'));
        });

        unmount();

        // A surviving timer would call setState on an unmounted tree.
        expect(() =>
            act(() => {
                vi.advanceTimersByTime(10_000);
            })
        ).not.toThrow();
        expect(vi.getTimerCount()).toBe(0);
    });
});

describe('useErrorHandler — retry', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('reports no retry status for an error with no scheduled retry', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });

        expect(result.current.getRetryStatus(id)).toBeNull();
    });

    it('schedules the first retry with the configured backoff', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const operation = vi.fn().mockResolvedValue('ok');

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });
        act(() => {
            result.current.setupRetry(id, operation, {
                maxAttempts: 3,
                backoffMs: 1000,
                backoffMultiplier: 2,
            });
        });

        const status = result.current.getRetryStatus(id);
        expect(status).not.toBeNull();
        expect(status?.attempt).toBe(1);
        expect(status?.timeUntilRetryMs).toBeGreaterThan(0);
        expect(operation).not.toHaveBeenCalled();
    });

    it('runs the operation once the backoff elapses and dismisses on success', async () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const operation = vi.fn().mockResolvedValue('ok');

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });
        act(() => {
            result.current.setupRetry(id, operation, {
                maxAttempts: 3,
                backoffMs: 1000,
                backoffMultiplier: 2,
            });
        });

        await act(async () => {
            await vi.advanceTimersByTimeAsync(1000);
        });

        expect(operation).toHaveBeenCalledTimes(1);
        expect(result.current.errors.find((e) => e.id === id)).toBeUndefined();
    });

    it('stops retrying and marks the error non-retryable past maxAttempts', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const operation = vi.fn().mockRejectedValue(new Error('still down'));
        const config = { maxAttempts: 1, backoffMs: 1000, backoffMultiplier: 2 };

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });
        expect(result.current.errors[0].retryable).toBe(true);

        // First call books attempt 1; the second exceeds maxAttempts.
        act(() => result.current.setupRetry(id, operation, config));
        act(() => result.current.setupRetry(id, operation, config));

        expect(result.current.errors[0].retryable).toBe(false);
    });

    it('manual retryError resolves true and clears the error on success', async () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const operation = vi.fn().mockResolvedValue('ok');
        const config = { maxAttempts: 3, backoffMs: 60_000, backoffMultiplier: 2 };

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });
        act(() => result.current.setupRetry(id, operation, config));

        let outcome: boolean | undefined;
        await act(async () => {
            outcome = await result.current.retryError(id);
        });

        expect(outcome).toBe(true);
        expect(operation).toHaveBeenCalledTimes(1);
        expect(result.current.errors.find((e) => e.id === id)).toBeUndefined();
    });

    it('manual retryError resolves false and keeps the error on failure', async () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const operation = vi.fn().mockRejectedValue(new Error('still down'));
        const config = { maxAttempts: 3, backoffMs: 60_000, backoffMultiplier: 2 };

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });
        act(() => result.current.setupRetry(id, operation, config));

        let outcome: boolean | undefined;
        await act(async () => {
            outcome = await result.current.retryError(id);
        });

        expect(outcome).toBe(false);
        expect(result.current.errors.find((e) => e.id === id)).toBeDefined();
    });

    it('manual retryError resolves false for an error with nothing scheduled', async () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        let outcome: boolean | undefined;
        await act(async () => {
            outcome = await result.current.retryError('error_unknown');
        });

        expect(outcome).toBe(false);
    });

    it('dismissing an error cancels its pending retry', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const operation = vi.fn().mockResolvedValue('ok');

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });
        act(() =>
            result.current.setupRetry(id, operation, {
                maxAttempts: 3,
                backoffMs: 1000,
                backoffMultiplier: 2,
            })
        );

        act(() => result.current.dismissError(id));

        act(() => {
            vi.advanceTimersByTime(10_000);
        });

        expect(operation).not.toHaveBeenCalled();
        expect(result.current.getRetryStatus(id)).toBeNull();
    });

    it('clearErrors cancels every pending retry', () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const operation = vi.fn().mockResolvedValue('ok');

        let id = '';
        act(() => {
            id = result.current.addError({ status: 500 });
        });
        act(() =>
            result.current.setupRetry(id, operation, {
                maxAttempts: 3,
                backoffMs: 1000,
                backoffMultiplier: 2,
            })
        );

        act(() => result.current.clearErrors());
        act(() => {
            vi.advanceTimersByTime(10_000);
        });

        expect(operation).not.toHaveBeenCalled();
    });
});

describe('useErrorHandler — handleError wrapper', () => {
    beforeEach(() => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('passes a successful result straight through and records nothing', async () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        let value: string | null = null;
        await act(async () => {
            value = await result.current.handleError(() => Promise.resolve('payload'));
        });

        expect(value).toBe('payload');
        expect(result.current.errors).toEqual([]);
    });

    it('records the failure and rethrows so callers can still branch', async () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));
        const boom = new Error('save failed');

        await act(async () => {
            await expect(
                result.current.handleError(() => Promise.reject(boom), {
                    operation: 'saveModel',
                })
            ).rejects.toThrow('save failed');
        });

        await waitFor(() => expect(result.current.errors).toHaveLength(1));
        expect(result.current.errors[0].context).toEqual({ operation: 'saveModel' });
    });

    it('swallows the failure and returns null when suppressErrors is set', async () => {
        const { result } = renderHook(() => useErrorHandler(NO_AUTODISMISS));

        let value: unknown = 'unset';
        await act(async () => {
            value = await result.current.handleError(
                () => Promise.reject(new Error('background poll failed')),
                { operation: 'poll' },
                { suppressErrors: true }
            );
        });

        expect(value).toBeNull();
        // Suppressed means suppressed: no banner for a background poll.
        expect(result.current.errors).toEqual([]);
    });
});

describe('useErrorBoundary', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('starts with no error', () => {
        const { result } = renderHook(() => useErrorBoundary());

        expect(result.current.error).toBeNull();
        expect(result.current.hasError).toBe(false);
    });

    it('captures and then resets an error', () => {
        const { result } = renderHook(() => useErrorBoundary());

        act(() => result.current.captureError(new Error('render blew up')));
        expect(result.current.hasError).toBe(true);
        expect(result.current.error?.message).toBe('render blew up');

        act(() => result.current.resetError());
        expect(result.current.error).toBeNull();
        expect(result.current.hasError).toBe(false);
    });

    it('captures a global error event', () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const { result } = renderHook(() => useErrorBoundary());

        act(() => {
            window.dispatchEvent(
                new ErrorEvent('error', { error: new Error('window blew up'), message: 'window blew up' })
            );
        });

        expect(result.current.error?.message).toBe('window blew up');
    });

    it('wraps a non-Error rejection reason so consumers always get an Error', () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const { result } = renderHook(() => useErrorBoundary());

        act(() => {
            const event = new Event('unhandledrejection') as Event & { reason?: unknown };
            event.reason = 'a bare string';
            window.dispatchEvent(event);
        });

        expect(result.current.error).toBeInstanceOf(Error);
        expect(result.current.error?.message).toContain('a bare string');
    });

    it('stops listening after unmount', () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const { result, unmount } = renderHook(() => useErrorBoundary());

        unmount();

        expect(() => {
            window.dispatchEvent(
                new ErrorEvent('error', { error: new Error('late'), message: 'late' })
            );
        }).not.toThrow();
        expect(result.current.error).toBeNull();
    });
});

describe('errorUtils', () => {
    it('identifies network errors from code, message shape and status 0', () => {
        expect(errorUtils.isNetworkError({ code: 'NETWORK_ERROR' })).toBe(true);
        expect(errorUtils.isNetworkError({ message: 'Failed to fetch' })).toBe(true);
        expect(errorUtils.isNetworkError({ message: 'Network Error' })).toBe(true);
        expect(errorUtils.isNetworkError({ status: 0 })).toBe(true);
        expect(errorUtils.isNetworkError({ status: 500 })).toBe(false);
    });

    it('identifies auth, server and client errors by status band', () => {
        expect(errorUtils.isAuthError({ status: 401 })).toBe(true);
        expect(errorUtils.isAuthError({ status: 403 })).toBe(true);
        expect(errorUtils.isAuthError({ status: 404 })).toBe(false);

        expect(errorUtils.isServerError({ status: 500 })).toBe(true);
        expect(errorUtils.isServerError({ status: 599 })).toBe(true);
        expect(errorUtils.isServerError({ status: 404 })).toBe(false);

        expect(errorUtils.isClientError({ status: 400 })).toBe(true);
        expect(errorUtils.isClientError({ status: 499 })).toBe(true);
        expect(errorUtils.isClientError({ status: 500 })).toBe(false);
    });

    it.each([
        [{ code: 'NETWORK_ERROR' }, /internet connection/i],
        [{ status: 401 }, /log in again/i],
        [{ status: 500 }, /server error/i],
        [{ status: 429 }, /too many requests/i],
        [{ status: 404 }, /was not found/i],
    ])('turns %o into an operator-readable message', (error, pattern) => {
        expect(errorUtils.getUserFriendlyMessage(error)).toMatch(pattern);
    });

    it('falls back to the raw message, then to a generic one', () => {
        expect(errorUtils.getUserFriendlyMessage({ message: 'model retired' })).toBe(
            'model retired'
        );
        expect(errorUtils.getUserFriendlyMessage({})).toBe('An unexpected error occurred.');
    });

    it.each([
        [{ code: 'NETWORK_ERROR' }, /internet connection/i],
        [{ status: 403 }, /log in again/i],
        [{ status: 503 }, /few minutes/i],
        [{ status: 429 }, /wait a moment/i],
        [{ status: 404 }, /try the operation again/i],
    ])('recommends an action for %o', (error, pattern) => {
        expect(errorUtils.getRecommendedAction(error)).toMatch(pattern);
    });
});
