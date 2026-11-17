/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * RG-resource per-resource dialogs — field→payload round-trips.
 *
 * These five dialogs are HAND-ROLLED (not built on the shared
 * presentational forms), so they carry independent drift risk. Each
 * dialog fetches its current config via the gateway hook, renders its
 * own fields, and on Save maps them back into a nested payload passed to
 * the gateway update method. These tests mock the gateway/toast hooks,
 * render each dialog in isolation, fill its fields, click Save, and
 * assert the update method was called with the correct nested shape.
 *
 * Covered:
 *   • LLMGovernanceDialog     — temperature / context_window / tool_use /
 *                               system_prompt (incl. mandatory fragment).
 *   • MCPServerLimitsDialog   — all execution / data / frequency fields.
 *   • MCPToolLimitsDialog     — per-tool frequency (5 fields) → per_tool map.
 *   • SkillLimitsDialog       — execution daily/monthly + warnings.
 *   • SkillGovernanceDialog   — execution_mode + approval_timeout_seconds
 *                               (asserts EXACTLY the reduced set).
 */

import { describe, expect, it, vi, beforeEach, type Mock } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import LLMGovernanceDialog from '../components/groups/subtabs/LLMGovernanceDialog';
import MCPServerLimitsDialog from '../components/groups/subtabs/MCPServerLimitsDialog';
import MCPToolLimitsDialog from '../components/groups/subtabs/MCPToolLimitsDialog';
import SkillLimitsDialog from '../components/groups/subtabs/SkillLimitsDialog';
import SkillGovernanceDialog from '../components/groups/subtabs/SkillGovernanceDialog';
import type { RefusedDocument } from '../lib/refusedSettings';
import type { ServerConfigInfo } from '../lib/groupTypes';
import { STORED_KEPT, STORED_WITH_REFUSED, controlPlaneRefusal } from '../test/controlPlaneRefusal';

// ---- Hook mocks -----------------------------------------------------------

const _toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('../components/common/Toast', () => ({ useToast: () => _toast }));

const gw = {
    // LLM governance
    getLLMModelGovernance: vi.fn(),
    updateLLMModelGovernance: vi.fn(),
    clearLLMModelGovernance: vi.fn(),
    // MCP server / tool limits
    fetchMCPServerLimits: vi.fn(),
    updateMCPServerLimits: vi.fn(),
    clearMCPServerLimits: vi.fn(),
    getMCPCapabilityFilters: vi.fn(),
    // Skill limits / governance
    fetchSkillItemLimits: vi.fn(),
    updateSkillItemLimits: vi.fn(),
    clearSkillItemLimits: vi.fn(),
    getSkillGovernance: vi.fn(),
    updateSkillGovernance: vi.fn(),
    clearSkillGovernance: vi.fn(),
};
vi.mock('../hooks/useGateway', () => ({ useGateway: () => gw }));

// ---- Fixtures -------------------------------------------------------------

const GID = 'group-1';
const GNAME = 'Engineering';
const model = { id: 'model-1', name: 'GPT-4o' } as any;
const server = { id: 'server-1', name: 'Postgres MCP' } as any;
const skill = { id: 'skill-1', name: 'Summarize' } as any;

const inputFor = (label: string): HTMLInputElement =>
    screen.getByText(label).parentElement!.querySelector('input')!;

beforeEach(() => {
    for (const fn of Object.values(gw)) (fn as any).mockReset();
    for (const fn of Object.values(_toast)) (fn as any).mockReset();
    // Sensible resolving defaults; individual tests override the getters.
    gw.updateLLMModelGovernance.mockResolvedValue(undefined);
    gw.updateMCPServerLimits.mockResolvedValue(undefined);
    gw.updateSkillItemLimits.mockResolvedValue(undefined);
    gw.updateSkillGovernance.mockResolvedValue(undefined);
});

// ---------------------------------------------------------------------------
// LLMGovernanceDialog
// ---------------------------------------------------------------------------

describe('LLMGovernanceDialog', () => {
    // Seed each section non-empty so the collapsible sections auto-expand.
    const seed = () =>
        gw.getLLMModelGovernance.mockResolvedValue({
            governance: {
                temperature: { max: 0.7 },
                context_window: { max_input_tokens: 4096 },
                tool_use: { access: 'enabled' },
                system_prompt: { banned_patterns: ['x'] },
            },
            inherited_governance: null,
        });

    it('exposes all four governance sections', async () => {
        seed();
        render(
            <LLMGovernanceDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                model={model}
            />,
        );
        for (const s of ['Temperature', 'Context Window', 'Tool Use', 'System Prompt']) {
            expect(await screen.findByText(s)).toBeInTheDocument();
        }
    });

    it('saves the correct nested shape across all four sections', async () => {
        seed();
        render(
            <LLMGovernanceDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                model={model}
            />,
        );
        await screen.findByText('Max Temperature');

        // temperature.max + temperature.enforce
        fireEvent.change(inputFor('Max Temperature'), { target: { value: '1.5' } });
        const enforceLabels = screen.getAllByText('Enforce Mode');
        fireEvent.change(enforceLabels[0].parentElement!.querySelector('select')!, {
            target: { value: 'clamp' },
        });
        // context_window.max_output_tokens + context_window.enforce
        fireEvent.change(inputFor('Max Output Tokens'), { target: { value: '2048' } });
        fireEvent.change(enforceLabels[1].parentElement!.querySelector('select')!, {
            target: { value: 'reject' },
        });
        // tool_use.allowed_tools (comma text -> array)
        fireEvent.change(inputFor('Allowed Tools (comma-separated)'), {
            target: { value: 'search, calc' },
        });
        // system_prompt.banned_patterns (comma text -> array)
        fireEvent.change(inputFor('Banned Patterns (comma-separated)'), {
            target: { value: 'ignore, act' },
        });

        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() =>
            expect(gw.updateLLMModelGovernance).toHaveBeenCalled(),
        );
        expect(gw.updateLLMModelGovernance).toHaveBeenCalledWith(
            GID,
            model.id,
            expect.objectContaining({
                temperature: expect.objectContaining({ max: 1.5, enforce: 'clamp' }),
                context_window: expect.objectContaining({
                    max_input_tokens: 4096,
                    max_output_tokens: 2048,
                    enforce: 'reject',
                }),
                tool_use: expect.objectContaining({
                    access: 'enabled',
                    allowed_tools: ['search', 'calc'],
                }),
                system_prompt: expect.objectContaining({
                    banned_patterns: ['ignore', 'act'],
                }),
            }),
        );
    });

    it('offers enabled and disabled as tool access, with no approval and no approval timeout', async () => {
        seed();
        render(
            <LLMGovernanceDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                model={model}
            />,
        );
        const access = (await screen.findByText('Access')).parentElement!.querySelector('select')!;
        expect(Array.from(access.options).map((o) => o.value)).toEqual(['', 'enabled', 'disabled']);
        expect(screen.queryByText('Approval Timeout (seconds)')).toBeNull();
        expect(screen.getByTestId('tool-use-no-approval')).toHaveTextContent(
            'A model\'s tool calls are not held for approval. To have a person approve a call, put the approval on the tool itself: an MCP capability, a skill, or an argument policy on the tool\'s own surface.',
        );
    });

    it('shows the Control Plane\'s refusal of a save as it words it', async () => {
        seed();
        const said =
            "tool_use.access: Approvals are not available for model tool calls: 'approval_required' is not a tool-use access. Use 'enabled' or 'disabled', and put an approval on the tool itself (an MCP capability, a skill, an argument policy on its own surface).";
        gw.updateLLMModelGovernance.mockRejectedValue(new Error(said));
        render(
            <LLMGovernanceDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                model={model}
            />,
        );
        await screen.findByText('Max Temperature');
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        expect(await screen.findByText(said)).toBeInTheDocument();
    });

    it('saves a mandatory system-prompt fragment with position + text', async () => {
        gw.getLLMModelGovernance.mockResolvedValue({
            governance: { system_prompt: { banned_patterns: ['x'] } },
            inherited_governance: null,
        });
        render(
            <LLMGovernanceDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                model={model}
            />,
        );
        await screen.findByText('Add Fragment');
        fireEvent.click(screen.getByText('Add Fragment'));

        fireEvent.change(screen.getByPlaceholderText('Fragment text...'), {
            target: { value: 'Always be concise.' },
        });
        // Only the fragment position select is present (system_prompt-only expand).
        fireEvent.change(screen.getByRole('combobox'), { target: { value: 'append' } });

        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() =>
            expect(gw.updateLLMModelGovernance).toHaveBeenCalled(),
        );
        expect(gw.updateLLMModelGovernance).toHaveBeenCalledWith(
            GID,
            model.id,
            expect.objectContaining({
                system_prompt: expect.objectContaining({
                    mandatory_fragments: [{ position: 'append', text: 'Always be concise.' }],
                }),
            }),
        );
    });
});

// ---------------------------------------------------------------------------
// MCPServerLimitsDialog
// ---------------------------------------------------------------------------

describe('MCPServerLimitsDialog', () => {
    it('saves every execution / data / frequency field it offers under the right key', async () => {
        gw.fetchMCPServerLimits.mockResolvedValue({
            limits: {},
            effective_global_limits: {},
            capacity: null,
        });
        render(
            <MCPServerLimitsDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                server={server}
            />,
        );
        await screen.findByText('Timeout (seconds)');

        const fills: [string, number][] = [
            ['Timeout (seconds)', 30],
            ['Max Concurrency / User', 4],
            ['Max Concurrency / Group', 16],
            ['Max Payload Size (MB)', 2],
            ['Max Calls / Minute', 60],
            ['Max Calls / Hour', 1000],
            ['Max Calls / Day', 5000],
            ['Burst Limit', 10],
            ['Cooldown (seconds)', 45],
        ];
        for (const [label, val] of fills) {
            fireEvent.change(inputFor(label), { target: { value: String(val) } });
        }

        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() => expect(gw.updateMCPServerLimits).toHaveBeenCalled());
        expect(gw.updateMCPServerLimits).toHaveBeenCalledWith(GID, server.id, {
            execution: {
                timeout_seconds: 30,
                max_concurrency_per_user: 4,
                max_concurrency_per_group: 16,
            },
            data: {
                max_payload_size_mb: 2,
            },
            frequency: {
                max_calls_per_minute: 60,
                max_calls_per_hour: 1000,
                max_calls_per_day: 5000,
                burst_limit: 10,
                cooldown_seconds: 45,
            },
        });
    });

    it('says what each execution and data limit does on this server binding', async () => {
        gw.fetchMCPServerLimits.mockResolvedValue({ limits: {}, effective_global_limits: {}, capacity: null });
        render(<MCPServerLimitsDialog isOpen onClose={vi.fn()} groupId={GID} groupName={GNAME} server={server as ServerConfigInfo} />);
        await screen.findByText('Timeout (seconds)');
        expect(screen.getByTestId('limit-help-execution.max_concurrency_per_group')).toHaveTextContent(
            'Simultaneous tools/call to this server from this group, across the cluster. Past it: 429 rate_limit_exceeded, scope server, with Retry-After.',
        );
        expect(screen.getByTestId('limit-help-execution.max_concurrency_per_user')).toHaveTextContent(
            'scope server-user, with Retry-After.',
        );
        expect(screen.getByTestId('limit-help-execution.timeout_seconds')).toHaveTextContent('The listener stream is exempt.');
        expect(screen.getByTestId('limit-help-data.max_payload_size_mb')).toHaveTextContent('A request is never above 10 MiB');
    });

    it.each([
        ['Max Concurrency / User', '2.5', 'Max Concurrency (per user) must be a whole number of at least 1.'],
        ['Max Concurrency / Group', '0', 'Max Concurrency (per group) must be a whole number of at least 1.'],
        ['Timeout (seconds)', '-1', 'Timeout (seconds) must be a positive number.'],
        ['Max Payload Size (MB)', '0', 'Max Payload Size (MB) must be a positive number.'],
    ])('%s of %s is not sent, and the dialog says why', async (label, typed, sentence) => {
        gw.fetchMCPServerLimits.mockResolvedValue({ limits: {}, effective_global_limits: {}, capacity: null });
        render(<MCPServerLimitsDialog isOpen onClose={vi.fn()} groupId={GID} groupName={GNAME} server={server as ServerConfigInfo} />);
        await screen.findByText('Timeout (seconds)');
        fireEvent.change(inputFor(label), { target: { value: typed } });
        expect(screen.getByTestId('mcp-limits-problem')).toHaveTextContent(sentence);
        const save = screen.getByRole('button', { name: 'Save' });
        expect(save).toBeDisabled();
        fireEvent.click(save);
        expect(gw.updateMCPServerLimits).not.toHaveBeenCalled();
    });

    it("shows the Control Plane's refusal of a save as it words it", async () => {
        gw.fetchMCPServerLimits.mockResolvedValue({ limits: {}, effective_global_limits: {}, capacity: null });
        gw.updateMCPServerLimits.mockRejectedValue(new Error('Max concurrency must be at least 1'));
        render(<MCPServerLimitsDialog isOpen onClose={vi.fn()} groupId={GID} groupName={GNAME} server={server as ServerConfigInfo} />);
        await screen.findByText('Timeout (seconds)');
        fireEvent.change(inputFor('Timeout (seconds)'), { target: { value: '30' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        expect(await screen.findByText('Max concurrency must be at least 1')).toBeInTheDocument();
    });

    it('preserves untouched per_tool overrides on save', async () => {
        gw.fetchMCPServerLimits.mockResolvedValue({
            limits: { per_tool: { search: { max_calls_per_minute: 5 } } },
            effective_global_limits: {},
            capacity: null,
        });
        render(
            <MCPServerLimitsDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                server={server}
            />,
        );
        await screen.findByText('Timeout (seconds)');
        fireEvent.change(inputFor('Timeout (seconds)'), { target: { value: '30' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() => expect(gw.updateMCPServerLimits).toHaveBeenCalled());
        expect(gw.updateMCPServerLimits).toHaveBeenCalledWith(
            GID,
            server.id,
            expect.objectContaining({
                execution: expect.objectContaining({ timeout_seconds: 30 }),
                per_tool: { search: { max_calls_per_minute: 5 } },
            }),
        );
    });
});

// ---------------------------------------------------------------------------
// MCPToolLimitsDialog
// ---------------------------------------------------------------------------

describe('MCPToolLimitsDialog', () => {
    it('saves all five per-tool frequency fields under per_tool', async () => {
        gw.fetchMCPServerLimits.mockResolvedValue({ limits: {} });
        gw.getMCPCapabilityFilters.mockResolvedValue({
            discovered_capabilities: [
                { capability_type: 'tool', name: 'run_query', description: 'runs a query' },
            ],
        });
        render(
            <MCPToolLimitsDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                server={server}
            />,
        );
        // Row appears once discovery resolves.
        await screen.findByText('run_query');

        // FREQUENCY_FIELDS order: /min, /hour, /day, burst, cooldown.
        const spin = screen.getAllByRole('spinbutton');
        expect(spin).toHaveLength(5);
        const vals = [60, 1000, 5000, 10, 45];
        vals.forEach((v, i) => fireEvent.change(spin[i], { target: { value: String(v) } }));

        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() => expect(gw.updateMCPServerLimits).toHaveBeenCalled());
        expect(gw.updateMCPServerLimits).toHaveBeenCalledWith(GID, server.id, {
            per_tool: {
                run_query: {
                    max_calls_per_minute: 60,
                    max_calls_per_hour: 1000,
                    max_calls_per_day: 5000,
                    burst_limit: 10,
                    cooldown_seconds: 45,
                },
            },
        });
    });
});

// ---------------------------------------------------------------------------
// SkillLimitsDialog
// ---------------------------------------------------------------------------

describe('SkillLimitsDialog', () => {
    it('saves execution daily/monthly limits + warning percents', async () => {
        gw.fetchSkillItemLimits.mockResolvedValue({
            limits: {},
            effective_global_limits: {},
            capacity: null,
        });
        render(
            <SkillLimitsDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                skill={skill}
            />,
        );
        await screen.findByText('Daily Limit');

        const fills: [string, number][] = [
            ['Daily Limit', 100],
            ['Monthly Limit', 2000],
            ['Daily Warning %', 80],
            ['Monthly Warning %', 90],
        ];
        for (const [label, val] of fills) {
            fireEvent.change(inputFor(label), { target: { value: String(val) } });
        }

        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() => expect(gw.updateSkillItemLimits).toHaveBeenCalled());
        expect(gw.updateSkillItemLimits).toHaveBeenCalledWith(GID, skill.id, {
            execution: {
                daily_limit: 100,
                monthly_limit: 2000,
                daily_warning_percent: 80,
                monthly_warning_percent: 90,
            },
        });
    });
});

// ---------------------------------------------------------------------------
// SkillGovernanceDialog
// ---------------------------------------------------------------------------

describe('SkillGovernanceDialog', () => {
    const RANGE = 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).';
    const openDialog = async () => {
        gw.getSkillGovernance.mockResolvedValue({ governance: {}, effective_global_governance: null });
        render(<SkillGovernanceDialog isOpen onClose={vi.fn()} groupId={GID} groupName={GNAME} skill={skill} />);
        await screen.findByText('Execution Mode');
    };

    it('bounds the approval timeout to 1 .. 604800 seconds and saves nothing outside it', async () => {
        await openDialog();
        const input = inputFor('Approval Timeout (seconds)');
        expect(input.min).toBe('1');
        expect(input.max).toBe('604800');
        for (const value of ['0', '604801']) {
            fireEvent.change(input, { target: { value } });
            expect(screen.getByRole('alert')).toHaveTextContent(RANGE);
            expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        }
        fireEvent.change(input, { target: { value: '604800' } });
        expect(screen.queryByRole('alert')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(gw.updateSkillGovernance).toHaveBeenCalledWith(GID, skill.id, { approval_timeout_seconds: 604800 }));
    });

    it('shows the Control Plane\'s refusal of a save as it words it', async () => {
        await openDialog();
        gw.updateSkillGovernance.mockRejectedValue(new Error('approval_timeout_seconds must be at most 604800 (7 days)'));
        fireEvent.change(inputFor('Approval Timeout (seconds)'), { target: { value: '120' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() =>
            expect(_toast.error).toHaveBeenCalledWith('Failed to save skill governance', 'approval_timeout_seconds must be at most 604800 (7 days)'),
        );
    });

    it('shows the Control Plane\'s refusal of a clear as it words it', async () => {
        await openDialog();
        gw.clearSkillGovernance.mockRejectedValue(new Error('You do not have the permission skill_governance:write'));
        fireEvent.click(screen.getByRole('button', { name: 'Clear Governance' }));
        await waitFor(() =>
            expect(_toast.error).toHaveBeenCalledWith('Failed to clear skill governance', 'You do not have the permission skill_governance:write'),
        );
        expect(_toast.success).not.toHaveBeenCalled();
    });

    it('shows why the governance could not be loaded', async () => {
        gw.getSkillGovernance.mockRejectedValue(new Error('Skill skill-1 is not in group group-1'));
        render(<SkillGovernanceDialog isOpen onClose={vi.fn()} groupId={GID} groupName={GNAME} skill={skill} />);
        await waitFor(() =>
            expect(_toast.error).toHaveBeenCalledWith('Failed to load skill governance', 'Skill skill-1 is not in group group-1'),
        );
    });

    it('saves EXACTLY execution_mode + approval_timeout_seconds (reduced set)', async () => {
        gw.getSkillGovernance.mockResolvedValue({
            governance: {},
            effective_global_governance: null,
        });
        render(
            <SkillGovernanceDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                skill={skill}
            />,
        );
        await screen.findByText('Execution Mode');

        fireEvent.change(screen.getByRole('combobox'), {
            target: { value: 'approval_required' },
        });
        fireEvent.change(inputFor('Approval Timeout (seconds)'), {
            target: { value: '120' },
        });

        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() => expect(gw.updateSkillGovernance).toHaveBeenCalled());
        expect(gw.updateSkillGovernance).toHaveBeenCalledWith(GID, skill.id, {
            execution_mode: 'approval_required',
            approval_timeout_seconds: 120,
        });
    });

    it('execution_mode select exposes the three governance modes', async () => {
        gw.getSkillGovernance.mockResolvedValue({
            governance: {},
            effective_global_governance: null,
        });
        render(
            <SkillGovernanceDialog
                isOpen
                onClose={vi.fn()}
                groupId={GID}
                groupName={GNAME}
                skill={skill}
            />,
        );
        await screen.findByText('Execution Mode');
        const opts = Array.from(
            screen.getByRole('combobox').querySelectorAll('option'),
        ).map((o) => (o).value);
        expect(opts).toEqual(['', 'unrestricted', 'approval_required', 'disabled']);
    });
});

// ---------------------------------------------------------------------------
// A stored setting the Control Plane refuses is not sent again
// ---------------------------------------------------------------------------

describe('the dialogs and a stored setting the Control Plane refuses', () => {
    /** The update stands in for the Control Plane: it refuses what it would refuse. */
    const refusing = (fn: Mock, kind: RefusedDocument) =>
        fn.mockImplementation((_g: string, _id: string, doc: unknown) => {
            const refusal = controlPlaneRefusal(kind, doc);
            return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve(undefined);
        });

    it('LLMGovernanceDialog: a save without an edit sends none of it, and is accepted', async () => {
        gw.getLLMModelGovernance.mockResolvedValue({ governance: STORED_WITH_REFUSED.llm_governance });
        refusing(gw.updateLLMModelGovernance, 'llm_governance');
        const onClose = vi.fn();
        render(<LLMGovernanceDialog isOpen onClose={onClose} groupId={GID} groupName={GNAME} model={model} />);
        await screen.findByText('Max Output Tokens');
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(onClose).toHaveBeenCalled());
        expect(gw.updateLLMModelGovernance).toHaveBeenCalledWith(GID, model.id, STORED_KEPT.llm_governance);
    });

    it('MCPServerLimitsDialog: a save without an edit sends none of it, and is accepted', async () => {
        gw.fetchMCPServerLimits.mockResolvedValue({ limits: STORED_WITH_REFUSED.mcp_limits, effective_global_limits: {}, capacity: null });
        refusing(gw.updateMCPServerLimits, 'mcp_limits');
        const onClose = vi.fn();
        render(<MCPServerLimitsDialog isOpen onClose={onClose} groupId={GID} groupName={GNAME} server={server} />);
        await screen.findByText('Timeout (seconds)');
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(onClose).toHaveBeenCalled());
        expect(gw.updateMCPServerLimits).toHaveBeenCalledWith(GID, server.id, STORED_KEPT.mcp_limits);
    });

    it('MCPToolLimitsDialog: a save of the per-tool limits sends none of the server-wide ones refused, and is accepted', async () => {
        gw.fetchMCPServerLimits.mockResolvedValue({ limits: STORED_WITH_REFUSED.mcp_limits });
        gw.getMCPCapabilityFilters.mockResolvedValue({
            discovered_capabilities: [{ capability_type: 'tool', name: 'run_query', description: 'runs a query' }],
        });
        refusing(gw.updateMCPServerLimits, 'mcp_limits');
        const onClose = vi.fn();
        render(<MCPToolLimitsDialog isOpen onClose={onClose} groupId={GID} groupName={GNAME} server={server} />);
        await screen.findByText('run_query');
        fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '60' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(onClose).toHaveBeenCalled());
        expect(gw.updateMCPServerLimits).toHaveBeenCalledWith(GID, server.id, {
            ...STORED_KEPT.mcp_limits,
            per_tool: { run_query: { max_calls_per_minute: 60 } },
        });
    });
});
