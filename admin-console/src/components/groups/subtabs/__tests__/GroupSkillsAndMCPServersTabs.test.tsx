/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The group's Agent Skills and MCP Servers tabs share one shape: effective,
 * direct and inherited lists, removal by typed confirmation, per-item dialogs.
 * Table-driven, with the skill-only discovery switch and the MCP-only tool limits
 * pinned beneath. Removal must target this group and the chosen item, and a
 * refusal must not claim success. Tables and dialogs are stubbed to their contract.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const gateway = {
    fetchEffectiveAgentSkills: vi.fn(),
    removeSkillFromGroup: vi.fn(),
    getSkillGlobalGovernance: vi.fn(),
    updateSkillGlobalGovernance: vi.fn(),
    fetchEffectiveServerConfigs: vi.fn(),
    removeServerFromGroup: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn() };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));

type Item = { id: string; name: string };

function table(listProp: 'skills' | 'servers') {
    return {
        default: (p: Record<string, unknown>) => {
            const items = p[listProp] as Item[];
            const call = (fn: string, ...args: unknown[]) => (p[fn] as (...a: unknown[]) => void)(...args);
            return (
                <div>
                    {items.map((m) => (
                        <div key={m.id} data-testid={`row-${m.id}`}>
                            <span>{m.name}</span>
                            <button onClick={() => call('onRemove', m.id, m.name)}>remove</button>
                            <button onClick={() => call('onConfigureLimits', m)}>limits</button>
                            {listProp === 'skills' ? (
                                <button onClick={() => call('onConfigureGovernance', m)}>second</button>
                            ) : (
                                <>
                                    <button onClick={() => call('onConfigureCapabilities', m)}>second</button>
                                    <button onClick={() => call('onConfigureToolLimits', m)}>tool-limits</button>
                                </>
                            )}
                        </div>
                    ))}
                </div>
            );
        },
    };
}

function dialog(id: string) {
    return {
        default: (p: { groupId?: string; onSave?: () => void; onClose: () => void }) => (
            <div data-testid={id}>
                {p.groupId}
                {p.onSave && <button onClick={p.onSave}>dialog-save</button>}
                <button onClick={p.onClose}>dialog-close</button>
            </div>
        ),
    };
}

vi.mock('../GroupAgentSkillsTableView', () => table('skills'));
vi.mock('../GroupMCPServersTableView', () => table('servers'));
vi.mock('../SkillLimitsDialog', () => dialog('limits-dialog'));
vi.mock('../SkillGovernanceDialog', () => dialog('second-dialog'));
vi.mock('../MCPServerLimitsDialog', () => dialog('limits-dialog'));
vi.mock('../MCPCapabilityFilterDialog', () => dialog('second-dialog'));
vi.mock('../MCPToolLimitsDialog', () => dialog('tool-limits-dialog'));

import GroupAgentSkillsTab from '../GroupAgentSkillsTab';
import GroupMCPServersTab from '../GroupMCPServersTab';

const GROUP = { id: 'rg-1', name: 'eng', display_name: 'Eng' } as ResourceGroup;
const onUpdate = vi.fn();

const surfaces = [
    {
        noun: 'skills',
        Tab: GroupAgentSkillsTab,
        load: gateway.fetchEffectiveAgentSkills,
        remove: gateway.removeSkillFromGroup,
        keys: { direct: 'direct_skills', inherited: 'inherited_skills', effective: 'effective_skills' },
        own: { id: 'own', name: 'summarize', description: 'makes it shorter', category: 'text' },
        heir: { id: 'heir', name: 'translate' },
        confirmWord: 'Summarize',
        removedToast: 'Removed Summarize from Eng',
        inheritanceTitle: 'Learn about skill inheritance',
        inheritanceHeading: 'What is Skill Inheritance?',
        addTitle: 'Learn how to add skills',
        addHeading: 'How to Add Skills to a Group',
        placeholder: 'Search skills...',
        emptyNames: { All: 'No agent skills available', Direct: 'No direct skills assigned yet', Inherited: 'No inherited skills from parent groups' },
        noMatch: 'No skills found matching your search',
    },
    {
        noun: 'servers',
        Tab: GroupMCPServersTab,
        load: gateway.fetchEffectiveServerConfigs,
        remove: gateway.removeServerFromGroup,
        keys: { direct: 'direct_configs', inherited: 'inherited_configs', effective: 'effective_configs' },
        own: { id: 'own', name: 'github' },
        heir: { id: 'heir', name: 'jira' },
        confirmWord: 'github',
        removedToast: 'Removed github from Eng',
        inheritanceTitle: 'Learn about server inheritance',
        inheritanceHeading: 'What is Server Inheritance?',
        addTitle: 'Learn how to add servers',
        addHeading: 'How to Add Servers to a Resource Group',
        placeholder: 'Search servers...',
        emptyNames: { All: 'No MCP servers available', Direct: 'No direct servers assigned yet', Inherited: 'No inherited servers from parent groups' },
        noMatch: 'No servers found matching your search',
    },
];

beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    for (const fn of Object.values(gateway)) fn.mockReset();
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe.each(surfaces)('Group $noun tab', (s) => {
    const payload = (direct: object[], inherited: object[]) => ({
        [s.keys.direct]: direct,
        [s.keys.inherited]: inherited,
        [s.keys.effective]: [...direct, ...inherited],
        direct_count: direct.length,
        inherited_count: inherited.length,
        effective_count: direct.length + inherited.length,
    });

    beforeEach(() => {
        s.load.mockResolvedValue(payload([s.own], [s.heir]));
        s.remove.mockResolvedValue({});
    });

    async function open() {
        render(<s.Tab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText(s.own.name);
        expect(s.load).toHaveBeenCalledWith('rg-1');
    }
    const row = (id: string) => within(screen.getByTestId(`row-${id}`));

    it('splits the views as the server did, and search narrows them', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Direct (1)' }));
        expect(screen.queryByText(s.heir.name)).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'Inherited (1)' }));
        expect(screen.queryByText(s.own.name)).toBeNull();
        expect(screen.getByText(/inherited from parent groups/)).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'All (2)' }));
        const search = screen.getByPlaceholderText(s.placeholder);
        fireEvent.change(search, { target: { value: s.heir.name.toUpperCase() } });
        expect(screen.queryByText(s.own.name)).toBeNull();
        expect(screen.getByText(s.heir.name)).toBeInTheDocument();

        fireEvent.change(search, { target: { value: 'zzz' } });
        expect(screen.getByText(s.noMatch)).toBeInTheDocument();
    });

    it.each(['All', 'Direct', 'Inherited'] as const)('the empty %s view says so', async (label) => {
        s.load.mockResolvedValue(payload([], []));
        render(<s.Tab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText(s.emptyNames.All);
        fireEvent.click(screen.getByRole('button', { name: `${label} (0)` }));
        expect(screen.getByText(s.emptyNames[label])).toBeInTheDocument();
    });

    it('removes from this group after typed confirmation, then reloads', async () => {
        await open();
        fireEvent.click(row('own').getByText('remove'));
        const input = await screen.findByPlaceholderText(`Enter "${s.confirmWord}"`);
        fireEvent.change(input, { target: { value: s.confirmWord } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(s.remove).toHaveBeenCalledWith('rg-1', 'own'));
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith(s.removedToast));
        expect(onUpdate).toHaveBeenCalledTimes(1);
        expect(s.load).toHaveBeenCalledTimes(2);
        expect(screen.queryByPlaceholderText(`Enter "${s.confirmWord}"`)).toBeNull();
    });

    it('a refused removal stays open and shows why', async () => {
        s.remove.mockRejectedValue({ detail: 'still referenced' });
        await open();
        fireEvent.click(row('own').getByText('remove'));
        const input = await screen.findByPlaceholderText(`Enter "${s.confirmWord}"`);
        fireEvent.change(input, { target: { value: s.confirmWord } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(await screen.findByText('still referenced')).toBeInTheDocument();
        expect(toast.success).not.toHaveBeenCalled();
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('opens per-item dialogs for this group and reloads when they save', async () => {
        await open();
        fireEvent.click(row('own').getByText('limits'));
        expect(screen.getByTestId('limits-dialog')).toHaveTextContent('rg-1');
        fireEvent.click(screen.getByText('dialog-save'));
        await waitFor(() => expect(s.load).toHaveBeenCalledTimes(2));
        fireEvent.click(await screen.findByText('dialog-close'));
        expect(screen.queryByTestId('limits-dialog')).toBeNull();

        fireEvent.click((await screen.findByTestId('row-own')).querySelectorAll('button')[2]);
        expect(screen.getByTestId('second-dialog')).toHaveTextContent('rg-1');
        fireEvent.click(screen.getByText('dialog-close'));
        expect(screen.queryByTestId('second-dialog')).toBeNull();
    });

    it('shows the two explainers and closes them', async () => {
        await open();
        fireEvent.click(screen.getByTitle(s.inheritanceTitle));
        expect(screen.getByText(s.inheritanceHeading)).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Got it' }));
        expect(screen.queryByText(s.inheritanceHeading)).toBeNull();

        fireEvent.click(screen.getByTitle(s.addTitle));
        expect(screen.getByText(s.addHeading)).toBeInTheDocument();
    });

    it('shows a load failure', async () => {
        s.load.mockRejectedValue(new Error('cp down'));
        render(<s.Tab group={GROUP} onUpdate={onUpdate} />);
        expect(await screen.findByText('cp down')).toBeInTheDocument();
    });
});

describe('GroupAgentSkillsTab discovery switch', () => {
    beforeEach(() => {
        gateway.fetchEffectiveAgentSkills.mockResolvedValue({ effective_skills: [], effective_count: 0 });
    });

    const open = async () => {
        render(<GroupAgentSkillsTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('No agent skills available');
    };

    // The switch is `discovery_enabled` of the group's own skill governance.
    // The update states the whole governance object, so what the group
    // already declares is read first and sent back with the switch changed.
    it.each([
        ['Disabled', false, 'disabled'],
        ['Enabled', true, 'enabled'],
    ])('%s writes the switch into this group\'s own skill governance, keeps the rest, and tells the parent', async (label, value, word) => {
        gateway.getSkillGlobalGovernance.mockResolvedValue({
            skill_global_governance: { execution_mode: 'sandboxed', discovery_enabled: !value },
            inherited_skill_governance: { blocked_skill_ids: ['from-above'] },
        });
        gateway.updateSkillGlobalGovernance.mockResolvedValue({});
        await open();
        fireEvent.click(screen.getByRole('button', { name: label }));
        await waitFor(() =>
            expect(gateway.updateSkillGlobalGovernance).toHaveBeenCalledWith('rg-1', { execution_mode: 'sandboxed', discovery_enabled: value }),
        );
        expect(gateway.getSkillGlobalGovernance).toHaveBeenCalledWith('rg-1');
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith(expect.stringContaining(`Programmatic skill discovery ${word}`)),
        );
        expect(onUpdate).toHaveBeenCalledTimes(1);
    });

    it('a group that declares no skill governance gets one that holds the switch alone', async () => {
        gateway.getSkillGlobalGovernance.mockResolvedValue({ skill_global_governance: null });
        gateway.updateSkillGlobalGovernance.mockResolvedValue({});
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Disabled' }));
        await waitFor(() => expect(gateway.updateSkillGlobalGovernance).toHaveBeenCalledWith('rg-1', { discovery_enabled: false }));
    });

    it('a refused change is reported and the parent is not told', async () => {
        gateway.getSkillGlobalGovernance.mockResolvedValue({ skill_global_governance: null });
        gateway.updateSkillGlobalGovernance.mockRejectedValue({ detail: 'not allowed' });
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Disabled' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('not allowed'));
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('nothing is written when the group\'s governance cannot be read', async () => {
        gateway.getSkillGlobalGovernance.mockRejectedValue({ detail: 'no such group' });
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Disabled' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('no such group'));
        expect(gateway.updateSkillGlobalGovernance).not.toHaveBeenCalled();
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('searches description and category too', async () => {
        gateway.fetchEffectiveAgentSkills.mockResolvedValue({
            effective_skills: [
                { id: 'a', name: 'alpha', description: 'makes it shorter' },
                { id: 'b', name: 'beta', category: 'vision' },
            ],
            effective_count: 2,
        });
        render(<GroupAgentSkillsTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('alpha');
        const search = screen.getByPlaceholderText('Search skills...');
        fireEvent.change(search, { target: { value: 'shorter' } });
        expect(screen.queryByText('beta')).toBeNull();
        fireEvent.change(search, { target: { value: 'vision' } });
        expect(screen.queryByText('alpha')).toBeNull();
        expect(screen.getByText('beta')).toBeInTheDocument();
    });
});

describe('GroupMCPServersTab tool limits', () => {
    it('opens for the chosen server and reloads on save', async () => {
        gateway.fetchEffectiveServerConfigs.mockResolvedValue({
            effective_configs: [{ id: 's1', name: 'github' }],
            effective_count: 1,
        });
        render(<GroupMCPServersTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('github');
        fireEvent.click(screen.getByText('tool-limits'));
        expect(screen.getByTestId('tool-limits-dialog')).toHaveTextContent('rg-1');
        fireEvent.click(screen.getByText('dialog-save'));
        await waitFor(() => expect(gateway.fetchEffectiveServerConfigs).toHaveBeenCalledTimes(2));
    });
});
