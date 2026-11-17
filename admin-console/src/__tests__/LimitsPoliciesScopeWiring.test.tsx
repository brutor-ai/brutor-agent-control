/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Limits / Policies scope-wiring guard.
 *
 * Every resource surface reuses the same handful of shared limit /
 * governance forms (DRY), but each surface must dispatch to the
 * CORRECT form for its resource type / mode. This suite guards that
 * ROUTING (not the deep field behavior — that lives in each form's
 * own test):
 *
 *   - LLModelForm: chat → LLM forms; media modes AND embedding →
 *     Media forms. The embedding→Media case is a recent fix
 *     (MEDIA_FORM_MODES = [...MEDIA_MODES, 'embedding']) and is the
 *     most important assertion here.
 *   - ServerForm: Limits→MCP forms, Policies→MCP forms.
 *   - AgentSkillDetailPanel: Skill forms.
 *   - AgentCardEditDialog: A2A forms.
 *   - GroupResourceLimitsTab / GroupResourceGovernanceTab: the
 *     RG-wide routers dispatch to the matching per-type child tab.
 *
 * Each check queries for a field/label unique to the expected form.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

// ── Shared dependency mocks (hoisted, factory-only) ──────────────────────────

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn(), loading: vi.fn(), dismiss: vi.fn() },
}));

vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {},
        groupTree: [],
        hasEnabledRegistries: false,
        loadRegistries: vi.fn(),
        convertServerToFormInfo: vi.fn(),
        createIdPConfig: vi.fn(),
    }),
}));

vi.mock('../hooks/useLlmProviders', () => ({
    useLlmProviders: () => ({ providers: [], loading: false }),
}));

vi.mock('../components/common/Toast', () => ({
    useToast: () => ({
        success: vi.fn(),
        error: vi.fn(),
        warning: vi.fn(),
        info: vi.fn(),
    }),
}));

vi.mock('../components/common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn(async () => true),
    ConfirmationProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// RG-wide routers read the tenant theme for the active-tab accent color.
vi.mock('../context/TenantThemeContext', () => ({
    useTenantTheme: () => ({ theme: { primary_color: '#1BB5E8' } }),
}));

// The RG-wide routers only decide WHICH per-type child tab to render.
// Stub each child to a marker so we test dispatch, not the (separately
// tested) data-fetching children.
vi.mock('../components/groups/subtabs/GroupLLMLimitsTab', () => ({
    default: () => <div>MARKER-LLM-LIMITS</div>,
}));
vi.mock('../components/groups/subtabs/GroupMCPLimitsTab', () => ({
    default: () => <div>MARKER-MCP-LIMITS</div>,
}));
vi.mock('../components/groups/subtabs/GroupSkillLimitsTab', () => ({
    default: () => <div>MARKER-SKILL-LIMITS</div>,
}));
vi.mock('../components/groups/subtabs/GroupA2ALimitsTab', () => ({
    default: () => <div>MARKER-A2A-LIMITS</div>,
}));
vi.mock('../components/groups/subtabs/GroupMediaLimitsTab', () => ({
    default: () => <div>MARKER-MEDIA-LIMITS</div>,
}));
vi.mock('../components/groups/subtabs/GroupLLMGovernanceTab', () => ({
    default: () => <div>MARKER-LLM-GOV</div>,
}));
vi.mock('../components/groups/subtabs/GroupMCPGovernanceTab', () => ({
    default: () => <div>MARKER-MCP-GOV</div>,
}));
vi.mock('../components/groups/subtabs/GroupSkillGovernanceTab', () => ({
    default: () => <div>MARKER-SKILL-GOV</div>,
}));
vi.mock('../components/groups/subtabs/GroupA2AGovernanceTab', () => ({
    default: () => <div>MARKER-A2A-GOV</div>,
}));
vi.mock('../components/groups/subtabs/GroupMediaGovernanceTab', () => ({
    default: () => <div>MARKER-MEDIA-GOV</div>,
}));

// ── Imports that rely on the mocks above ─────────────────────────────────────

import LLModelForm from '../components/llm/config/LLModelForm';
import ServerForm from '../components/mcp/config/server/ServerForm';
import AgentSkillDetailPanel from '../components/skills/AgentSkillDetailPanel';
import { AgentCardEditDialog } from '../components/a2a/AgentCardEditDialog';
import GroupResourceLimitsTab from '../components/groups/subtabs/GroupResourceLimitsTab';
import GroupResourceGovernanceTab from '../components/groups/subtabs/GroupResourceGovernanceTab';

// Field labels unique to each shared form — used as routing fingerprints.
const FINGERPRINT = {
    llmLimits: 'Throughput', // LLMLimitsForm section header
    llmGovernance: 'Max Temperature', // LLMGovernanceForm-only label
    mediaLimitsEmbedding: "This release has no media limit of its own for Embeddings: the group's LLM limits bound it.", // MediaLimitsForm embedding: no limit of its own
    mediaGovernanceEmbedding: 'Max batch size', // MediaGovernanceForm embedding field
    mcpLimits: 'Data Limits', // MCPLimitsForm-only section
    mcpGovernance: 'Default Access Levels', // MCPGovernanceForm section
    skillLimits: 'Execution Limits', // SkillLimitsForm section
    skillGovernance: 'Skill Execution Settings', // SkillGovernanceForm section
    a2aLimits: 'Invocations', // A2ALimitsForm card
    a2aGovernance: 'Capability access', // A2AGovernanceForm card
} as const;

const baseModel = {
    id: 'llm-1',
    tenant_id: 't1',
    name: 'M',
    provider: 'openai',
    model_name: 'gpt-test',
    mode: 'chat',
    api_url_auto_discovered: true,
    auth_type: 'api_key',
    default_temperature: 0.7,
    default_max_tokens: 4096,
    timeout: 60,
    enabled: true,
    health_status: { status: 'unknown' },
    performance_metrics: {},
    created_at: '2026-05-13T00:00:00Z',
    updated_at: '2026-05-13T00:00:00Z',
};

function renderModel(mode: string) {
    render(
        <LLModelForm
            model={{ ...baseModel, mode } as any}
            onSave={vi.fn(async () => undefined)}
            onClose={vi.fn()}
        />,
    );
}

// ─────────────────────────────────────────────────────────────────────────────
describe('LLModelForm — Limits/Policies dispatch by mode', () => {
    it('chat mode → LLM limits + LLM governance forms', () => {
        renderModel('chat');
        fireEvent.click(screen.getByRole('button', { name: 'Limits' }));
        expect(screen.getByText(FINGERPRINT.llmLimits)).toBeInTheDocument();
        expect(screen.queryByText(FINGERPRINT.mediaLimitsEmbedding)).not.toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Policies' }));
        expect(screen.getByText(FINGERPRINT.llmGovernance)).toBeInTheDocument();
    });

    it('image_generation mode → Media limits + Media governance forms', () => {
        renderModel('image_generation');
        fireEvent.click(screen.getByRole('button', { name: 'Limits' }));
        // Media (image) limits present; chat throughput absent.
        expect(screen.getByText('Daily images')).toBeInTheDocument();
        expect(screen.queryByText(FINGERPRINT.llmLimits)).not.toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Policies' }));
        expect(screen.queryByText(FINGERPRINT.llmGovernance)).not.toBeInTheDocument();
        expect(screen.getByText('Allowed sizes')).toBeInTheDocument();
    });

    // ★ The load-bearing guard: embedding is NOT in MEDIA_MODES
    // (no Media *tab*), but IS in MEDIA_FORM_MODES, so its tenant-wide
    // Limits/Policies must render the Media forms, not the chat LLM forms.
    it('embedding mode → Media limits + Media governance forms (not chat forms)', () => {
        renderModel('embedding');

        fireEvent.click(screen.getByRole('button', { name: 'Limits' }));
        expect(screen.getByText(FINGERPRINT.mediaLimitsEmbedding)).toBeInTheDocument();
        // The chat LLM limits form must NOT render.
        expect(screen.queryByText(FINGERPRINT.llmLimits)).not.toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Policies' }));
        expect(screen.getByText(FINGERPRINT.mediaGovernanceEmbedding)).toBeInTheDocument();
        // Chat-only governance (temperature.enforce / Max Temperature) must NOT render.
        expect(screen.queryByText(FINGERPRINT.llmGovernance)).not.toBeInTheDocument();
        expect(screen.queryByText('Enforce Mode')).not.toBeInTheDocument();
    });
});

// ─────────────────────────────────────────────────────────────────────────────
describe('ServerForm — Limits/Policies dispatch to MCP forms', () => {
    it('Limits tab → MCPLimitsForm, Policies tab → MCPGovernanceForm', () => {
        render(
            <ServerForm
                server={null}
                onSave={vi.fn(async () => undefined)}
                onCancel={vi.fn()}
            />,
        );
        fireEvent.click(screen.getByRole('button', { name: 'Limits' }));
        expect(screen.getByText(FINGERPRINT.mcpLimits)).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Policies' }));
        expect(screen.getByText(FINGERPRINT.mcpGovernance)).toBeInTheDocument();
    });
});

// ─────────────────────────────────────────────────────────────────────────────
describe('AgentSkillDetailPanel — Limits/Policies dispatch to Skill forms', () => {
    const skill = {
        id: 'skill-1',
        name: 'S',
        version: '1.0.0',
        status: 'draft',
        enabled: true,
        metadata: {},
        groups: [],
    };

    /** The panel's actions, none of which these cases press. */
    const PANEL_ACTIONS = {
        onClose: vi.fn(),
        onCopy: vi.fn(),
        onDelete: vi.fn(),
        onPublish: vi.fn(),
        onValidate: vi.fn(),
        onDeprecate: vi.fn(),
        onArchive: vi.fn(),
    };

    it('Limits tab → SkillLimitsForm', () => {
        render(
            <AgentSkillDetailPanel
                skill={skill as any}
                initialTab="limits"
                {...PANEL_ACTIONS}
                onSave={vi.fn(async () => undefined)}
            />,
        );
        expect(screen.getByText(FINGERPRINT.skillLimits)).toBeInTheDocument();
    });

    // A skill's own policies are not acted on in this release: no Policies tab, a stored one shown read-only.
    it('has no Policies tab, and shows a stored skill policy read-only on the Limits tab', () => {
        render(
            <AgentSkillDetailPanel
                skill={{ ...(skill as object), tenant_governance: { execution_mode: 'disabled' } } as any}
                initialTab="limits"
                {...PANEL_ACTIONS}
                onSave={vi.fn(async () => undefined)}
            />,
        );
        expect(screen.queryByRole('button', { name: 'Policies' })).toBeNull();
        expect(screen.queryByText(FINGERPRINT.skillGovernance)).toBeNull();
        expect(screen.getByTestId('skill-governance-unavailable-execution_mode')).toHaveTextContent('disabled — not available in this release');
    });
});

// ─────────────────────────────────────────────────────────────────────────────
describe('AgentCardEditDialog — Limits/Policies dispatch to A2A forms', () => {
    it('Limits tab → A2ALimitsForm, Policies tab → A2AGovernanceForm', () => {
        render(
            <AgentCardEditDialog
                initial={null}
                groupTree={[]}
                onClose={vi.fn()}
                onSaved={vi.fn()}
            />,
        );
        fireEvent.click(screen.getByRole('button', { name: 'Limits' }));
        expect(screen.getByText(FINGERPRINT.a2aLimits)).toBeInTheDocument();
        expect(screen.getByText('max_delegation_depth')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Policies' }));
        expect(screen.getByText(FINGERPRINT.a2aGovernance)).toBeInTheDocument();
    });
});

// ─────────────────────────────────────────────────────────────────────────────
describe('GroupResourceLimitsTab — RG-wide router dispatch by resourceType', () => {
    const group = { id: 'g1', name: 'G' };

    it('dispatches each resourceType to its matching per-type limits tab', () => {
        render(<GroupResourceLimitsTab group={group as any} onUpdate={vi.fn()} />);
        // Default sub-tab is LLM.
        expect(screen.getByText('MARKER-LLM-LIMITS')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /MCP Limits/i }));
        expect(screen.getByText('MARKER-MCP-LIMITS')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /Skill Limits/i }));
        expect(screen.getByText('MARKER-SKILL-LIMITS')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /Agent Limits/i }));
        expect(screen.getByText('MARKER-A2A-LIMITS')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /Media Limits/i }));
        expect(screen.getByText('MARKER-MEDIA-LIMITS')).toBeInTheDocument();
    });
});

// ─────────────────────────────────────────────────────────────────────────────
describe('GroupResourceGovernanceTab — RG-wide router dispatch by resourceType', () => {
    const group = { id: 'g1', name: 'G' };

    it('dispatches each resourceType to its matching per-type policies tab', () => {
        render(<GroupResourceGovernanceTab group={group as any} onUpdate={vi.fn()} />);
        // Default sub-tab is LLM.
        expect(screen.getByText('MARKER-LLM-GOV')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /MCP Policies/i }));
        expect(screen.getByText('MARKER-MCP-GOV')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /Skill Policies/i }));
        expect(screen.getByText('MARKER-SKILL-GOV')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /Agent Policies/i }));
        expect(screen.getByText('MARKER-A2A-GOV')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /Media Policies/i }));
        expect(screen.getByText('MARKER-MEDIA-GOV')).toBeInTheDocument();
    });
});
