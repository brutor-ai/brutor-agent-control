/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { asyncHandler, reportAsyncError, runBackground, runUserAction } from '../asyncSafe';

const toastError = vi.hoisted(() => vi.fn());
vi.mock('react-hot-toast', () => ({
    default: { error: toastError },
}));

describe('asyncSafe', () => {
    beforeEach(() => {
        vi.spyOn(console, 'error').mockImplementation(() => undefined);
        toastError.mockClear();
    });
    afterEach(() => {
        vi.restoreAllMocks();
    });

    const flush = () => new Promise((r) => setTimeout(r, 0));

    it('reportAsyncError writes the context tag and the kind of the failure, never the failure itself', () => {
        reportAsyncError(new Error('the answer of the server'), 'load models');
        expect(console.error).toHaveBeenCalledTimes(1);
        expect(console.error).toHaveBeenCalledWith('[async:load models]', 'Error');
    });

    it('reportAsyncError writes the numeric status of a refused request beside its class', () => {
        const refused = Object.assign(new Error('token sk-1 refused'), { name: 'GatewayAPIError', status: 403, details: { detail: 'token sk-1 refused' } });
        reportAsyncError(refused, 'save');
        expect(console.error).toHaveBeenCalledWith('[async:save]', 'GatewayAPIError 403');
    });

    describe('runBackground', () => {
        it('logs a rejection without toasting the user', async () => {
            runBackground(Promise.reject(new Error('poll failed')), 'poll');
            await flush();
            expect(console.error).toHaveBeenCalled();
            expect(toastError).not.toHaveBeenCalled();
        });

        it('stays quiet when the promise resolves', async () => {
            runBackground(Promise.resolve('ok'), 'poll');
            await flush();
            expect(console.error).not.toHaveBeenCalled();
        });

        it('swallows the rejection so it never becomes an unhandled rejection', async () => {
            const onUnhandled = vi.fn();
            process.on('unhandledRejection', onUnhandled);
            runBackground(Promise.reject(new Error('x')), 'poll');
            await flush();
            process.off('unhandledRejection', onUnhandled);
            expect(onUnhandled).not.toHaveBeenCalled();
        });
    });

    describe('runUserAction', () => {
        it('logs AND surfaces the failure', async () => {
            runUserAction(Promise.reject(new Error('save failed')), 'save');
            await flush();
            expect(console.error).toHaveBeenCalled();
            expect(toastError).toHaveBeenCalledWith('save failed');
        });

        it('falls back to a context-derived message when the error carries none', async () => {
            // Rejecting with a non-Error is the point of this case: the helper
            // must still produce a usable message rather than "undefined".
            // eslint-disable-next-line @typescript-eslint/prefer-promise-reject-errors
            runUserAction(Promise.reject({}), 'save');
            await flush();
            expect(toastError).toHaveBeenCalledWith('save failed');
        });
    });


    describe('non-promise inputs', () => {
        it('ignores a void return without throwing', () => {
            // react-router's `navigate` is typed `void | Promise<void>`; the
            // void arm must be a no-op rather than a TypeError on `.catch`.
            expect(() => { runBackground(undefined, 'navigate'); }).not.toThrow();
            expect(() => { runUserAction(undefined, 'navigate'); }).not.toThrow();
            expect(console.error).not.toHaveBeenCalled();
            expect(toastError).not.toHaveBeenCalled();
        });
    });

    describe('asyncHandler', () => {
        it('returns a void-returning function that forwards its arguments', async () => {
            const fn = vi.fn().mockResolvedValue(undefined);
            const handler = asyncHandler(fn, 'submit');
            const result = handler('a', 2);
            expect(result).toBeUndefined();
            expect(fn).toHaveBeenCalledWith('a', 2);
            await flush();
        });

        it('reports a rejection to the user by default', async () => {
            const handler = asyncHandler(() => Promise.reject(new Error('boom')), 'submit');
            handler();
            await flush();
            expect(toastError).toHaveBeenCalledWith('boom');
        });

        it('stays silent when told the work is background', async () => {
            const handler = asyncHandler(
                () => Promise.reject(new Error('boom')),
                'refresh',
                'background'
            );
            handler();
            await flush();
            expect(console.error).toHaveBeenCalled();
            expect(toastError).not.toHaveBeenCalled();
        });
    });
});
