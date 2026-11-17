/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const listComplianceFrameworks = vi.fn();
const getAISystemComplianceProfile = vi.fn();
const listEvidenceRecords = vi.fn();
const fetchSystemObligations = vi.fn();

const gateway = {
    fetchSystemObligations,
    gatewayClient: { listComplianceFrameworks, getAISystemComplianceProfile, listEvidenceRecords },
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));

import SystemCompliancePanel from '../SystemCompliancePanel';
import { FRAMEWORK_STORAGE_KEY } from '../complianceSections';

const REGISTRY = {
    items: [
        { id: 'eu-ai-act', title: 'EU AI Act', enabled: true, obligation_count: 2, offered: true },
        { id: 'gdpr', title: 'GDPR', enabled: true, obligation_count: 0, offered: true },
    ],
};

const OBLIGATIONS = {
    framework: { id: 'eu-ai-act', title: 'EU AI Act', jurisdiction: 'EU', reference: 'Reg', obligation_count: 2 },
    system_id: 'sys-1',
    system_name: 'Copilot',
    as_of: '2026-09-19',
    obligations: [
        {
            key: 'art50', title: 'Transparency', reference: 'Art 50', summary: 'Tell people.', applies_from: '2026-08-02',
            in_force: true, status: 'applies', reason: 'interacts with persons', needs_review: false,
            evidence: [{ key: 'e1', statement: 'Notice shown in n of m conversations', basis: 'recomputed', resolver: 'notice.ratio', limit: 'gateway-rendered only' }],
        },
        {
            key: 'art9', title: 'Risk management', reference: 'Art 9', summary: '', applies_from: '2027-12-02',
            in_force: false, status: 'undetermined', reason: 'role not declared', needs_review: false, evidence: [],
        },
    ],
};

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    listComplianceFrameworks.mockResolvedValue(REGISTRY);
    getAISystemComplianceProfile.mockResolvedValue({
        ai_system_id: 'sys-1', tenant_id: 't', exists: true, declaration_record_id: 'rec-9',
        core: { operator_role: 'builder' },
        frameworks: { 'eu-ai-act': { role: 'provider', risk_tier: 'high' } },
    });
    listEvidenceRecords.mockResolvedValue({ items: [], total: 57, limit: 1, offset: 0 });
    fetchSystemObligations.mockResolvedValue(OBLIGATIONS);
});

describe('SystemCompliancePanel', () => {
    it('shows the profile summary, obligation split, record count and evidence drill-in', async () => {
        const setActiveTab = vi.fn();
        render(<SystemCompliancePanel aiSystemId="sys-1" setActiveTab={setActiveTab} />);
        await screen.findByTestId('system-compliance-panel');
        await waitFor(() => expect(fetchSystemObligations).toHaveBeenCalledWith('sys-1', 'eu-ai-act'));
        const profile = screen.getByTestId('system-compliance-profile');
        expect(profile).toHaveTextContent('Builder');
        expect(profile).toHaveTextContent('Provider');
        expect(profile).toHaveTextContent('high');
        expect(profile).toHaveTextContent('rec-9');
        expect(screen.getByTestId('system-compliance-records')).toHaveTextContent('57');
        await waitFor(() => expect(screen.getByTestId('system-compliance-obligations-summary')).toHaveTextContent('1 in force · 1 upcoming'));
        expect(listEvidenceRecords).toHaveBeenCalledWith({ ai_system_id: 'sys-1', limit: 1 });

        fireEvent.click(screen.getByText('Transparency'));
        expect(screen.getByTestId('system-obligation-evidence-art50-e1')).toHaveTextContent('recomputed');
        expect(screen.getByTestId('system-obligation-evidence-art50-e1-limit')).toHaveTextContent('gateway-rendered only');
        expect(screen.getByTestId('system-obligation-status-art9')).toHaveTextContent('undetermined');

        fireEvent.click(screen.getByTestId('system-compliance-edit'));
        expect(setActiveTab).toHaveBeenCalledWith('compliance-profiles');
    });

    it('switching framework persists and skips the board when there is no catalog', async () => {
        render(<SystemCompliancePanel aiSystemId="sys-1" />);
        await screen.findByTestId('system-compliance-panel');
        await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2));
        fireEvent.change(screen.getByTestId('system-compliance-framework'), { target: { value: 'gdpr' } });
        expect(window.localStorage.getItem(FRAMEWORK_STORAGE_KEY)).toBe('gdpr');
        await waitFor(() => expect(screen.getByTestId('system-compliance-obligations-summary')).toHaveTextContent('GDPR has no obligation catalog yet'));
        expect(fetchSystemObligations).toHaveBeenCalledTimes(1);
        expect(listComplianceFrameworks).toHaveBeenCalledTimes(2);
    });

    it('names an undeclared profile with the prefilled group facts', async () => {
        getAISystemComplianceProfile.mockResolvedValue({
            ai_system_id: 'sys-1', tenant_id: 't', exists: false, declaration_record_id: null,
            core: {}, frameworks: { 'eu-ai-act': { role: null, risk_tier: 'limited' } },
        });
        render(<SystemCompliancePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('system-compliance-undeclared')).toHaveTextContent('limited, no role');
    });

    it('reports a profile load failure', async () => {
        getAISystemComplianceProfile.mockRejectedValue(new Error('profile down'));
        render(<SystemCompliancePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('system-compliance-error')).toHaveTextContent('profile down');
    });
    it('says why the obligations and the record count could not be loaded, each where it would stand', async () => {
        fetchSystemObligations.mockRejectedValue(new Error('board timeout'));
        listEvidenceRecords.mockRejectedValue(new Error('ledger forbidden'));
        render(<SystemCompliancePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('system-compliance-obligations-summary')).toHaveTextContent(
            'Failed to load the obligations: board timeout',
        );
        expect(screen.getByTestId('system-compliance-records')).toHaveTextContent('Failed to count the evidence records: ledger forbidden');
    });
});

// The obligations above were evaluated at the EFFECTIVE tier});

// The obligations above were evaluated at the EFFECTIVE tier
// when a contributing caller lifted it; the panel says so, with the source.
describe('SystemCompliancePanel — evaluated tier', () => {
    it('shows evaluated vs declared with the system that lifted it', async () => {
        getAISystemComplianceProfile.mockResolvedValue({
            ai_system_id: 'sys-1', tenant_id: 't', exists: true, declaration_record_id: 'rec-9',
            core: { operator_role: 'builder' },
            frameworks: { 'eu-ai-act': { role: 'provider', risk_tier: 'minimal' } },
        });
        fetchSystemObligations.mockResolvedValue({
            ...OBLIGATIONS,
            eu_ai_act_risk_tier: 'minimal',
            risk_tier_evaluated: 'high',
            effective_tier_sources: [{ system_id: 'sys-demo', system_name: 'Brutor Demo System', tier: 'high' }],
        });
        render(<SystemCompliancePanel aiSystemId="sys-1" />);
        const tier = await screen.findByTestId('system-compliance-tier');
        await waitFor(() => expect(tier).toHaveAttribute('data-lifted', 'true'));
        expect(tier).toHaveTextContent('evaluated high (declared minimal, via Brutor Demo System)');
        expect(screen.getByTestId('system-compliance-evaluated-at')).toHaveTextContent('Evaluated at the effective tier high');
    });

    it('a system evaluated at its declared tier shows just that tier', async () => {
        fetchSystemObligations.mockResolvedValue({ ...OBLIGATIONS, eu_ai_act_risk_tier: 'high', risk_tier_evaluated: 'high' });
        render(<SystemCompliancePanel aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('system-compliance-obligations-summary')).toHaveTextContent('1 in force'));
        expect(screen.getByTestId('system-compliance-tier')).toHaveAttribute('data-lifted', 'false');
        expect(screen.queryByTestId('system-compliance-evaluated-at')).toBeNull();
    });
});
