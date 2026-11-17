/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Systems & Profiles: the profile table from `GET /compliance/systems`
 * (declared / sealed, the selected framework's role and classification),
 * the editor it opens — which edits ONE framework block and carries the
 * others through on save — and the documentation export panel that seals
 * a record per changed document.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const listComplianceSystems = vi.fn();
const getAISystemComplianceProfile = vi.fn();
const putAISystemComplianceProfile = vi.fn();
const listSystemDocumentationExports = vi.fn();
const exportSystemDocumentation = vi.fn();
const getComplianceOverview = vi.fn();
const toast = { success: vi.fn(), error: vi.fn() };

const gateway = {
    gatewayClient: { listComplianceSystems, getAISystemComplianceProfile, putAISystemComplianceProfile, listSystemDocumentationExports, exportSystemDocumentation, getComplianceOverview },
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../RecordDialog', () => ({
    default: (p: { recordId: string; onClose: () => void }) => (
        <div data-testid="record-dialog-stub">
            {p.recordId}
            <button onClick={p.onClose}>close</button>
        </div>
    ),
}));

import ProfilesPage from '../pages/ProfilesPage';
import { clearFrameworkCatalogCache } from '../useFrameworkCatalog';
import type { ComplianceFramework } from '../../../lib/complianceEvidenceTypes';

const FRAMEWORKS = [
    { id: 'eu-ai-act', title: 'EU AI Act', obligation_count: 9 },
    { id: 'hipaa', title: 'HIPAA', obligation_count: 0 },
] as unknown as ComplianceFramework[];

const SYSTEMS = {
    items: [
        { ai_system_id: 'sys-1', tenant_id: 'default', name: 'Copilot', lifecycle_stage: 'production', core: { operator_role: 'operator' }, frameworks: { 'eu-ai-act': { role: 'deployer', risk_tier: 'high' } }, declaration_record_id: 'rec-0001', sealed: true, exists: true, active_frameworks: ['eu-ai-act'], retention_floor_days: 3650, updated_at: '2026-09-19T09:00:00Z' },
        { ai_system_id: 'sys-2', tenant_id: 'default', name: 'Triage', lifecycle_stage: 'pilot', core: {}, frameworks: {}, declaration_record_id: null, sealed: false, exists: false, active_frameworks: [], retention_floor_days: null },
    ],
    frameworks_enabled: ['eu-ai-act'],
};

const PROFILE = {
    ai_system_id: 'sys-1',
    tenant_id: 'default',
    core: { operator_role: 'operator', jurisdictions: ['EU'], interacts_with_natural_persons: true, sensitivity: { personal_data: true } },
    frameworks: { 'eu-ai-act': { role: 'deployer', risk_tier: 'high' }, hipaa: { covered_entity: 'no' } },
    declaration_record_id: 'rec-0001',
    sealed: true,
    exists: true,
    updated_by: 'admin',
    updated_at: '2026-09-19T09:00:00Z',
};

const props = (framework = 'eu-ai-act') => ({
    framework,
    frameworks: FRAMEWORKS,
    registry: FRAMEWORKS,
    refreshFrameworks: vi.fn(),
    onTabChange: vi.fn(),
    setActiveTab: vi.fn(),
});

beforeEach(() => {
    vi.clearAllMocks();
    clearFrameworkCatalogCache();
    getComplianceOverview.mockResolvedValue({
        framework: { id: 'eu-ai-act', title: 'EU AI Act', roles: ['provider', 'deployer', 'provider_and_deployer'], classifications: ['minimal', 'limited', 'high', 'unacceptable'], versions: [], deadline_rules: {}, retention_floors: {} },
        window: { from: '', to: '' }, evidence: { record_lag: {}, open_capture_gap_items: 0 }, timeline: [], systems: [],
    });
    listComplianceSystems.mockResolvedValue(SYSTEMS);
    getAISystemComplianceProfile.mockResolvedValue(PROFILE);
    putAISystemComplianceProfile.mockImplementation((_id: string, body: unknown) =>
        Promise.resolve({ ...PROFILE, ...(body as object), declaration_record_id: 'rec-0002' }),
    );
    listSystemDocumentationExports.mockResolvedValue({ items: [], total: 0 });
    exportSystemDocumentation.mockResolvedValue({ digest: 'd'.repeat(64), template: 'annex_iv', skeleton: true, reused: false, seal: { sealed: true, record_id: 'rec-doc' }, record_id: 'rec-doc' });
});

describe('ProfilesPage', () => {
    it('lists the fleet with the declared / sealed state and the selected framework’s facts', async () => {
        render(<ProfilesPage {...props()} />);
        await screen.findByTestId('compliance-profiles');
        expect(screen.getByTestId('system-profile-sys-1')).toHaveTextContent('sealed');
        expect(screen.getByTestId('system-fw-sys-1')).toHaveTextContent('deployer · high');
        expect(screen.getByTestId('system-profile-sys-2')).toHaveTextContent('not declared');
        expect(screen.getByTestId('system-fw-sys-2')).toHaveTextContent('not declared');
        expect(screen.getByTestId('systems-no-selection')).toBeInTheDocument();
        expect(screen.getByText(/enabled frameworks: eu-ai-act/)).toBeInTheDocument();
    });

    it('opens the profile editor for the selected system and links to its detail page', async () => {
        const p = props();
        render(<ProfilesPage {...p} />);
        await screen.findByTestId('compliance-profiles');
        fireEvent.click(screen.getByTestId('system-row-sys-1'));
        await screen.findByTestId('profile-editor');
        expect(getAISystemComplianceProfile).toHaveBeenCalledWith('sys-1');
        expect(screen.getByTestId<HTMLSelectElement>('profile-operator-role').value).toBe('operator');
        expect(screen.getByTestId<HTMLSelectElement>('profile-eu-role').value).toBe('deployer');
        expect(screen.getByTestId<HTMLSelectElement>('profile-eu-risk-tier').value).toBe('high');
        expect(screen.getByTestId('profile-flag-interacts_with_natural_persons')).toBeChecked();
        expect(screen.getByTestId('profile-declaration-record')).toHaveTextContent('rec-0001');
        fireEvent.click(screen.getByTestId('systems-open-detail'));
        expect(p.setActiveTab).toHaveBeenCalledWith('ai-systems:sys-1:compliance');
        // The sealed declaration opens the shared record dialog.
        fireEvent.click(within(screen.getByTestId('profile-declaration-record')).getByRole('button'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-0001');
    });

    it('saves the edited block and carries other framework blocks through untouched', async () => {
        render(<ProfilesPage {...props()} />);
        await screen.findByTestId('compliance-profiles');
        fireEvent.click(screen.getByTestId('system-row-sys-1'));
        await screen.findByTestId('profile-editor');
        expect(screen.getByTestId('profile-save')).toBeDisabled();
        fireEvent.change(screen.getByTestId('profile-eu-risk-tier'), { target: { value: 'limited' } });
        fireEvent.change(screen.getByTestId('profile-jurisdictions'), { target: { value: 'EU, UK' } });
        fireEvent.click(screen.getByTestId('profile-save'));
        await waitFor(() => expect(putAISystemComplianceProfile).toHaveBeenCalled());
        const [id, body] = putAISystemComplianceProfile.mock.calls[0] as [string, { core: Record<string, unknown>; frameworks: Record<string, unknown> }];
        expect(id).toBe('sys-1');
        expect(body.frameworks).toEqual({ 'eu-ai-act': { role: 'deployer', risk_tier: 'limited' }, hipaa: { covered_entity: 'no' } });
        expect(body.core).toEqual({
            operator_role: 'operator',
            jurisdictions: ['EU', 'UK'],
            sensitivity: { personal_data: true },
            interacts_with_natural_persons: true,
        });
        await waitFor(() => expect(screen.getByTestId('profile-declaration-record')).toHaveTextContent('rec-0002'));
        expect(toast.success).toHaveBeenCalled();
        // The table reloads after a save.
        await waitFor(() => expect(listComplianceSystems).toHaveBeenCalledTimes(2));
    });

    it('offers a key/value editor for a framework without a dedicated form', async () => {
        render(<ProfilesPage {...props('hipaa')} />);
        await screen.findByTestId('compliance-profiles');
        fireEvent.click(screen.getByTestId('system-row-sys-1'));
        await screen.findByTestId('profile-editor');
        const kv = screen.getByTestId('profile-kv-editor');
        expect(within(kv).getByDisplayValue('covered_entity')).toBeInTheDocument();
        expect(within(kv).getByDisplayValue('no')).toBeInTheDocument();
    });

    it('exports the documentation skeleton for the selected system and shows its seal', async () => {
        render(<ProfilesPage {...props()} />);
        await screen.findByTestId('compliance-profiles');
        fireEvent.click(screen.getByTestId('system-row-sys-1'));
        await screen.findByTestId('documentation-panel');
        expect(await screen.findByTestId('doc-empty')).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('doc-export-json'));
        await screen.findByTestId('doc-latest');
        expect(exportSystemDocumentation).toHaveBeenCalledWith('sys-1', 'eu-ai-act');
        expect(screen.getByTestId('doc-latest')).toHaveTextContent('Skeleton · template annex_iv');
        expect(screen.getByTestId('doc-seal')).toHaveTextContent('sealed as record');
    });

    it('renders empty and error states', async () => {
        listComplianceSystems.mockResolvedValueOnce({ items: [], frameworks_enabled: [] });
        const { unmount } = render(<ProfilesPage {...props()} />);
        expect(await screen.findByTestId('systems-empty')).toBeInTheDocument();
        unmount();

        listComplianceSystems.mockRejectedValueOnce(new Error('fleet down'));
        render(<ProfilesPage {...props()} />);
        expect(await screen.findByTestId('systems-error')).toHaveTextContent('fleet down');
    });

    it('reports a profile that cannot be loaded', async () => {
        getAISystemComplianceProfile.mockRejectedValueOnce(new Error('no profile'));
        render(<ProfilesPage {...props()} />);
        await screen.findByTestId('compliance-profiles');
        fireEvent.click(screen.getByTestId('system-row-sys-2'));
        expect(await screen.findByTestId('profile-error')).toHaveTextContent('no profile');
    });
});
