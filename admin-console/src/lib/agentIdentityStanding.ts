/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Whether an agent identity stands, as the gateway decides it when a
 * request arrives.
 *
 * The gateway refuses an identity that is disabled, whose status is not
 * `active`, or whose `expires_at` has passed. Nothing changes the stored
 * status when the expiry passes: the Control Plane goes on answering
 * `active` for an identity the gateway refuses. So the console reads the
 * expiry itself, against the browser's clock, and shows the server's value
 * beside what it concludes.
 */

export type AgentStandingState = 'active' | 'expired' | 'revoked';

export interface AgentStanding {
    state: AgentStandingState;
    /** The word of the status badge. */
    label: string;
    /** What an expired identity means, with the server's `expires_at`; null otherwise. */
    detail: string | null;
}

/** An ISO-8601 instant without an offset is the server's, which states UTC. */
const HAS_ZONE = /(?:[zZ]|[+-]\d{2}:?\d{2})$/;

/** The instant of a server timestamp, in milliseconds; NaN for one that cannot be read. */
export function serverInstant(value: string): number {
    return Date.parse(HAS_ZONE.test(value) ? value : `${value}Z`);
}

export function agentIdentityStanding(
    identity: { enabled: boolean; status: string; expires_at?: string | null },
    nowMs: number,
): AgentStanding {
    // What the stored row says comes first: an identity that was revoked is
    // revoked, whether or not it would also have expired.
    if (!identity.enabled || identity.status !== 'active') return { state: 'revoked', label: 'revoked', detail: null };
    const expiresAt = identity.expires_at;
    // The gateway counts the moment of the expiry itself as passed.
    if (expiresAt && serverInstant(expiresAt) <= nowMs) {
        return {
            state: 'expired',
            label: 'expired',
            detail: `This identity expired (expires_at ${expiresAt}). The gateway refuses every request made with it. Its stored status is still "active": nothing changes it when the expiry passes.`,
        };
    }
    return { state: 'active', label: 'active', detail: null };
}

/**
 * A stored expiry as the value of a date-and-time field
 * (`2027-01-01T09:30`, in the reader's own time zone); '' for none, or for
 * one that cannot be read.
 */
export function expiryFieldValue(expiresAt: string | null | undefined): string {
    if (!expiresAt) return '';
    const instant = serverInstant(expiresAt);
    if (Number.isNaN(instant)) return '';
    const at = new Date(instant);
    const pad = (n: number): string => String(n).padStart(2, '0');
    return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/**
 * What a date-and-time field holds, as the instant the control plane is
 * sent: ISO 8601 with its zone (UTC), so the server does not have to guess
 * one. `null` for an empty field (no expiry) or a value that is no moment.
 */
export function expiryFromField(value: string): string | null {
    if (!value) return null;
    const at = new Date(value);
    return Number.isNaN(at.getTime()) ? null : at.toISOString();
}
