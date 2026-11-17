/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Connection status — THE mapping every Composition view uses.
 *
 * The control plane computes one status per connection
 * (`ai_system_connection_status.connection_status`, whose call-count rule is
 * the platform's one "down" rule, `ai_system_down_rule`). This module is the
 * ONLY place the UI maps a status to its label, legend meaning, line style
 * and colour class; the Simple graph's lines, the Diagram's node badges and
 * the List's Status column all read it, so the three views cannot disagree
 * (guard: `src/lib/__tests__/compositionConnection.test.ts` — no other
 * module defines a status → colour map).
 *
 * `idle` is grey on purpose: no calls in the window is no evidence, never
 * "all good". Colour is never the only signal: every status also has its
 * own line style, and the legend spells both out.
 */

import type {
  CompositionConnection,
  CompositionConnectionStatus,
  RunActionOutcome,
} from './aiSystemTypes';

export const CONNECTION_STATUS_ORDER: readonly CompositionConnectionStatus[] = [
  'ok',
  'degraded',
  'down',
  'idle',
];

export interface ConnectionStatusMeta {
  label: string;
  /** The legend's explanation — what the colour AND the style mean. */
  meaning: string;
  /** SVG stroke-dasharray; null = solid. */
  dash: string | null;
  /** A glyph drawn on the line's middle, beyond colour and dash. */
  glyph: 'cross' | null;
  /** Style in words — the legend says it, so colour is never alone. */
  style: string;
  /** The colour class (tokens in `styles/globals.css`, light and dark). */
  className: string;
}

export const CONNECTION_STATUS_META: Record<
  CompositionConnectionStatus,
  ConnectionStatusMeta
> = {
  ok: {
    label: 'OK',
    meaning:
      'Calls in the window: none of the last 3 failed or were blocked, and under 5% did overall. Calls held for approval are counted apart: decided, or still inside their timeout, is all good.',
    dash: null,
    glyph: null,
    style: 'solid line',
    className: 'composition-connection-ok',
  },
  degraded: {
    label: 'Degraded',
    meaning:
      'One of the last 3 calls failed or was blocked, at least 5% of the calls in the window did, a call held for approval expired without a decision, or the resource reports degraded health.',
    dash: '8 3 2 3',
    glyph: null,
    style: 'dash-dot line',
    className: 'composition-connection-degraded',
  },
  down: {
    label: 'Down',
    meaning:
      'Communication stopped: the last 3 calls all failed or were blocked, or the resource is disabled, retired or unhealthy.',
    dash: null,
    glyph: 'cross',
    style: 'solid line with a cross',
    className: 'composition-connection-down',
  },
  idle: {
    label: 'No calls',
    meaning:
      'No calls in the window — no evidence either way, which is not the same as all good.',
    dash: '4 4',
    glyph: null,
    style: 'grey dashed line',
    className: 'composition-connection-idle',
  },
};

/** A node the payload sent no connection for (never, per the contract —
 *  but an absent status must read "no evidence", never green). */
export const NO_EVIDENCE: CompositionConnection = {
  status: 'idle',
  reason: 'No connection status',
  calls: 0,
  errors: 0,
  blocked: 0,
  held: 0,
  held_lapsed: 0,
  last_call_at: null,
};

/** "Loan Core — Down: Last 3 calls blocked by policy" — a line's tooltip,
 *  a badge's title. */
export function connectionTitle(
  name: string,
  c: CompositionConnection
): string {
  return `${name} — ${CONNECTION_STATUS_META[c.status].label}: ${c.reason}`;
}

/** "1 ok, 1 down" — the statuses of a set of connections, in legend order. */
export function statusMix(
  items: readonly { connection: CompositionConnection }[]
): string {
  return CONNECTION_STATUS_ORDER.map((s) => {
    const n = items.filter((i) => i.connection.status === s).length;
    return n ? `${n} ${CONNECTION_STATUS_META[s].label.toLowerCase()}` : null;
  })
    .filter(Boolean)
    .join(', ');
}

// ---------------------------------------------------------------------------
// A run's actions on the Simple graph (the run overlay)
// ---------------------------------------------------------------------------

/**
 * A run action's outcome, drawn with THE connection mapping above: an
 * answered call is an `ok` connection, an error a `degraded` one, and a
 * gateway refusal — blocked, or held for a person — a `down` one. The
 * colour class is always `CONNECTION_STATUS_META[status].className`, so a
 * badge and the line it sits on cannot disagree about what "down" looks
 * like. Colour is never alone: every outcome has a word and (all but
 * answered) a glyph, and a hold has its own glyph, distinct from a block.
 */
export const RUN_OUTCOME_META: Record<
  RunActionOutcome,
  {
    status: CompositionConnectionStatus;
    label: string;
    /** Drawn after the number: none | ! (error) | × (blocked) | ⏸ (held). */
    glyph: string | null;
    /** The legend's explanation. */
    meaning: string;
  }
> = {
  answered: {
    status: 'ok',
    label: 'Answered',
    glyph: null,
    meaning: 'The call went through and was answered.',
  },
  error: {
    status: 'degraded',
    label: 'Error',
    glyph: '!',
    meaning: 'The call failed (not answered, or HTTP 400 and above).',
  },
  blocked: {
    status: 'down',
    label: 'Blocked',
    glyph: '×',
    meaning:
      'The gateway refused the call: a guardrail, a policy, an authorisation or a limit.',
  },
  held: {
    status: 'down',
    label: 'Held for approval',
    glyph: '⏸',
    meaning: 'Stopped until a person approves it — not a failure.',
  },
};

export const RUN_OUTCOME_ORDER: readonly RunActionOutcome[] = [
  'answered',
  'error',
  'blocked',
  'held',
];

/** The colour class of a run action's outcome — THE status class. */
export function runOutcomeClass(outcome: RunActionOutcome): string {
  return CONNECTION_STATUS_META[RUN_OUTCOME_META[outcome].status].className;
}
