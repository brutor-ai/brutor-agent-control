/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control — the Discovery Coverage section of the Governance tab.
 *
 * Shadow-AI discovery is an Enterprise feature, so the section (its tiles,
 * the Top Ungoverned Assets list and the Onboard / Dismiss actions) reaches
 * the page through the `mission-control.governance-sections` slot. The first
 * block is the page with the section contributed, including the recorded
 * markup. The last block is the page with nothing contributed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react';

const fetchGovernancePosture = vi.fn();
const getComplianceDashboard = vi.fn();
const onboardDiscoveredAsset = vi.fn();
const dismissDiscoveredAsset = vi.fn();

const page = vi.hoisted(() => () => import('../../../test/monitoring/missionControlPage'));
vi.mock('../../../hooks/useGateway', async () =>
    (await page()).useGatewayModule(() => ({ fetchGovernancePosture, getComplianceDashboard, onboardDiscoveredAsset, dismissDiscoveredAsset })),
);
vi.mock('../../common/Toast', async () => (await page()).toastModule());
vi.mock('../../common/ConfirmationDialog', async () => (await page()).confirmModule());

import MissionControl from '../MissionControl';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import { DISCOVERY, headings, posture, resetMissionControlPage, tile, toast } from '../../../test/monitoring/missionControlPage';
import { SlotProvider } from '../../../app/SlotProvider';

vi.setConfig({ testTimeout: 30_000 });

/** `slots: false` renders the page with nothing contributed to its slots. */
async function openGovernance({ slots = true } = {}) {
    render(<MissionControl />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await screen.findByText('srv-alpha');
    fireEvent.click(screen.getByRole('tab', { name: 'Governance' }));
    await screen.findByText('Guardrail Blocks');
}

/** The last of a list that has one. */
const last = <T,>(list: T[]): T => list[list.length - 1];

beforeEach(() => {
    resetMissionControlPage();
    fetchGovernancePosture.mockResolvedValue(posture(DISCOVERY));
    getComplianceDashboard.mockResolvedValue({ cards: [] });
    onboardDiscoveredAsset.mockResolvedValue({ suggested_config: { target: 'mcp_server_config' } });
    dismissDiscoveredAsset.mockResolvedValue({});
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe('MissionControl — Discovery Coverage on the Governance tab', () => {

    it.each([
        ['no discovery in the answer', undefined],
        ['nothing discovered', { ...DISCOVERY, total: 0 }],
        ['a total that is absent', { ...DISCOVERY, total: undefined }],
    ])('is not shown with %s', async (_name, discovery) => {
        fetchGovernancePosture.mockResolvedValue(posture(discovery));
        await openGovernance();
        expect(screen.queryByText('Discovery Coverage')).toBeNull();
        expect(screen.queryByText('Top Ungoverned Assets')).toBeNull();
        expect(headings()).toContain('Compliance Coverage');
    });
});

describe('MissionControl — what the Governance tab gives a contributed section', () => {
    const seen: { posture: unknown; onChanged: () => Promise<void> }[] = [];
    const section = (id: string) => ({
        id,
        render: (p: { posture: unknown; onChanged: () => Promise<void> }) => {
            seen.push(p);
            return <h3 key={id}>{`section-${id}`}</h3>;
        },
    });

    it('places the sections in order above Compliance Coverage and hands each the posture as answered', async () => {
        seen.length = 0;
        const answer = posture(DISCOVERY);
        fetchGovernancePosture.mockResolvedValue(answer);
        render(
            <SlotProvider slots={{ 'mission-control.governance-sections': [section('a'), section('b')] }}>
                <MissionControl />
            </SlotProvider>,
        );
        await screen.findByText('srv-alpha');
        fireEvent.click(screen.getByRole('tab', { name: 'Governance' }));
        await screen.findByText('section-a');
        const all = headings();
        const at = all.indexOf('section-a');
        expect(all.slice(at - 1, at + 3)).toEqual([
            'Shadow AI / Un-approved Models',
            'section-a',
            'section-b',
            'Compliance Coverage',
        ]);
        expect(Object.keys(last(seen)).sort()).toEqual(['onChanged', 'posture']);
        expect(last(seen).posture).toBe(answer);
        // `onChanged` reads the posture again and resolves when it has.
        expect(fetchGovernancePosture).toHaveBeenCalledTimes(1);
        const next = posture({ ...DISCOVERY, total: 99 });
        fetchGovernancePosture.mockResolvedValue(next);
        await act(() => last(seen).onChanged());
        expect(fetchGovernancePosture).toHaveBeenCalledTimes(2);
        await waitFor(() => expect(last(seen).posture).toBe(next));
    });

    it('the re-read asks for the period and the group only, and one that fails leaves the posture as it was', async () => {
        seen.length = 0;
        const answer = posture(DISCOVERY);
        fetchGovernancePosture.mockResolvedValue(answer);
        render(
            <SlotProvider slots={{ 'mission-control.governance-sections': [section('a')] }}>
                <MissionControl />
            </SlotProvider>,
        );
        await screen.findByText('srv-alpha');
        fireEvent.click(screen.getByRole('tab', { name: 'Governance' }));
        await screen.findByText('section-a');
        await waitFor(() => expect(last(seen).posture).toBe(answer));
        const next = posture({ ...DISCOVERY, total: 99 });
        fetchGovernancePosture.mockResolvedValue(next);
        await act(() => last(seen).onChanged());
        // The page's own read sends the range bounds as well; the re-read does not.
        const [first] = fetchGovernancePosture.mock.calls[0] as [Record<string, unknown>];
        const [again] = fetchGovernancePosture.mock.calls[1] as [Record<string, unknown>];
        expect(Object.keys(first).sort()).toEqual(['end', 'group_id', 'period_hours', 'start']);
        expect(Object.keys(again).sort()).toEqual(['group_id', 'period_hours']);
        expect(again.group_id).toBeUndefined();
        expect(again.period_hours).toBe(first.period_hours);
        await waitFor(() => expect(last(seen).posture).toBe(next));
        // A re-read that fails resolves all the same: the section keeps the posture it had, and nothing is reported.
        fetchGovernancePosture.mockRejectedValue(new Error('down'));
        await act(() => last(seen).onChanged());
        expect(fetchGovernancePosture).toHaveBeenCalledTimes(3);
        expect(last(seen).posture).toBe(next);
        expect(screen.getByText('section-a')).toBeInTheDocument();
        expect(toast.error).not.toHaveBeenCalled();
    });
});

describe('MissionControl — nothing contributed to the Governance tab', () => {
    it('has no Discovery Coverage, even when the answer reports discovered assets', async () => {
        await openGovernance({ slots: false });
        expect(screen.queryByText('Discovery Coverage')).toBeNull();
        expect(screen.queryByText('Top Ungoverned Assets')).toBeNull();
        expect(screen.queryByRole('button', { name: 'Onboard' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull();
        const all = headings();
        const at = all.indexOf('Shadow AI / Un-approved Models');
        expect(all[at + 1]).toBe('Compliance Coverage');
    });

    it('still shows the enforcement outcomes and never calls the discovery API', async () => {
        await openGovernance({ slots: false });
        expect(tile('Guardrail Blocks')).toHaveTextContent('Guardrail Blocks41.5% of requests');
        expect(tile('Policy Denials')).toHaveTextContent('Policy Denials2argument policies');
        expect(fetchGovernancePosture).toHaveBeenCalledTimes(1);
        expect(onboardDiscoveredAsset).not.toHaveBeenCalled();
        expect(dismissDiscoveredAsset).not.toHaveBeenCalled();
    });
});
