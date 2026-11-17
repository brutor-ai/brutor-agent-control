/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Connection status in EVERY Composition view. The Simple
 * graph draws it on its lines; were the Diagram and the List to show nothing,
 * an operator switching views would lose the one fact the graph leads with.
 * Every node with a `connection` shows its status and reason in all three
 * views, through the one mapping (`lib/compositionConnection.ts`): the same
 * label, the same colour class, the same line sample, the same legend.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

const mockFetchComposition = vi.fn();
const mockFetchContracts = vi.fn();
const mockFetchHealth = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
  useGateway: () => ({
    fetchAISystemComposition: mockFetchComposition,
    fetchAISystemContracts: mockFetchContracts,
    fetchSystemHealth: mockFetchHealth,
  }),
}));

import CompositionTab from '../CompositionTab';
import { buildCompositionModel } from '../../../lib/composition';
import { buildSimpleGraph } from '../../../lib/compositionSimpleGraph';
import { CONNECTION_STATUS_META } from '../../../lib/compositionConnection';
import type { CompositionConnection } from '../../../lib/aiSystemTypes';
import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';

async function renderView(view: 'simple' | 'diagram' | 'list') {
  render(<CompositionTab aiSystemId="sys-demo" windowHours={168} />);
  await screen.findByTestId('composition-tab');
  fireEvent.click(screen.getByTestId(`composition-view-${view}`));
}

/** Every node of the model that carries a connection, and its connection. */
const CONNECTED = buildCompositionModel(COMPOSITION_PAYLOAD)
  .sections.flatMap((s) => s.nodes)
  .filter(
    (n): n is typeof n & { connection: CompositionConnection } =>
      n.connection !== null
  )
  .map((n) => [n.key, n.label, n.connection] as const);

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  mockFetchComposition.mockResolvedValue(COMPOSITION_PAYLOAD);
  mockFetchContracts.mockResolvedValue({
    contracts: [],
    total: 0,
    config_drifted: false,
  });
  mockFetchHealth.mockResolvedValue({ dependency_health: [] });
});

describe('the fixture exercises every status', () => {
  it('ok, degraded, down and idle all appear on connected nodes', () => {
    expect(new Set(CONNECTED.map(([, , c]) => c.status))).toEqual(
      new Set(['ok', 'degraded', 'down', 'idle'])
    );
  });
});

describe.each(['diagram', 'list'] as const)('%s view', (view) => {
  const prefix = view === 'diagram' ? 'connection-' : 'list-connection-';

  it.each(CONNECTED)(
    '%s shows its status and reason through the one mapping',
    async (key, label, conn) => {
      await renderView(view);
      const badge = screen.getByTestId(`${prefix}${key}`);
      const meta = CONNECTION_STATUS_META[conn.status];
      expect(badge).toHaveAttribute('data-status', conn.status);
      expect(badge).toHaveClass(meta.className);
      expect(badge).toHaveTextContent(meta.label);
      expect(badge).toHaveTextContent(conn.reason);
      expect(badge).toHaveAttribute(
        'title',
        `${label} — ${meta.label}: ${conn.reason}`
      );
      // Colour is never alone: the line sample carries the style, and
      // only `down` carries the cross.
      expect(
        within(badge).queryByTestId('simple-graph-glyph-cross') !== null
      ).toBe(conn.status === 'down');
    }
  );

  it('idle reads "No calls", never OK', async () => {
    await renderView(view);
    const idle = screen.getByTestId(`${prefix}kb:kb-policy`);
    expect(idle).toHaveTextContent('No calls');
    expect(idle).not.toHaveTextContent(CONNECTION_STATUS_META.ok.label + ':');
    expect(idle).not.toHaveClass(CONNECTION_STATUS_META.ok.className);
  });

  it('nodes without a connection (policies, limits, agents) show none', async () => {
    await renderView(view);
    expect(screen.queryByTestId(`${prefix}agent:agent-intake`)).toBeNull();
    const all = screen.getAllByTestId(new RegExp(`^${prefix}`));
    expect(all).toHaveLength(CONNECTED.length);
  });

  it('explains the statuses with the same legend as the Simple graph', async () => {
    await renderView(view);
    const legend = screen.getByTestId(`composition-${view}-connection-legend`);
    for (const s of ['ok', 'degraded', 'down', 'idle'] as const) {
      const row = within(legend).getByTestId(
        `composition-${view}-connection-legend-${s}`
      );
      expect(row).toHaveTextContent(CONNECTION_STATUS_META[s].label);
      expect(row).toHaveTextContent(CONNECTION_STATUS_META[s].meaning);
      expect(row).toHaveTextContent(CONNECTION_STATUS_META[s].style);
    }
  });
});

describe('list view', () => {
  it('has a Status column', async () => {
    await renderView('list');
    const tables = within(screen.getByTestId('composition-list')).getAllByRole(
      'table'
    );
    for (const t of tables) {
      const headers = within(t)
        .getAllByRole('columnheader')
        .map((h) => h.textContent);
      expect(headers).toContain('Status');
    }
  });
});

/** The nodes the Simple graph draws a line to (it leaves the platform's
 *  own system MCP servers out of its MCP lane). */
const LINED = new Set(
  buildSimpleGraph(
    COMPOSITION_PAYLOAD,
    buildCompositionModel(COMPOSITION_PAYLOAD)
  ).lanes.flatMap((l) => l.items.map((i) => i.key))
);

describe('the three views agree', () => {
  it('every lined node is a connected node', () => {
    expect(LINED.size).toBeGreaterThan(0);
    for (const key of LINED) {
      expect(CONNECTED.map(([k]) => k)).toContain(key);
    }
  });

  it.each(CONNECTED.filter(([key]) => LINED.has(key)))(
    '%s: the Simple graph line, the Diagram badge and the List badge carry one status',
    async (key, _label, conn) => {
      await renderView('simple');
      expect(screen.getByTestId(`simple-graph-line-${key}`)).toHaveAttribute(
        'data-status',
        conn.status
      );
      fireEvent.click(screen.getByTestId('composition-view-diagram'));
      expect(screen.getByTestId(`connection-${key}`)).toHaveAttribute(
        'data-status',
        conn.status
      );
      fireEvent.click(screen.getByTestId('composition-view-list'));
      expect(screen.getByTestId(`list-connection-${key}`)).toHaveAttribute(
        'data-status',
        conn.status
      );
    }
  );
});
