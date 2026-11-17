/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the console says for the control plane's 423 (`AccountLockedResponse`):
 * the text follows `reason`, and a timed lock names its end in the reader's
 * own time, not the UTC text of the body.
 */

import { describe, expect, it } from 'vitest';

import { accountLockMessage, lockEnd } from '../accountLock';
import { GatewayAPIError } from '../gatewayApi/base';
import { accountLocked, LOCKED_UNTIL, SERVER_TEXT } from '../../test/accountLocked';

describe('lockEnd', () => {
    it('writes the moment in the time zone asked for, not in UTC', () => {
        expect(lockEnd(LOCKED_UNTIL, 'en-GB', 'Europe/Stockholm')).toBe('12 Nov 2026, 11:15:00');
        expect(lockEnd(LOCKED_UNTIL, 'en-GB', 'America/New_York')).toBe('12 Nov 2026, 05:15:00');
        // The same moment however the offset is written.
        expect(lockEnd('2026-11-12T10:15:00Z', 'en-GB', 'Europe/Stockholm')).toBe('12 Nov 2026, 11:15:00');
    });

    it("uses the reader's own locale and time zone when none is named", () => {
        expect(lockEnd(LOCKED_UNTIL)).toBe(
            new Date(LOCKED_UNTIL).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' }),
        );
    });

    it('has nothing to say of a text that is no moment', () => {
        expect(lockEnd('soon')).toBeUndefined();
        expect(lockEnd('')).toBeUndefined();
    });
});

describe('accountLockMessage', () => {
    it('a timed lock: until when, in local time, and that an administrator can end it sooner', () => {
        const text = accountLockMessage(accountLocked('timed_lock'));
        expect(text).toBe(`This account is locked until ${lockEnd(LOCKED_UNTIL)}. An administrator can unlock it sooner.`);
        expect(text).not.toContain(LOCKED_UNTIL);
    });

    it('a lock without an end: an administrator must unlock it', () => {
        expect(accountLockMessage(accountLocked('lock_without_end', null, 'Locked: ask someone.'))).toBe(
            'This account is locked. An administrator must unlock it.',
        );
    });

    it('a deactivated account: an administrator must reactivate it', () => {
        expect(accountLockMessage(accountLocked('deactivated', null, 'Switched off.'))).toBe(
            'This account is deactivated. An administrator must reactivate it.',
        );
    });

    it("an unknown or missing reason: the control plane's own text", () => {
        expect(accountLockMessage(accountLocked('frozen', null, 'Frozen by the operator.'))).toBe('Frozen by the operator.');
        expect(accountLockMessage(accountLocked(undefined, null, 'Locked, somehow.'))).toBe('Locked, somehow.');
        // A 423 with no body at all still says what the client made of it.
        expect(accountLockMessage(new GatewayAPIError('HTTP 423: Locked', 423))).toBe('HTTP 423: Locked');
    });

    it("a timed lock whose end is missing or unreadable: the control plane's own text", () => {
        expect(accountLockMessage(accountLocked('timed_lock', null))).toBe(SERVER_TEXT.timed_lock);
        expect(accountLockMessage(accountLocked('timed_lock', 'soon'))).toBe(SERVER_TEXT.timed_lock);
    });

    it('reads the reason, not the words: the text does not decide', () => {
        expect(accountLockMessage(accountLocked('deactivated', null, SERVER_TEXT.lock_without_end))).toBe(
            'This account is deactivated. An administrator must reactivate it.',
        );
    });

    it('is nothing for an error that is no 423', () => {
        expect(accountLockMessage(new GatewayAPIError('Invalid credentials', 401))).toBeUndefined();
        expect(accountLockMessage(new Error('locked'))).toBeUndefined();
        expect(accountLockMessage(undefined)).toBeUndefined();
        // The body of a 423 on another status is not a lock.
        expect(
            accountLockMessage(new GatewayAPIError('x', 403, undefined, { reason: 'deactivated', detail: 'x' })),
        ).toBeUndefined();
    });
});
