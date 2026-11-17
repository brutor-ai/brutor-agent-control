/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent Skills list: load, filter, save, lifecycle, single and bulk delete.
 *
 * The thing worth pinning is the save funnel. The detail panel hands every save
 * to this tab. Limits and Policies each send only their own key, so a tab that
 * copied a fixed set of keys into the PATCH would send an empty body while the
 * panel says "saved", and lose name, version and disclosure mode the same way.
 * These tests hold the bytes that reach the API, not the panel's toast.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

import type { AgentSkillConfig } from '../../../lib/agentSkillTypes';
import { thrownByClient } from '../../../test/serverAnswers';

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({ default: toast }));

const gatewayClient = {
    listAgentSkills: vi.fn(),
    createAgentSkill: vi.fn(),
    updateAgentSkill: vi.fn(),
    deleteAgentSkill: vi.fn(),
    bulkDeleteAgentSkills: vi.fn(),
    validateAgentSkill: vi.fn(),
    publishAgentSkill: vi.fn(),
    deprecateAgentSkill: vi.fn(),
    archiveAgentSkill: vi.fn(),
    importAgentSkills: vi.fn(),
    listAgentSkillExecutions: vi.fn(),
};
const gateway = {
    gatewayClient,
    fetchGroupHierarchyTree: vi.fn(),
    listSkillResources: vi.fn(),
    listSkillScripts: vi.fn(),
    listSkillAssets: vi.fn(),
};
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

interface PanelProps {
    skill: AgentSkillConfig;
    initialTab: string;
    onClose: () => void;
    onCopy: () => void;
    onSave: (data: Record<string, unknown>) => Promise<string | undefined>;
    onDelete: () => void;
    onPublish: () => void;
    onValidate: () => void;
    onDeprecate: (reason: string) => void;
    onArchive: () => void;
    loadExecutions: (id: string, offset: number, limit: number) => Promise<unknown>;
    loadResources: (id: string) => Promise<unknown>;
    loadScripts: (id: string) => Promise<unknown>;
    loadAssets: (id: string) => Promise<unknown>;
    fetchGroupTree: () => Promise<unknown>;
    onUpdateGroups: (id: string, groups: string[]) => Promise<void>;
}

// The panel has its own surface; what is under test is what this tab does with
// the callbacks it hands over, so a test reaches them through this reference.
const panel = vi.hoisted((): { props: unknown } => ({ props: null }));
function panelProps(): PanelProps {
    return panel.props as PanelProps;
}

vi.mock('../AgentSkillDetailPanel', () => ({
    default: (props: PanelProps) => {
        panel.props = props;
        return (
            <div data-testid="panel">
                panel for {props.skill.name || '(new)'} on {props.initialTab}
                <button onClick={props.onClose}>close panel</button>
            </div>
        );
    },
}));
vi.mock('../AgentSkillImportModal', () => ({
    default: ({
        onClose,
        onImport,
        onImportComplete,
        limits,
    }: {
        onClose: () => void;
        onImport: (files: File[]) => unknown;
        onImportComplete: () => void;
        limits: { skill_max_bytes: number } | null;
    }) => (
        <div>
            import modal
            <span>limit {limits ? limits.skill_max_bytes : 'none'}</span>
            <button onClick={() => { onImport([new File(['x'], 's.zip')]); }}>do import</button>
            <button onClick={onImportComplete}>import done</button>
            <button onClick={onClose}>close import</button>
        </div>
    ),
}));

import AgentSkillsTab from '../AgentSkillsTab';

function skill(id: string, over: Partial<AgentSkillConfig> = {}): AgentSkillConfig {
    return {
        id,
        tenant_id: 't-1',
        name: id,
        description: `${id} description`,
        version: '1.2.0',
        status: 'draft',
        enabled: true,
        skill_md_hash: 'abcdef0123456789',
        execution_count: 1234,
        is_global: false,
        group_ids: [],
        ...over,
    };
}

const DRAFT = skill('summarize', {
    metadata: { category: 'Writing', tags: ['alpha', 'beta', 'c', 'd', 'e', 'f'], cost_estimate_usd: 0.5, approval_required: true },
    allowed_tools: ['a', 'b', 'c', 'd'],
    license: 'MIT',
    last_executed_at: '2026-07-01T00:00:00Z',
});
const PUBLISHED = skill('translate', {
    status: 'published',
    metadata: { category: 'Language', skill_type: 'composite' },
});
const DISABLED = skill('archive-me', { enabled: false, status: 'deprecated' });

function list(...skills: AgentSkillConfig[]) {
    return { skills: Object.fromEntries(skills.map((s) => [s.id, s])) };
}

function cardOf(name: string): HTMLElement {
    const el = screen.getByTitle(name).closest('.border-2');
    if (!(el instanceof HTMLElement)) throw new Error(`no card for ${name}`);
    return el;
}

function openMenu(name: string): HTMLElement {
    const card = cardOf(name);
    const trigger = card.querySelector('button');
    if (!trigger) throw new Error('no menu button');
    fireEvent.click(trigger);
    return card;
}

async function load() {
    render(<AgentSkillsTab />);
    await screen.findByTitle('summarize');
}

describe('AgentSkillsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        localStorage.clear();
        panel.props = null;
        vi.spyOn(console, 'error').mockImplementation(() => {});
        gatewayClient.listAgentSkills.mockResolvedValue(list(DRAFT, PUBLISHED, DISABLED));
        gatewayClient.createAgentSkill.mockResolvedValue({ skill: { id: 'new-id' }, auto_bound_server_group_ids: [] });
        gatewayClient.updateAgentSkill.mockResolvedValue({ auto_bound_server_group_ids: [] });
        gatewayClient.deleteAgentSkill.mockResolvedValue({});
        gatewayClient.validateAgentSkill.mockResolvedValue({ valid: true, errors: [] });
        gatewayClient.publishAgentSkill.mockResolvedValue({});
        gatewayClient.deprecateAgentSkill.mockResolvedValue({});
        gatewayClient.archiveAgentSkill.mockResolvedValue({});
        gatewayClient.bulkDeleteAgentSkills.mockResolvedValue({ deleted_count: 2, skipped_count: 0 });
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('loading and filtering', () => {
        it('asks for content and summarises what came back', async () => {
            await load();
            expect(gatewayClient.listAgentSkills).toHaveBeenCalledWith({ include_content: true });
            expect(screen.getByText(/3 skills total/)).toBeTruthy();
            expect(screen.getByText(/1 published/)).toBeTruthy();
            expect(screen.getByText(/1 drafts/)).toBeTruthy();
        });

        it('puts enabled skills first, then sorts by name', async () => {
            await load();
            const names = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
            expect(names).toEqual(['summarize', 'translate', 'archive-me']);
        });

        it('renders the card details that drive an operator decision', async () => {
            await load();
            const card = within(cardOf('summarize'));
            expect(card.getByText('+1 more')).toBeTruthy();
            expect(card.getByText('$0.50')).toBeTruthy();
            expect(card.getByText('Approval')).toBeTruthy();
            expect(card.getByText('abcdef01')).toBeTruthy();
            expect(card.getByText(/\+1 more$/, { selector: 'div' })).toBeTruthy();
            expect(card.getByText('MIT')).toBeTruthy();
            expect(within(cardOf('archive-me')).getByText('Disabled')).toBeTruthy();
            expect(within(cardOf('translate')).getByText('Composite')).toBeTruthy();
        });

        it('filters by name, description and tag, and says when nothing matches', async () => {
            await load();
            const box = screen.getByPlaceholderText('Search skills by name, description, tags...');

            fireEvent.change(box, { target: { value: 'BETA' } });
            expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['summarize']);

            fireEvent.change(box, { target: { value: 'translate description' } });
            expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['translate']);

            fireEvent.change(box, { target: { value: 'zzz' } });
            expect(screen.getByText('No Matching Skills')).toBeTruthy();
        });

        it('filters by status and category', async () => {
            await load();
            const [status, category] = screen.getAllByRole('combobox');

            fireEvent.change(status, { target: { value: 'published' } });
            expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['translate']);

            fireEvent.change(status, { target: { value: '' } });
            fireEvent.change(category, { target: { value: 'Other' } });
            expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['archive-me']);
        });

        it('switches to the list view and counts what is shown', async () => {
            await load();
            fireEvent.click(screen.getByTitle('List view'));
            expect(screen.getByText('Showing', { exact: false }).textContent).toContain('Showing 3 of 3 skills');
        });

        it('shows the empty state, which opens a new draft', async () => {
            gatewayClient.listAgentSkills.mockResolvedValue({ skills: {} });
            render(<AgentSkillsTab />);
            fireEvent.click(await screen.findByText('Create Your First Skill'));
            expect(screen.getByTestId('panel').textContent).toContain('panel for (new) on overview');
            expect(panelProps().skill.id).toBe('');
            expect(panelProps().skill.status).toBe('draft');
        });

        it('reports a load failure', async () => {
            gatewayClient.listAgentSkills.mockRejectedValue(new Error('down'));
            render(<AgentSkillsTab />);
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to load agent skills: down'));
            expect(await screen.findByText('No Agent Skills Yet')).toBeTruthy();
        });
    });

    describe('create via the panel', () => {
        async function openNew() {
            await load();
            fireEvent.click(screen.getByText('New Skill'));
        }

        it('sends the whole draft, including disclosure mode, and returns the new id', async () => {
            await openNew();
            let id: string | undefined;
            await panelProps().onSave({
                name: 'n1',
                description: 'd',
                skill_md_content: '# md',
                version: '2.0.0',
                license: 'MIT',
                compatibility: ['x'],
                allowed_tools: ['t'],
                disclosure_mode: 'full',
                metadata: { category: 'c' },
                enabled: false,
                group_ids: ['g-1'],
            }).then((r) => { id = r; });

            expect(id).toBe('new-id');
            expect(gatewayClient.createAgentSkill).toHaveBeenCalledWith({
                name: 'n1',
                description: 'd',
                skill_md_content: '# md',
                version: '2.0.0',
                license: 'MIT',
                compatibility: ['x'],
                allowed_tools: ['t'],
                disclosure_mode: 'full',
                metadata: { category: 'c' },
                enabled: false,
                group_ids: ['g-1'],
            });
            expect(toast.success).toHaveBeenCalledWith('Skill "n1" created');
        });

        it('refuses a draft with no SKILL.md without calling the API', async () => {
            await openNew();
            const result = await panelProps().onSave({ name: 'n1', description: 'd' });
            expect(result).toBeUndefined();
            expect(toast.error).toHaveBeenCalledWith('Missing required fields');
            expect(gatewayClient.createAgentSkill).not.toHaveBeenCalled();
        });

        it('explains the auto-bound execution server', async () => {
            gatewayClient.createAgentSkill.mockResolvedValue({ skill: { id: 'x' }, auto_bound_server_group_ids: ['g-1', 'g-2'] });
            await openNew();
            await panelProps().onSave({ name: 'n', description: 'd', skill_md_content: 'm' });
            expect(toast.success).toHaveBeenCalledWith(
                'Agent Skills execution server added to 2 groups so the skill can run.',
                { duration: 6000 },
            );
        });

        it('rethrows the server detail so the panel can show it', async () => {
            gatewayClient.createAgentSkill.mockRejectedValue(
                await thrownByClient(409, { detail: { code: 'dup', message: 'a skill of this name exists' } }),
            );
            await openNew();
            await expect(panelProps().onSave({ name: 'n', description: 'd', skill_md_content: 'm' })).rejects.toThrow('a skill of this name exists');

            gatewayClient.createAgentSkill.mockRejectedValue(await thrownByClient(409, { detail: 'name taken' }));
            await expect(panelProps().onSave({ name: 'n', description: 'd', skill_md_content: 'm' })).rejects.toThrow('name taken');

            gatewayClient.createAgentSkill.mockRejectedValue(new Error('gateway down'));
            await expect(panelProps().onSave({ name: 'n', description: 'd', skill_md_content: 'm' })).rejects.toThrow('gateway down');
        });
    });

    describe('update via the panel', () => {
        async function openEdit() {
            await load();
            openMenu('summarize');
            fireEvent.click(within(cardOf('summarize')).getByText('Edit'));
            expect(screen.getByTestId('panel').textContent).toContain('panel for summarize on overview');
        }

        /** What the wire would carry: JSON drops undefined. */
        function sentBody(): unknown {
            const body: unknown = gatewayClient.updateAgentSkill.mock.calls[0][1];
            return JSON.parse(JSON.stringify(body));
        }

        it('sends the limits the Limits tab saved', async () => {
            await openEdit();
            await panelProps().onSave({ tenant_limits: { max_rpm: 5 } });
            expect(gatewayClient.updateAgentSkill.mock.calls[0][0]).toBe('summarize');
            expect(sentBody()).toEqual({ tenant_limits: { max_rpm: 5 } });
        });

        it('sends no policy of the skill itself, which this release refuses', async () => {
            await openEdit();
            await panelProps().onSave({ tenant_limits: { execution: { daily_limit: 3 } }, tenant_governance: { allowed_tools: ['a'] } });
            expect(sentBody()).toEqual({ tenant_limits: { execution: { daily_limit: 3 } } });
        });

        it('sends name, version and disclosure mode from Basic Info', async () => {
            await openEdit();
            await panelProps().onSave({
                name: 'renamed',
                version: '1.3.0',
                description: 'd',
                license: 'MIT',
                enabled: false,
                disclosure_mode: 'full',
                compatibility: ['c'],
                allowed_tools: ['t'],
            });
            expect(sentBody()).toEqual({
                name: 'renamed',
                version: '1.3.0',
                description: 'd',
                license: 'MIT',
                enabled: false,
                disclosure_mode: 'full',
                compatibility: ['c'],
                allowed_tools: ['t'],
            });
            expect(toast.success).toHaveBeenCalledWith('Skill updated');
            await waitFor(() => expect(gatewayClient.listAgentSkills).toHaveBeenCalledTimes(2));
        });

        it('sends SKILL.md, metadata and groups, and explains an auto-bind', async () => {
            gatewayClient.updateAgentSkill.mockResolvedValue({ auto_bound_server_group_ids: ['g-1'] });
            await openEdit();
            await panelProps().onSave({ skill_md_content: 'm', metadata: { a: 1 }, group_ids: ['g-1'] });
            expect(sentBody()).toEqual({ skill_md_content: 'm', metadata: { a: 1 }, group_ids: ['g-1'] });
            expect(toast.success).toHaveBeenCalledWith(
                'Agent Skills execution server added to 1 group so the skill can run.',
                { duration: 6000 },
            );
        });

        it('never prints the input of a validation error', async () => {
            // A 422 repeats the `input` that failed; the panel shows where and what.
            await openEdit();
            gatewayClient.updateAgentSkill.mockRejectedValue(
                await thrownByClient(422, {
                    detail: [{ loc: ['body', 'name'], msg: 'too long', input: 'typed-skill-name', type: 'string_too_long' }],
                }),
            );
            const refused: unknown = await panelProps().onSave({ description: 'd' }).catch((e: unknown) => e);
            expect(refused).toBeInstanceOf(Error);
            expect((refused as Error).message).toBe('name: too long');
            expect(JSON.stringify(refused, Object.getOwnPropertyNames(refused as object))).not.toMatch(/typed-skill-name|input/);
        });

        it('rethrows the server detail on failure and does not claim success', async () => {
            await openEdit();
            gatewayClient.updateAgentSkill.mockRejectedValue(
                await thrownByClient(409, { detail: 'published skills are immutable' }),
            );
            await expect(panelProps().onSave({ description: 'd' })).rejects.toThrow('published skills are immutable');
            expect(toast.success).not.toHaveBeenCalled();
        });

        it('assigns groups directly from the panel shortcut', async () => {
            gatewayClient.updateAgentSkill.mockResolvedValue({ auto_bound_server_group_ids: ['g-1'] });
            await openEdit();
            await panelProps().onUpdateGroups('summarize', ['g-1']);
            expect(gatewayClient.updateAgentSkill).toHaveBeenCalledWith('summarize', { group_ids: ['g-1'] });
            expect(toast.success).toHaveBeenCalledWith(
                'Agent Skills execution server added to 1 group so the skill can run.',
                { duration: 6000 },
            );
        });

        it('passes the panel its loaders', async () => {
            gateway.listSkillResources.mockResolvedValue({ resources: ['r'] });
            gateway.listSkillScripts.mockResolvedValue({});
            gateway.listSkillAssets.mockResolvedValue({ assets: ['a'] });
            gateway.fetchGroupHierarchyTree.mockResolvedValue({ tree: ['t'] });
            gatewayClient.listAgentSkillExecutions.mockResolvedValue({ executions: [] });
            await openEdit();

            const p = panelProps();
            expect(p).not.toHaveProperty('availableSkills');
            await expect(p.loadResources('s')).resolves.toEqual(['r']);
            await expect(p.loadScripts('s')).resolves.toEqual([]);
            await expect(p.loadAssets('s')).resolves.toEqual(['a']);
            await expect(p.fetchGroupTree()).resolves.toEqual(['t']);
            await p.loadExecutions('s', 10, 5);
            expect(gatewayClient.listAgentSkillExecutions).toHaveBeenCalledWith('s', { offset: 10, limit: 5 });
        });

        it('closes', async () => {
            await openEdit();
            fireEvent.click(screen.getByText('close panel'));
            expect(screen.queryByTestId('panel')).toBeNull();
        });
    });

    describe('lifecycle from the card menu', () => {
        it('validates and reports the outcome', async () => {
            await load();
            const card = openMenu('summarize');
            fireEvent.click(within(card).getByText('Validate'));
            await waitFor(() => expect(gatewayClient.validateAgentSkill).toHaveBeenCalledWith('summarize'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Skill validated successfully'));
            await waitFor(() => expect(gatewayClient.listAgentSkills).toHaveBeenCalledTimes(2));
        });

        it('reports the error count of a failed validation', async () => {
            gatewayClient.validateAgentSkill.mockResolvedValue({ valid: false, errors: ['a', 'b'] });
            await load();
            fireEvent.click(within(openMenu('summarize')).getByText('Validate'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Validation failed: 2 error(s)'));
        });

        it('reports a validate request that fails', async () => {
            gatewayClient.validateAgentSkill.mockRejectedValue(new Error('boom'));
            await load();
            fireEvent.click(within(openMenu('summarize')).getByText('Validate'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('boom'));
        });

        it('publishes, and reports failure', async () => {
            await load();
            fireEvent.click(within(openMenu('summarize')).getByText('Publish'));
            await waitFor(() => expect(gatewayClient.publishAgentSkill).toHaveBeenCalledWith('summarize'));
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith('Skill published! It is now immutable and available for execution.'),
            );

            gatewayClient.publishAgentSkill.mockRejectedValue(new Error('not validated'));
            fireEvent.click(within(openMenu('summarize')).getByText('Publish'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('not validated'));
        });

        it('archives, and reports failure', async () => {
            await load();
            fireEvent.click(within(openMenu('archive-me')).getByText('Archive'));
            await waitFor(() => expect(gatewayClient.archiveAgentSkill).toHaveBeenCalledWith('archive-me'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Skill archived'));

            gatewayClient.archiveAgentSkill.mockRejectedValue(new Error('still published'));
            fireEvent.click(within(openMenu('archive-me')).getByText('Archive'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('still published'));
        });

        it('deprecates with the reason from the panel, and reports failure', async () => {
            await load();
            fireEvent.click(within(openMenu('translate')).getByText('View'));
            panelProps().onDeprecate('replaced');
            await waitFor(() => expect(gatewayClient.deprecateAgentSkill).toHaveBeenCalledWith('translate', 'replaced'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Skill deprecated'));

            gatewayClient.deprecateAgentSkill.mockRejectedValue(new Error('nope'));
            panelProps().onDeprecate('again');
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('nope'));
        });

        it('offers View on a published composite, not Edit, and no Workflow', async () => {
            await load();
            const card = within(openMenu('translate'));
            expect(card.queryByText('Edit')).toBeNull();
            expect(card.queryByText('Workflow')).toBeNull();
            fireEvent.click(card.getByText('View'));
            expect(screen.getByTestId('panel').textContent).toContain('panel for translate on overview');
        });

        it('duplicates as a new draft without server-managed fields', async () => {
            await load();
            fireEvent.click(within(openMenu('summarize')).getByText('Duplicate'));

            const copy = panelProps().skill;
            expect(copy.id).toBe('');
            expect(copy.name).toBe('summarize-copy');
            expect(copy.status).toBe('draft');
            expect(copy.execution_count).toBe(0);
            expect(copy.skill_md_hash).toBe('');
            expect(copy.last_executed_at).toBeUndefined();
            expect(copy.description).toBe('summarize description');
            expect(copy.allowed_tools).toEqual(['a', 'b', 'c', 'd']);
        });
    });

    describe('delete', () => {
        async function askDelete() {
            await load();
            fireEvent.click(within(openMenu('summarize')).getByText('Delete'));
            fireEvent.change(screen.getByPlaceholderText('Enter "summarize"'), { target: { value: 'summarize' } });
        }

        function confirm() {
            fireEvent.click(screen.getByRole('button', { name: /^Delete Agent Skill/ }));
        }

        it('deletes after the typed confirmation and reloads', async () => {
            await askDelete();
            confirm();
            await waitFor(() => expect(gatewayClient.deleteAgentSkill).toHaveBeenCalledWith('summarize'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Skill "summarize" deleted'));
            await waitFor(() => expect(gatewayClient.listAgentSkills).toHaveBeenCalledTimes(2));
            expect(screen.queryByPlaceholderText('Enter "summarize"')).toBeNull();
        });

        it('shows the server reason and keeps the dialog open', async () => {
            gatewayClient.deleteAgentSkill.mockRejectedValue(
                await thrownByClient(409, { detail: 'published skills cannot be deleted' }),
            );
            await askDelete();
            confirm();
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('published skills cannot be deleted'));
            expect(screen.getByPlaceholderText('Enter "summarize"')).toBeTruthy();
            expect(gatewayClient.listAgentSkills).toHaveBeenCalledTimes(1);
        });

        it('deleting from the panel closes it and asks for confirmation', async () => {
            await load();
            fireEvent.click(within(openMenu('summarize')).getByText('Edit'));
            const { onDelete } = panelProps();
            await waitFor(() => expect(screen.getByTestId('panel')).toBeTruthy());
            React.act(() => onDelete());

            expect(screen.queryByTestId('panel')).toBeNull();
            expect(screen.getByPlaceholderText('Enter "summarize"')).toBeTruthy();
            expect(gatewayClient.deleteAgentSkill).not.toHaveBeenCalled();
        });
    });

    describe('bulk delete', () => {
        async function select() {
            await load();
            fireEvent.click(screen.getByTitle('Select skills to bulk-delete'));
            fireEvent.click(screen.getByRole('checkbox', { name: 'Select summarize' }));
            fireEvent.click(screen.getByRole('checkbox', { name: 'Select translate' }));
            fireEvent.click(screen.getByRole('button', { name: 'Delete (2)' }));
        }

        function confirmBulk() {
            const heading = screen.getByText('Delete 2 skills?');
            const dialog = heading.closest('.max-w-md');
            if (!(dialog instanceof HTMLElement)) throw new Error('no dialog');
            fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
        }

        it('warns about the published ones and sends the selected ids', async () => {
            await select();
            expect(screen.getByText(/1 published skill will/).textContent).toContain('1 skill will be deleted.');

            confirmBulk();
            await waitFor(() => expect(gatewayClient.bulkDeleteAgentSkills).toHaveBeenCalledWith(['summarize', 'translate']));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Deleted 2 skills'));
            await waitFor(() => expect(screen.queryByText('Delete 2 skills?')).toBeNull());
            expect(screen.queryByRole('checkbox', { name: 'Select summarize' })).toBeNull();
        });

        it('says out loud what was skipped', async () => {
            gatewayClient.bulkDeleteAgentSkills.mockResolvedValue({ deleted_count: 1, skipped_count: 1 });
            await select();
            confirmBulk();
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Deleted 1 skill'));
            expect(toast.error).toHaveBeenCalledWith(
                'Skipped 1 skill (published skills must be deprecated then archived first)',
                { duration: 7000 },
            );
        });

        it('keeps the dialog open with the reason when it fails', async () => {
            gatewayClient.bulkDeleteAgentSkills.mockRejectedValue(new Error('gateway down'));
            await select();
            confirmBulk();
            expect(await screen.findByText('gateway down')).toBeTruthy();
            expect(screen.getByText('Delete 2 skills?')).toBeTruthy();
            expect(toast.error).toHaveBeenCalledWith('gateway down');
        });

        it('selects all that are visible, clears, and leaves selection mode', async () => {
            await load();
            fireEvent.click(screen.getByTitle('Select skills to bulk-delete'));
            const all = screen.getByRole('checkbox', { name: 'Select all 3' });

            fireEvent.click(all);
            expect(screen.getByText('3 selected')).toBeTruthy();
            fireEvent.click(screen.getByText('Clear'));
            expect(screen.getByText('Select all 3')).toBeTruthy();

            fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
            expect(screen.queryByText(/Delete \d skills?\?/)).toBeNull();

            fireEvent.click(screen.getByTitle('Exit selection mode'));
            expect(screen.queryByText('Select all 3')).toBeNull();
        });
    });

    describe('import', () => {
        it('passes files to the API and reloads when done', async () => {
            await load();
            fireEvent.click(screen.getByText('Import'));
            fireEvent.click(screen.getByText('do import'));
            expect(gatewayClient.importAgentSkills).toHaveBeenCalledTimes(1);

            fireEvent.click(screen.getByText('import done'));
            await waitFor(() => expect(gatewayClient.listAgentSkills).toHaveBeenCalledTimes(2));

            fireEvent.click(screen.getByText('close import'));
            expect(screen.queryByText('import modal')).toBeNull();
        });

        it('hands the dialog the limits the list of skills states', async () => {
            gatewayClient.listAgentSkills.mockResolvedValue({
                ...list(DRAFT),
                limits: { skill_max_bytes: 30_000_000, archive_max_bytes: 300_000_000, archive_max_skills: 50 },
            });
            await load();
            fireEvent.click(screen.getByText('Import'));
            expect(screen.getByText('limit 30000000')).toBeTruthy();
        });
    });
});
