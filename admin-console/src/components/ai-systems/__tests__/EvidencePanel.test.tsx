/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for EvidencePanel.
 *
 * What is worth defending:
 *
 *   - provenance renders as three distinct states — independent,
 *     self-assessed, unstated — because "we don't know" is a different
 *     claim from "we assessed ourselves";
 *   - an expired artifact stays visible with an explicit badge (the gate
 *     refuses on it, so hiding it would hide the reason);
 *   - the empty state says plainly that everything else is
 *     platform-generated;
 *   - attaching omits `independent` when unstated, and reports the change
 *     upward so the lifecycle checklist can refresh.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetch = vi.fn();
const mockCreate = vi.fn();
const mockDelete = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemEvidence: mockFetch,
        createAISystemEvidence: mockCreate,
        deleteAISystemEvidence: mockDelete,
    }),
}));

import EvidencePanel from '../EvidencePanel';
import type { AISystemEvidenceItem } from '../../../lib/aiSystemTypes';
import { EVIDENCE_KINDS } from '../../../lib/complianceEvidenceTypes';

function item(overrides: Partial<AISystemEvidenceItem> = {}): AISystemEvidenceItem {
    return {
        id: 'ev-1',
        ai_system_id: 'sys-1',
        kind: 'impact_assessment',
        title: 'FRIA 2026',
        is_expired: false,
        ...overrides,
    };
}

function listing(items: AISystemEvidenceItem[]) {
    return { evidence: items, total: items.length };
}

beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValue(listing([]));
    mockCreate.mockResolvedValue(item());
    mockDelete.mockResolvedValue({ deleted: true });
});

describe('EvidencePanel', () => {
    it('states the empty case instead of leaving blank space', async () => {
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('evidence-empty')).toBeInTheDocument()
        );
        expect(screen.getByTestId('evidence-empty').textContent).toMatch(
            /platform-generated/
        );
    });

    it('renders the three provenance states distinctly', async () => {
        mockFetch.mockResolvedValue(
            listing([
                item({ id: 'ev-a', independent: true }),
                item({ id: 'ev-b', independent: false }),
                item({ id: 'ev-c', independent: null }),
            ])
        );
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('evidence-ev-a')).toBeInTheDocument()
        );
        expect(screen.getByTestId('evidence-ev-a').textContent).toMatch(
            /independent/
        );
        expect(screen.getByTestId('evidence-ev-b').textContent).toMatch(
            /self-assessed/
        );
        expect(screen.getByTestId('evidence-ev-c').textContent).toMatch(
            /provenance unstated/
        );
    });

    it('keeps an expired artifact visible with an explicit badge', async () => {
        mockFetch.mockResolvedValue(
            listing([item({ id: 'ev-old', is_expired: true })])
        );
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('evidence-expired')).toBeInTheDocument()
        );
    });

    it('labels every kind the control plane accepts, never a raw key', async () => {
        // risk_assessment, data_governance and dpia are labelled like the
        // rest; none falls through to the raw `item.kind`.
        mockFetch.mockResolvedValue(
            listing([
                item({ id: 'ev-risk', kind: 'risk_assessment' }),
                item({ id: 'ev-data', kind: 'data_governance' }),
                item({ id: 'ev-dpia', kind: 'dpia' }),
            ])
        );
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('evidence-ev-risk')).toBeInTheDocument()
        );
        expect(screen.getByTestId('evidence-ev-risk').textContent).toMatch(
            /Risk assessment/
        );
        expect(screen.getByTestId('evidence-ev-data').textContent).toMatch(
            /Data governance/
        );
        expect(screen.getByTestId('evidence-ev-dpia').textContent).toMatch(
            /Data protection impact assessment \(DPIA\)/
        );
        for (const id of ['ev-risk', 'ev-data', 'ev-dpia']) {
            expect(screen.getByTestId(`evidence-${id}`).textContent).not.toMatch(
                /risk_assessment|data_governance|dpia/
            );
        }
    });

    it('offers every accepted kind in the attach dropdown', async () => {
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());
        fireEvent.click(screen.getByTestId('evidence-add-toggle'));
        const select = screen.getByTestId('evidence-kind');
        const values = Array.from(select.querySelectorAll('option')).map((o) => o.value);
        expect(values).toEqual(EVIDENCE_KINDS);
    });

    it('attaches with independent omitted when unstated, and notifies upward', async () => {
        const onChanged = vi.fn();
        render(<EvidencePanel aiSystemId="sys-1" onChanged={onChanged} />);
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());

        fireEvent.click(screen.getByTestId('evidence-add-toggle'));
        fireEvent.change(screen.getByTestId('evidence-title'), {
            target: { value: 'FRIA 2026' },
        });
        fireEvent.click(screen.getByTestId('evidence-save'));

        await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
        const [, payload] = mockCreate.mock.calls[0];
        expect(payload.title).toBe('FRIA 2026');
        expect('independent' in payload).toBe(false);
        await waitFor(() => expect(onChanged).toHaveBeenCalled());
    });

    it('sends independent=true when stated', async () => {
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());

        fireEvent.click(screen.getByTestId('evidence-add-toggle'));
        fireEvent.change(screen.getByTestId('evidence-title'), {
            target: { value: 'External audit' },
        });
        fireEvent.change(screen.getByTestId('evidence-independent'), {
            target: { value: 'yes' },
        });
        fireEvent.click(screen.getByTestId('evidence-save'));

        await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
        expect(mockCreate.mock.calls[0][1].independent).toBe(true);
    });

    it('refuses to save without a title', async () => {
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());
        fireEvent.click(screen.getByTestId('evidence-add-toggle'));
        expect(screen.getByTestId('evidence-save')).toBeDisabled();
    });

    it('detaches and notifies upward', async () => {
        const onChanged = vi.fn();
        mockFetch.mockResolvedValue(listing([item({ id: 'ev-x' })]));
        render(<EvidencePanel aiSystemId="sys-1" onChanged={onChanged} />);
        await waitFor(() =>
            expect(screen.getByTestId('evidence-delete-ev-x')).toBeInTheDocument()
        );
        fireEvent.click(screen.getByTestId('evidence-delete-ev-x'));
        await waitFor(() =>
            expect(mockDelete).toHaveBeenCalledWith('sys-1', 'ev-x')
        );
        await waitFor(() => expect(onChanged).toHaveBeenCalled());
    });

    it('surfaces a load failure instead of rendering an empty success', async () => {
        mockFetch.mockRejectedValue(new Error('boom'));
        render(<EvidencePanel aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
        expect(screen.queryByTestId('evidence-empty')).not.toBeInTheDocument();
    });
});
