/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import toast from 'react-hot-toast';

import { errorKind, errorMessage } from './narrow';

/**
 * Handling for promises that nothing awaits.
 *
 * An `async` function whose promise is dropped — a fire-and-forget refresh, an
 * `onClick={async () => ...}` — reports its failure to nobody. The rejection
 * becomes an unhandled promise rejection: no console entry the user will see,
 * no toast, no audit row. In a console whose whole claim is "we record what
 * happened", a silently discarded failure is a product defect, not a lint nit.
 *
 * `void somePromise()` satisfies the linter and keeps the bug. These helpers
 * attach a real rejection handler instead, and the choice between them is the
 * choice of who needs to know:
 *
 *   - `runBackground` — the user did not ask for this (a poll, a prefetch, a
 *     cache warm). Record it; do not interrupt them with a toast for work they
 *     never initiated.
 *   - `runUserAction` — the user pressed something. If it failed they must be
 *     told, or the UI silently lies about what happened.
 */

/**
 * Some APIs return `void | Promise<void>` depending on how they were called —
 * react-router's `navigate` is the one this codebase hits. Accepting the union
 * keeps call sites from having to know which arm they got.
 */
export type MaybePromise = Promise<unknown> | void | undefined;

/** Write the kind of a rejection (`errorKind`) with its context. Never throws. */
export function reportAsyncError(error: unknown, context: string): void {
    console.error(`[async:${context}]`, errorKind(error));
}

/**
 * Fire-and-forget work the user did not initiate. Failure is logged, not
 * surfaced — a failed background refresh should not raise a toast over
 * whatever the user is actually doing.
 */
export function runBackground(promise: MaybePromise, context: string): void {
    if (!(promise instanceof Promise)) return;
    void promise.catch((error: unknown) => {
        reportAsyncError(error, context);
    });
}

/**
 * Fire-and-forget work the user *did* initiate. Failure is logged and shown,
 * because a button that silently does nothing is worse than one that reports
 * an error.
 */
export function runUserAction(promise: MaybePromise, context: string): void {
    if (!(promise instanceof Promise)) return;
    void promise.catch((error: unknown) => {
        reportAsyncError(error, context);
        toast.error(errorMessage(error, `${context} failed`));
    });
}

/**
 * Adapt an async handler for a prop typed `(...args) => void`.
 *
 * Prefer this at the *definition* site (inside `useCallback`) rather than
 * inline in JSX: wrapping inline mints a new function identity on every
 * render, which defeats memoisation on the child and, in a tree that already
 * has `set-state-in-effect` findings, can turn a re-render into a loop.
 */
export function asyncHandler<A extends unknown[]>(
    fn: (...args: A) => unknown,
    context: string,
    notify: 'user' | 'background' = 'user'
): (...args: A) => void {
    return (...args: A): void => {
        const run = notify === 'user' ? runUserAction : runBackground;
        // Guarded rather than typed as `Promise<unknown>`: a handler like
        // `() => isReady && save()` returns `false | Promise<void>`, and the
        // short-circuit arm is a legitimate no-op, not an error.
        const result: unknown = fn(...args);
        run(result instanceof Promise ? result : undefined, context);
    };
}
