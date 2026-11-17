/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Incidents: the register with per-framework deadline chips (shown, never
 * enforced), create / edit / report / close / note, the sealed timeline
 * with record ids, and the seal block of every write shown honestly.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const listComplianceIncidents = vi.fn();
const getComplianceIncident = vi.fn();
const createComplianceIncident = vi.fn();
const updateComplianceIncident = vi.fn();
const reportComplianceIncident = vi.fn();
const closeComplianceIncident = vi.fn();
const addComplianceIncidentNote = vi.fn();
const recordComplianceIncidentSubmission = vi.fn();
const fetchAISystems = vi.fn();

const gateway = {
    gatewayClient: { listComplianceIncidents, getComplianceIncident, createComplianceIncident, updateComplianceIncident, reportComplianceIncident, closeComplianceIncident, addComplianceIncidentNote, recordComplianceIncidentSubmission },
    fetchAISystems,
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../RecordDialog', () => ({
    default: (p: { recordId: string }) => <div data-testid="record-dialog-stub">{p.recordId}</div>,
}));

import IncidentsPage from '../pages/IncidentsPage';
import type { ComplianceFramework, ComplianceIncident } from '../../../lib/complianceEvidenceTypes';

const FRAMEWORKS = [
    { id: 'eu-ai-act', title: 'EU AI Act', enabled: true },
    { id: 'gdpr', title: 'GDPR', enabled: true },
    { id: 'hipaa', title: 'HIPAA', enabled: false },
] as unknown as ComplianceFramework[];

const INCIDENT: ComplianceIncident = {
    id: 'inc-1', tenant_id: 'default', ai_system_id: 'sys-1', title: 'Model leaked PII', description: 'A prompt echoed a customer record.',
    classification: 'serious_incident', severity: 'high', became_aware_at: '2026-09-19T08:00:00Z', frameworks: ['eu-ai-act', 'gdpr'],
    deadlines: {}, deadlines_enforced: false, status: 'open', reported_at: null, authority: null, external_reference: null,
    linked_run_ids: ['run-1'], linked_inbox_item_ids: ['ibx-1'], last_record_id: 'rec-inc-1',
    classified_at: null, created_at: null, created_by: null, stage_submissions: {}, updated_at: null,
    deadline_status: [
        { framework_id: 'gdpr', anchor: 'became_aware_at', staged: false, rule: 'breach_notification', stage: 'report', due_at: '2026-09-22T08:00:00Z', status: 'due_soon', hours_remaining: 5.5, anchor_at: null, submitted_at: null, waiting_for: null },
        { framework_id: 'eu-ai-act', anchor: 'became_aware_at', staged: false, rule: 'serious_incident', stage: 'report', due_at: '2026-10-04T08:00:00Z', status: 'open', hours_remaining: 300, anchor_at: null, submitted_at: null, waiting_for: null },
    ],
};
const DETAIL = {
    ...INCIDENT,
    events: [
        { id: 'ev-1', incident_id: 'inc-1', tenant_id: 'default', event: 'opened', at: '2026-09-19T08:05:00Z', actor_id: 'admin', detail: {}, record_id: 'rec-inc-1' },
        { id: 'ev-2', incident_id: 'inc-1', tenant_id: 'default', event: 'note_added', at: '2026-09-19T09:00:00Z', actor_id: 'admin', detail: { note: 'DPO informed' }, record_id: null },
    ],
};
const CLOSED = { ...INCIDENT, id: 'inc-2', title: 'Old one', status: 'closed', last_record_id: null, deadline_status: [], frameworks: ['gdpr'] };

const change = (incident: ComplianceIncident, seal: Record<string, unknown>, event = 'updated') => ({
    incident, event: { id: 'ev-x', incident_id: incident.id, tenant_id: 'default', event, at: '2026-09-19T10:00:00Z', actor_id: 'admin', detail: {}, record_id: seal.record_id ?? null }, seal,
});

const props = () => ({ framework: 'eu-ai-act', frameworks: FRAMEWORKS, registry: FRAMEWORKS, refreshFrameworks: vi.fn(), onTabChange: vi.fn(), setActiveTab: vi.fn() });

beforeEach(() => {
    vi.clearAllMocks();
    listComplianceIncidents.mockResolvedValue({ items: [INCIDENT, CLOSED], total: 2 });
    getComplianceIncident.mockResolvedValue(DETAIL);
    fetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-1', name: 'copilot', display_name: 'Copilot', governance_gaps: [] }], total: 1 });
});

describe('IncidentsPage', () => {
    it('lists the register with the lead deadline per row and says deadlines are shown, not enforced', async () => {
        render(<IncidentsPage {...props()} />);
        await screen.findByTestId('incident-row-inc-1');
        expect(screen.getByTestId('compliance-incidents')).toHaveTextContent('Deadlines shown, not enforced.');
        const row = screen.getByTestId('incident-row-inc-1');
        expect(row).toHaveTextContent('serious_incident');
        expect(row).toHaveTextContent('high');
        expect(row).toHaveTextContent('eu-ai-act, gdpr');
        expect(row).toHaveTextContent('sealed');
        expect(screen.getByTestId('incident-deadline-inc-1')).toHaveTextContent('gdpr report: due soon · 5 h left');
        expect(screen.getByTestId('incident-deadline-inc-2')).toHaveTextContent('no rule applies');
        expect(screen.getByTestId('incident-row-inc-2')).toHaveTextContent('not sealed');
        await waitFor(() => expect(row).toHaveTextContent('Copilot'));
        fireEvent.change(screen.getByTestId('incidents-status'), { target: { value: 'open' } });
        await waitFor(() => expect(listComplianceIncidents).toHaveBeenLastCalledWith({ status: 'open', framework: undefined }));
        fireEvent.click(screen.getByTestId('incidents-only-framework'));
        await waitFor(() => expect(listComplianceIncidents).toHaveBeenLastCalledWith({ status: 'open', framework: 'eu-ai-act' }));
    });

    it('creates an incident with the enabled frameworks in scope and shows its seal', async () => {
        createComplianceIncident.mockResolvedValueOnce(change({ ...INCIDENT, id: 'inc-9' }, { sealed: true, record_id: 'rec-inc-9' }, 'opened'));
        getComplianceIncident.mockResolvedValueOnce({ ...DETAIL, id: 'inc-9' });
        render(<IncidentsPage {...props()} />);
        await screen.findByTestId('incident-row-inc-1');
        fireEvent.click(screen.getByTestId('incidents-new'));
        const form = screen.getByTestId('incident-form');
        expect(within(form).queryByTestId('incident-framework-hipaa')).toBeNull(); // not enabled
        fireEvent.click(screen.getByTestId('incident-submit'));
        expect(screen.getByTestId('incident-form-error')).toHaveTextContent('A title is required.');
        fireEvent.change(screen.getByTestId('incident-title'), { target: { value: 'Prompt injection via ticket' } });
        fireEvent.change(screen.getByTestId('incident-classification'), { target: { value: 'serious_incident' } });
        fireEvent.change(screen.getByTestId('incident-severity'), { target: { value: 'critical' } });
        fireEvent.change(screen.getByTestId('incident-became-aware'), { target: { value: '2026-09-20T09:30' } });
        fireEvent.click(screen.getByTestId('incident-framework-eu-ai-act'));
        fireEvent.change(await screen.findByTestId('incident-system'), { target: { value: 'sys-1' } });
        fireEvent.change(screen.getByTestId('incident-runs'), { target: { value: 'run-5\nrun-6' } });
        fireEvent.click(screen.getByTestId('incident-submit'));
        await screen.findByTestId('incident-detail');
        expect(createComplianceIncident).toHaveBeenCalledWith(
            expect.objectContaining({ title: 'Prompt injection via ticket', classification: 'serious_incident', severity: 'critical', frameworks: ['eu-ai-act'], ai_system_id: 'sys-1', linked_run_ids: ['run-5', 'run-6'] }),
        );
        expect(screen.getByTestId('incident-seal')).toHaveTextContent('incident is sealed as record');
        await waitFor(() => expect(listComplianceIncidents).toHaveBeenCalledTimes(2));
    });

    it('opens an incident: facts, deadlines, sealed history with record links, and adds a note', async () => {
        addComplianceIncidentNote.mockResolvedValueOnce(change(INCIDENT, { sealed: true, record_id: 'rec-note' }, 'note_added'));
        const p = props();
        render(<IncidentsPage {...p} />);
        fireEvent.click(await screen.findByTestId('incident-row-inc-1'));
        await screen.findByTestId('incident-detail');
        expect(getComplianceIncident).toHaveBeenCalledWith('inc-1');
        expect(screen.getByTestId('incident-detail')).toHaveTextContent('Deadlines shown, not enforced.');
        expect(screen.getByTestId('incident-deadline-gdpr-breach_notification')).toHaveTextContent('due soon · 5 h left');
        expect(screen.getByTestId('incident-deadline-eu-ai-act-serious_incident')).toHaveTextContent('open · 12 d 12 h left');
        expect(screen.getByTestId('incident-event-ev-1')).toHaveTextContent('opened');
        expect(screen.getByTestId('incident-event-ev-2')).toHaveTextContent('note added · DPO informed');
        expect(screen.getByTestId('incident-event-ev-2')).toHaveTextContent('not sealed');
        fireEvent.click(within(screen.getByTestId('incident-event-ev-1')).getByTestId('record-link-rec-inc-1'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-inc-1');
        fireEvent.click(within(screen.getByTestId('incident-facts')).getByText('1 item →'));
        expect(p.setActiveTab).toHaveBeenCalledWith('inbox');

        fireEvent.change(screen.getByTestId('incident-note'), { target: { value: 'Regulator called back' } });
        fireEvent.click(screen.getByTestId('incident-note-submit'));
        await waitFor(() => expect(addComplianceIncidentNote).toHaveBeenCalledWith('inc-1', 'Regulator called back'));
        await waitFor(() => expect(screen.getByTestId('incident-seal')).toHaveTextContent('note is sealed as record'));
        expect(screen.getByTestId<HTMLInputElement>('incident-note').value).toBe('');
    });

    it('records the report to an authority, then closes; an unsealed write says so', async () => {
        reportComplianceIncident.mockResolvedValueOnce(change({ ...INCIDENT, status: 'reported', authority: 'AI Office', reported_at: '2026-09-21T10:00:00Z' }, { sealed: false, seal_error: 'core_unreachable', seal_detail: 'Retry later.' }, 'reported'));
        closeComplianceIncident.mockResolvedValueOnce(change({ ...INCIDENT, status: 'closed' }, { sealed: true, record_id: 'rec-close' }, 'closed'));
        render(<IncidentsPage {...props()} />);
        fireEvent.click(await screen.findByTestId('incident-row-inc-1'));
        await screen.findByTestId('incident-detail');

        fireEvent.click(screen.getByTestId('incident-report'));
        fireEvent.click(screen.getByTestId('incident-report-submit'));
        expect(await screen.findByTestId('incident-action-error')).toHaveTextContent('Name the authority');
        fireEvent.change(screen.getByTestId('incident-authority'), { target: { value: 'AI Office' } });
        fireEvent.change(screen.getByTestId('incident-reference'), { target: { value: 'REF-42' } });
        fireEvent.click(screen.getByTestId('incident-report-submit'));
        await waitFor(() => expect(reportComplianceIncident).toHaveBeenCalledWith('inc-1', { authority: 'AI Office', reported_at: null, external_reference: 'REF-42' }));
        await waitFor(() => expect(screen.getByTestId('incident-seal')).toHaveTextContent('report is saved but not sealed (core_unreachable)'));
        expect(screen.getByTestId('incident-seal')).toHaveTextContent('Retry later.');

        getComplianceIncident.mockResolvedValueOnce({ ...DETAIL, status: 'reported', authority: 'AI Office', reported_at: '2026-09-21T10:00:00Z' });
        await waitFor(() => expect(getComplianceIncident).toHaveBeenCalledTimes(2));
        fireEvent.click(screen.getByTestId('incident-close'));
        fireEvent.change(screen.getByTestId('incident-close-note'), { target: { value: 'Remediated' } });
        fireEvent.click(screen.getByTestId('incident-close-submit'));
        await waitFor(() => expect(closeComplianceIncident).toHaveBeenCalledWith('inc-1', 'Remediated'));
        await waitFor(() => expect(screen.getByTestId('incident-seal')).toHaveTextContent('closure is sealed as record'));
    });

    it('edits an incident through PATCH', async () => {
        updateComplianceIncident.mockResolvedValueOnce(change({ ...INCIDENT, severity: 'critical' }, { sealed: true, record_id: 'rec-upd' }));
        render(<IncidentsPage {...props()} />);
        fireEvent.click(await screen.findByTestId('incident-row-inc-1'));
        await screen.findByTestId('incident-detail');
        fireEvent.click(screen.getByTestId('incident-edit'));
        expect(screen.getByTestId<HTMLInputElement>('incident-title').value).toBe('Model leaked PII');
        expect(screen.getByTestId('incident-framework-gdpr')).toBeChecked();
        fireEvent.change(screen.getByTestId('incident-severity'), { target: { value: 'critical' } });
        fireEvent.click(screen.getByTestId('incident-submit'));
        await waitFor(() => expect(updateComplianceIncident).toHaveBeenCalledWith('inc-1', expect.objectContaining({ severity: 'critical', frameworks: ['eu-ai-act', 'gdpr'], linked_run_ids: ['run-1'] })));
        await waitFor(() => expect(screen.getByTestId('incident-seal')).toHaveTextContent('change is sealed as record'));
    });

    it('staged (DORA): each stage shows its anchor, waiting stages have no due time, and a submission is recorded per stage', async () => {
        const staged = (rule: string, extra: Record<string, unknown>) => ({ framework_id: 'dora', rule, staged: true, hours_remaining: null, anchor_at: null, waiting_for: null, submitted_at: null, ...extra });
        const DORA = {
            ...DETAIL,
            frameworks: ['dora'],
            classified_at: '2026-09-19T18:00:00Z',
            deadline_status: [
                staged('initial', { stage: 'report', anchor: 'classified', anchor_at: '2026-09-19T18:00:00Z', due_at: '2026-09-19T22:00:00Z', status: 'due_soon', hours_remaining: 3 }),
                staged('intermediate', { stage: 'follow_up', anchor: 'stage:initial', waiting_for: 'initial', due_at: null, status: 'waiting' }),
                staged('final', { stage: 'follow_up', anchor: 'stage:intermediate', waiting_for: 'intermediate', due_at: null, status: 'waiting' }),
            ],
        };
        getComplianceIncident.mockResolvedValue(DORA);
        recordComplianceIncidentSubmission.mockResolvedValueOnce(change(INCIDENT, { sealed: true, record_id: 'rec-sub' }, 'stage_submitted'));
        render(<IncidentsPage {...props()} />);
        fireEvent.click(await screen.findByTestId('incident-row-inc-1'));
        await screen.findByTestId('incident-detail');
        expect(screen.getByTestId('incident-classified-at-value')).not.toHaveTextContent('not recorded');
        expect(screen.getByTestId('incident-deadline-anchor-dora-initial')).toHaveTextContent('from classification');
        expect(screen.getByTestId('incident-deadline-dora-intermediate')).toHaveTextContent('no due time yet');
        expect(screen.getByTestId('incident-deadline-anchor-dora-intermediate')).toHaveTextContent('waiting for the initial submission');
        // Only a stage whose clock runs and is not yet submitted can be recorded.
        expect(screen.queryByTestId('incident-submit-dora-intermediate')).toBeNull();
        fireEvent.click(screen.getByTestId('incident-submit-dora-initial'));
        fireEvent.change(screen.getByTestId('incident-submitted-at-dora-initial'), { target: { value: '2026-09-19T20:00' } });
        fireEvent.click(screen.getByTestId('incident-submit-save-dora-initial'));
        await waitFor(() =>
            expect(recordComplianceIncidentSubmission).toHaveBeenCalledWith('inc-1', {
                framework_id: 'dora', stage: 'initial', submitted_at: new Date('2026-09-19T20:00').toISOString(),
            }),
        );
        await waitFor(() => expect(screen.getByTestId('incident-seal')).toHaveTextContent('submission is sealed as record'));
    });

    it('a closed incident offers no actions; empty and error states', async () => {
        getComplianceIncident.mockResolvedValueOnce({ ...DETAIL, ...CLOSED, events: [] });
        render(<IncidentsPage {...props()} />);
        fireEvent.click(await screen.findByTestId('incident-row-inc-2'));
        await screen.findByTestId('incident-detail');
        expect(screen.queryByTestId('incident-edit')).toBeNull();
        expect(screen.queryByTestId('incident-note-form')).toBeNull();
        expect(screen.getByTestId('incident-deadlines')).toHaveTextContent('No deadline rule');

        listComplianceIncidents.mockResolvedValueOnce({ items: [], total: 0 });
        fireEvent.change(screen.getByTestId('incidents-status'), { target: { value: 'closed' } });
        expect(await screen.findByTestId('incidents-empty')).toHaveTextContent('with status closed');

        listComplianceIncidents.mockRejectedValueOnce(new Error('register down'));
        fireEvent.change(screen.getByTestId('incidents-status'), { target: { value: 'reported' } });
        expect(await screen.findByTestId('incidents-error')).toHaveTextContent('register down');
    });
});
