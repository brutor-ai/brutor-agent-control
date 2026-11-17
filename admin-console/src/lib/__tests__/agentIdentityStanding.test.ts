/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Whether an agent identity stands, as the gateway decides it at request
 * time. The gateway refuses an identity that is disabled, not active, or
 * past its `expires_at`. Nothing changes the stored status when the expiry
 * passes, so the console reads the expiry itself, on the browser's clock.
 */

import { describe, expect, it } from 'vitest';

import { agentIdentityStanding } from '../agentIdentityStanding';

const NOW = Date.parse('2026-11-13T12:00:00Z');
const identity = (over: Record<string, unknown> = {}) => ({ enabled: true, status: 'active', ...over });

describe('agentIdentityStanding', () => {
    it('is active with no expiry, or one that is still ahead', () => {
        expect(agentIdentityStanding(identity(), NOW)).toEqual({ state: 'active', label: 'active', detail: null });
        expect(agentIdentityStanding(identity({ expires_at: null }), NOW).state).toBe('active');
        expect(agentIdentityStanding(identity({ expires_at: '2026-11-13T12:00:01+00:00' }), NOW).state).toBe('active');
    });

    it('is expired once the expiry has passed, although its stored status is still active', () => {
        expect(agentIdentityStanding(identity({ expires_at: '2026-11-12T08:30:00+00:00' }), NOW)).toEqual({
            state: 'expired',
            label: 'expired',
            detail:
                'This identity expired (expires_at 2026-11-12T08:30:00+00:00). The gateway refuses every request made with it. Its stored status is still "active": nothing changes it when the expiry passes.',
        });
    });

    it('is expired at the very moment of the expiry, as the gateway counts it', () => {
        expect(agentIdentityStanding(identity({ expires_at: '2026-11-13T12:00:00+00:00' }), NOW).state).toBe('expired');
    });

    it('reads an expiry with another offset, and one with none as UTC', () => {
        // 13:30 at +02:00 is 11:30 UTC: passed.
        expect(agentIdentityStanding(identity({ expires_at: '2026-11-13T13:30:00+02:00' }), NOW).state).toBe('expired');
        expect(agentIdentityStanding(identity({ expires_at: '2026-11-13T11:59:59' }), NOW).state).toBe('expired');
        expect(agentIdentityStanding(identity({ expires_at: '2026-11-13T12:00:01' }), NOW).state).toBe('active');
    });

    it('is revoked when it is disabled or not active, whatever its expiry', () => {
        expect(agentIdentityStanding(identity({ enabled: false }), NOW)).toEqual({ state: 'revoked', label: 'revoked', detail: null });
        expect(agentIdentityStanding(identity({ status: 'suspended', expires_at: '2020-01-01T00:00:00Z' }), NOW).state).toBe('revoked');
    });

    it('does not call an identity expired on an expiry it cannot read', () => {
        expect(agentIdentityStanding(identity({ expires_at: 'soon' }), NOW).state).toBe('active');
    });
});
