/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Liveness is ONE computation — and one presentation.
 *
 * The control plane's `liveness_state` (app/services/ai_system_liveness_state.py)
 * is the only place the verdict is computed; the liveness endpoint, the AI
 * Systems list and the health scorer all read it. Every UI surface renders
 * that `state` (with its `reason` as the tooltip / subtext) through THIS map.
 *
 * A verdict re-derived from `has_ever_run` / `is_silent` / `configured`
 * cannot express `idle` (an on-demand service nobody called in the window)
 * or `unstarted` — a quiet service would read "Alive" or "Not configured",
 * and a brand-new one "Never run" on one surface and something else on
 * another. A static guard
 * (`src/lib/__tests__/livenessSingleMapping.test.ts`) fails if a component
 * reads those fields to decide a label.
 *
 * Only `alive` earns green; only `silent` is a finding (red). `idle`,
 * `unstarted` and `not_configured` are neutral: none is a fault, and none
 * may be rounded up to "alive".
 */

import type { LucideIcon } from 'lucide-react';
import { AlertTriangle, CheckCircle2, Clock, Moon, SlidersHorizontal } from 'lucide-react';

import type { AISystemLivenessState } from './aiSystemTypes';

export type LivenessStateName = AISystemLivenessState['state'];

export type LivenessTone = 'ok' | 'alert' | 'neutral';

export interface LivenessPresentation {
    state: LivenessStateName | null;
    label: string;
    tone: LivenessTone;
    icon: LucideIcon;
    /** One line on what the state means, for help text. */
    meaning: string;
}

/** Background + text + border per tone — the whole palette of the verdict. */
export const LIVENESS_TONE_CLASSES: Record<LivenessTone, string> = {
    ok: 'bg-emerald-100 text-emerald-800 border-emerald-200',
    alert: 'bg-red-100 text-red-800 border-red-200',
    neutral: 'bg-slate-100 text-slate-700 border-slate-200',
};

export const LIVENESS_STATE_PRESENTATION: Record<LivenessStateName, LivenessPresentation> = {
    alive: {
        state: 'alive',
        label: 'Alive',
        tone: 'ok',
        icon: CheckCircle2,
        meaning: 'Active within its expected window, or answering its inbound calls (one unanswered among the latest 3 reads degraded).',
    },
    silent: {
        state: 'silent',
        label: 'Silent',
        tone: 'alert',
        icon: AlertTriangle,
        meaning: 'It was active before and has stopped, or its latest 3 inbound calls all went unanswered.',
    },
    idle: {
        state: 'idle',
        label: 'Idle',
        tone: 'neutral',
        icon: Moon,
        meaning: 'No calls in the window — on demand, so not a fault.',
    },
    unstarted: {
        state: 'unstarted',
        label: 'Unstarted',
        tone: 'neutral',
        icon: Clock,
        meaning: 'Registered but has never run — not a finding.',
    },
    not_configured: {
        state: 'not_configured',
        label: 'Not configured',
        tone: 'neutral',
        icon: SlidersHorizontal,
        meaning: 'No enabled liveness expectation, so nothing is watching it — never scored green.',
    },
};

const UNKNOWN: LivenessPresentation = {
    state: null,
    label: '—',
    tone: 'neutral',
    icon: Clock,
    meaning: 'Liveness not loaded.',
};

export const LIVENESS_STATES = Object.keys(LIVENESS_STATE_PRESENTATION) as LivenessStateName[];

/** The presentation of the endpoint's `state`. Missing or an unknown value
 *  (a newer server) renders neutral "—" — never green. */
export function livenessPresentation(state: string | null | undefined): LivenessPresentation {
    if (state && Object.prototype.hasOwnProperty.call(LIVENESS_STATE_PRESENTATION, state)) {
        return LIVENESS_STATE_PRESENTATION[state as LivenessStateName];
    }
    return UNKNOWN;
}
