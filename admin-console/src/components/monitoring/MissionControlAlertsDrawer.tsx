/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Mission Control alerts drawer — the single alerts surface.
 *
 * Presentational: the dashboard owns the feeds and the mutations; this
 * component renders four sections and forwards intents. What can be
 * done with an alert is all here:
 *
 *  - usage (quota) alerts with acknowledge and the utilization bar;
 *  - system alerts with acknowledge ("take ownership so the
 *    auto-resolver leaves it alone"), resolve, the model deep-link,
 *    the counters deep-link, and the actions contributed to
 *    the `mission-control.alert-actions` slot (the one-click KB
 *    vector-index rebuild for kb_vector_dim_mismatch is one);
 *  - metric-threshold alerts (read-only hints);
 *  - open drift findings, linking to the system's drift panel.
 */

import React from 'react';
import { useSlot, type AlertActionSlotItem } from '../../app/slots';
import {
    AlertTriangle,
    Check,
    CheckCircle2,
    ExternalLink,
    Eye,
    X,
    XCircle,
} from 'lucide-react';
import type { UsageAlertInfo, SystemAlert } from '../../lib/groupTypes';
import type { DriftEvent } from '../../lib/aiSystemTypes';
import {
    deepLinkInputFromSystemAlert,
    shouldShowCountersLinkForAlert,
} from '../system/countersDeepLink';
import { CountersDeepLinkButton } from '../system/CountersDeepLinkButton';
import type { ReadFailure } from '../../hooks/useRead';
import type { AlertFeed } from '../../lib/alertFeeds';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface AlertsDrawerProps {
    usageAlerts: UsageAlertInfo[];
    systemAlerts: SystemAlert[];
    /** For the bell count; drift findings are read in the Inbox. */
    driftFeed: DriftEvent[];
    /** The feeds that could not be read. A feed named here shows what it held before, if anything. */
    feedFailures?: Partial<Record<AlertFeed, ReadFailure>>;
    onRetryFeeds?: () => void;
    /** Contributed alert actions in flight, as `<action id>:<alert id>`. Held by
     *  the caller so it outlives the drawer being closed and reopened. */
    busyActions: ReadonlySet<string>;
    onActionBusy: (key: string, busy: boolean) => void;
    /** A contributed action resolved the alert: drop it from the list. */
    onSystemAlertResolved: (alertId: string) => void;
    onClose: () => void;
    onAcknowledgeUsage: (alertId: string) => void;
    onAcknowledgeSystem: (alertId: string) => void;
    onResolveSystem: (alertId: string) => void;
    setActiveTab?: (tab: string) => void;
    /** Unresolved assurance findings; the drawer points at the Inbox for them. */
    inboxOpen?: number | null;
    onOpenInbox?: () => void;
}

const sevChip = (severity: string): string =>
    severity === 'critical' || severity === 'high'
        ? 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300'
        : 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300';

const SectionHeading: React.FC<{ label: string; count: number }> = ({ label, count }) => (
    <h3 className="text-xs font-semibold text-muted uppercase tracking-wide mt-5 mb-2">
        {label} ({count})
    </h3>
);

/** The drawer, given the alert actions to offer. */
const AlertsDrawerView: React.FC<AlertsDrawerProps & { alertActions: readonly AlertActionSlotItem[] }> = ({
    alertActions,
    usageAlerts,
    systemAlerts,
    feedFailures,
    onRetryFeeds,
    busyActions,
    onActionBusy,
    onSystemAlertResolved,
    onClose,
    onAcknowledgeUsage,
    onAcknowledgeSystem,
    onResolveSystem,
    setActiveTab,
    inboxOpen,
    onOpenInbox,
}) => (
    <div className="fixed inset-0 z-40" onClick={onClose}>
        <div className="absolute inset-0 bg-black/30" aria-hidden="true" />
        <aside
            className="absolute right-0 top-0 h-full w-full max-w-md bg-surface border-l border-default shadow-xl p-4 overflow-y-auto"
            data-testid="mc-alerts-drawer"
            onClick={(e) => e.stopPropagation()}
        >
            <div className="flex items-center justify-between">
                <h2 className="text-base font-semibold text-default">Alerts</h2>
                <button
                    type="button"
                    aria-label="Close alerts"
                    className="p-1 text-muted hover:text-default"
                    onClick={onClose}
                >
                    <X className="w-4 h-4" />
                </button>
            </div>

            <ReadFailureNotice compact className="mt-3" failure={feedFailures?.inbox} onRetry={onRetryFeeds} />
            {!feedFailures?.inbox && <ReadFailureNotice compact className="mt-3" failure={feedFailures?.drift} onRetry={onRetryFeeds} />}
            {/* Assurance findings live in the Inbox, with dispositions. One line
                here, never the list twice. */}
            {inboxOpen !== null && inboxOpen !== undefined && (
                <button
                    type="button"
                    onClick={onOpenInbox}
                    disabled={!onOpenInbox}
                    className="mt-3 w-full flex items-center justify-between px-3 py-2 rounded-lg border border-primary-200 bg-primary-50 text-sm text-primary-900 hover:bg-primary-100 disabled:opacity-60"
                    data-testid="mc-drawer-inbox-line"
                >
                    <span>
                        <span className="font-semibold">{inboxOpen}</span> open assurance finding{inboxOpen === 1 ? '' : 's'}
                    </span>
                    <span className="font-medium">Inbox &rarr;</span>
                </button>
            )}

            {/* ── Usage (quota) alerts ─────────────────────────────── */}
            <SectionHeading label="Usage alerts" count={usageAlerts.length} />
            <ReadFailureNotice compact className="mb-2" failure={feedFailures?.usage} onRetry={onRetryFeeds} />
            {usageAlerts.length === 0 && feedFailures?.usage ? null : usageAlerts.length === 0 ? (
                <p className="text-sm text-muted">No active usage alerts.</p>
            ) : (
                <ul className="space-y-2">
                    {usageAlerts.map((a) => (
                        <li
                            key={a.id}
                            data-testid={`mc-usage-alert-${a.id}`}
                            className={`p-2.5 rounded-lg border ${
                                a.alert_type === 'hard_stop'
                                    ? 'border-red-300 bg-red-50 dark:bg-red-900/20 dark:border-red-800'
                                    : 'border-yellow-300 bg-yellow-50 dark:bg-yellow-900/20 dark:border-yellow-800'
                            }`}
                        >
                            <div className="flex items-start justify-between gap-2">
                                <div className="min-w-0">
                                    <div className="flex items-center gap-2">
                                        {a.alert_type === 'hard_stop' ? (
                                            <XCircle className="w-3.5 h-3.5 text-red-500 shrink-0" />
                                        ) : (
                                            <AlertTriangle className="w-3.5 h-3.5 text-yellow-500 shrink-0" />
                                        )}
                                        <span
                                            className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${
                                                a.alert_type === 'hard_stop'
                                                    ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                                                    : 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300'
                                            }`}
                                        >
                                            {a.alert_type === 'hard_stop' ? 'Blocked' : 'Warning'}
                                        </span>
                                        <span className="text-sm text-default truncate">
                                            {a.group_name || a.group_id}
                                        </span>
                                    </div>
                                    <p className="text-xs text-muted mt-1">
                                        {a.metric.replace(/_/g, ' ')} — {a.utilization_pct.toFixed(0)}% used
                                    </p>
                                    <div className="w-full bg-surface-inset rounded-full h-1.5 mt-1.5">
                                        <div
                                            className={`h-1.5 rounded-full ${
                                                a.utilization_pct >= 100 ? 'bg-red-500' : 'bg-yellow-500'
                                            }`}
                                            style={{ width: `${Math.min(100, a.utilization_pct)}%` }}
                                        />
                                    </div>
                                </div>
                                <button
                                    type="button"
                                    aria-label="Acknowledge usage alert"
                                    title="Acknowledge alert"
                                    className="shrink-0 p-1.5 rounded-md text-muted hover:text-green-600"
                                    onClick={() => onAcknowledgeUsage(a.id)}
                                >
                                    <Check className="w-4 h-4" />
                                </button>
                            </div>
                        </li>
                    ))}
                </ul>
            )}

            {/* ── System alerts ────────────────────────────────────── */}
            <SectionHeading label="System alerts" count={systemAlerts.length} />
            <ReadFailureNotice compact className="mb-2" failure={feedFailures?.system} onRetry={onRetryFeeds} />
            {systemAlerts.length === 0 && feedFailures?.system ? null : systemAlerts.length === 0 ? (
                <p className="text-sm text-muted">No active system alerts.</p>
            ) : (
                <ul className="space-y-2">
                    {systemAlerts.map((a) => {
                        const isAcked = a.status === 'acknowledged';
                        const modelLink =
                            a.resource_type === 'llm_model' && a.resource_id && setActiveTab
                                ? `llm-models:${a.resource_id}`
                                : null;
                        return (
                            <li
                                key={a.id}
                                data-testid={`mc-system-alert-${a.id}`}
                                className="p-2.5 rounded-lg border border-default bg-surface-subtle"
                            >
                                <div className="flex items-center gap-2 flex-wrap">
                                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${sevChip(a.severity)}`}>
                                        {a.severity}
                                    </span>
                                    <span className="text-sm text-default">{a.title}</span>
                                    {isAcked && (
                                        <span className="text-[10px] text-muted">
                                            acked{a.acknowledged_by ? ` by ${a.acknowledged_by}` : ''}
                                        </span>
                                    )}
                                </div>
                                {a.message && <p className="text-xs text-muted mt-1">{a.message}</p>}
                                <div className="flex items-center gap-1 mt-1.5 flex-wrap">
                                    {modelLink && (
                                        <button
                                            type="button"
                                            className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium border border-default rounded-md bg-surface hover:bg-surface-hover text-default"
                                            title="Open this model in LLM Configuration"
                                            onClick={() => setActiveTab?.(modelLink)}
                                        >
                                            <ExternalLink className="w-3.5 h-3.5" />
                                            Open
                                        </button>
                                    )}
                                    {shouldShowCountersLinkForAlert(a) && (
                                        <CountersDeepLinkButton
                                            input={deepLinkInputFromSystemAlert(a)}
                                            testId={`mc-counters-link-${a.id}`}
                                            className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium border border-default rounded-md bg-surface hover:bg-surface-hover text-default"
                                        />
                                    )}
                                    {/* Actions contributed to the
                                        `mission-control.alert-actions` slot (the KB
                                        vector-index rebuild is). */}
                                    {alertActions
                                        .filter((action) => action.appliesTo(a))
                                        .map((action) => {
                                            const key = `${action.id}:${a.id}`;
                                            return (
                                                <action.Action
                                                    key={key}
                                                    alert={a}
                                                    busy={busyActions.has(key)}
                                                    setBusy={(busy) => onActionBusy(key, busy)}
                                                    onResolved={() => onSystemAlertResolved(a.id)}
                                                />
                                            );
                                        })}
                                    {!isAcked && (
                                        <button
                                            type="button"
                                            aria-label="Acknowledge system alert"
                                            title="Acknowledge — take ownership so the auto-resolver leaves this alone"
                                            className="p-1.5 rounded-md text-muted hover:text-default ml-auto"
                                            onClick={() => onAcknowledgeSystem(a.id)}
                                        >
                                            <Eye className="w-4 h-4" />
                                        </button>
                                    )}
                                    <button
                                        type="button"
                                        aria-label="Resolve system alert"
                                        title="Mark resolved"
                                        className={`p-1.5 rounded-md text-muted hover:text-default ${isAcked ? 'ml-auto' : ''}`}
                                        onClick={() => onResolveSystem(a.id)}
                                    >
                                        <CheckCircle2 className="w-4 h-4" />
                                    </button>
                                </div>
                            </li>
                        );
                    })}
                </ul>
            )}

        </aside>
    </div>
);

/** The drawer with the actions contributed to its slot. */
export const MissionControlAlertsDrawer: React.FC<AlertsDrawerProps> = (props) => {
    const alertActions = useSlot('mission-control.alert-actions');
    return <AlertsDrawerView {...props} alertActions={alertActions} />;
};

export default MissionControlAlertsDrawer;
