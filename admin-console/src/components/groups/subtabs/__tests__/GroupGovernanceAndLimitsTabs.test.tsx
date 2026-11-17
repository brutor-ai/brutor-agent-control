/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The look-alike Group*Governance and Group*Limits tabs, table-driven.
 *
 * Pinned: a save sends the group's OWN edited blob (never the merged effective
 * view, which would freeze ancestors' values into the child), a value looser
 * than the inherited policy keeps Save off so it is never sent, a server refusal
 * is shown and leaves the edit dirty, and the effective/capacity figures on
 * screen are the ones the save returned: all of these PATCHes return the new
 * effective view, and every tab uses it.
 * The forms have their own suites, so they are stubbed to their contract.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import type { ResourceGroup } from '../../../../lib/groupTypes';
import { governanceRelaxations } from '../../../../lib/governanceInheritance';
import type { RefusedDocument } from '../../../../lib/refusedSettings';
import { STORED_KEPT, STORED_WITH_REFUSED, controlPlaneRefusal } from '../../../../test/controlPlaneRefusal';

const gateway = {
    getLLMGlobalGovernance: vi.fn(),
    updateLLMGlobalGovernance: vi.fn(),
    getMCPGlobalGovernance: vi.fn(),
    updateMCPGlobalGovernance: vi.fn(),
    getSkillGlobalGovernance: vi.fn(),
    updateSkillGlobalGovernance: vi.fn(),
    fetchMediaGlobalGovernance: vi.fn(),
    updateMediaGlobalGovernance: vi.fn(),
    fetchLLMGlobalLimits: vi.fn(),
    updateLLMGlobalLimits: vi.fn(),
    fetchMCPGlobalLimits: vi.fn(),
    updateMCPGlobalLimits: vi.fn(),
};
const gatewayClient = {
    getGroupA2AGovernance: vi.fn(),
    updateResourceGroup: vi.fn(),
};
const auth = { gatewayClient };
const toast = { success: vi.fn(), error: vi.fn() };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../hooks/useAuth', () => ({ useAuth: () => auth }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));

/** What each stub form was last told to edit to. */
const edit: { next: unknown } = { next: {} };

function StubForm(p: {
    value?: unknown;
    limits?: unknown;
    onChange: (v: never) => void;
    inherited?: unknown;
    inheritedLimits?: unknown;
    effective?: unknown;
    effectiveLimits?: unknown;
    capacity?: unknown;
    onRelaxationsChange?: (r: never) => void;
}) {
    const shown = p.value ?? p.limits;
    return (
        <div>
            <pre data-testid="form-value">{JSON.stringify(shown)}</pre>
            <pre data-testid="form-inherited">{JSON.stringify(p.inherited ?? p.inheritedLimits ?? null)}</pre>
            <pre data-testid="form-effective">{JSON.stringify(p.effective ?? p.effectiveLimits ?? null)}</pre>
            <pre data-testid="form-capacity">{JSON.stringify(p.capacity ?? null)}</pre>
            <button
                onClick={() => {
                    p.onChange(edit.next as never);
                    // The LLM form reports relaxations itself; the real one
                    // computes them with the same library the others use.
                    p.onRelaxationsChange?.(
                        governanceRelaxations(edit.next, p.inherited, 'llm') as never,
                    );
                }}
            >
                edit
            </button>
        </div>
    );
}

vi.mock('../LLMGovernanceForm', () => ({ default: StubForm }));
vi.mock('../MCPGovernanceForm', () => ({ default: StubForm }));
vi.mock('../SkillGovernanceForm', () => ({ default: StubForm }));
vi.mock('../A2AGovernanceForm', () => ({ default: StubForm }));
vi.mock('../LLMLimitsForm', () => ({ default: StubForm }));
vi.mock('../MCPLimitsForm', () => ({ default: StubForm }));
vi.mock('../MediaGovernanceForm', () => ({
    default: (p: { mode: string; value: unknown; onChange: (v: unknown) => void }) => (
        <div>
            <span data-testid="media-mode">{p.mode}</span>
            <pre data-testid="form-value">{JSON.stringify(p.value)}</pre>
            <button onClick={() => p.onChange(edit.next)}>edit</button>
        </div>
    ),
    MEDIA_GOVERNANCE_MODES: [
        { id: 'audio_speech', label: 'Speech', icon: () => null },
        { id: 'image_generation', label: 'Images', icon: () => null },
    ],
}));

import GroupLLMGovernanceTab from '../GroupLLMGovernanceTab';
import GroupMCPGovernanceTab from '../GroupMCPGovernanceTab';
import GroupSkillGovernanceTab from '../GroupSkillGovernanceTab';
import GroupA2AGovernanceTab from '../GroupA2AGovernanceTab';
import GroupMediaGovernanceTab from '../GroupMediaGovernanceTab';
import GroupLLMLimitsTab from '../GroupLLMLimitsTab';
import GroupMCPLimitsTab from '../GroupMCPLimitsTab';

const GROUP = { id: 'rg-1', name: 'eng', parent_group_id: 'rg-0' } as ResourceGroup;
const ROOT = { id: 'rg-1', name: 'eng' } as ResourceGroup;
const onUpdate = vi.fn();

type Tab = React.FC<{ group: ResourceGroup; onUpdate: () => void }>;

interface Surface {
    name: string;
    Tab: Tab;
    load: ReturnType<typeof vi.fn>;
    save: ReturnType<typeof vi.fn>;
    keys: { own: string; inherited: string; effective: string };
    own: Record<string, unknown>;
    /** An edit that only tightens. */
    tighter: Record<string, unknown>;
    /** An edit looser than `inherited`, or null when the UI leaves that to the server. */
    looser: Record<string, unknown> | null;
    inherited: Record<string, unknown>;
    loadError: string;
    saveError: string;
    capacity?: boolean;
    heading: string;
}

const surfaces: Surface[] = [
    {
        name: 'LLM governance',
        Tab: GroupLLMGovernanceTab,
        load: gateway.getLLMGlobalGovernance,
        save: gateway.updateLLMGlobalGovernance,
        keys: { own: 'llm_global_governance', inherited: 'inherited_llm_governance', effective: 'effective_llm_governance' },
        own: { temperature: { max: 0.5 } },
        tighter: { temperature: { max: 0.3 } },
        looser: { temperature: { max: 0.9 } },
        inherited: { temperature: { max: 0.7 } },
        loadError: 'Failed to load LLM governance',
        saveError: 'Failed to update LLM governance',
        heading: 'LLM Global Governance',
    },
    {
        name: 'MCP governance',
        Tab: GroupMCPGovernanceTab,
        load: gateway.getMCPGlobalGovernance,
        save: gateway.updateMCPGlobalGovernance,
        keys: { own: 'mcp_global_governance', inherited: 'inherited_mcp_governance', effective: 'effective_mcp_governance' },
        own: { approval_timeout_seconds: 60 },
        tighter: { approval_timeout_seconds: 30 },
        looser: { approval_timeout_seconds: 600 },
        inherited: { approval_timeout_seconds: 120 },
        loadError: 'Failed to load MCP governance',
        saveError: 'Failed to update MCP governance',
        heading: 'MCP Global Governance',
    },
    {
        name: 'Skill governance',
        Tab: GroupSkillGovernanceTab,
        load: gateway.getSkillGlobalGovernance,
        save: gateway.updateSkillGlobalGovernance,
        keys: { own: 'skill_global_governance', inherited: 'inherited_skill_governance', effective: 'effective_skill_governance' },
        own: { max_concurrent_executions: 2 },
        tighter: { max_concurrent_executions: 1 },
        looser: { max_concurrent_executions: 50 },
        inherited: { max_concurrent_executions: 4 },
        loadError: 'Failed to load skill governance',
        saveError: 'Failed to update skill governance',
        heading: 'Skill Global Governance',
    },
    {
        name: 'LLM limits',
        Tab: GroupLLMLimitsTab,
        load: gateway.fetchLLMGlobalLimits,
        save: gateway.updateLLMGlobalLimits,
        keys: { own: 'llm_global_limits', inherited: 'inherited_llm_limits', effective: 'effective_llm_limits' },
        own: { monthly_budget: 50 },
        tighter: { monthly_budget: 40 },
        looser: null,
        inherited: { monthly_budget: 100 },
        loadError: 'Failed to load LLM limits',
        saveError: 'Failed to update LLM limits',
        capacity: true,
        heading: 'LLM Usage Limits',
    },
    {
        name: 'MCP limits',
        Tab: GroupMCPLimitsTab,
        load: gateway.fetchMCPGlobalLimits,
        save: gateway.updateMCPGlobalLimits,
        keys: { own: 'mcp_global_limits', inherited: 'inherited_mcp_limits', effective: 'effective_mcp_limits' },
        // An MCP limits document holds only the keys the contract defines: a save sends no other.
        own: { frequency: { max_calls_per_hour: 50 } },
        tighter: { frequency: { max_calls_per_hour: 40 } },
        looser: null,
        inherited: { frequency: { max_calls_per_hour: 100 } },
        loadError: 'Failed to load MCP limits',
        saveError: 'Failed to update MCP limits',
        capacity: true,
        heading: 'MCP Server Limits',
    },
];

const HELP_TITLE = 'MCP Governance Help';

const jsonOf = (testId: string): unknown => JSON.parse(screen.getByTestId(testId).textContent ?? 'null') as unknown;

beforeEach(() => {
    vi.clearAllMocks();
    edit.next = {};
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe.each(surfaces)('$name tab', (s) => {
    const response = (effective: unknown, capacity: unknown = null) => ({
        [s.keys.own]: s.own,
        [s.keys.inherited]: s.inherited,
        [s.keys.effective]: effective,
        capacity,
    });

    beforeEach(() => {
        s.load.mockReset();
        s.save.mockReset();
        s.load.mockResolvedValue(response({ loaded: true }, { parent_name: 'Org' }));
    });

    async function open(group: ResourceGroup = GROUP) {
        render(<s.Tab group={group} onUpdate={onUpdate} />);
        expect(await screen.findByText(s.heading)).toBeInTheDocument();
        expect(s.load).toHaveBeenCalledWith('rg-1');
    }

    it('feeds the form own, inherited and effective separately, with no Save until edited', async () => {
        await open();
        expect(jsonOf('form-value')).toEqual(s.own);
        expect(jsonOf('form-inherited')).toEqual(s.inherited);
        expect(jsonOf('form-effective')).toEqual({ loaded: true });
        expect(screen.queryByRole('button', { name: 'Save Changes' })).toBeNull();
    });

    it('saves exactly the edited own blob, then shows what the save returned', async () => {
        s.save.mockResolvedValue(response({ fresh: true }, { parent_name: 'Fresh' }));
        await open();
        edit.next = s.tighter;
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

        await waitFor(() => expect(s.save).toHaveBeenCalledWith('rg-1', s.tighter));
        await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
        // The effective view moved with the write; the form must not show the old one.
        expect(jsonOf('form-effective')).toEqual({ fresh: true });
        expect(jsonOf('form-capacity')).toEqual(s.capacity ? { parent_name: 'Fresh' } : null);
        // Clean again.
        expect(screen.queryByRole('button', { name: 'Save Changes' })).toBeNull();
    });

    it('a refused save shows the reason and keeps the edit', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        s.save.mockRejectedValue(new Error('Cannot set looser than inherited'));
        await open();
        edit.next = s.tighter;
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

        expect(await screen.findByText('Cannot set looser than inherited')).toBeInTheDocument();
        expect(onUpdate).not.toHaveBeenCalled();
        expect(jsonOf('form-value')).toEqual(s.tighter);
        expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
    });

    it('Cancel throws the edit away by reloading from the server', async () => {
        await open();
        edit.next = s.tighter;
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        await waitFor(() => expect(s.load).toHaveBeenCalledTimes(2));
        await screen.findByText(s.heading);
        expect(jsonOf('form-value')).toEqual(s.own);
        expect(s.save).not.toHaveBeenCalled();
    });

    it('a failed load names what failed', async () => {
        s.load.mockRejectedValue({});
        render(<s.Tab group={GROUP} onUpdate={onUpdate} />);
        expect(await screen.findByText(s.loadError)).toBeInTheDocument();
    });

    it('a failed save with no message falls back to a named error', async () => {
        s.save.mockRejectedValue({});
        await open();
        edit.next = s.tighter;
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
        expect(await screen.findByText(s.saveError)).toBeInTheDocument();
    });

    if (s.name === 'MCP governance' || s.name === 'Skill governance') {
        it('an approval timeout outside 1 .. 604800 seconds keeps Save off, says the range, and is never sent', async () => {
            await open();
            for (const seconds of [0, 604801]) {
                edit.next = { ...s.tighter, approval_timeout_seconds: seconds };
                fireEvent.click(screen.getByText('edit'));
                const save = screen.getByTestId('governance-save');
                expect(save).toBeDisabled();
                expect(save).toHaveAttribute('title', 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).');
                fireEvent.click(save);
            }
            expect(s.save).not.toHaveBeenCalled();

            // A timeout in range releases it.
            edit.next = { ...s.tighter, approval_timeout_seconds: 30 };
            fireEvent.click(screen.getByText('edit'));
            expect(screen.getByTestId('governance-save')).toBeEnabled();
        });
    }

    if (s.name === 'MCP limits') {
        it('a limit the gateway cannot read keeps Save off, says why, and is never sent', async () => {
            await open();
            for (const [next, sentence] of [
                [{ execution: { max_concurrency_per_group: 2.5 } }, 'Max Concurrency (per group) must be a whole number of at least 1.'],
                [{ execution: { timeout_seconds: 0 } }, 'Timeout (seconds) must be a positive number.'],
                [{ data: { max_payload_size_mb: -1 } }, 'Max Payload Size (MB) must be a positive number.'],
            ] as const) {
                edit.next = next;
                fireEvent.click(screen.getByText('edit'));
                expect(screen.getByTestId('mcp-limits-problem')).toHaveTextContent(sentence);
                const save = screen.getByRole('button', { name: 'Save Changes' });
                expect(save).toBeDisabled();
                fireEvent.click(save);
            }
            expect(s.save).not.toHaveBeenCalled();

            edit.next = { execution: { max_concurrency_per_group: 3 } };
            fireEvent.click(screen.getByText('edit'));
            expect(screen.queryByTestId('mcp-limits-problem')).toBeNull();
            expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
        });
    }

    if (s.looser) {
        const looser = s.looser;
        it('a value looser than the inherited policy keeps Save off and is never sent', async () => {
            await open();
            edit.next = looser;
            fireEvent.click(screen.getByText('edit'));

            const save = screen.getByTestId('governance-save');
            expect(save).toBeDisabled();
            expect(screen.getByTestId('governance-relaxations')).toHaveTextContent('Saving is blocked');
            fireEvent.click(save);
            expect(s.save).not.toHaveBeenCalled();

            // Tightening back releases it.
            edit.next = s.tighter;
            fireEvent.click(screen.getByText('edit'));
            expect(screen.getByTestId('governance-save')).toBeEnabled();
            expect(screen.queryByTestId('governance-relaxations')).toBeNull();
        });
    }
});

describe('LLM limits inheritance notes', () => {
    it('explains inherited limits and names the budget holder only for a child group', async () => {
        gateway.fetchLLMGlobalLimits.mockResolvedValue({
            llm_global_limits: {},
            inherited_llm_limits: { monthly_budget: 10 },
            capacity: { parent_name: 'Org', parent_is_immediate: false },
        });
        render(<GroupLLMLimitsTab group={GROUP} onUpdate={onUpdate} />);
        expect(await screen.findByTestId('llm-limits-inherited-note')).toBeInTheDocument();
        expect(screen.getByTestId('llm-limits-capacity-note')).toHaveTextContent(
            'The budget comes from Org further up the hierarchy',
        );

        cleanup();
        render(<GroupLLMLimitsTab group={ROOT} onUpdate={onUpdate} />);
        await screen.findByText('LLM Usage Limits');
        expect(screen.queryByTestId('llm-limits-inherited-note')).toBeNull();
        expect(screen.queryByTestId('llm-limits-capacity-note')).toBeNull();
    });
});

describe('Governance help', () => {
    it('opens the MCP help modal from the header', async () => {
        gateway.getMCPGlobalGovernance.mockResolvedValue({ mcp_global_governance: {} });
        render(<GroupMCPGovernanceTab group={ROOT} onUpdate={onUpdate} />);
        await screen.findByText('MCP Global Governance');
        expect(screen.queryByText(HELP_TITLE)).toBeNull();
        fireEvent.click(screen.getByTitle('Help'));
        expect(await screen.findByText(HELP_TITLE)).toBeInTheDocument();
    });
});

describe('A2A governance tab', () => {
    const load = {
        a2a_global_governance: {
            capability: { default_access: 'approval' },
            data: { deny_classifications: ['pii'] },
        },
        inherited_a2a_governance: { capability: { default_access: 'allow' } },
        effective_a2a_governance: { capability: { default_access: 'approval' } },
    };

    beforeEach(() => {
        gatewayClient.getGroupA2AGovernance.mockReset();
        gatewayClient.updateResourceGroup.mockReset();
        gatewayClient.getGroupA2AGovernance.mockResolvedValue(load);
    });

    async function open() {
        render(<GroupA2AGovernanceTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('edit');
    }

    it('Save and Revert stay off until something changed', async () => {
        await open();
        expect(jsonOf('form-inherited')).toEqual(load.inherited_a2a_governance);
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        expect(screen.getByRole('button', { name: 'Revert' })).toBeDisabled();
    });

    it('an approval timeout outside 1 .. 604800 seconds keeps Save off and is never sent', async () => {
        await open();
        for (const seconds of [0, 604801]) {
            edit.next = { capability: { default_access: 'approval_required', approval_timeout_seconds: seconds } };
            fireEvent.click(screen.getByText('edit'));
            const save = screen.getByRole('button', { name: 'Save' });
            expect(save).toBeDisabled();
            expect(save).toHaveAttribute('title', 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).');
            fireEvent.click(save);
        }
        expect(gatewayClient.updateResourceGroup).not.toHaveBeenCalled();

        edit.next = { capability: { default_access: 'approval_required', approval_timeout_seconds: 600 } };
        fireEvent.click(screen.getByText('edit'));
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    });

    it('strips empty sections from what it sends, but keeps the timeout', async () => {
        gatewayClient.updateResourceGroup.mockResolvedValue({});
        await open();
        edit.next = {
            capability: { default_access: '', approval_timeout_seconds: 1 },
            data: { deny_classifications: [] },
            remote: { allowed_globs: ['https://*.acme.test/*'] },
        };
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() =>
            expect(gatewayClient.updateResourceGroup).toHaveBeenCalledWith('rg-1', {
                a2a_global_governance: {
                    capability: { approval_timeout_seconds: 1 },
                    remote: { allowed_globs: ['https://*.acme.test/*'] },
                },
            }),
        );
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('A2A governance saved'));
        expect(onUpdate).toHaveBeenCalledTimes(1);
        // Reloaded, so the effective view on screen follows the write.
        await waitFor(() => expect(gatewayClient.getGroupA2AGovernance).toHaveBeenCalledTimes(2));
    });

    it('an entirely emptied policy is sent as {} so it clears server state', async () => {
        gatewayClient.updateResourceGroup.mockResolvedValue({});
        await open();
        edit.next = { capability: {}, data: {}, remote: {} };
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() =>
            expect(gatewayClient.updateResourceGroup).toHaveBeenCalledWith('rg-1', { a2a_global_governance: {} }),
        );
    });

    it('a refused save is toasted and the edit stays dirty', async () => {
        gatewayClient.updateResourceGroup.mockRejectedValue(new Error('looser than parent'));
        await open();
        edit.next = { capability: { default_access: 'allow' } };
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Save failed', 'looser than parent'));
        expect(onUpdate).not.toHaveBeenCalled();
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    });

    it('a messageless failure names what failed instead of "Unknown error"', async () => {
        gatewayClient.updateResourceGroup.mockRejectedValue({});
        await open();
        edit.next = { capability: { default_access: 'allow' } };
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() =>
            expect(toast.error).toHaveBeenCalledWith('Save failed', 'Failed to update A2A governance'),
        );

        cleanup();
        gatewayClient.getGroupA2AGovernance.mockRejectedValue({});
        render(<GroupA2AGovernanceTab group={GROUP} onUpdate={onUpdate} />);
        expect(await screen.findByText('Failed to load A2A governance')).toBeInTheDocument();
    });

    it('Revert reloads, and a failed load is shown', async () => {
        await open();
        edit.next = { capability: { default_access: 'allow' } };
        fireEvent.click(screen.getByText('edit'));
        gatewayClient.getGroupA2AGovernance.mockRejectedValue(new Error('gone'));
        fireEvent.click(screen.getByRole('button', { name: 'Revert' }));
        expect(await screen.findByText('gone')).toBeInTheDocument();
    });
});

describe('Media governance tab', () => {
    beforeEach(() => {
        gateway.fetchMediaGlobalGovernance.mockReset();
        gateway.updateMediaGlobalGovernance.mockReset();
        gateway.fetchMediaGlobalGovernance.mockResolvedValue({
            media_global_governance: { audio_speech: { max_chars: 100 }, image_generation: { n: 1 } },
        });
    });

    async function open() {
        render(<GroupMediaGovernanceTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('Media governance');
    }

    it('edits are sent whole and a failure is shown', async () => {
        await open();
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        edit.next = { audio_speech: { max_chars: 50 } };
        fireEvent.click(screen.getByText('edit'));

        gateway.updateMediaGlobalGovernance.mockRejectedValueOnce(new Error('refused'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        expect(await screen.findByText('refused')).toBeInTheDocument();
        expect(onUpdate).not.toHaveBeenCalled();

        gateway.updateMediaGlobalGovernance.mockResolvedValueOnce({});
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
        expect(gateway.updateMediaGlobalGovernance).toHaveBeenLastCalledWith('rg-1', {
            audio_speech: { max_chars: 50 },
        });
        expect(screen.queryByText('refused')).toBeNull();
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    });

    it('clearing a mode removes only that mode from what is sent', async () => {
        gateway.updateMediaGlobalGovernance.mockResolvedValue({});
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Images' }));
        expect(screen.getByTestId('media-mode')).toHaveTextContent('image_generation');
        fireEvent.click(screen.getByRole('button', { name: 'Clear all Images governance' }));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() =>
            expect(gateway.updateMediaGlobalGovernance).toHaveBeenCalledWith('rg-1', {
                audio_speech: { max_chars: 100 },
            }),
        );
    });

    it('Discard reloads from the server', async () => {
        await open();
        edit.next = {};
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
        await waitFor(() => expect(gateway.fetchMediaGlobalGovernance).toHaveBeenCalledTimes(2));
        await screen.findByText('Media governance');
        expect(jsonOf('form-value')).toEqual({ audio_speech: { max_chars: 100 }, image_generation: { n: 1 } });
    });

    it('a failed load is shown', async () => {
        gateway.fetchMediaGlobalGovernance.mockRejectedValue(new Error('down'));
        await open();
        expect(screen.getByText('down')).toBeInTheDocument();
    });
});

// A document read from the Control Plane may hold a value of a
// setting it now refuses (stored before). Saving the tab without touching
// that value must not send it again: the save would be refused.
describe('a stored setting the Control Plane refuses is not sent again', () => {
    const cases: Array<{ name: string; Tab: Tab; load: Mock; save: Mock; own: string; kind: RefusedDocument; heading: string }> = [
        { name: 'LLM governance', Tab: GroupLLMGovernanceTab, load: gateway.getLLMGlobalGovernance, save: gateway.updateLLMGlobalGovernance, own: 'llm_global_governance', kind: 'llm_governance', heading: 'LLM Global Governance' },
        { name: 'LLM limits', Tab: GroupLLMLimitsTab, load: gateway.fetchLLMGlobalLimits, save: gateway.updateLLMGlobalLimits, own: 'llm_global_limits', kind: 'llm_limits', heading: 'LLM Usage Limits' },
        { name: 'MCP limits', Tab: GroupMCPLimitsTab, load: gateway.fetchMCPGlobalLimits, save: gateway.updateMCPGlobalLimits, own: 'mcp_global_limits', kind: 'mcp_limits', heading: 'MCP Server Limits' },
    ];

    it.each(cases)('$name: a save of the document as it was read sends none of it, and is accepted', async (c) => {
        c.load.mockReset();
        c.save.mockReset();
        const stored = STORED_WITH_REFUSED[c.kind];
        c.load.mockResolvedValue({ [c.own]: stored, capacity: null });
        c.save.mockImplementation((_id: string, doc: unknown) => {
            const refusal = controlPlaneRefusal(c.kind, doc);
            return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve({ [c.own]: doc, capacity: null });
        });
        render(<c.Tab group={ROOT} onUpdate={onUpdate} />);
        await screen.findByText(c.heading);
        // The form hands the document back unchanged.
        edit.next = stored;
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
        await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
        expect(c.save).toHaveBeenCalledWith('rg-1', STORED_KEPT[c.kind]);
        expect(screen.queryByText(/not available in this release/)).toBeNull();
    });

    it('Media governance: clearing another mode sends none of the stored refused settings, and is accepted', async () => {
        gateway.fetchMediaGlobalGovernance.mockReset();
        gateway.updateMediaGlobalGovernance.mockReset();
        gateway.fetchMediaGlobalGovernance.mockResolvedValue({
            media_global_governance: { ...STORED_WITH_REFUSED.media_governance, audio_speech: { max_chars: 100 } },
        });
        gateway.updateMediaGlobalGovernance.mockImplementation((_id: string, doc: unknown) => {
            const refusal = controlPlaneRefusal('media_governance', doc);
            return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve({});
        });
        render(<GroupMediaGovernanceTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('Media governance');
        fireEvent.click(screen.getByRole('button', { name: 'Clear all Speech governance' }));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
        expect(gateway.updateMediaGlobalGovernance).toHaveBeenCalledWith('rg-1', STORED_KEPT.media_governance);
    });
});
