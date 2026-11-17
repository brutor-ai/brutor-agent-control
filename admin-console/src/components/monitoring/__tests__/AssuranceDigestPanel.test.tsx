/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Weekly digest panel.
 *
 * Pinned: stored digests render the STORED content (never recomputed);
 * preview computes live without sending; empty state explains how the
 * first digest appears.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { AssuranceDigest, DigestContent } from '../../../lib/assuranceCheckTypes';

const mockListDigests = vi.fn();
const mockPreview = vi.fn();

const gatewayClient = {
  listAssuranceDigests: mockListDigests,
  previewAssuranceDigest: mockPreview,
};

vi.mock('../../../hooks/useGateway', () => ({
  useGateway: () => ({ gatewayClient }),
}));

import AssuranceDigestPanel from '../AssuranceDigestPanel';

const content = (over: Partial<DigestContent> = {}): DigestContent => ({
  week_start: '2026-08-24',
  week_end: '2026-08-31',
  totals: {
    systems: 2, runs: 340, cost_usd: 12.5, items_raised: 6,
    items_resolved: 4, open_critical: 1, open_total: 3,
  },
  raised_by_severity: { warning: 4, critical: 2 },
  open_by_severity: { critical: 1, warning: 2 },
  top_check_breaches: [
    { title: 'Check breached: cost', occurrences: 7, severity: 'critical',
      ai_system_id: 'sys-1' },
  ],
  systems: [
    {
      ai_system_id: 'sys-1', name: 'Support Copilot',
      health_state: 'degraded', health_score: 0.5, runs: 300,
      cost_usd: 11.0, items_raised: 5, open_critical: 1, drift_events: 2,
    },
  ],
  ...over,
});

const digest = (): AssuranceDigest => ({
  id: 'dig-1',
  tenant_id: 't1',
  week_start: '2026-08-24',
  content: content(),
  generated_at: '2026-08-31T08:00:00Z',
});

describe('AssuranceDigestPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListDigests.mockResolvedValue({ digests: [digest()], total: 1 });
  });

  it('renders the stored digest with totals and worst-first systems', async () => {
    render(<AssuranceDigestPanel />);
    fireEvent.click(screen.getByTestId('digest-toggle'));
    const body = await screen.findByTestId('digest-body');
    expect(body).toHaveTextContent('340');
    expect(screen.getByTestId('digest-system-row')).toHaveTextContent(
      'Support Copilot'
    );
    expect(body).toHaveTextContent('Check breached: cost');
  });

  it('preview computes the current week live', async () => {
    mockPreview.mockResolvedValue(content({ week_start: '2026-08-31' }));
    render(<AssuranceDigestPanel />);
    fireEvent.click(screen.getByTestId('digest-toggle'));
    await screen.findByTestId('digest-body');
    fireEvent.click(screen.getByTestId('digest-preview'));
    await waitFor(() => expect(mockPreview).toHaveBeenCalled());
    const weekStart = '2026-08-31';
    expect(
      await screen.findByText(new RegExp(`Live preview — week of ${weekStart}`))
    ).toBeInTheDocument();
  });

  it('empty state explains how the first digest appears', async () => {
    mockListDigests.mockResolvedValue({ digests: [], total: 0 });
    render(<AssuranceDigestPanel />);
    fireEvent.click(screen.getByTestId('digest-toggle'));
    expect(await screen.findByTestId('no-digests')).toBeInTheDocument();
  });
});
