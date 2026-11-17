/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Assurance Inbox — the evidence kinds. Each renders with its
 * label and icon, explains itself in the drawer, and links to where it is
 * worked; every link target is a tab a screen actually answers (an unrouted
 * id is a silent no-op).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import type { AssuranceInboxItem, InboxCategory } from '../../../lib/assuranceInboxTypes';
import { INBOX_CATEGORIES } from '../../../lib/assuranceInboxTypes';
import { EVIDENCE_INBOX_KINDS, evidenceInboxLinks, type EvidenceInboxKind } from '../evidenceInboxKinds';

const mockList = vi.fn();
const mockCounts = vi.fn();
const mockGet = vi.fn();
const gatewayClient = { listAssuranceInbox: mockList, getAssuranceInboxCounts: mockCounts, getAssuranceInboxItem: mockGet };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient }) }));

import AssuranceInbox from '../AssuranceInbox';
import { SCREENS, resolveScreen } from '../../../app/screens';

const KINDS = Object.keys(EVIDENCE_INBOX_KINDS) as EvidenceInboxKind[];

const EVIDENCE: Record<EvidenceInboxKind, Record<string, unknown>> = {
    evidence_unwitnessed: { log_id: 'log-1', heads: 3 },
    capture_gap: { record_type: 'ai.brutor.declaration', error: 'core_unreachable' },
    judge_uncalibrated: { check_id: 'chk-7', observed_agreement: 0.7, n: 20 },
    calibration_stalled: { check_id: 'chk-8', oldest_pending_at: '2026-09-01T00:00:00Z' },
    incident_deadline: { incident_id: 'inc-1', framework_id: 'eu-ai-act', rule: 'art73', due_at: '2026-09-20T00:00:00Z' },
};

const EXPECTED_TAB: Record<EvidenceInboxKind, string> = {
    evidence_unwitnessed: 'config:tenant:evidence',
    capture_gap: 'compliance-evidence',
    judge_uncalibrated: 'inbox:reviews:chk-7',
    calibration_stalled: 'inbox:reviews:chk-8',
    incident_deadline: 'compliance-incidents',
};

const item = (category: InboxCategory): AssuranceInboxItem => ({
    id: `item-${category}`,
    tenant_id: 't1',
    ai_system_id: null,
    category,
    severity: 'warning',
    fingerprint: `${category}:x`,
    title: `Finding ${category}`,
    evidence: EVIDENCE[category as EvidenceInboxKind] ?? {},
    occurrence_count: 1,
    first_seen_at: '2026-09-19T10:00:00Z',
    last_seen_at: '2026-09-19T10:00:00Z',
    state: 'open',
    acknowledged_at: null,
    acknowledged_by: null,
    assignee_admin: null,
    disposition: null,
    resolution_note: null,
    resolved_at: null,
    resolved_by: null,
});

beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({ items: KINDS.map(item), total: KINDS.length, limit: 100, offset: 0 });
    mockCounts.mockResolvedValue({ open: 5, acknowledged: 0, resolved: 0, open_critical: 0 });
});

describe('evidence inbox kinds', () => {
    it('are all in the pinned inbox vocabulary', () => {
        for (const k of KINDS) expect(INBOX_CATEGORIES).toContain(k);
    });

    it('render in the list with their label and icon', async () => {
        render(<AssuranceInbox />);
        await screen.findAllByTestId('inbox-row');
        for (const k of KINDS) {
            const chips = screen.getAllByTestId(`inbox-category-${k}`);
            expect(chips[0].querySelector('svg')).not.toBeNull();
            expect(chips[0].textContent).not.toBe(k); // a label, not the raw id
        }
        expect(screen.getAllByTestId('inbox-category-capture_gap')[0]).toHaveTextContent('Capture gap');
    });

    it.each(KINDS)('%s opens with its meaning and links to where it is worked', async (kind) => {
        mockGet.mockResolvedValue({ ...item(kind), events: [] });
        const setActiveTab = vi.fn();
        render(<AssuranceInbox setActiveTab={setActiveTab} />);
        const rows = await screen.findAllByTestId('inbox-row');
        fireEvent.click(rows[KINDS.indexOf(kind)]);
        expect(await screen.findByTestId('inbox-kind-meaning')).toHaveTextContent(EVIDENCE_INBOX_KINDS[kind].meaning);
        const [link] = evidenceInboxLinks(kind, EVIDENCE[kind]);
        fireEvent.click(screen.getByRole('button', { name: link.label }));
        expect(setActiveTab).toHaveBeenCalledWith(EXPECTED_TAB[kind]);
    });

    it('judge findings without a check id fall back to the inbox page', () => {
        expect(evidenceInboxLinks('judge_uncalibrated', {})).toEqual([{ label: 'Open the blind review queue', tab: 'inbox' }]);
        expect(evidenceInboxLinks('drift', { check_id: 'x' })).toEqual([]);
    });

    it('every link target is answered by a screen', () => {
        for (const k of KINDS) {
            const tab = EXPECTED_TAB[k];
            expect(resolveScreen(SCREENS, tab), `${k} → ${tab} has no screen`).toBeDefined();
        }
    });
});
