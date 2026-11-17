/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Assurance Inbox — Mission Control triage queue.
 *
 * What is pinned:
 *   * default filters hide `info` and `resolved` (warn-mode pilots must
 *     not drown the queue), with explicit chips to reveal them;
 *   * the resolve flow REQUIRES a note (dispositions are evidence) and
 *     sends the typed disposition;
 *   * the detail drawer shows the append-only trail;
 *   * the open-critical badge reads from /counts.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { AssuranceInboxItem } from '../../../lib/assuranceInboxTypes';

const mockList = vi.fn();
const mockCounts = vi.fn();
const mockGet = vi.fn();
const mockAck = vi.fn();
const mockResolve = vi.fn();
const mockReopen = vi.fn();

const gatewayClient = {
  listAssuranceInbox: mockList,
  getAssuranceInboxCounts: mockCounts,
  getAssuranceInboxItem: mockGet,
  acknowledgeAssuranceInboxItem: mockAck,
  resolveAssuranceInboxItem: mockResolve,
  reopenAssuranceInboxItem: mockReopen,
};

vi.mock('../../../hooks/useGateway', () => ({
  useGateway: () => ({ gatewayClient }),
}));

import AssuranceInbox from '../AssuranceInbox';

const item = (over: Partial<AssuranceInboxItem> = {}): AssuranceInboxItem => ({
  id: 'inbox-1',
  tenant_id: 't1',
  ai_system_id: 'sys-1',
  category: 'drift',
  severity: 'critical',
  fingerprint: 'drift:sys-1:behavioural:step_count',
  title: 'Drift detected: behavioural on step_count',
  evidence: { drift_event_id: 'drift-9' },
  occurrence_count: 3,
  first_seen_at: '2026-08-30T10:00:00Z',
  last_seen_at: '2026-08-31T10:00:00Z',
  state: 'open',
  acknowledged_at: null,
  acknowledged_by: null,
  assignee_admin: null,
  disposition: null,
  resolution_note: null,
  resolved_at: null,
  resolved_by: null,
  ...over,
});

describe('AssuranceInbox', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({ items: [item()], total: 1, limit: 100, offset: 0 });
    mockCounts.mockResolvedValue({
      open: 2,
      acknowledged: 1,
      resolved: 5,
      open_critical: 2,
    });
    mockGet.mockResolvedValue({
      ...item(),
      events: [
        { id: 'e1', item_id: 'inbox-1', at: '2026-08-30T10:00:00Z', admin: null, event: 'created', detail: null },
        { id: 'e2', item_id: 'inbox-1', at: '2026-08-31T10:00:00Z', admin: null, event: 'reoccurred', detail: null },
      ],
    });
  });

  it('renders rows, occurrence counter, and the open-critical badge', async () => {
    render(<AssuranceInbox />);
    expect(await screen.findByTestId('inbox-row')).toHaveTextContent(
      'Drift detected: behavioural on step_count'
    );
    expect(screen.getByText('×3')).toBeTruthy();
    expect(screen.getByTestId('inbox-open-critical')).toHaveTextContent('2 critical');
  });

  it('defaults exclude resolved state and info severity, chips reveal them', async () => {
    render(<AssuranceInbox />);
    await screen.findByTestId('inbox-row');
    // Defaults: composite filters do NOT send state/severity to the API…
    const params = mockList.mock.calls[0][0];
    expect(params.state).toBeUndefined();
    expect(params.severity).toBeUndefined();

    // …and info rows are projected out client-side.
    mockList.mockResolvedValue({
      items: [item({ id: 'i2', severity: 'info', fingerprint: 'f2' })],
      total: 1, limit: 100, offset: 0,
    });
    fireEvent.change(screen.getByLabelText('Severity filter'), {
      target: { value: 'info' },
    });
    await waitFor(() =>
      expect(mockList).toHaveBeenLastCalledWith(
        expect.objectContaining({ severity: 'info' })
      )
    );
  });

  it('resolve flow requires a note and records the disposition', async () => {
    mockResolve.mockResolvedValue(item({ state: 'resolved' }));
    render(<AssuranceInbox />);
    fireEvent.click(await screen.findByTestId('inbox-row'));
    const drawer = await screen.findByTestId('inbox-detail');

    fireEvent.click(within(drawer).getByRole('button', { name: /resolve…/i }));
    const form = await within(drawer).findByTestId('resolve-form');

    // Empty note → the primary action stays disabled (evidence rule).
    const record = within(form).getByRole('button', { name: /record disposition/i });
    expect(record).toBeDisabled();

    fireEvent.change(within(form).getByLabelText('Disposition'), {
      target: { value: 'false_positive' },
    });
    fireEvent.change(within(form).getByLabelText('Resolution note'), {
      target: { value: 'Threshold tuned; detector was over-eager.' },
    });
    expect(record).toBeEnabled();
    fireEvent.click(record);

    await waitFor(() =>
      expect(mockResolve).toHaveBeenCalledWith(
        'inbox-1',
        'false_positive',
        'Threshold tuned; detector was over-eager.'
      )
    );
  });

  it('drawer shows the append-only trail and acknowledge works', async () => {
    mockAck.mockResolvedValue(item({ state: 'acknowledged' }));
    render(<AssuranceInbox />);
    fireEvent.click(await screen.findByTestId('inbox-row'));
    const drawer = await screen.findByTestId('inbox-detail');
    expect(within(drawer).getByText('created')).toBeTruthy();
    expect(within(drawer).getByText('reoccurred')).toBeTruthy();

    fireEvent.click(within(drawer).getByRole('button', { name: /acknowledge/i }));
    await waitFor(() => expect(mockAck).toHaveBeenCalledWith('inbox-1'));
  });

  it('empty queue reads as a clean state, not an error', async () => {
    mockList.mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
    render(<AssuranceInbox />);
    expect(await screen.findByTestId('inbox-empty')).toHaveTextContent(
      'Nothing awaiting review'
    );
  });

  it('pre-filters to one AI System when given', async () => {
    render(<AssuranceInbox aiSystemId="sys-42" />);
    await waitFor(() =>
      expect(mockList).toHaveBeenCalledWith(
        expect.objectContaining({ ai_system_id: 'sys-42' })
      )
    );
  });

  // -------------------------------------------------------------------
  // Evidence links
  //
  // `setActiveTab` takes a tab id that App.tsx switches on. An id with no
  // matching case renders a perfectly normal-looking link that does
  // nothing when clicked — no error, no console warning, no visual cue.
  // That is exactly how 'proxy-logs' (the real id is
  // 'monitoring-proxy-logs') shipped and stayed broken. These pin the ids.
  // -------------------------------------------------------------------

  const KNOWN_TAB_IDS = new Set([
    'monitoring-proxy-logs',
    'monitoring-audit-logs',
    'ai-systems',
  ]);

  it('the proxy-log link targets the tab id App.tsx actually handles', async () => {
    const setActiveTab = vi.fn();
    mockGet.mockResolvedValue({
      ...item({ evidence: { proxy_log_id: 'pl-1' } }),
      events: [],
    });
    render(<AssuranceInbox setActiveTab={setActiveTab} />);
    fireEvent.click(await screen.findByTestId('inbox-row'));
    const drawer = await screen.findByTestId('inbox-detail');

    fireEvent.click(within(drawer).getByRole('button', { name: /view proxy logs/i }));
    expect(setActiveTab).toHaveBeenCalledWith('monitoring-proxy-logs');
  });

  it('every evidence link targets a real destination', async () => {
    const setActiveTab = vi.fn();
    mockGet.mockResolvedValue({
      ...item({
        ai_system_id: 'sys-1',
        category: 'health_transition',
        evidence: {
          proxy_log_id: 'pl-1',
          run_id: 'run-1',
          drift_event_id: 'drift-9',
        },
      }),
      events: [],
    });
    render(<AssuranceInbox setActiveTab={setActiveTab} />);
    fireEvent.click(await screen.findByTestId('inbox-row'));
    const drawer = await screen.findByTestId('inbox-detail');

    const links = within(drawer).getAllByRole('button', { name: /^View / });
    expect(links.length).toBeGreaterThan(1);
    for (const link of links) {
      setActiveTab.mockClear();
      fireEvent.click(link);
      const tab = setActiveTab.mock.calls[0]?.[0] as string;
      // Either a flat id App switches on, or the `ai-systems:<id>:<section>`
      // deep-link form App parses before the switch.
      const ok = KNOWN_TAB_IDS.has(tab) || /^ai-systems:[^:]+:[^:]+$/.test(tab);
      expect(ok, `"${link.textContent}" navigates to unknown tab "${tab}"`).toBe(true);
    }
  });

  // Composition findings are worked on the Composition tab.
  it('labels a composition finding and links it to the Composition tab', async () => {
    const setActiveTab = vi.fn();
    const composition = item({
      id: 'inbox-comp',
      category: 'composition',
      severity: 'warning',
      fingerprint: 'composition:sys-1:composition.dependency_pin_stale:sys-scoring',
      title: "Brutor Demo System is pinned to Scoring Service's contract v2, but Scoring Service now runs under v3",
      evidence: { code: 'composition.dependency_pin_stale', subject_id: 'sys-scoring' },
    });
    mockList.mockResolvedValue({ items: [composition], total: 1, limit: 100, offset: 0 });
    mockGet.mockResolvedValue({ ...composition, events: [] });
    render(<AssuranceInbox setActiveTab={setActiveTab} />);
    const row = await screen.findByTestId('inbox-row');
    expect(within(row).getByTestId('inbox-category-composition')).toHaveTextContent('Composition');
    fireEvent.click(row);
    const drawer = await screen.findByTestId('inbox-detail');
    fireEvent.click(within(drawer).getByRole('button', { name: /view composition/i }));
    expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-1:composition');
  });
});
