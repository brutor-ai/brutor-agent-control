/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Continuous checks panel.
 *
 * Pinned:
 *   * checks render with tier badge, version, and the enable toggle;
 *   * the compile button surfaces the evaluator's verdict verbatim;
 *   * the NL assist fills the form from the proposal and discloses a
 *     tier-B downgrade;
 *   * create sends the deterministic spec shape (expression + value
 *     threshold);
 *   * backtest shows "what would have breached".
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { AssuranceCheck } from '../../../lib/assuranceCheckTypes';
import { RUN_FACT_KEYS } from '../../../lib/runFacts';

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockDelete = vi.fn();
const mockVersions = vi.fn();
const mockBacktest = vi.fn();
const mockResults = vi.fn();
const mockCompile = vi.fn();
const mockAssist = vi.fn();

const gatewayClient = {
  listAssuranceChecks: mockList,
  createAssuranceCheck: mockCreate,
  updateAssuranceCheck: mockUpdate,
  deleteAssuranceCheck: mockDelete,
  listAssuranceCheckVersions: mockVersions,
  backtestAssuranceCheck: mockBacktest,
  listAssuranceCheckResults: mockResults,
  compileAssuranceCheck: mockCompile,
  compileAssistAssuranceCheck: mockAssist,
};

vi.mock('../../../hooks/useGateway', () => ({
  useGateway: () => ({ gatewayClient }),
}));

import AssuranceChecks from '../AssuranceChecks';

const check = (over: Partial<AssuranceCheck> = {}): AssuranceCheck => ({
  id: 'chk-1',
  tenant_id: 't1',
  name: 'payment-needs-approval',
  description_nl: null,
  version: 2,
  spec_hash: 'abc',
  scope: {},
  tier: 'deterministic',
  evaluator: { expression: 'run.approval_request_count == 0' },
  output: 'boolean',
  trigger: 'on_finalize',
  threshold: { kind: 'value', op: 'eq', value: true, severity: 'critical' },
  route_to_inbox: true,
  enabled: true,
  created_by: 'admin',
  approved_by: 'admin',
  created_at: '2026-08-31T10:00:00Z',
  updated_at: '2026-08-31T10:00:00Z',
  ...over,
});

const openPanel = async () => {
  render(<AssuranceChecks />);
  fireEvent.click(screen.getByTestId('checks-toggle'));
  await waitFor(() => expect(mockList).toHaveBeenCalled());
};

describe('AssuranceChecks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue([check()]);
    mockResults.mockResolvedValue({ results: [], total: 0 });
  });

  it('renders checks with tier, version, and enablement', async () => {
    await openPanel();
    const row = await screen.findByTestId('check-row');
    expect(row).toHaveTextContent('payment-needs-approval');
    expect(row).toHaveTextContent('TIER A');
    expect(row).toHaveTextContent('v2');
    expect(within(row).getByTestId('check-enabled-toggle')).toHaveTextContent(
      'Enabled'
    );
  });

  it('toggling enablement patches the check', async () => {
    mockUpdate.mockResolvedValue(check({ enabled: false }));
    await openPanel();
    const row = await screen.findByTestId('check-row');
    fireEvent.click(within(row).getByTestId('check-enabled-toggle'));
    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith('chk-1', { enabled: false })
    );
  });

  it('compile-check surfaces the evaluator verdict verbatim', async () => {
    mockCompile.mockResolvedValue({
      ok: false,
      errors: ["unknown variable 'foo' — only 'run' is available"],
    });
    await openPanel();
    fireEvent.click(screen.getByTestId('new-check'));
    const form = await screen.findByTestId('check-form');
    fireEvent.change(within(form).getByTestId('expression-input'), {
      target: { value: 'foo > 1' },
    });
    fireEvent.click(within(form).getByTestId('compile-run'));
    expect(await screen.findByTestId('compile-result')).toHaveTextContent(
      "unknown variable 'foo'"
    );
    expect(mockCompile).toHaveBeenCalledWith('foo > 1', 'boolean');
  });

  it('creates a deterministic check with the value threshold', async () => {
    mockCreate.mockResolvedValue(check({ enabled: false, version: 1 }));
    await openPanel();
    fireEvent.click(screen.getByTestId('new-check'));
    const form = await screen.findByTestId('check-form');
    fireEvent.change(within(form).getByPlaceholderText('payment-tools-need-approval'), {
      target: { value: 'expensive-runs' },
    });
    fireEvent.change(within(form).getByTestId('expression-input'), {
      target: { value: 'run.total_cost_usd > 5.0' },
    });
    fireEvent.click(within(form).getByTestId('create-check'));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith({
        name: 'expensive-runs',
        description_nl: null,
        tier: 'deterministic',
        evaluator: { expression: 'run.total_cost_usd > 5.0' },
        output: 'boolean',
        threshold: { kind: 'value', op: 'eq', value: true, severity: 'warning' },
      })
    );
  });

  it('NL assist fills the form and discloses a semantic downgrade', async () => {
    mockAssist.mockResolvedValue({
      proposal: {
        tier: 'semantic',
        reason: 'needs message content',
        evaluator: { rubric: 'Did it leak the system prompt?' },
        output: 'boolean',
      },
      downgraded: true,
      compile: null,
      model: 'judge-model',
    });
    await openPanel();
    fireEvent.click(screen.getByTestId('new-check'));
    const form = await screen.findByTestId('check-form');
    fireEvent.change(within(form).getByTestId('assist-question'), {
      target: { value: 'Did the assistant leak the system prompt?' },
    });
    fireEvent.click(within(form).getByTestId('assist-run'));
    const note = await screen.findByTestId('assist-note');
    expect(note).toHaveTextContent('semantic');
    expect(within(form).getByTestId('rubric-input')).toHaveValue(
      'Did it leak the system prompt?'
    );
  });

  it('backtest reports what would have breached', async () => {
    mockBacktest.mockResolvedValue({
      ok: true, evaluated: 250, breaches: 9, errors: 0, window_days: 7,
      samples: [],
    });
    await openPanel();
    fireEvent.click(await screen.findByTestId('check-row'));
    const details = await screen.findByTestId('check-details');
    fireEvent.click(within(details).getByTestId('run-backtest'));
    const summary = await screen.findByTestId('backtest-result');
    expect(summary).toHaveTextContent('Evaluated 250 runs');
    expect(summary).toHaveTextContent('9 would have breached');
    expect(mockBacktest).toHaveBeenCalledWith('chk-1', 7);
  });

  it('the expression info button lists every run fact and inserts one', async () => {
    await openPanel();
    fireEvent.click(screen.getByTestId('new-check'));
    const form = await screen.findByTestId('check-form');
    expect(within(form).queryByTestId('run-facts-help')).toBeNull();
    fireEvent.click(within(form).getByTestId('run-facts-toggle'));
    const help = within(form).getByTestId('run-facts-help');
    expect(within(help).getAllByTestId('run-fact')).toHaveLength(RUN_FACT_KEYS.length);
    fireEvent.click(within(help).getByText('run.max_delegation_depth'));
    expect(within(form).getByTestId('expression-input')).toHaveValue(
      'run.max_delegation_depth'
    );
    fireEvent.change(within(help).getByTestId('run-facts-filter'), {
      target: { value: 'delegation' },
    });
    expect(within(help).getAllByTestId('run-fact')).toHaveLength(1);
  });

  it('using an example fills the expression and its output type', async () => {
    mockCompile.mockResolvedValue({ ok: true, errors: [], sample_value: 3 });
    await openPanel();
    fireEvent.click(screen.getByTestId('new-check'));
    const form = await screen.findByTestId('check-form');
    fireEvent.click(within(form).getByTestId('run-facts-toggle'));
    fireEvent.click(within(form).getByText('Examples'));
    const example = within(form)
      .getAllByTestId('check-example')
      .find((el) => el.textContent?.includes('Failures per run'));
    fireEvent.click(within(example!).getByText('Use'));
    expect(within(form).getByTestId('expression-input')).toHaveValue(
      'run.error_count + run.tool_error_count'
    );
    fireEvent.click(within(form).getByTestId('compile-run'));
    await waitFor(() =>
      expect(mockCompile).toHaveBeenCalledWith('run.error_count + run.tool_error_count', 'count')
    );
  });

  it('empty state invites the first check', async () => {
    mockList.mockResolvedValue([]);
    await openPanel();
    expect(await screen.findByTestId('no-checks')).toBeInTheDocument();
  });

  it("shows the judge's reason and marks a truncated transcript", async () => {
    const result = (id: string, evidence: Record<string, unknown>) => ({
      id, check_id: 'chk-1', check_version: 2, run_id: `run-${id}`,
      window_start: null, window_end: null, value: { value: true },
      evidence, evaluator_fingerprint: 'fp', backtest: false,
      tokens_used: 120, evaluated_at: '2026-11-07T10:00:00Z',
    });
    mockResults.mockResolvedValue({
      results: [
        result('r1', {
          reason: 'The agent quoted the system prompt verbatim.',
          transcript: { transcript_version: 1, hops: 4, tool_hops: 2, truncated: true },
        }),
        result('r2', { reason: null, transcript: { truncated: false } }),
      ],
      total: 2,
    });
    await openPanel();
    fireEvent.click(await screen.findByTestId('check-row'));
    const rows = await screen.findAllByTestId('check-result-row');
    expect(within(rows[0]).getByTestId('check-result-reason')).toHaveTextContent(
      'Reason: The agent quoted the system prompt verbatim.'
    );
    expect(within(rows[0]).getByTestId('check-result-truncated')).toHaveTextContent(
      'transcript truncated'
    );
    // No reason, full transcript: nothing extra on the row.
    expect(within(rows[1]).queryByTestId('check-result-reason')).toBeNull();
    expect(within(rows[1]).queryByTestId('check-result-truncated')).toBeNull();
  });
});
