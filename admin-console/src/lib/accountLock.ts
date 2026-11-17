/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the console says when the control plane answers 423: the account
 * cannot be used.
 *
 * The body (`AccountLockedResponse` in the generated types) says which case
 * it is:
 *
 *     {"detail": <text>, "reason": "timed_lock" | "lock_without_end" | "deactivated",
 *      "locked_until": <ISO 8601, UTC> | null, "lockout_minutes": 15}
 *
 * The text here follows `reason`, and a timed lock names its end in the
 * reader's own time: the control plane's `detail` writes it in UTC. A reason
 * this console does not know (or none) is answered with that `detail`.
 *
 * The API client throws the 423 with the body as the error's `details`; it
 * is read through `narrow.ts`, as every caught value is.
 */

import { errorMessage, getNumber, getRecord, getString } from './narrow';

const LOCKED = 423;

/**
 * The permission the control plane asks of whoever unlocks a tenant
 * administrator (`POST /v1/admin/users/tenant/{admin_id}/unlock`). The
 * console offers the action to a viewer who holds it and to nobody else.
 */
export const ADMIN_USER_UPDATE = 'admin:user:update';

/**
 * The moment `iso` names, written for a reader: in their locale and time
 * zone unless others are named. `undefined` when `iso` is no moment.
 */
export function lockEnd(iso: string, locale?: string, timeZone?: string): string | undefined {
    const moment = new Date(iso);
    if (Number.isNaN(moment.getTime())) return undefined;
    return moment.toLocaleString(locale, {
        dateStyle: 'medium',
        // To the second: a lock that ends at 10:15:40 has not ended at 10:15.
        timeStyle: 'medium',
        ...(timeZone ? { timeZone } : {}),
    });
}

/**
 * The text for a caught 423, or `undefined` for anything else (the caller
 * then says what it says of any refusal).
 */
export function accountLockMessage(error: unknown): string | undefined {
    if (getNumber(error, 'status') !== LOCKED) return undefined;
    const body = getRecord(error, 'details');
    const said = getString(body, 'detail') ?? errorMessage(error, 'This account cannot be used right now.');

    switch (getString(body, 'reason')) {
        case 'timed_lock': {
            const until = lockEnd(getString(body, 'locked_until') ?? '');
            return until ? `This account is locked until ${until}. An administrator can unlock it sooner.` : said;
        }
        case 'lock_without_end':
            return 'This account is locked. An administrator must unlock it.';
        case 'deactivated':
            return 'This account is deactivated. An administrator must reactivate it.';
        default:
            return said;
    }
}
