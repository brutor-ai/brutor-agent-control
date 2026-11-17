/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const buildEvidenceBundle = vi.fn();

const gateway = { gatewayClient: { buildEvidenceBundle } };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../complianceFormat', async (importOriginal) => {
    const actual = await importOriginal<typeof import('../complianceFormat')>();
    return { ...actual, downloadJson: vi.fn() };
});

import BundleExportPanel from '../BundleExportPanel';
import { downloadJson } from '../complianceFormat';
import type { ComplianceFramework } from '../../../lib/complianceEvidenceTypes';

const FRAMEWORKS = [{ id: 'eu-ai-act', title: 'EU AI Act' }, { id: 'gdpr', title: 'GDPR' }] as unknown as ComplianceFramework[];
const SYSTEMS = [{ id: 'sys-1', name: 'Copilot' }];

const BUILT = {
    bundle_id: 'b-1', bundle_digest: 'f'.repeat(64), record_id: 'rec-bundle',
    bundle: { type: 'ai.brutor/evidence-bundle/v1', records: [{ record_id: 'a' }, { record_id: 'b' }], manifest: { completeness: { missing: ['c'] } } },
    missing: ['c'], record_count: 2,
};

beforeEach(() => {
    vi.clearAllMocks();
    buildEvidenceBundle.mockResolvedValue(BUILT);
});

describe('BundleExportPanel', () => {
    it('builds a bundle over record ids with the selected framework, shows the digest, downloads it', async () => {
        const onOpenRecord = vi.fn();
        render(<BundleExportPanel systems={SYSTEMS} frameworks={FRAMEWORKS} framework="eu-ai-act" onOpenRecord={onOpenRecord} />);
        fireEvent.change(screen.getByTestId('bundle-record-ids'), { target: { value: 'a\nb\na' } });
        fireEvent.change(screen.getByTestId('bundle-depth'), { target: { value: '2' } });
        fireEvent.click(screen.getByTestId('bundle-build'));
        await screen.findByTestId('bundle-result');
        expect(buildEvidenceBundle).toHaveBeenCalledWith({ selector: { record_ids: ['a', 'b'] }, audience: null, framework_id: 'eu-ai-act', closure_depth: 2 });
        expect(screen.getByTestId('bundle-digest')).toHaveTextContent('f'.repeat(64));
        expect(screen.getByTestId('bundle-result')).toHaveTextContent('2 records');
        expect(screen.getByTestId('bundle-result')).toHaveTextContent('1 referenced record(s) not held');
        fireEvent.click(screen.getByTestId('bundle-download'));
        expect(downloadJson).toHaveBeenCalledWith(BUILT.bundle, `evidence-bundle-${'f'.repeat(16)}.json`);
        fireEvent.click(screen.getByTestId('record-link-rec-bundle'));
        expect(onOpenRecord).toHaveBeenCalledWith('rec-bundle');
        expect(screen.getByTestId('bundle-offline-link')).toHaveAttribute('href', 'https://docs.brutor.ai/tools/verify/');
    });

    it('a run selector with an audience seals a disclosure and tells the list to reload', async () => {
        const onDisclosed = vi.fn();
        render(<BundleExportPanel systems={SYSTEMS} frameworks={FRAMEWORKS} framework="eu-ai-act" onDisclosed={onDisclosed} />);
        fireEvent.click(screen.getByTestId('bundle-selector-root_task_id'));
        fireEvent.change(screen.getByTestId('bundle-run-id'), { target: { value: 'run-7' } });
        fireEvent.change(screen.getByTestId('bundle-audience'), { target: { value: 'external auditor' } });
        fireEvent.change(screen.getByTestId('bundle-framework'), { target: { value: 'gdpr' } });
        fireEvent.click(screen.getByTestId('bundle-build'));
        await screen.findByTestId('bundle-result');
        expect(buildEvidenceBundle).toHaveBeenCalledWith({ selector: { root_task_id: 'run-7' }, audience: 'external auditor', framework_id: 'gdpr', closure_depth: 1 });
        expect(onDisclosed).toHaveBeenCalled();
        expect(screen.getByTestId('bundle-result')).toHaveTextContent('disclosed to external auditor');
    });

    it('a system selector needs a window; the request carries the day bounds', async () => {
        render(<BundleExportPanel systems={SYSTEMS} frameworks={FRAMEWORKS} framework="eu-ai-act" />);
        fireEvent.click(screen.getByTestId('bundle-selector-ai_system_id'));
        fireEvent.click(screen.getByTestId('bundle-build'));
        expect(screen.getByTestId('bundle-input-error')).toHaveTextContent('Choose an AI System.');
        fireEvent.change(screen.getByTestId('bundle-system'), { target: { value: 'sys-1' } });
        fireEvent.change(screen.getByTestId('bundle-from'), { target: { value: '2026-09-01' } });
        fireEvent.change(screen.getByTestId('bundle-to'), { target: { value: '2026-09-30' } });
        fireEvent.click(screen.getByTestId('bundle-build'));
        await screen.findByTestId('bundle-result');
        expect(buildEvidenceBundle).toHaveBeenCalledWith(
            expect.objectContaining({ selector: { ai_system_id: 'sys-1', from: '2026-09-01T00:00:00Z', to: '2026-09-30T23:59:59Z' } }),
        );
    });

    it('shows a build failure and an unsealed bundle honestly', async () => {
        buildEvidenceBundle.mockRejectedValueOnce(new Error('core refused'));
        render(<BundleExportPanel systems={SYSTEMS} frameworks={FRAMEWORKS} framework="eu-ai-act" initialRecordIds={['a']} />);
        fireEvent.click(screen.getByTestId('bundle-build'));
        expect(await screen.findByTestId('bundle-error')).toHaveTextContent('core refused');

        buildEvidenceBundle.mockResolvedValueOnce({ ...BUILT, record_id: null, bundle_digest: null });
        fireEvent.click(screen.getByTestId('bundle-build'));
        await waitFor(() => expect(screen.getByTestId('bundle-result')).toHaveTextContent('No sealed record was returned'));
        expect(screen.getByTestId('bundle-digest')).toHaveTextContent('— (not stated)');
    });
});
