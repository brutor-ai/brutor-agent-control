/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI System detail → Compliance: the console's per-system
 * panel, then the profile's seal state, the latest report per framework,
 * the notice config summary and the documentation export — which is
 * generated only on request, because every changed export seals a record.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const getAISystemComplianceProfile = vi.fn();
const listComplianceFrameworks = vi.fn();
const listSystemComplianceReports = vi.fn();
const getEffectiveNoticeConfig = vi.fn();
const listSystemDocumentationExports = vi.fn();
const exportSystemDocumentation = vi.fn();
const exportSystemDocumentationHtml = vi.fn();
const downloadJson = vi.fn();

const gateway = {
    gatewayClient: {
        getAISystemComplianceProfile, listComplianceFrameworks, listSystemComplianceReports, getEffectiveNoticeConfig,
        listSystemDocumentationExports, exportSystemDocumentation, exportSystemDocumentationHtml,
    },
};
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../lib/downloadJson', () => ({ downloadJson: (...a: unknown[]) => downloadJson(...a) }));
vi.mock('../../compliance/SystemCompliancePanel', () => ({
    default: (p: { aiSystemId: string }) => <div data-testid="system-compliance-panel-stub">{p.aiSystemId}</div>,
}));

import AISystemComplianceTab, { latestReportPerFramework } from '../AISystemComplianceTab';

const report = (id: string, framework: string, period: string, extra: Record<string, unknown> = {}) => ({
    id, tenant_id: 't', framework_id: framework, ai_system_id: 'sys-1', cadence: 'monthly', period_start: period,
    period_end: period, catalog_version: '2026-09', status: 'ready', summary: {}, record_id: 'ab'.repeat(32),
    bundle_id: null, countersignatures: [], countersignature_status: 'none', ...extra,
});

beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    getAISystemComplianceProfile.mockResolvedValue({
        ai_system_id: 'sys-1', tenant_id: 't', exists: true, sealed: true, declaration_record_id: 'de'.repeat(32), updated_at: '2026-09-19T10:00:00Z',
    });
    listComplianceFrameworks.mockResolvedValue({
        items: [
            { id: 'eu-ai-act', title: 'EU AI Act', offered: true },
            { id: 'iso-42001', title: 'ISO/IEC 42001', offered: true },
        ],
        total: 2,
    });
    listSystemComplianceReports.mockResolvedValue({
        items: [
            report('r3', 'eu-ai-act', '2026-09-01', { countersignature_status: 'awaiting_countersignature' }),
            report('r2', 'iso-42001', '2026-09-01', { record_id: null }),
            report('r1', 'eu-ai-act', '2026-08-01'),
        ],
        total: 3,
    });
    getEffectiveNoticeConfig.mockResolvedValue({
        tenant_id: 't', ai_system_id: 'sys-1', enabled: true, surface: 'banner', texts: { en: 'AI', sv: 'AI' },
        text_digests: {}, exemption: null, record_id: 'no'.repeat(32), source: 'system',
    });
    listSystemDocumentationExports.mockResolvedValue({ items: [], total: 0 });
});

describe('AISystemComplianceTab', () => {
    it('keeps the console panel and adds the evidence cards without exporting anything', async () => {
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        expect(screen.getByTestId('system-compliance-panel-stub')).toHaveTextContent('sys-1');
        await screen.findByTestId('system-compliance-sealed');
        expect(listSystemComplianceReports).toHaveBeenCalledWith('sys-1', { limit: 100 });
        expect(getEffectiveNoticeConfig).toHaveBeenCalledWith('sys-1');
        expect(exportSystemDocumentation).not.toHaveBeenCalled();
    });

    it('shows the latest report per framework with seal and countersignature state', async () => {
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        const eu = await screen.findByTestId('system-report-eu-ai-act');
        expect(eu).toHaveTextContent('EU AI Act');
        expect(eu).toHaveTextContent('2026-09-01');
        expect(eu).toHaveTextContent('awaiting countersignature');
        expect(screen.getByTestId('system-report-iso-42001')).toHaveTextContent('not sealed');
        expect(screen.getAllByTestId(/^system-report-/)).toHaveLength(2);
    });

    it('latestReportPerFramework keeps the first (newest) per framework', () => {
        const out = latestReportPerFramework([report('a', 'x', '2026-09-01'), report('b', 'x', '2026-08-01'), report('c', 'y', '2026-07-01')] as never);
        expect(out.map((r) => r.id)).toEqual(['a', 'c']);
    });

    it('summarises the effective notice config', async () => {
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        const notice = await screen.findByTestId('system-compliance-notice');
        expect(notice).toHaveTextContent('yes — banner');
        expect(notice).toHaveTextContent('this system');
        expect(notice).toHaveTextContent('en, sv');
    });

    it('says when the profile was saved but not sealed', async () => {
        getAISystemComplianceProfile.mockResolvedValue({ ai_system_id: 'sys-1', tenant_id: 't', exists: true, sealed: false, seal_error: 'core_unreachable' });
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        expect(await screen.findByTestId('system-compliance-not-sealed')).toHaveTextContent('core_unreachable');
    });

    it('exports documentation for the chosen framework on request and reports its seal', async () => {
        exportSystemDocumentation.mockResolvedValue({
            id: 'doc-1', ai_system_id: 'sys-1', framework_id: 'iso-42001', template: 'iso', digest: 'dd'.repeat(32),
            record_id: 'ee'.repeat(32), content: {}, skeleton: true, reused: false, seal: { sealed: true, record_id: 'ee'.repeat(32) },
        });
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        const select = await screen.findByTestId('system-doc-framework');
        fireEvent.change(select, { target: { value: 'iso-42001' } });
        fireEvent.click(screen.getByTestId('system-doc-export'));
        await waitFor(() => expect(exportSystemDocumentation).toHaveBeenCalledWith('sys-1', 'iso-42001'));
        expect(downloadJson).toHaveBeenCalledWith('documentation-sys-1-iso-42001.json', expect.objectContaining({ id: 'doc-1' }));
        expect(await screen.findByTestId('system-doc-note')).toHaveTextContent('sealed as eeeeeeee');
    });

    it('an export failure is shown, not swallowed', async () => {
        exportSystemDocumentation.mockRejectedValue(new Error('no contract'));
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('system-doc-export'));
        expect(await screen.findByTestId('system-doc-note')).toHaveTextContent('Export failed: no contract');
    });
});

describe('AISystemComplianceTab — a read that failed', () => {
    it('each card says what the Control Plane said of its own read, and the others stand', async () => {
        getAISystemComplianceProfile.mockRejectedValue(new Error('profile store down'));
        listSystemComplianceReports.mockRejectedValue(new Error('reports forbidden'));
        getEffectiveNoticeConfig.mockRejectedValue(new Error('notice timeout'));
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        expect(await screen.findByTestId('system-compliance-seal')).toHaveTextContent('Failed to load the profile declaration: profile store down');
        expect(screen.getByTestId('system-compliance-reports')).toHaveTextContent('Failed to load the reports: reports forbidden');
        expect(screen.getByTestId('system-compliance-notice')).toHaveTextContent('Failed to load the notice configuration: notice timeout');
        // The documentation card read nothing that failed.
        expect(screen.getByTestId('system-compliance-documentation')).not.toHaveTextContent('Failed to load');
    });

    it('the documentation card says which of its two reads failed, and offers the read again', async () => {
        listComplianceFrameworks.mockRejectedValueOnce(new Error('registry down'));
        listSystemDocumentationExports.mockRejectedValueOnce(new Error('exports down'));
        render(<AISystemComplianceTab aiSystemId="sys-1" />);
        const card = await screen.findByTestId('system-compliance-documentation');
        expect(card).toHaveTextContent('Failed to load the frameworks to export under: registry down');
        expect(card).toHaveTextContent('Failed to load the earlier exports: exports down');
        fireEvent.click(within(card).getAllByRole('button', { name: 'Retry' })[0]);
        await waitFor(() => expect(card).not.toHaveTextContent('Failed to load'));
    });
});
