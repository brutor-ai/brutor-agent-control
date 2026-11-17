/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Connection status, drawn — the pieces every Composition view shares: the line sample (colour AND style, the cross for `down`),
 * the status badge the Diagram's nodes and the List's Status column show,
 * and the legend that explains all four. The mapping itself (labels,
 * meanings, styles, colour classes) is `lib/compositionConnection.ts`.
 */

import {
  CONNECTION_STATUS_META,
  CONNECTION_STATUS_ORDER,
  connectionTitle,
} from '../../lib/compositionConnection';
import type {
  CompositionConnection,
  CompositionConnectionStatus,
} from '../../lib/aiSystemTypes';

/** The `down` glyph: a cross on a surface-coloured disc, so it reads over a line. */
export function CrossGlyph({ at }: { at: { x: number; y: number } }) {
  return (
    <g data-testid="simple-graph-glyph-cross">
      <circle
        cx={at.x}
        cy={at.y}
        r={7}
        className="composition-glyph-bg composition-connection-stroke"
        strokeWidth={1.5}
      />
      <path
        d={`M ${at.x - 3} ${at.y - 3} L ${at.x + 3} ${at.y + 3} M ${at.x + 3} ${at.y - 3} L ${at.x - 3} ${at.y + 3}`}
        className="composition-connection-stroke"
        strokeWidth={2}
        strokeLinecap="round"
      />
    </g>
  );
}

/** A short sample of a status's line, as the Simple graph draws it. */
export function LineSample({
  status,
}: {
  status: CompositionConnectionStatus;
}) {
  const meta = CONNECTION_STATUS_META[status];
  return (
    <svg
      width="44"
      height="14"
      viewBox="0 0 44 14"
      className={`shrink-0 ${meta.className}`}
      aria-hidden="true"
    >
      <line
        x1="1"
        y1="7"
        x2="43"
        y2="7"
        className="composition-connection-stroke"
        strokeWidth="2"
        strokeDasharray={meta.dash ?? undefined}
      />
      {meta.glyph === 'cross' && <CrossGlyph at={{ x: 22, y: 7 }} />}
    </svg>
  );
}

/**
 * A connection's status as text: the line sample, the label and (unless
 * `compact`) the reason. The whole sentence is the title too, so a
 * compact badge still says why on hover. Colour is never alone: the label
 * is words, the sample carries the line style.
 */
export function ConnectionStatusBadge({
  name,
  connection,
  testId,
  compact = false,
}: {
  name: string;
  connection: CompositionConnection;
  testId: string;
  compact?: boolean;
}) {
  const meta = CONNECTION_STATUS_META[connection.status];
  return (
    <span
      className={`inline-flex items-start gap-1.5 text-xs ${meta.className}`}
      data-testid={testId}
      data-status={connection.status}
      title={connectionTitle(name, connection)}
    >
      <span className="mt-0.5">
        <LineSample status={connection.status} />
      </span>
      <span>
        <span className="composition-connection-swatch font-semibold">
          {meta.label}
        </span>
        {!compact && (
          <span className="text-secondary">: {connection.reason}</span>
        )}
      </span>
    </span>
  );
}

/** What the statuses mean — outside any image, so it is read as text. */
export function ConnectionLegend({
  testId = 'simple-graph-legend',
  showExternal = true,
}: {
  testId?: string;
  /** The Simple graph's dashed-ring key; the Diagram's own legend says it. */
  showExternal?: boolean;
}) {
  return (
    <div
      className="rounded-lg border border-default bg-surface px-3 py-2"
      data-testid={testId}
    >
      <p className="mb-1 text-xs font-medium text-secondary">
        Connections (this system’s own calls in the window)
      </p>
      <ul className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
        {CONNECTION_STATUS_ORDER.map((s) => {
          const meta = CONNECTION_STATUS_META[s];
          return (
            <li
              key={s}
              className="flex items-start gap-2 text-xs text-secondary"
              data-testid={`${testId}-${s}`}
            >
              <span className="mt-0.5">
                <LineSample status={s} />
              </span>
              <span>
                <span className="font-semibold text-default">{meta.label}</span>{' '}
                <span className="text-muted">({meta.style})</span> —{' '}
                {meta.meaning}
              </span>
            </li>
          );
        })}
        {showExternal && (
          <li
            className="flex items-start gap-2 text-xs text-secondary"
            data-testid={`${testId}-external`}
          >
            <span
              className="composition-cat-delegation-agent composition-icon-ring mt-0.5 inline-block h-3.5 w-3.5 shrink-0 rounded-full"
              data-dashed="true"
              aria-hidden="true"
            />
            <span>
              <span className="font-semibold text-default">Dashed ring</span> —
              an external agent: assumed, not tested (seen only at the gateway
              boundary).
            </span>
          </li>
        )}
      </ul>
    </div>
  );
}
