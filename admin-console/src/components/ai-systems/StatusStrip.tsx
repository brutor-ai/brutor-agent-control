/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The one-row status strip pinned above the AI System's tabs.
 *
 * The rule: the numbers' qualifiers must not be tabbable away. A pinned band
 * holding the full liveness form and every drift card would keep them
 * "always visible" while pushing the tab strip below a 1080p fold, so the
 * strip pins less: a qualifier is a *verdict*, not a form. The verdicts live
 * here in one row;
 * the forms and detail cards live in their tabs, one click behind their
 * chip.
 *
 * Four chips, in the order an operator triages:
 *
 *   **Health** — the headline number.
 *   Worst-of state, score, what the score is over (the
 *   fixed health window, or the latest runs when it is failing — it does
 *   not follow the range pills beside it), and — because a materialised
 *   verdict is only worth its timestamp — "as of" with a refresh.
 *   **Liveness** — the only signal that can fire on absence.
 *   **Drift** — open findings count.
 *   **Coverage** — how much of the traffic the figures below actually cover,
 *   with provenance (signed vs client-asserted) in the tooltip rather than
 *   collapsed into one misleading number.
 */

import { useCallback, useEffect, useState } from 'react';
import {
    Activity,
    HeartPulse,
    RefreshCw,
    ShieldQuestion,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { ago } from '../../lib/relativeTime';
import { LIVENESS_TONE_CLASSES, livenessPresentation } from '../../lib/livenessState';
import { healthBasisTag, healthExplainer } from '../../lib/healthWindows';
import {
    HEALTH_STYLE,
    humanizeSeconds,
    LivenessStatus,
    RunStatsResponse,
    SystemHealth,
} from '../../lib/aiSystemTypes';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface StatusStripProps {
    aiSystemId: string;
    /** The stats the parent already loaded — coverage comes from here so the
     *  strip and the signals can never disagree about the same window. */
    stats?: RunStatsResponse | null;
    onOpenTab?: (tab: 'signals' | 'drift' | 'assurance') => void;
    /** Bump to make the strip re-read its verdicts — e.g. after a drift
     *  triage below it changes the open-findings count. The chips are
     *  verdicts, and a verdict that survives the action it reports on is
     *  worse than no verdict at all. */
    reloadKey?: number;
}

export function StatusStrip({
    aiSystemId,
    stats,
    onOpenTab,
    reloadKey = 0,
}: StatusStripProps) {
    const { fetchSystemHealth, fetchAISystemLiveness, fetchAISystemDrift } =
        useGateway();

    const [health, setHealth] = useState<SystemHealth | null>(null);
    const [liveness, setLiveness] = useState<LivenessStatus | null>(null);
    const [openDrift, setOpenDrift] = useState<number | null>(null);
    const [refreshing, setRefreshing] = useState(false);
    const [readFailure, reads] = useReadFailures<'status'>();

    const load = useCallback(
        async (refresh = false) => {
            try {
                if (refresh) setRefreshing(true);
                const [h, l, d] = await Promise.all([
                    fetchSystemHealth(aiSystemId, refresh),
                    fetchAISystemLiveness(aiSystemId),
                    fetchAISystemDrift(aiSystemId),
                ]);
                setHealth(h);
                setLiveness(l);
                setOpenDrift(d?.open_count ?? null);
                reads.loaded('status');
            } catch (err) {
                // The chips keep what they showed (or "—"): the strip says so.
                reads.failed('status', 'Failed to load the status of this system', err);
            } finally {
                setRefreshing(false);
            }
        },
        [aiSystemId, fetchSystemHealth, fetchAISystemLiveness, fetchAISystemDrift, reads]
    );

    useEffect(() => {
        void load();
    }, [load, reloadKey]);

    // THE health colours and labels (`HEALTH_STYLE`), shared with the Overview
    // so Degraded reads the same colour on both.
    const healthState = health?.health_state ?? null;
    const healthStyle = healthState ? HEALTH_STYLE[healthState] : undefined;
    const healthTone = healthStyle
        ? `${healthStyle.chip} border-transparent`
        : 'bg-surface-subtle text-secondary border-default';
    const healthTitle = health
        ? [...(health.reasons || []), '', 'How health is calculated:', ...healthExplainer(health)].join('\n')
        : undefined;

    // Liveness verdict: the endpoint's ONE `state` through
    // the shared mapping — never re-derived here.
    const live = livenessPresentation(liveness?.state);
    const LiveIcon = live.icon;
    const liveTitle = liveness
        ? [
              liveness.reason,
              liveness.quiet_for_seconds
                  ? `Quiet for ${humanizeSeconds(liveness.quiet_for_seconds)}.`
                  : null,
          ]
              .filter(Boolean)
              .join(' ')
        : undefined;

    const observed = stats?.observed_pct;
    const asserted = stats?.asserted_pct ?? 0;
    const external = stats?.external_pct ?? 0;
    const signed = stats?.chain_integrity_pct ?? 0;
    const coverageAmber = observed !== null && observed !== undefined && observed < 1;

    return (
        <div
            className="flex flex-wrap items-center gap-2"
            data-testid="status-strip"
            role="group"
            aria-label="System status"
        >
            <ReadFailureNotice compact className="basis-full" failure={readFailure('status')} onRetry={() => void load(true)} />
            {/* Health — the headline. */}
            <button
                type="button"
                onClick={() => onOpenTab?.('assurance')}
                data-testid="strip-health"
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm ${healthTone}`}
                title={healthTitle}
            >
                <HeartPulse className="w-4 h-4" />
                <span className="font-semibold">
                    {healthState ? (healthStyle?.label ?? healthState) : '—'}
                </span>
                {health?.health_score !== null && health?.health_score !== undefined && (
                    <span className="tabular-nums">
                        {(health.health_score * 100).toFixed(0)}%
                    </span>
                )}
                {health && (
                    <span className="text-xs opacity-80" data-testid="health-basis">
                        · {healthBasisTag(health)}
                    </span>
                )}
                <span className="text-xs opacity-70" data-testid="health-as-of">
                    as of {ago(health?.computed_at)}
                </span>
            </button>
            <button
                type="button"
                onClick={() => void load(true)}
                aria-label="Refresh health"
                data-testid="strip-refresh"
                className="p-1.5 border border-default rounded-lg hover:bg-surface-hover"
            >
                <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            </button>

            {/* Liveness verdict. */}
            <button
                type="button"
                onClick={() => onOpenTab?.('signals')}
                data-testid="strip-liveness"
                data-state={live.state ?? 'unknown'}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm ${LIVENESS_TONE_CLASSES[live.tone]}`}
                title={liveTitle}
            >
                <LiveIcon className="w-4 h-4" />
                {live.label}
            </button>

            {/* Open drift findings. */}
            <button
                type="button"
                onClick={() => onOpenTab?.('drift')}
                data-testid="strip-drift"
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm ${
                    openDrift
                        ? 'bg-amber-100 text-amber-900 border-amber-200'
                        : 'bg-surface-subtle text-secondary border-default'
                }`}
            >
                <Activity className="w-4 h-4" />
                {openDrift === null ? 'Drift —' : `Drift ${openDrift} open`}
            </button>

            {/* Coverage + provenance. */}
            <span
                data-testid="strip-coverage"
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm ${
                    coverageAmber
                        ? 'bg-amber-100 text-amber-900 border-amber-200'
                        : 'bg-surface-subtle text-secondary border-default'
                }`}
                title={
                    observed === null || observed === undefined
                        ? 'No runs in this window'
                        : `${(signed * 100).toFixed(0)}% gateway-signed · ${(
                              asserted * 100
                          ).toFixed(0)}% client-asserted · ${(external * 100).toFixed(
                              0,
                          )}% reported (OpenTelemetry) · ${(
                              (1 - observed) * 100
                          ).toFixed(0)}% partial/broken`
                }
            >
                <ShieldQuestion className="w-4 h-4" />
                {observed === null || observed === undefined
                    ? 'Coverage —'
                    : `Coverage ${(observed * 100).toFixed(0)}%`}
            </span>
        </div>
    );
}

export default StatusStrip;
