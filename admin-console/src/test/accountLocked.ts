/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The control plane's 423 as the API client throws it: the body
 * (`AccountLockedResponse`) is the error's `details`, its `detail` text the
 * message. For the tests of every place that shows one.
 */

import { GatewayAPIError } from '../lib/gatewayApi/base';

/** When the timed lock of these tests ends (UTC). */
export const LOCKED_UNTIL = '2026-11-12T10:15:00+00:00';

/** The control plane's own words for each reason (`app/core/account_lock.py`). */
export const SERVER_TEXT = {
    timed_lock: `This account is locked after too many failed attempts. The lock lasts 15 minutes and ends at ${LOCKED_UNTIL}; an administrator can unlock it earlier.`,
    lock_without_end: 'This account is locked. An administrator must unlock it.',
    deactivated: 'This account is deactivated. An administrator must reactivate it.',
} as const;

export function accountLocked(
    reason: string | undefined,
    lockedUntil: string | null = reason === 'timed_lock' ? LOCKED_UNTIL : null,
    detail: string = SERVER_TEXT[reason as keyof typeof SERVER_TEXT] ?? 'The account cannot be used.',
): GatewayAPIError {
    const body: Record<string, unknown> = { detail, locked_until: lockedUntil, lockout_minutes: 15 };
    if (reason !== undefined) body.reason = reason;
    return new GatewayAPIError(detail, 423, undefined, body);
}
