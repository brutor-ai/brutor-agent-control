/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Transparency: notice config per scope (texts per locale with digests,
 * surface, enabled, exemption with basis and rationale ⇒ a sealed
 * declaration), the effective config, the transparency statements as
 * n of m with their limits, and the plain statement that content-level
 * marking is not provided.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const listNoticeConfigs = vi.fn();
const getEffectiveNoticeConfig = vi.fn();
const getComplianceTransparency = vi.fn();
const putNoticeConfig = vi.fn();
const deleteNoticeConfig = vi.fn();
const fetchAISystems = vi.fn();

const gateway = {
    gatewayClient: { listNoticeConfigs, getEffectiveNoticeConfig, getComplianceTransparency, putNoticeConfig, deleteNoticeConfig },
    fetchAISystems,
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../RecordDialog', () => ({
    default: (p: { recordId: string }) => <div data-testid="record-dialog-stub">{p.recordId}</div>,
}));

import TransparencyPage from '../pages/TransparencyPage';
import { PORTAL_LOCALES } from '../../../lib/complianceEvidenceTypes';

const TENANT_CONFIG = {
    id: 'nc-tenant', tenant_id: 'default', ai_system_id: null, enabled: true, surface: 'both',
    texts: { en: 'You are talking to an AI.' }, text_digests: { en: 'a1b2c3d4e5f6a7b8c9d0' }, exemption: null,
    record_id: 'rec-nc-tenant', updated_by: 'admin', updated_at: '2026-09-19T09:00:00Z',
};
const SYSTEM_CONFIG = {
    id: 'nc-sys', tenant_id: 'default', ai_system_id: 'sys-1', enabled: false, surface: 'inline', texts: {}, text_digests: {},
    exemption: { asserted: true, basis: 'no_natural_person', rationale: 'Batch only', declaration_record_id: 'rec-decl' },
    record_id: null, updated_by: 'admin', updated_at: '2026-09-18T09:00:00Z',
};
const SUMMARY = (scope: string | null) => ({
    tenant_id: 'default', ai_system_id: scope, window_start: '2026-08-23T00:00:00Z', window_end: '2026-09-22T00:00:00Z',
    config: { ...TENANT_CONFIG, source: 'tenant' },
    notice_evidence: { key: 'notice.shown', statement: 'AI-interaction notice shown in n of m conversations', basis: 'recomputed', status: 'not_met', n: 40, m: 60, detail: '', limit: 'Counts notices the gateway rendered.', citations: ['rec-n1'], facts: {} },
    notice_text: { key: 'notice.text', statement: 'Notice text declared per locale', basis: 'declared', status: 'declared', n: null, m: null, detail: '1 locale', limit: null, citations: ['rec-nc-tenant'], facts: {} },
    synthetic_content: { key: 'synthetic.marking', statement: 'Synthetic-content marking approach', basis: 'declared', status: 'declared', n: null, m: null, detail: 'X-Brutor-AI-Generated header only', limit: 'Content-level marking (watermarking, C2PA) is not provided.', citations: [], facts: {} },
    generates_synthetic_content: true, interacts_with_natural_persons: null,
    limits: ['Notice evidence counts conversations through the Brutor portal.', 'Content-level marking (watermarking, C2PA) is not provided; outputs carry an X-Brutor-AI-Generated header only.'],
});

const props = () => ({ framework: 'eu-ai-act', frameworks: [], registry: [], refreshFrameworks: vi.fn(), onTabChange: vi.fn(), openObligation: vi.fn() });

beforeEach(() => {
    vi.clearAllMocks();
    listNoticeConfigs.mockResolvedValue({ items: [TENANT_CONFIG, SYSTEM_CONFIG], total: 2 });
    getEffectiveNoticeConfig.mockResolvedValue({ ...SYSTEM_CONFIG, source: 'system' });
    getComplianceTransparency.mockImplementation((p: { ai_system_id?: string }) => Promise.resolve(SUMMARY(p.ai_system_id ?? null)));
    putNoticeConfig.mockResolvedValue({ config: { ...TENANT_CONFIG, record_id: 'rec-new' }, seal: { sealed: true, record_id: 'rec-new' }, declaration: null });
    deleteNoticeConfig.mockResolvedValue({ deleted: true, seal: { sealed: true, record_id: 'rec-del' } });
    fetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-1', name: 'copilot', display_name: 'Copilot', governance_gaps: [] }], total: 1 });
});

describe('TransparencyPage', () => {
    it('shows the tenant default with its texts, digests, evidence as n of m, limits and the marking statement', async () => {
        render(<TransparencyPage {...props()} />);
        await screen.findByTestId('notice-editor');
        expect(screen.getByTestId('notice-editor')).toHaveTextContent('Notice — tenant default');
        expect(screen.getByTestId('notice-config-record')).toHaveTextContent('rec-nc-tenant');
        expect(screen.getByTestId<HTMLTextAreaElement>('notice-text-en').value).toBe('You are talking to an AI.');
        expect(screen.getByTestId('notice-digest-en')).toHaveTextContent('digest a1b2c3d4e5f6');
        for (const l of PORTAL_LOCALES) expect(screen.getByTestId(`notice-text-${l.code}`)).toBeInTheDocument();
        expect(PORTAL_LOCALES).toHaveLength(8);

        expect(getComplianceTransparency).toHaveBeenCalledWith(expect.objectContaining({ ai_system_id: undefined }));
        const evidence = screen.getByTestId('transparency-statements-notice.shown');
        expect(within(evidence).getByTestId('transparency-statements-notice.shown-nofm')).toHaveTextContent('40 of 60');
        expect(within(evidence).getByTestId('transparency-statements-notice.shown-basis')).toHaveTextContent('RECOMPUTED');
        expect(screen.getByTestId('transparency-statements-synthetic.marking-limit')).toHaveTextContent('Content-level marking (watermarking, C2PA) is not provided.');
        expect(screen.getByTestId('transparency-limits')).toHaveTextContent('X-Brutor-AI-Generated header only');
        expect(screen.getByTestId('compliance-transparency')).toHaveTextContent('Content-level marking of outputs is not provided');
        expect(screen.getByTestId('transparency-flags')).toHaveTextContent('Interacts with natural persons: not declared · generates synthetic content: yes');
        expect(screen.getByTestId('transparency-config-sys-1')).toHaveTextContent('exemption declared');
        expect(screen.getByTestId('transparency-config-sys-1')).toHaveTextContent('not sealed');
    });

    it('saves the tenant config with an exemption and shows both seals', async () => {
        putNoticeConfig.mockResolvedValueOnce({
            config: TENANT_CONFIG, seal: { sealed: true, record_id: 'rec-cfg' }, declaration: { sealed: true, record_id: 'rec-decl-new' },
        });
        render(<TransparencyPage {...props()} />);
        await screen.findByTestId('notice-editor');
        fireEvent.change(screen.getByTestId('notice-surface'), { target: { value: 'banner' } });
        fireEvent.change(screen.getByTestId('notice-text-de'), { target: { value: 'Sie sprechen mit einer KI.' } });
        fireEvent.click(screen.getByTestId('notice-exemption-asserted'));
        fireEvent.click(screen.getByTestId('notice-save'));
        expect(await screen.findByTestId('notice-error')).toHaveTextContent('An asserted exemption needs a basis.');
        expect(putNoticeConfig).not.toHaveBeenCalled();
        fireEvent.change(screen.getByTestId('notice-exemption-basis'), { target: { value: 'obvious_from_context' } });
        fireEvent.change(screen.getByTestId('notice-exemption-rationale'), { target: { value: 'The banner says AI.' } });
        fireEvent.click(screen.getByTestId('notice-save'));
        await screen.findByTestId('notice-saved');
        expect(putNoticeConfig).toHaveBeenCalledWith(
            {
                enabled: true, surface: 'banner', texts: { en: 'You are talking to an AI.', de: 'Sie sprechen mit einer KI.' },
                exemption: { asserted: true, basis: 'obvious_from_context', rationale: 'The banner says AI.' },
            },
            undefined,
        );
        expect(screen.getByTestId('notice-seal')).toHaveTextContent('sealed as record');
        expect(screen.getByTestId('notice-declaration-seal')).toHaveTextContent('exemption declaration is sealed');
        await waitFor(() => expect(listNoticeConfigs).toHaveBeenCalledTimes(2));
    });

    it('an unsealed save is shown as saved but not sealed', async () => {
        putNoticeConfig.mockResolvedValueOnce({ config: TENANT_CONFIG, seal: { sealed: false, seal_error: 'core_unreachable', seal_detail: 'No record exists yet.' }, declaration: null });
        render(<TransparencyPage {...props()} />);
        await screen.findByTestId('notice-editor');
        fireEvent.click(screen.getByTestId('notice-save'));
        expect(await screen.findByTestId('notice-seal')).toHaveTextContent('saved but not sealed (core_unreachable)');
        expect(screen.getByTestId('notice-seal')).toHaveTextContent('No record exists yet.');
    });

    it('switches to a system override, shows its declaration record, saves to that scope and removes it', async () => {
        Object.defineProperty(window, 'confirm', { value: vi.fn(() => true), configurable: true, writable: true });
        render(<TransparencyPage {...props()} />);
        await screen.findByTestId('notice-editor');
        fireEvent.change(await screen.findByTestId('transparency-scope'), { target: { value: 'sys-1' } });
        await waitFor(() => expect(screen.getByTestId('notice-editor')).toHaveTextContent('Notice — Copilot'));
        expect(getEffectiveNoticeConfig).toHaveBeenCalledWith('sys-1');
        expect(getComplianceTransparency).toHaveBeenLastCalledWith(expect.objectContaining({ ai_system_id: 'sys-1' }));
        expect(screen.getByTestId('notice-exemption-asserted')).toBeChecked();
        expect(screen.getByTestId('notice-exemption-record')).toHaveTextContent('rec-decl');
        expect(screen.getByTestId('notice-editor')).toHaveTextContent('Stored but not sealed');
        fireEvent.click(screen.getByTestId('notice-save'));
        await waitFor(() => expect(putNoticeConfig).toHaveBeenCalledWith(expect.objectContaining({ enabled: false, surface: 'inline' }), 'sys-1'));
        fireEvent.click(screen.getByTestId('notice-delete'));
        await waitFor(() => expect(deleteNoticeConfig).toHaveBeenCalledWith('nc-sys'));
        expect(await screen.findByTestId('notice-delete-seal')).toHaveTextContent('removal is sealed');
    });

    it('reports failures independently', async () => {
        listNoticeConfigs.mockRejectedValueOnce(new Error('configs down'));
        getComplianceTransparency.mockRejectedValueOnce(new Error('summary down'));
        render(<TransparencyPage {...props()} />);
        expect(await screen.findByTestId('transparency-configs-error')).toHaveTextContent('configs down');
        expect(screen.getByTestId('transparency-summary-error')).toHaveTextContent('summary down');
        expect(screen.getByTestId('notice-editor')).toBeInTheDocument();
    });
});

describe('TransparencyPage — the notice a system resolves to', () => {
    it('says why it could not be loaded, beside the editor', async () => {
        getEffectiveNoticeConfig.mockRejectedValue(new Error('resolver down'));
        render(<TransparencyPage {...props()} />);
        fireEvent.change(await screen.findByTestId('transparency-scope'), { target: { value: 'sys-1' } });
        expect(await screen.findByTestId('transparency-effective-error')).toHaveTextContent(
            'Failed to load the notice this system resolves to: resolver down',
        );
        expect(screen.getByTestId('notice-editor')).toBeInTheDocument();
    });
});
