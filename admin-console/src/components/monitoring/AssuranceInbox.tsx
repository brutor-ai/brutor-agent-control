/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Assurance Inbox — Mission Control's triage queue of
 * findings awaiting human review. One list, deduped server-side; every
 * resolution records a disposition + note that become Assurance Report
 * evidence, so the resolve form treats them as required.
 *
 * Severity `info` (warn-mode guardrail hits, approval timeouts) is
 * default-filtered OUT so pilots build history without drowning the
 * queue; the filter chip shows what's hidden.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle,
  ChevronRight,
  Clock,
  Inbox,
  RefreshCw,
  RotateCcw,
  Shield,
  User,
  X,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import {
  AssuranceInboxCounts,
  AssuranceInboxItem,
  AssuranceInboxItemDetail,
  INBOX_CATEGORIES,
  INBOX_DISPOSITIONS,
  agentInboxLinks,
  inboxCategoryLabel,
  InboxDisposition,
  InboxSeverity,
  InboxState,
} from '../../lib/assuranceInboxTypes';
import { EVIDENCE_INBOX_KINDS, evidenceInboxLinks, isEvidenceInboxKind } from './evidenceInboxKinds';

/** Category chip; the evidence kinds carry their icon. */
function CategoryChip({ category }: { category: string }) {
  const Icon = isEvidenceInboxKind(category) ? EVIDENCE_INBOX_KINDS[category].icon : null;
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-surface-raised text-secondary shrink-0"
      title={isEvidenceInboxKind(category) ? EVIDENCE_INBOX_KINDS[category].meaning : undefined}
      data-testid={`inbox-category-${category}`}
    >
      {Icon && <Icon className="w-3 h-3" />}
      {inboxCategoryLabel(category)}
    </span>
  );
}


const DISPOSITION_LABELS: Record<InboxDisposition, string> = {
  true_positive: 'True positive — the finding was real',
  false_positive: 'False positive — detector was wrong',
  expected: 'Expected — known/intentional behavior',
  fixed: 'Fixed — underlying cause addressed',
};

const severityBadge = (severity: InboxSeverity) =>
  severity === 'critical'
    ? 'bg-red-100 text-red-800'
    : severity === 'warning'
      ? 'bg-yellow-100 text-yellow-800'
      : 'bg-surface-raised text-secondary';

const stateBadge = (state: InboxState) =>
  state === 'open'
    ? 'bg-blue-100 text-blue-800'
    : state === 'acknowledged'
      ? 'bg-purple-100 text-purple-800'
      : 'bg-green-100 text-green-800';

const fmtTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '—');

interface AssuranceInboxProps {
  /** Pre-filter to one AI System (the RG card deep-link). */
  aiSystemId?: string;
  /** Navigate to a native evidence view (run detail, drift, …). */
  setActiveTab?: (tab: string) => void;
}

export const AssuranceInbox: React.FC<AssuranceInboxProps> = ({
  aiSystemId,
  setActiveTab,
}) => {
  const { gatewayClient } = useGateway();
  const [items, setItems] = useState<AssuranceInboxItem[]>([]);
  const [counts, setCounts] = useState<AssuranceInboxCounts | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [stateFilter, setStateFilter] = useState<InboxState | 'unresolved'>(
    'unresolved'
  );
  const [severityFilter, setSeverityFilter] = useState<InboxSeverity | 'actionable'>(
    'actionable'
  );
  const [categoryFilter, setCategoryFilter] = useState<string>('');

  const [selected, setSelected] = useState<AssuranceInboxItemDetail | null>(null);
  const [resolving, setResolving] = useState(false);
  const [disposition, setDisposition] = useState<InboxDisposition>('true_positive');
  const [note, setNote] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!gatewayClient) return;
    setLoading(true);
    setError(null);
    try {
      const params: Parameters<typeof gatewayClient.listAssuranceInbox>[0] = {
        limit: 100,
      };
      if (aiSystemId) params.ai_system_id = aiSystemId;
      if (stateFilter !== 'unresolved') params.state = stateFilter;
      if (severityFilter !== 'actionable') params.severity = severityFilter;
      if (categoryFilter) params.category = categoryFilter;
      const [list, badge] = await Promise.all([
        gatewayClient.listAssuranceInbox(params),
        gatewayClient.getAssuranceInboxCounts(),
      ]);
      let rows = list.items;
      // Client-side projections of the two composite filters.
      if (stateFilter === 'unresolved') {
        rows = rows.filter((i) => i.state !== 'resolved');
      }
      if (severityFilter === 'actionable') {
        rows = rows.filter((i) => i.severity !== 'info');
      }
      setItems(rows);
      setTotal(list.total);
      setCounts(badge);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load the assurance inbox');
    } finally {
      setLoading(false);
    }
  }, [gatewayClient, aiSystemId, stateFilter, severityFilter, categoryFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const openDetail = useCallback(async (item: AssuranceInboxItem) => {
    if (!gatewayClient) return;
    setActionError(null);
    setResolving(false);
    setNote('');
    try {
      setSelected(await gatewayClient.getAssuranceInboxItem(item.id));
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Failed to load item');
    }
  }, [gatewayClient]);

  const refreshSelected = useCallback(
    async (itemId: string) => {
      if (!gatewayClient) return;
      setSelected(await gatewayClient.getAssuranceInboxItem(itemId));
      await load();
    },
    [gatewayClient, load]
  );

  const act = useCallback(
    async (fn: () => Promise<unknown>, itemId: string) => {
      setBusy(true);
      setActionError(null);
      try {
        await fn();
        await refreshSelected(itemId);
      } catch (e) {
        setActionError(e instanceof Error ? e.message : 'Action failed');
      } finally {
        setBusy(false);
      }
    },
    [refreshSelected]
  );

  const evidenceLinks = useMemo(() => {
    if (!selected) return [];
    const ev = selected.evidence || {};
    const links: Array<{ label: string; tab: string }> = [];
    if (ev.drift_event_id && selected.ai_system_id) {
      links.push({
        label: 'View drift finding',
        tab: `ai-systems:${selected.ai_system_id}:signals`,
      });
    }
    if (selected.category === 'health_transition' && selected.ai_system_id) {
      links.push({
        label: 'View system health',
        tab: `ai-systems:${selected.ai_system_id}:signals`,
      });
    }
    // A composition finding is worked where it is drawn.
    if (selected.category === 'composition' && selected.ai_system_id) {
      links.push({
        label: 'View composition',
        tab: `ai-systems:${selected.ai_system_id}:composition`,
      });
    }
    if (ev.run_id) {
      links.push({ label: 'View runs', tab: 'ai-systems' });
    }
    // Agent findings link to the identity's Implementation.
    links.push(...agentInboxLinks(selected.category, ev, selected.ai_system_id));
    // Evidence findings link to where they are worked.
    links.push(...evidenceInboxLinks(selected.category, ev));
    if (ev.proxy_log_id) {
      // The id must match a case in App.tsx's tab switch. One that does
      // not ('proxy-logs', say) renders a clickable link that does nothing —
      // an unhandled tab id is a silent no-op, not an error.
      links.push({ label: 'View proxy logs', tab: 'monitoring-proxy-logs' });
    }
    return links;
  }, [selected]);

  return (
    <div data-testid="assurance-inbox" className="bg-surface rounded-lg border">
      {/* Header */}
      <div className="px-4 py-3 border-b flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <Inbox className="w-5 h-5 text-primary-600" />
          <h3 className="font-semibold text-default">Assurance Inbox</h3>
          {counts && (
            <span
              data-testid="inbox-open-critical"
              className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                counts.open_critical > 0
                  ? 'bg-red-100 text-red-800'
                  : 'bg-surface-raised text-secondary'
              }`}
              title="Open critical items"
            >
              {counts.open_critical} critical
            </span>
          )}
          {counts && (
            <span className="text-xs text-muted">
              {counts.open} open · {counts.acknowledged} acknowledged
            </span>
          )}
        </div>
        <button
          onClick={() => void load()}
          className="p-1.5 text-muted hover:text-default rounded"
          title="Refresh"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Filters */}
      <div className="px-4 py-2 border-b flex items-center gap-2 flex-wrap text-sm">
        <select
          aria-label="State filter"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value as InboxState | 'unresolved')}
          className="px-2 py-1 border border-strong rounded-md text-sm"
        >
          <option value="unresolved">Open + acknowledged</option>
          <option value="open">Open</option>
          <option value="acknowledged">Acknowledged</option>
          <option value="resolved">Resolved</option>
        </select>
        <select
          aria-label="Severity filter"
          value={severityFilter}
          onChange={(e) =>
            setSeverityFilter(e.target.value as InboxSeverity | 'actionable')
          }
          className="px-2 py-1 border border-strong rounded-md text-sm"
        >
          <option value="actionable">Warning + critical</option>
          <option value="critical">Critical</option>
          <option value="warning">Warning</option>
          <option value="info">Info (incl. warn-mode hits)</option>
        </select>
        <select
          aria-label="Category filter"
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="px-2 py-1 border border-strong rounded-md text-sm"
        >
          <option value="">All categories</option>
          {INBOX_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {inboxCategoryLabel(c)}
            </option>
          ))}
        </select>
        <span className="text-xs text-muted ml-auto">
          {items.length} shown{total > items.length ? ` of ${total}` : ''}
        </span>
      </div>

      {/* Body */}
      {error ? (
        <div className="p-4 text-sm text-red-700 bg-red-50">{error}</div>
      ) : loading && items.length === 0 ? (
        <div className="p-8 text-center text-muted text-sm">Loading…</div>
      ) : items.length === 0 ? (
        <div className="p-8 text-center" data-testid="inbox-empty">
          <CheckCircle className="w-8 h-8 text-green-500 mx-auto mb-2" />
          <p className="text-sm text-secondary">
            Nothing awaiting review with the current filters.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-default max-h-[28rem] overflow-y-auto">
          {items.map((item) => (
            <button
              key={item.id}
              onClick={() => void openDetail(item)}
              className="w-full text-left px-4 py-2.5 hover:bg-surface-alt flex items-center gap-3"
              data-testid="inbox-row"
            >
              <span
                className={`px-2 py-0.5 rounded text-xs font-medium shrink-0 ${severityBadge(item.severity)}`}
              >
                {item.severity}
              </span>
              <CategoryChip category={item.category} />
              <span className="text-sm text-default truncate flex-1 min-h-0 min-w-0">{item.title}</span>
              {item.occurrence_count > 1 && (
                <span className="text-xs text-muted shrink-0" title="Occurrences">
                  ×{item.occurrence_count}
                </span>
              )}
              <span
                className={`px-2 py-0.5 rounded text-xs shrink-0 ${stateBadge(item.state)}`}
              >
                {item.state}
              </span>
              <span className="text-xs text-muted shrink-0 hidden md:inline">
                {fmtTime(item.last_seen_at)}
              </span>
              <ChevronRight className="w-4 h-4 text-muted shrink-0" />
            </button>
          ))}
        </div>
      )}

      {/* Detail drawer */}
      {selected && (
        <div className="modal-overlay">
          <div className="modal-content max-w-2xl" data-testid="inbox-detail">
            <div className="px-6 py-4 border-b flex items-start justify-between">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-medium ${severityBadge(selected.severity)}`}
                  >
                    {selected.severity}
                  </span>
                  <CategoryChip category={selected.category} />
                  <span
                    className={`px-2 py-0.5 rounded text-xs ${stateBadge(selected.state)}`}
                  >
                    {selected.state}
                  </span>
                </div>
                <h3 className="font-semibold text-default">{selected.title}</h3>
                <p className="text-xs text-muted mt-1">
                  First seen {fmtTime(selected.first_seen_at)} · last{' '}
                  {fmtTime(selected.last_seen_at)} · ×{selected.occurrence_count}
                  {selected.ai_system_id && <> · system {selected.ai_system_id}</>}
                </p>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="p-1 text-muted hover:text-default rounded"
                aria-label="Close"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="px-6 py-4 space-y-4">
              {actionError && (
                <div className="p-2 text-sm text-red-700 bg-red-50 rounded">
                  {actionError}
                </div>
              )}

              {/* Evidence */}
              <div>
                <h4 className="text-sm font-medium text-default mb-1 flex items-center gap-1">
                  <Shield className="w-4 h-4" /> Evidence
                </h4>
                {isEvidenceInboxKind(selected.category) && (
                  <p className="text-xs text-secondary mb-1" data-testid="inbox-kind-meaning">
                    {EVIDENCE_INBOX_KINDS[selected.category].meaning}
                  </p>
                )}
                <pre className="text-xs bg-surface-alt rounded p-2 overflow-x-auto">
                  {JSON.stringify(selected.evidence, null, 2)}
                </pre>
                {evidenceLinks.length > 0 && (
                  <div className="mt-2 flex gap-2 flex-wrap">
                    {evidenceLinks.map((l) => (
                      <button
                        key={l.tab + l.label}
                        onClick={() => setActiveTab?.(l.tab)}
                        className="text-xs text-primary-600 hover:text-primary-800 underline"
                      >
                        {l.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Trail */}
              <div>
                <h4 className="text-sm font-medium text-default mb-1 flex items-center gap-1">
                  <Clock className="w-4 h-4" /> History
                </h4>
                <ul className="text-xs text-secondary space-y-1">
                  {selected.events.map((e) => (
                    <li key={e.id} className="flex items-center gap-2">
                      <span className="text-muted w-36 shrink-0">{fmtTime(e.at)}</span>
                      <span className="font-medium">{e.event}</span>
                      {e.admin && (
                        <span className="flex items-center gap-1">
                          <User className="w-3 h-3" />
                          {e.admin}
                        </span>
                      )}
                      {e.detail && (
                        <span className="text-muted truncate">
                          {JSON.stringify(e.detail)}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>

              {selected.state === 'resolved' && (
                <div className="p-3 bg-green-50 rounded text-sm" data-testid="resolution">
                  <span className="font-medium text-green-800">
                    {selected.disposition}
                  </span>
                  <span className="text-green-700"> — {selected.resolution_note}</span>
                  <span className="text-muted">
                    {' '}
                    ({selected.resolved_by}, {fmtTime(selected.resolved_at)})
                  </span>
                </div>
              )}

              {/* Resolve form */}
              {resolving && selected.state !== 'resolved' && (
                <div className="p-3 border rounded-md space-y-2" data-testid="resolve-form">
                  <label className="block text-sm font-medium text-default">
                    Disposition
                  </label>
                  <select
                    aria-label="Disposition"
                    value={disposition}
                    onChange={(e) => setDisposition(e.target.value as InboxDisposition)}
                    className="w-full px-2 py-1.5 border border-strong rounded-md text-sm"
                  >
                    {INBOX_DISPOSITIONS.map((d) => (
                      <option key={d} value={d}>
                        {DISPOSITION_LABELS[d]}
                      </option>
                    ))}
                  </select>
                  <label className="block text-sm font-medium text-default">
                    Resolution note{' '}
                    <span className="text-muted font-normal">
                      (required — dispositions are report evidence)
                    </span>
                  </label>
                  <textarea
                    aria-label="Resolution note"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    rows={2}
                    className="w-full px-2 py-1.5 border border-strong rounded-md text-sm"
                    placeholder="What did you conclude, and why?"
                  />
                  <div className="flex gap-2">
                    <button
                      disabled={busy || !note.trim()}
                      onClick={() =>
                        gatewayClient &&
                        void act(
                          () =>
                            gatewayClient.resolveAssuranceInboxItem(
                              selected.id,
                              disposition,
                              note.trim()
                            ),
                          selected.id
                        )
                      }
                      className="px-3 py-1.5 bg-primary-500 text-white rounded-md text-sm disabled:opacity-50"
                    >
                      Record disposition
                    </button>
                    <button
                      onClick={() => setResolving(false)}
                      className="px-3 py-1.5 border border-strong text-secondary rounded-md text-sm"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Actions */}
            <div className="px-6 py-3 border-t flex items-center gap-2">
              {selected.state === 'open' && (
                <button
                  disabled={busy}
                  onClick={() =>
                    gatewayClient &&
                    void act(
                      () => gatewayClient.acknowledgeAssuranceInboxItem(selected.id),
                      selected.id
                    )
                  }
                  className="px-3 py-1.5 border border-strong text-secondary rounded-md text-sm hover:bg-surface-alt disabled:opacity-50"
                >
                  Acknowledge
                </button>
              )}
              {selected.state !== 'resolved' && !resolving && (
                <button
                  disabled={busy}
                  onClick={() => setResolving(true)}
                  className="px-3 py-1.5 bg-primary-500 text-white rounded-md text-sm hover:bg-primary-600 disabled:opacity-50"
                >
                  Resolve…
                </button>
              )}
              {selected.state === 'resolved' && (
                <button
                  disabled={busy}
                  onClick={() => {
                    const reason = window.prompt('Reason for reopening?');
                    if (reason && reason.trim() && gatewayClient) {
                      void act(
                        () =>
                          gatewayClient.reopenAssuranceInboxItem(
                            selected.id,
                            reason.trim()
                          ),
                        selected.id
                      );
                    }
                  }}
                  className="px-3 py-1.5 border border-strong text-secondary rounded-md text-sm hover:bg-surface-alt disabled:opacity-50 flex items-center gap-1"
                >
                  <RotateCcw className="w-3.5 h-3.5" /> Reopen
                </button>
              )}
              {selected.severity === 'critical' && selected.state !== 'resolved' && (
                <span className="ml-auto text-xs text-red-700 flex items-center gap-1">
                  <AlertTriangle className="w-3.5 h-3.5" />
                  Counts toward review debt until resolved
                </span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default AssuranceInbox;
