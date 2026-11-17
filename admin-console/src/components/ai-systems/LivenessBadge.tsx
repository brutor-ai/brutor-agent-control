/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The liveness verdict as a pill — the endpoint's `state` through the one
 * mapping in `lib/livenessState.ts`, its `reason` as the tooltip. Every
 * surface that shows liveness renders this (or reads the same mapping).
 */

import { LIVENESS_TONE_CLASSES, livenessPresentation } from '../../lib/livenessState';

interface LivenessBadgeProps {
    state: string | null | undefined;
    reason?: string | null;
    testId?: string;
    /** Extra classes (padding / size) for the surface. */
    className?: string;
}

export function LivenessBadge({ state, reason, testId, className }: LivenessBadgeProps) {
    const p = livenessPresentation(state);
    const Icon = p.icon;
    return (
        <span
            data-testid={testId}
            data-state={p.state ?? 'unknown'}
            data-tone={p.tone}
            title={reason || p.meaning}
            className={`inline-flex items-center gap-1.5 rounded border font-medium ${LIVENESS_TONE_CLASSES[p.tone]} ${
                className ?? 'px-2 py-0.5 text-xs'
            }`}
        >
            <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            {p.label}
        </span>
    );
}

export default LivenessBadge;
