/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Weekly assurance digest — the in-console twin of the
 * Slack digest. Stored digests are EXACTLY what was sent to channels
 * (same row); "Preview current week" computes live without sending, so
 * the digest is testable without a webhook.
 */

import { useCallback, useEffect, useState } from 'react';
import { CalendarDays, ChevronDown, ChevronRight, Eye } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { AssuranceDigest, DigestContent } from '../../lib/assuranceCheckTypes';
import { failureSentence } from '../../lib/narrow';
import toast from 'react-hot-toast';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

const healthDot = (state: string) =>
  state === 'healthy'
    ? 'bg-green-500'
    : state === 'unknown'
      ? 'bg-gray-400'
      : 'bg-red-500';

function DigestBody({ content }: { content: DigestContent }) {
  return (
    <div className="space-y-3" data-testid="digest-body">
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 text-center">
        {(
          [
            ['Systems', content.totals.systems],
            ['Runs', content.totals.runs],
            ['Cost $', content.totals.cost_usd],
            ['Raised', content.totals.items_raised],
            ['Resolved', content.totals.items_resolved],
            ['Critical open', content.totals.open_critical],
          ] as const
        ).map(([label, value]) => (
          <div
            key={label}
            className="rounded-lg border border-default bg-surface px-2 py-1.5"
          >
            <div className="text-sm font-semibold text-default tabular-nums">
              {value}
            </div>
            <div className="text-[10px] text-secondary">{label}</div>
          </div>
        ))}
      </div>

      {content.systems.length > 0 && (
        <ul className="space-y-1">
          {content.systems.map((system) => (
            <li
              key={system.ai_system_id}
              className="flex items-center gap-2 text-sm"
              data-testid="digest-system-row"
            >
              <span
                className={`w-2 h-2 rounded-full ${healthDot(system.health_state)}`}
              />
              <span className="text-default font-medium">{system.name}</span>
              <span className="text-xs text-secondary">
                {system.health_state} · {system.runs} runs · $
                {system.cost_usd} · {system.open_critical} critical open
                {system.drift_events > 0 && ` · ${system.drift_events} drift`}
              </span>
            </li>
          ))}
        </ul>
      )}

      {content.top_check_breaches.length > 0 && (
        <div>
          <p className="text-xs font-medium text-secondary mb-1">
            Top check breaches
          </p>
          <ul className="space-y-0.5">
            {content.top_check_breaches.map((breach) => (
              <li key={breach.title} className="text-xs text-secondary">
                [{breach.severity.toUpperCase()}] {breach.title} (×
                {breach.occurrences})
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function AssuranceDigestPanel() {
  const { gatewayClient } = useGateway();
  const [collapsed, setCollapsed] = useState(true);
  const [digests, setDigests] = useState<AssuranceDigest[]>([]);
  const [readFailure, reads] = useReadFailures<'digests'>();
  const [preview, setPreview] = useState<DigestContent | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [selected, setSelected] = useState<AssuranceDigest | null>(null);

  const load = useCallback(async () => {
    try {
      const listed = await gatewayClient.listAssuranceDigests(12);
      setDigests(listed.digests);
      setSelected(listed.digests[0] ?? null);
      reads.loaded('digests');
    } catch (err) {
      setDigests([]);
      reads.lost('digests', 'Failed to load the digests', err);
    }
  }, [gatewayClient, reads]);

  useEffect(() => {
    if (!collapsed) void load();
  }, [collapsed, load]);

  const runPreview = async () => {
    setPreviewBusy(true);
    try {
      setPreview(await gatewayClient.previewAssuranceDigest());
    } catch (err) {
      setPreview(null);
      toast.error(failureSentence('The digest could not be previewed', err));
    } finally {
      setPreviewBusy(false);
    }
  };

  return (
    <div
      className="rounded-xl border border-default bg-surface"
      data-testid="assurance-digest"
    >
      <button
        type="button"
        className="w-full flex items-center justify-between px-4 py-3"
        onClick={() => setCollapsed((c) => !c)}
        data-testid="digest-toggle"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-default">
          <CalendarDays className="w-4 h-4 text-primary-600" />
          Weekly summary
          {digests[0] && (
            <span className="text-xs font-normal text-secondary">
              latest: week of {digests[0].week_start}
            </span>
          )}
        </span>
        {collapsed ? (
          <ChevronRight className="w-4 h-4 text-secondary" />
        ) : (
          <ChevronDown className="w-4 h-4 text-secondary" />
        )}
      </button>

      {!collapsed && (
        <div className="px-4 pb-4 space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-xs text-secondary max-w-xl">
              A stored projection of the assurance report — what channels
              subscribed to <code>digest_weekly</code> received. Configure
              delivery under Settings → Notifications.
            </p>
            <button
              type="button"
              disabled={previewBusy}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs rounded-lg border border-default hover:bg-surface-raised"
              onClick={() => void runPreview()}
              data-testid="digest-preview"
            >
              <Eye className="w-3.5 h-3.5" />
              {previewBusy ? 'Computing…' : 'Preview current week'}
            </button>
          </div>

          {preview && (
            <div className="rounded-lg border border-dashed border-default p-3">
              <p className="text-xs font-medium text-secondary mb-2">
                Live preview — week of {preview.week_start} (not sent)
              </p>
              <DigestBody content={preview} />
            </div>
          )}

          {readFailure('digests') ? (
            <ReadFailureNotice failure={readFailure('digests')} onRetry={() => void load()} />
          ) : digests.length === 0 ? (
            <p className="text-sm text-secondary" data-testid="no-digests">
              No digests generated yet — the first one is produced at the
              scheduled weekly send point once a <code>digest_weekly</code>
              {' '}subscription exists.
            </p>
          ) : (
            <div className="space-y-2">
              {digests.length > 1 && (
                <div className="flex flex-wrap gap-1">
                  {digests.map((digest) => (
                    <button
                      key={digest.id}
                      type="button"
                      className={`px-2 py-0.5 text-[11px] rounded-full border ${
                        selected?.id === digest.id
                          ? 'border-primary-600 text-primary-600'
                          : 'border-default text-secondary hover:bg-surface-raised'
                      }`}
                      onClick={() => setSelected(digest)}
                    >
                      {digest.week_start}
                    </button>
                  ))}
                </div>
              )}
              {selected && <DigestBody content={selected.content} />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
