/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Shared formatting helpers for the A2A admin screens.
 *
 * Kept separate from the React components so they can be unit-tested
 * without rendering, and so multiple screens (card list, graph list,
 * graph detail, stats header) all produce identical output for the
 * same input.
 */

import type { AgentCardTrustTier } from '../../lib/a2aTypes';

export function formatDate(value?: string | null): string {
    if (!value) return '—';
    try {
        return new Date(value).toLocaleString();
    } catch {
        return '—';
    }
}

export function formatRelative(value?: string | null): string {
    if (!value) return '—';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return '—';
    const diffMs = Date.now() - d.getTime();
    const secs = Math.round(diffMs / 1000);
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.round(hrs / 24);
    if (days < 30) return `${days}d ago`;
    return d.toLocaleDateString();
}

export function formatDurationMs(value?: number | null): string {
    if (value === null || value === undefined) return '—';
    if (value < 1) return `${value.toFixed(2)} ms`;
    if (value < 1000) return `${Math.round(value)} ms`;
    const seconds = value / 1000;
    if (seconds < 60) return `${seconds.toFixed(2)} s`;
    const minutes = seconds / 60;
    return `${minutes.toFixed(1)} min`;
}

export function formatPercent(value?: number | null, digits = 1): string {
    if (value === null || value === undefined) return '—';
    return `${(value * 100).toFixed(digits)}%`;
}

export function formatInt(value?: number | null): string {
    if (value === null || value === undefined) return '—';
    return value.toLocaleString();
}

export interface TrustTierBadge {
    label: string;
    bg: string;
    text: string;
    /** Tooltip explaining the tier — surfaces the same semantics the Python
     * model docstring carries, so hovering tells operators what the value
     * actually means. */
    title: string;
}

export function trustTierBadge(tier: AgentCardTrustTier): TrustTierBadge {
    switch (tier) {
        case 'tenant_signed':
            return {
                label: 'Tenant signed',
                bg: 'bg-green-100 dark:bg-green-900/30',
                text: 'text-green-700 dark:text-green-300',
                title: 'Signed by this tenant\u2019s Ed25519 key.',
            };
        case 'partner_signed':
            return {
                label: 'Partner signed',
                bg: 'bg-blue-100 dark:bg-blue-900/30',
                text: 'text-blue-700 dark:text-blue-300',
                title: 'Signed by an external partner / trust anchor.',
            };
        case 'unsigned':
            return {
                label: 'Unsigned',
                bg: 'bg-yellow-100 dark:bg-yellow-900/30',
                text: 'text-yellow-800 dark:text-yellow-300',
                title: 'Card has no signature yet \u2014 default-deny under production policy.',
            };
        case 'revoked':
            return {
                label: 'Revoked',
                bg: 'bg-red-100 dark:bg-red-900/30',
                text: 'text-red-700 dark:text-red-300',
                title: 'Card has been revoked. Terminal \u2014 create a new card to replace it.',
            };
    }
}

/** Returns "direction" colour + label for a delegation step. */
export function directionBadge(direction?: string | null): {
    label: string;
    bg: string;
    text: string;
} {
    if (direction === 'inbound') {
        return {
            label: 'Inbound',
            bg: 'bg-sky-100 dark:bg-sky-900/30',
            text: 'text-sky-700 dark:text-sky-300',
        };
    }
    if (direction === 'outbound') {
        return {
            label: 'Outbound',
            bg: 'bg-indigo-100 dark:bg-indigo-900/30',
            text: 'text-indigo-700 dark:text-indigo-300',
        };
    }
    return {
        label: direction ?? '\u2014',
        bg: 'bg-surface-raised',
        text: 'text-secondary',
    };
}
