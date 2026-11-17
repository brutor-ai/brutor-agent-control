/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Per-group compliance posture: EU AI Act risk tier and the HIPAA PHI flag.
 *
 * Both fields are operator ATTESTATIONS that drive audit tags — but only
 * when the tenant has declared the matching framework. A tier set on a
 * tenant that never declared EU AI Act persists and fires nothing, which is
 * exactly the kind of silent no-op that erodes trust in an assurance
 * product. So the tab loads the tenant's compliance profile and says so on
 * the face of the field.
 *
 * The other load-bearing behaviour is the explicit null: clearing the tier
 * has to reach the wire as `null`, not as an omitted key, or the control
 * plane's `model_fields_set` branch treats "unclassified" as "unchanged".
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockUpdateResourceGroup = vi.fn();
const mockGetComplianceProfile = vi.fn();
const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

// The tab keys its profile load on the client: one identity for the whole
// file, or every render hands the effect a new dependency and it loads for ever.
const stableGateway = {
    updateResourceGroup: mockUpdateResourceGroup,
    gatewayClient: { getComplianceProfile: mockGetComplianceProfile },
};

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => stableGateway,
}));

vi.mock('../../../common/Toast', () => ({
    useToast: () => ({ success: mockToastSuccess, error: mockToastError }),
}));

import GroupComplianceTab from '../GroupComplianceTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';
import { callsWhileIdle } from '../../../../test/idle';

function group(overrides: Partial<ResourceGroup> = {}): ResourceGroup {
    return {
        id: 'rg-1',
        name: 'clinical-triage',
        ...overrides,
    } as ResourceGroup;
}

const renderTab = (g = group()) => {
    const onUpdate = vi.fn();
    render(<GroupComplianceTab group={g} onUpdate={onUpdate} />);
    return { onUpdate };
};

const tierSelect = () => screen.getByRole('combobox');
const phiCheckbox = () => screen.getByRole('checkbox', { name: /may contain PHI/i });
const sensitiveCheckbox = () => screen.getByTestId('rg-sensitive-data-toggle');
const saveButton = () => screen.getByRole('button', { name: /save compliance posture/i });

beforeEach(() => {
    vi.clearAllMocks();
    mockUpdateResourceGroup.mockResolvedValue({});
    mockGetComplianceProfile.mockResolvedValue({ profile: { frameworks: ['eu_ai_act', 'hipaa'] } });
});

describe('GroupComplianceTab — the tenant frameworks context', () => {
    it('lists the frameworks the declared posture will actually feed', async () => {
        renderTab();
        expect(await screen.findByText('eu_ai_act')).toBeInTheDocument();
        expect(screen.getByText('hipaa')).toBeInTheDocument();
    });

    it('warns that a tier is a no-op when the tenant never declared the regime', async () => {
        mockGetComplianceProfile.mockResolvedValue({ profile: { frameworks: ['hipaa'] } });
        renderTab();
        expect(await screen.findByText(/has not declared EU AI Act/i)).toBeInTheDocument();
        expect(screen.queryByText(/has not declared HIPAA/i)).toBeNull();
    });

    it('warns on both fields when the tenant declared nothing', async () => {
        mockGetComplianceProfile.mockResolvedValue({ profile: { frameworks: [] } });
        renderTab();
        expect(await screen.findByText('none declared')).toBeInTheDocument();
        expect(screen.getByText(/has not declared EU AI Act/i)).toBeInTheDocument();
        expect(screen.getByText(/has not declared HIPAA/i)).toBeInTheDocument();
    });

    it('a profile that will not load says so, and stops claiming the tags will fire', async () => {
        mockGetComplianceProfile.mockRejectedValue(new Error('profile unavailable'));
        renderTab();
        expect(await screen.findByText('profile unavailable')).toBeInTheDocument();
        expect(screen.getByText(/has not declared EU AI Act/i)).toBeInTheDocument();
    });

    it('makes no claim either way while the profile is still loading', () => {
        mockGetComplianceProfile.mockReturnValue(new Promise(() => { /* never settles */ }));
        renderTab();
        expect(screen.getByText('loading…')).toBeInTheDocument();
        expect(screen.queryByText(/has not declared/i)).toBeNull();
    });
});

describe('GroupComplianceTab — the tier field', () => {
    it('reads back the group’s stored tier and explains what it means', async () => {
        renderTab(group({ eu_ai_act_risk_tier: 'high' }));
        await waitFor(() => expect(tierSelect()).toHaveValue('high'));
        expect(screen.getByText(/conformity assessment/i)).toBeInTheDocument();
    });

    it('an unclassified group offers no tier blurb at all', async () => {
        renderTab();
        await waitFor(() => expect(tierSelect()).toHaveValue(''));
        expect(screen.queryByText(/conformity assessment/i)).toBeNull();
    });

    it('picking a tier swaps in that tier’s obligations', async () => {
        renderTab();
        await waitFor(() => expect(tierSelect()).toBeInTheDocument());
        fireEvent.change(tierSelect(), { target: { value: 'unacceptable' } });
        expect(screen.getByText(/Prohibited under the AI Act/i)).toBeInTheDocument();
    });

    it('offers every tier the platform recognises', async () => {
        renderTab();
        await waitFor(() => expect(tierSelect()).toBeInTheDocument());
        for (const label of ['Minimal', 'Limited', 'High', 'Unacceptable']) {
            expect(screen.getByRole('option', { name: label })).toBeInTheDocument();
        }
    });
});

describe('GroupComplianceTab — saving', () => {
    it('stays disabled until something actually changes', async () => {
        renderTab();
        await waitFor(() => expect(saveButton()).toBeDisabled());
        fireEvent.click(phiCheckbox());
        expect(saveButton()).toBeEnabled();
    });

    it('writes both attestations together', async () => {
        const { onUpdate } = renderTab();
        await waitFor(() => expect(tierSelect()).toBeInTheDocument());
        fireEvent.change(tierSelect(), { target: { value: 'high' } });
        fireEvent.click(phiCheckbox());
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', {
                eu_ai_act_risk_tier: 'high',
                phi_resource: true,
            }),
        );
        expect(mockToastSuccess).toHaveBeenCalled();
        expect(onUpdate).toHaveBeenCalled();
    });

    it('clearing the tier reaches the wire as an explicit null', async () => {
        renderTab(group({ eu_ai_act_risk_tier: 'high' }));
        await waitFor(() => expect(tierSelect()).toHaveValue('high'));
        fireEvent.change(tierSelect(), { target: { value: '' } });
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', {
                eu_ai_act_risk_tier: null,
                phi_resource: false,
            }),
        );
    });

    it('discard puts the stored posture back and re-disables Save', async () => {
        renderTab(group({ eu_ai_act_risk_tier: 'limited', phi_resource: true }));
        await waitFor(() => expect(tierSelect()).toHaveValue('limited'));
        fireEvent.change(tierSelect(), { target: { value: 'high' } });
        fireEvent.click(phiCheckbox());

        fireEvent.click(screen.getByRole('button', { name: /discard changes/i }));
        expect(tierSelect()).toHaveValue('limited');
        expect(phiCheckbox()).toBeChecked();
        expect(saveButton()).toBeDisabled();
    });

    it('discard is only offered while there is something to discard', async () => {
        renderTab();
        await waitFor(() => expect(saveButton()).toBeDisabled());
        expect(screen.queryByRole('button', { name: /discard changes/i })).toBeNull();
    });

    it('a failed save keeps the edit and does NOT claim the posture was recorded', async () => {
        mockUpdateResourceGroup.mockRejectedValue(new Error('conflict'));
        const { onUpdate } = renderTab();
        await waitFor(() => expect(tierSelect()).toBeInTheDocument());
        fireEvent.change(tierSelect(), { target: { value: 'high' } });
        fireEvent.click(saveButton());

        await waitFor(() => expect(mockToastError).toHaveBeenCalledWith('Save failed', 'conflict'));
        expect(tierSelect()).toHaveValue('high');
        expect(mockToastSuccess).not.toHaveBeenCalled();
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('re-seeds from the group after a reload, dropping the dirty state', async () => {
        const { rerender } = render(<GroupComplianceTab group={group()} onUpdate={vi.fn()} />);
        await waitFor(() => expect(tierSelect()).toBeInTheDocument());
        fireEvent.change(tierSelect(), { target: { value: 'high' } });

        rerender(
            <GroupComplianceTab
                group={group({ eu_ai_act_risk_tier: 'high' })}
                onUpdate={vi.fn()}
            />,
        );
        await waitFor(() => expect(saveButton()).toBeDisabled());
        expect(tierSelect()).toHaveValue('high');
    });
});

describe('GroupComplianceTab — sensitive data (privileged-read rule)', () => {
    it('reads the stored flag, defaulting to off when the CP does not send it', async () => {
        renderTab(group({ sensitive_data: true }));
        await waitFor(() => expect(sensitiveCheckbox()).toBeChecked());
    });

    it('explains that read-only tool calls on sensitive groups are sealed', async () => {
        renderTab();
        await waitFor(() => expect(sensitiveCheckbox()).not.toBeChecked());
        expect(screen.getByTestId('rg-sensitive-data')).toHaveTextContent(/read-only tool calls/i);
    });

    it('sends sensitive_data when toggled, beside phi_resource', async () => {
        renderTab();
        await waitFor(() => expect(saveButton()).toBeDisabled());
        fireEvent.click(sensitiveCheckbox());
        expect(saveButton()).toBeEnabled();
        fireEvent.click(saveButton());
        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', {
                eu_ai_act_risk_tier: null,
                phi_resource: false,
                sensitive_data: true,
            }),
        );
    });

    it('clearing the flag reaches the wire as false', async () => {
        renderTab(group({ sensitive_data: true }));
        await waitFor(() => expect(sensitiveCheckbox()).toBeChecked());
        fireEvent.click(sensitiveCheckbox());
        fireEvent.click(saveButton());
        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', expect.objectContaining({ sensitive_data: false })),
        );
    });

    it('an untouched flag is not sent, and discard restores it', async () => {
        renderTab(group({ sensitive_data: true }));
        await waitFor(() => expect(sensitiveCheckbox()).toBeChecked());
        fireEvent.click(phiCheckbox());
        fireEvent.click(saveButton());
        await waitFor(() => expect(mockUpdateResourceGroup).toHaveBeenCalled());
        expect(mockUpdateResourceGroup.mock.calls[0][1]).not.toHaveProperty('sensitive_data');
    });

    it('discard puts the flag back', async () => {
        renderTab(group({ sensitive_data: false }));
        await waitFor(() => expect(saveButton()).toBeDisabled());
        fireEvent.click(sensitiveCheckbox());
        fireEvent.click(screen.getByRole('button', { name: /discard changes/i }));
        expect(sensitiveCheckbox()).not.toBeChecked();
        expect(saveButton()).toBeDisabled();
    });
});

describe('GroupComplianceTab — at rest', () => {
    it('asks for the profile once, also when it will not load', async () => {
        mockGetComplianceProfile.mockRejectedValue(new Error('profile unavailable'));
        renderTab();
        await screen.findByText('profile unavailable');
        expect(await callsWhileIdle([mockGetComplianceProfile])).toBe(0);
        expect(mockGetComplianceProfile).toHaveBeenCalledTimes(1);
    });
});
