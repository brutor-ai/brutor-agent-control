/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for ContractsPanel.
 *
 * The properties worth defending are all about what an approver and an
 * operator are shown *before* they act:
 *
 *   - a diff leads with what the system **gains**, not with a change count;
 *   - "grants nothing new" is stated rather than left as an empty space;
 *   - the lifecycle gate shows its whole checklist up front, not a toast
 *     after the click;
 *   - config that has drifted from the active contract is named plainly,
 *     because that is the question an auditor asks first;
 *   - minting when nothing changed says so instead of looking broken.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockContracts = vi.fn();
const mockLifecycle = vi.fn();
const mockMint = vi.fn();
const mockApprove = vi.fn();
const mockReject = vi.fn();
const mockPromote = vi.fn();
const mockTransition = vi.fn();
const mockVersion = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemContracts: mockContracts,
        fetchAISystemLifecycle: mockLifecycle,
        mintAISystemContract: mockMint,
        approveContract: mockApprove,
        rejectContract: mockReject,
        promoteContract: mockPromote,
        transitionAISystemLifecycle: mockTransition,
        fetchContractVersion: mockVersion,
    }),
}));

vi.mock('../EnvelopePanel', () => ({
    default: () => <div data-testid="envelope-panel-stub" />,
}));
vi.mock('../EvidencePanel', () => ({
    default: () => <div data-testid="evidence-panel-stub" />,
}));
vi.mock('../OutcomesPanel', () => ({
    default: () => <div data-testid="outcomes-panel-stub" />,
}));

vi.mock('../../../hooks/useAdminUsername', () => ({
    useAdminUsername: () => 'admin',
}));

import ContractsPanel from '../ContractsPanel';
import type {
    AISystemContract,
    DiffSummary,
    LifecycleResponse,
} from '../../../lib/aiSystemTypes';

function diff(overrides: Partial<DiffSummary> = {}): DiffSummary {
    const widened = overrides.widened ?? [];
    const narrowed = overrides.narrowed ?? [];
    const changed = overrides.changed ?? [];
    return {
        widened,
        narrowed,
        changed,
        widened_count: widened.length,
        narrowed_count: narrowed.length,
        changed_count: changed.length,
        has_widened_permissions: widened.length > 0,
        ...overrides,
    };
}

function contract(overrides: Partial<AISystemContract> = {}): AISystemContract {
    return {
        id: 'ctr-1',
        ai_system_id: 'sys-1',
        version: 2,
        contract_hash: 'sha256:9f2c1234567890abcdef',
        short_hash: '9f2c12345678',
        status: 'draft',
        diff_from_version: 1,
        diff_summary: diff(),
        created_by: 'alice',
        is_active: false,
        is_approved: false,
        ...overrides,
    };
}

function contractList(contracts: AISystemContract[], extra = {}) {
    return {
        contracts,
        total: contracts.length,
        active_version: contracts.find((c) => c.is_active)?.version ?? null,
        config_drifted: false,
        ...extra,
    };
}

function lifecycle(overrides: Partial<LifecycleResponse> = {}): LifecycleResponse {
    return {
        ai_system_id: 'sys-1',
        current_stage: 'proposed',
        transitions: [
            {
                to_stage: 'approved',
                allowed: false,
                requirements: [
                    { key: 'contract_minted', label: 'A contract has been minted', satisfied: true },
                    {
                        key: 'owner_set',
                        label: 'An accountable owner is named',
                        satisfied: false,
                        detail: 'No owner set on this AI System',
                    },
                ],
                unmet: [
                    {
                        key: 'owner_set',
                        label: 'An accountable owner is named',
                        satisfied: false,
                        detail: 'No owner set on this AI System',
                    },
                ],
            },
        ],
        ...overrides,
    };
}

describe('ContractsPanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockContracts.mockResolvedValue(contractList([contract()]));
        mockLifecycle.mockResolvedValue(lifecycle());
        mockMint.mockResolvedValue({ contract: contract({ version: 3 }), created: true });
        mockApprove.mockResolvedValue(contract({ is_approved: true }));
        mockReject.mockResolvedValue(contract({ status: 'rejected' }));
        mockPromote.mockResolvedValue(contract({ status: 'active', is_active: true }));
        mockTransition.mockResolvedValue({});
    });

    // -- the diff ---------------------------------------------------------

    it('leads with what the new version grants', async () => {
        mockContracts.mockResolvedValue(
            contractList([
                contract({
                    diff_summary: diff({
                        widened: [
                            { kind: 'resource_added', detail: 'mcp_servers gained payments-api' },
                        ],
                        narrowed: [{ kind: 'limit', detail: 'limits.llm.max_tokens 5000 → 1000' }],
                    }),
                }),
            ])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        const widened = await screen.findByTestId('widened');
        expect(widened).toHaveTextContent('payments-api');
        expect(widened).toHaveTextContent('more than v1');
        // The tightening is present but subordinate.
        expect(screen.getByTestId('narrowed')).toHaveTextContent('max_tokens');
        expect(widened).not.toHaveTextContent('max_tokens');
    });

    it('says plainly when a version grants nothing new', async () => {
        // An empty space reads as "no diff computed"; the point of the
        // panel is that an approver knows this one is safe.
        mockContracts.mockResolvedValue(
            contractList([
                contract({
                    diff_summary: diff({
                        narrowed: [{ kind: 'limit', detail: 'limits.llm.cap 100 → 10' }],
                    }),
                }),
            ])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('no-widening')).toHaveTextContent(
            'Grants nothing new'
        );
        expect(screen.queryByTestId('widened')).not.toBeInTheDocument();
    });

    it('does not count documentation edits as permission changes', async () => {
        mockContracts.mockResolvedValue(
            contractList([
                contract({
                    diff_summary: diff({
                        changed: [{ kind: 'attribute', detail: "owner 'a' → 'b'" }],
                    }),
                }),
            ])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('no-widening')).toBeInTheDocument();
        expect(screen.getByTestId('changed-count')).toHaveTextContent('1 documentation');
    });

    // -- the identity policy in the diff ------------------------------------

    it('names a loosened identity policy field, its values before and after, as widened', async () => {
        mockContracts.mockResolvedValue(
            contractList([
                contract({
                    diff_summary: diff({
                        widened: [
                            { kind: 'identity_policy', field: 'min_assurance', from: 'workload', to: 'key', detail: "identity policy min_assurance: 'workload' → 'key' (looser)" },
                            { kind: 'identity_policy', field: 'allowed_issuers', from: ['idp-1'], to: null, detail: "identity policy allowed_issuers: ['idp-1'] → None (looser)" },
                        ],
                    }),
                }),
            ])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        const widened = await screen.findByTestId('widened');
        const entries = within(widened).getAllByTestId('diff-identity-policy');
        expect(entries).toHaveLength(2);
        expect(entries[0]).toHaveTextContent('Identity policy · Minimum credential level: Workload → Key');
        expect(entries[1]).toHaveTextContent('Identity policy · Identity providers: idp-1 → any');
        for (const entry of entries) expect(within(entry).getByTestId('diff-identity-direction')).toHaveTextContent('widened');
        // The server's sentence, with its Python spelling, is not what is shown.
        expect(widened).not.toHaveTextContent('(looser)');
    });

    it('names a tightened identity policy field as narrowed, and the version as granting nothing new', async () => {
        mockContracts.mockResolvedValue(
            contractList([
                contract({
                    diff_summary: diff({
                        narrowed: [
                            { kind: 'identity_policy', field: 'mode', from: 'audit', to: 'enforce', detail: "identity policy mode: 'audit' → 'enforce' (stricter)" },
                            { kind: 'identity_policy', field: 'max_key_ttl_days', from: null, to: 30, detail: 'identity policy max_key_ttl_days: None → 30 (stricter)' },
                        ],
                    }),
                }),
            ])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('no-widening')).toBeInTheDocument();
        const entries = within(screen.getByTestId('narrowed')).getAllByTestId('diff-identity-policy');
        expect(entries[0]).toHaveTextContent('Identity policy · Mode: audit → enforce');
        expect(entries[1]).toHaveTextContent('Identity policy · Longest API key lifetime: no ceiling → 30 days');
        for (const entry of entries) expect(within(entry).getByTestId('diff-identity-direction')).toHaveTextContent('narrowed');
    });

    it('names an explicit identity policy set to the default as a change that moves no field', async () => {
        mockContracts.mockResolvedValue(
            contractList([
                contract({
                    diff_summary: diff({
                        changed: [
                            {
                                kind: 'identity_policy',
                                field: 'policy',
                                from: null,
                                to: { mode: 'audit', min_assurance: 'key', allow_user_keys: true, allowed_issuers: null, max_token_lifetime_s: 3600, max_key_ttl_days: null },
                                detail: 'explicit identity policy set (same as the default)',
                            },
                        ],
                    }),
                }),
            ])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('no-widening')).toBeInTheDocument();
        const entry = within(screen.getByTestId('changed-identity-policy')).getByTestId('diff-identity-policy');
        expect(entry).toHaveTextContent('Identity policy: an explicit policy was set, the same as the default; no field moved');
        expect(within(entry).queryByTestId('diff-identity-direction')).not.toBeInTheDocument();
    });

    // -- conformance ------------------------------------------------------

    it('names configuration drift rather than implying it', async () => {
        mockContracts.mockResolvedValue(
            contractList([contract({ status: 'active', is_active: true, version: 1 })], {
                config_drifted: true,
                active_version: 1,
            })
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        const banner = await screen.findByTestId('config-drifted');
        expect(banner).toHaveTextContent('nobody approved');
        expect(banner).toHaveTextContent('v1');
    });

    it('shows no drift banner when the contract still matches', async () => {
        render(<ContractsPanel aiSystemId="sys-1" />);
        await screen.findByTestId('contract-v2');
        expect(screen.queryByTestId('config-drifted')).not.toBeInTheDocument();
    });

    it('distinguishes never-pinned from up-to-date', async () => {
        mockContracts.mockResolvedValue(contractList([]));
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByText('No contract yet')).toBeInTheDocument();
        expect(screen.queryByTestId('config-drifted')).not.toBeInTheDocument();
    });

    // -- minting ----------------------------------------------------------

    it('mints from live config', async () => {
        // The button only lights when live config differs from the governing version.
        mockContracts.mockResolvedValue(contractList([contract()], { config_drifted: true }));
        render(<ContractsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('mint-button'));
        await waitFor(() => expect(mockMint).toHaveBeenCalledWith('sys-1'));
    });

    it('says so when nothing had changed instead of looking broken', async () => {
        // The button only lights when live config differs from the governing version.
        mockContracts.mockResolvedValue(contractList([contract()], { config_drifted: true }));
        mockMint.mockResolvedValue({ contract: contract({ version: 2 }), created: false });
        render(<ContractsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('mint-button'));
        expect(await screen.findByTestId('contract-notice')).toHaveTextContent(
            'already matches contract v2'
        );
    });

    // -- approval ---------------------------------------------------------

    it('offers approve, promote and reject on a draft', async () => {
        render(<ContractsPanel aiSystemId="sys-1" />);
        const row = await screen.findByTestId('contract-v2');
        expect(within(row).getByTestId('approve-button')).toBeInTheDocument();
        expect(within(row).getByTestId('promote-button')).toBeInTheDocument();
        expect(within(row).getByTestId('reject-button')).toBeInTheDocument();
    });

    it('hides triage controls on a contract that is no longer a draft', async () => {
        mockContracts.mockResolvedValue(
            contractList([contract({ status: 'active', is_active: true })])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        await screen.findByTestId('contract-v2');
        expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument();
        expect(screen.queryByTestId('promote-button')).not.toBeInTheDocument();
    });

    it('requires a reason before a rejection can be sent', async () => {
        // A refusal with no reason is unusable to whoever has to fix it.
        render(<ContractsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('reject-button'));
        const confirm = screen.getByTestId('reject-confirm');
        expect(confirm).toBeDisabled();

        fireEvent.change(screen.getByTestId('reject-reason'), {
            target: { value: 'grants too much' },
        });
        fireEvent.click(confirm);
        await waitFor(() =>
            expect(mockReject).toHaveBeenCalledWith('ctr-1', 'grants too much')
        );
    });

    it('shows why a contract was rejected', async () => {
        mockContracts.mockResolvedValue(
            contractList([
                contract({ status: 'rejected', rejection_reason: 'grants too much' }),
            ])
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('rejection-reason')).toHaveTextContent(
            'grants too much'
        );
    });

    it('promotes a draft', async () => {
        render(<ContractsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('promote-button'));
        await waitFor(() => expect(mockPromote).toHaveBeenCalledWith('ctr-1'));
    });

    it('reloads after an action so the list reflects it', async () => {
        render(<ContractsPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockContracts).toHaveBeenCalledTimes(1));
        fireEvent.click(screen.getByTestId('promote-button'));
        await waitFor(() => expect(mockContracts).toHaveBeenCalledTimes(2));
    });

    // ── Re-attestation: minting a signature, not a change ─────────────
    it('re-attests without a config change, and says so', async () => {
        mockMint.mockResolvedValue({
            created: true,
            contract: { ...contract({ version: 4, status: 'draft' }) },
        });
        render(<ContractsPanel aiSystemId="sys-1" />);

        fireEvent.click(await screen.findByTestId('reattest-button'));

        await waitFor(() =>
            expect(mockMint).toHaveBeenCalledWith('sys-1', { force: true })
        );
        // The point of the version is the signature, so the notice has to
        // send the operator on to approving it.
        expect(await screen.findByTestId('contract-notice')).toHaveTextContent(
            /no configuration change/i
        );
    });

    it('keeps ordinary minting idempotent', async () => {
        // The button only lights when live config differs from the governing version.
        mockContracts.mockResolvedValue(contractList([contract()], { config_drifted: true }));
        // Re-attestation is opt-in precisely so the normal button stays
        // honest: unchanged config must not pile up versions.
        mockMint.mockResolvedValue({
            created: false,
            contract: { ...contract({ version: 2 }) },
        });
        render(<ContractsPanel aiSystemId="sys-1" />);

        fireEvent.click(await screen.findByTestId('mint-button'));

        await waitFor(() => expect(mockMint).toHaveBeenCalledWith('sys-1'));
        expect(await screen.findByTestId('contract-notice')).toHaveTextContent(
            /already matches contract v2/i
        );
    });

    it('offers no new version while live config matches the governing one', async () => {
        // Default fixture: one contract, no drift → nothing to record.
        render(<ContractsPanel aiSystemId="sys-1" />);
        const mint = await screen.findByTestId('mint-button');
        expect(mint).toBeDisabled();
        expect(mint).toHaveTextContent('New version from current config');
        expect(mint.getAttribute('title')).toMatch(/nothing new to record/i);
    });

    it('re-attestation is for a different approver — disabled for the one already on the version', async () => {
        mockContracts.mockResolvedValue(
            contractList([contract({ status: 'active', is_active: true, is_approved: true, approved_by: 'admin' })]),
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        const btn = await screen.findByTestId('reattest-button');
        expect(btn).toBeDisabled();
        expect(btn.getAttribute('title')).toMatch(/already attested by admin/i);
    });

    it('re-attestation opens for an approver not yet on the version', async () => {
        mockContracts.mockResolvedValue(
            contractList([contract({ status: 'active', is_active: true, is_approved: true, approved_by: 'bob' })]),
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('reattest-button')).not.toBeDisabled();
    });

    // -- sub-tabs -----------------------------------------------------------

    it('opens on Active with the contract-states explainer, and no lifecycle gate', async () => {
        // The gate has its own tab: two state machines with a shared
        // word do not belong on one screen with two sets of buttons.
        render(<ContractsPanel aiSystemId="sys-1" />);
        await screen.findByTestId('contract-active');
        expect(screen.getByTestId('contract-states-diagram')).toBeInTheDocument();
        expect(screen.getByTestId('contract-subtab-active')).toHaveAttribute('aria-selected', 'true');
        expect(screen.queryByTestId('lifecycle-gate')).not.toBeInTheDocument();
        expect(screen.queryByTestId('stage-diagram')).not.toBeInTheDocument();
    });

    it('switches to the envelope and evidence sub-tabs', async () => {
        render(<ContractsPanel aiSystemId="sys-1" />);
        await screen.findByTestId('contract-active');
        fireEvent.click(screen.getByTestId('contract-subtab-envelope'));
        expect(await screen.findByTestId('envelope-panel-stub')).toBeInTheDocument();
        expect(screen.queryByTestId('contract-active')).not.toBeInTheDocument();
        fireEvent.click(screen.getByTestId('contract-subtab-evidence'));
        expect(await screen.findByTestId('evidence-panel-stub')).toBeInTheDocument();
    });

    it('lands on the sub-tab a hand-off asks for', async () => {
        // The Lifecycle tab sends "attach an impact assessment" here.
        render(<ContractsPanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('evidence-panel-stub')).toBeInTheDocument();
        expect(screen.getByTestId('contract-subtab-evidence')).toHaveAttribute('aria-selected', 'true');
    });

    // -- autonomy ---------------------------------------------------------

    it('spells out what suspension means', async () => {
        // "Suspended" alone reads as a status. It is a fleet-wide denial.
        mockLifecycle.mockResolvedValue(lifecycle({ autonomy_level: 'suspended' }));
        render(<ContractsPanel aiSystemId="sys-1" />);
        const badge = await screen.findByTestId('autonomy-level');
        expect(badge).toHaveTextContent('Suspended');
        expect(badge).toHaveTextContent('Every actor in this system is denied');
    });

    it('shows a restricted system as read-only', async () => {
        mockLifecycle.mockResolvedValue(lifecycle({ autonomy_level: 'restricted' }));
        render(<ContractsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('autonomy-level')).toHaveTextContent('read-only');
    });

    // -- failure ----------------------------------------------------------

    it('surfaces an API failure instead of an empty panel', async () => {
        mockContracts.mockRejectedValue(new Error('control plane unreachable'));
        mockLifecycle.mockRejectedValue(new Error('control plane unreachable'));
        render(<ContractsPanel aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument();
        });
        expect(screen.queryByText('No contract yet')).not.toBeInTheDocument();
    });
});

// ---------------------------------------------------------------------------
// The stamped closure — what an approver is actually signing
//
// The version list omits `resolved_closure` and the diff only exists from v2
// onwards, so without the closure view promoting v1 means approving a
// hash-pinned document whose entire body is invisible unless you download
// the JSON.
// ---------------------------------------------------------------------------

const CLOSURE = {
    ai_system: {
        id: 'sys-1',
        name: 'Claims triage agent',
        system_kind: 'agent',
        owner: 'Claims Operations',
        intended_use: 'Triage inbound motor claims before a handler sees them',
        intended_client_labels: ['Claims handlers (internal)', 'Claims portal'],
        intended_client_systems: ['sys-intake'],
        eu_ai_act_role: 'deployer',
        eu_ai_act_risk_tier: 'high',
        phi_resource: false,
        baseline_opt_out: false,
        run_idle_timeout_seconds: 900,
    },
    autonomy_level: 'autonomous',
    inheritance_chain: ['grp-sys', 'grp-team', 'grp-org'],
    composition: {
        llm_models: [
            { id: 'llm-1', name: 'GPT-5.2', inherited: false },
            { id: 'llm-2', name: 'Claude Sonnet 5', inherited: true },
        ],
        mcp_servers: [{ id: 'srv-1', name: 'Claims Core', inherited: false }],
        skills: [{ id: 'sk-1', name: 'complaint-digest', inherited: false }],
        agent_identities: [],
        guardrails: [{ id: 'g-1', name: 'PII & Secrets', inherited: true }],
        argument_policies: [],
        cache_configs: [],
    },
    response_policies: [{ id: 'rp-1', name: 'tighten-on-drift' }],
    // The closure keys limits and governance by SURFACE — llm / mcp / skill /
    // media — and uses the same four names in both maps. `llm_global_limits`
    // is the resource-group COLUMN name and never appears in a closure, so a
    // fixture keyed that way would test a shape production never produces.
    limits: { llm: { daily_budget_usd: 50 }, mcp: null },
    governance: { llm: { max_temperature: 1 } },
    agent_grants: [
        {
            agent_id: 'agent-1', agent_name: 'claims-worker',
            action_type: 'mcp_tool', target: 'claims_lookup', effect: 'allow',
            constraints: null, expires_at: null,
        },
        {
            agent_id: 'agent-1', agent_name: 'claims-worker',
            action_type: 'mcp_tool', target: 'claims_update_queue',
            effect: 'approval_required', constraints: null, expires_at: null,
        },
        {
            agent_id: 'agent-1', agent_name: 'claims-worker',
            action_type: 'a2a_call', target: 'fraud-screening-agent',
            effect: 'allow', constraints: { max_delegation_depth: 2 },
            expires_at: null,
        },
    ],
};

describe('stamped closure', () => {
    beforeEach(() => {
        // The base beforeEach lives inside the first describe block, so this
        // one must re-establish the happy path itself — otherwise the
        // rejected mocks from the failure tests above leak in here.
        vi.clearAllMocks();
        mockContracts.mockResolvedValue(contractList([contract()]));
        mockLifecycle.mockResolvedValue(lifecycle());
        mockVersion.mockResolvedValue(contract({ resolved_closure: CLOSURE }));
    });

    async function openClosure(c = contract()) {
        mockContracts.mockResolvedValue({
            contracts: [c], total: 1, active_version: null, config_drifted: false,
        });
        render(<ContractsPanel aiSystemId="sys-1" />);
        const toggle = await screen.findByTestId(`closure-toggle-v${c.version}`);
        fireEvent.click(toggle);
        return screen.findByTestId(`closure-v${c.version}`);
    }

    it('shows the resources the version froze, not live config', async () => {
        const body = await openClosure();
        await waitFor(() => expect(mockVersion).toHaveBeenCalledWith('sys-1', 2));
        expect(body).toHaveTextContent('GPT-5.2');
        expect(body).toHaveTextContent('Claims Core');
        expect(body).toHaveTextContent('complaint-digest');
        // The reader must not mistake a frozen snapshot for current config.
        expect(body).toHaveTextContent(/not today's live configuration/i);
        expect(body).toHaveTextContent('9f2c12345678');
    });

    it('marks inherited grants so the approver knows where they came from', async () => {
        const body = await openClosure();
        await screen.findByText('Claude Sonnet 5');
        const inherited = within(body).getAllByText('inherited');
        // Claude Sonnet 5 and the PII guardrail are both inherited.
        expect(inherited.length).toBe(2);
    });

    it('shows policies, response policies, limits and autonomy', async () => {
        const body = await openClosure();
        await screen.findByText('PII & Secrets');
        expect(body).toHaveTextContent('tighten-on-drift');
        const limitsBox = within(body).getByTestId('closure-limits');
        // The VALUE is the contract term. Rendering only the key tells an
        // approver a limit exists but never what they are signing.
        expect(limitsBox).toHaveTextContent('daily budget usd');
        expect(limitsBox).toHaveTextContent('50');
        expect(limitsBox).toHaveTextContent('max temperature');
        // A null limit is undeclared, not a limit of zero — it must not render.
        expect(limitsBox).not.toHaveTextContent('MCP tools');
        // `limits` and `governance` share the same four surface keys, so
        // concatenating them would print every surface twice.
        expect(within(limitsBox).getAllByText('LLM')).toHaveLength(1);
        expect(within(body).getByTestId('closure-autonomy')).toHaveTextContent('autonomous');
        expect(within(body).getByTestId('closure-autonomy')).toHaveTextContent('3 group(s)');
    });

    it('shows ownership and purpose as sealed, beside the autonomy level', async () => {
        const body = await openClosure();
        const box = within(body).getByTestId('closure-ownership');
        expect(box).toHaveTextContent('Claims Operations');
        expect(box).toHaveTextContent('Triage inbound motor claims');
        expect(box).toHaveTextContent('Claims handlers (internal), Claims portal');
        expect(box).toHaveTextContent('Intended client systems');
        expect(box).toHaveTextContent('sys-intake');
        expect(box).toHaveTextContent('deployer');
        expect(box).toHaveTextContent('high');
        expect(box).toHaveTextContent('900 s');
        // Unset flags are not terms and must not print as "no".
        expect(box).not.toHaveTextContent('Handles PHI');
        expect(within(box).getByTestId('closure-autonomy')).toHaveTextContent('autonomous');
    });

    it('a closure without ownership declarations says so instead of hiding the block', async () => {
        mockVersion.mockResolvedValue(
            contract({ resolved_closure: { ...CLOSURE, ai_system: { id: 'sys-1', name: 'x' } } }),
        );
        const body = await openClosure();
        expect(within(body).getByTestId('closure-ownership')).toHaveTextContent(/no ownership declarations/i);
    });

    it('shows what the agents may do, with the effect on each action', async () => {
        // Agent identities are in the closure, and so are the grants that decide
        // allow / approval / deny on every action: without them a contract
        // would claim to be the full closure while the actual authorization
        // sat outside the hash.
        const body = await openClosure();
        await screen.findByTestId('closure-grants');
        const grants = within(body).getAllByTestId('closure-grant-item');
        expect(grants).toHaveLength(3);
        expect(grants[0]).toHaveTextContent('allow');
        expect(grants[0]).toHaveTextContent('claims_lookup');
        expect(grants[1]).toHaveTextContent('approval');
        expect(grants[1]).toHaveTextContent('claims_update_queue');
        // Constraints are terms too — the depth cap must be visible.
        expect(grants[2]).toHaveTextContent('max_delegation_depth');
        expect(grants[2]).toHaveTextContent('claims-worker');
    });

    it('omits the grants block for a system whose agents hold none', async () => {
        mockVersion.mockResolvedValue(
            contract({ resolved_closure: { ...CLOSURE, agent_grants: [] } }),
        );
        const body = await openClosure();
        await screen.findByTestId('closure-limits');
        expect(within(body).queryByTestId('closure-grants')).toBeNull();
    });

    it('v1 has no diff, so the closure is the only thing an approver can read', async () => {
        // The exact case that motivated this: a first contract awaiting
        // promotion, with nothing to diff against.
        const v1 = contract({ version: 1, diff_from_version: null, diff_summary: null });
        const body = await openClosure(v1);
        await waitFor(() => expect(mockVersion).toHaveBeenCalledWith('sys-1', 1));
        expect(body).toHaveTextContent('GPT-5.2');
    });

    it('loads lazily — closures are large and most rows are never opened', async () => {
        mockContracts.mockResolvedValue({
            contracts: [contract()], total: 1, active_version: null, config_drifted: false,
        });
        render(<ContractsPanel aiSystemId="sys-1" />);
        await screen.findByTestId('closure-toggle-v2');
        expect(mockVersion).not.toHaveBeenCalled();
    });

    it('a failed load says so instead of rendering an empty contract', async () => {
        mockVersion.mockRejectedValue(new Error('closure unavailable'));
        const body = await openClosure();
        await waitFor(() =>
            expect(body).toHaveTextContent('closure unavailable'),
        );
    });
});

// ---------------------------------------------------------------------------
// Declared outcomes: their own sub-tab, a closure section, and
// the widenings / narrowings they put on the diff.
// ---------------------------------------------------------------------------

describe('declared outcomes on the contract', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockContracts.mockResolvedValue(contractList([contract()]));
        mockLifecycle.mockResolvedValue(lifecycle());
    });

    it('has an Outcomes sub-tab beside the envelope', async () => {
        render(<ContractsPanel aiSystemId="sys-1" />);
        await screen.findByTestId('contract-active');
        const tabs = within(screen.getByTestId('contract-subtabs')).getAllByRole('tab');
        expect(tabs.map((t) => t.textContent)).toEqual(['Active', 'Outcomes', 'Operating envelope', 'External evidence']);
        fireEvent.click(screen.getByTestId('contract-subtab-outcomes'));
        expect(await screen.findByTestId('outcomes-panel-stub')).toBeInTheDocument();
        expect(screen.queryByTestId('contract-active')).not.toBeInTheDocument();
    });

    it('lands on Outcomes when a hand-off asks for it', async () => {
        render(<ContractsPanel aiSystemId="sys-1" initialSubTab="outcomes" />);
        expect(await screen.findByTestId('outcomes-panel-stub')).toBeInTheDocument();
    });

    it('marks outcome changes on the diff, and calls out an outcome taken out of judgement', async () => {
        mockContracts.mockResolvedValue(
            contractList([
                contract({
                    diff_summary: diff({
                        widened: [
                            {
                                kind: 'outcome_variant_added', section: 'outcomes', id: 'application_processed.loan_referred',
                                detail: 'variant application_processed.loan_referred (Recommended: refer) declared',
                            },
                            {
                                kind: 'outcome_baseline_flag', section: 'outcomes', id: 'application_processed',
                                from: true, to: false, called_out: true,
                                detail: 'outcome application_processed no longer counts toward the baseline — its runs leave the success rate, the cost of the work and drift detection; read why',
                            },
                            {
                                kind: 'outcome_expect_loosened', section: 'outcomes', id: 'nothing_to_do', field: 'max_actions',
                                detail: 'outcome nothing_to_do expect: max_actions raised 2 → 5',
                            },
                        ],
                        narrowed: [
                            {
                                kind: 'outcome_removed', section: 'outcomes', id: 'task_completed',
                                detail: 'outcome task_completed (Task completed) no longer declared',
                            },
                        ],
                    }),
                }),
            ]),
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        const widened = await screen.findByTestId('widened');
        expect(widened).toHaveTextContent('loan_referred');
        expect(widened).toHaveTextContent('max_actions raised 2 → 5');
        expect(within(widened).getAllByTestId('diff-outcome-entry')).toHaveLength(3);
        const callouts = within(widened).getAllByTestId('diff-called-out');
        expect(callouts).toHaveLength(1);
        expect(callouts[0]).toHaveTextContent('takes runs out of judgement');
        expect(callouts[0].closest('li')).toHaveTextContent('no longer counts toward the baseline');
        const narrowed = screen.getByTestId('narrowed');
        expect(narrowed).toHaveTextContent('task_completed');
        expect(within(narrowed).queryByTestId('diff-called-out')).toBeNull();
    });

    it('shows the outcomes a version signs in its closure', async () => {
        mockVersion.mockResolvedValue(
            contract({
                resolved_closure: {
                    ...CLOSURE,
                    outcomes: [
                        {
                            id: 'application_processed', label: 'Application processed', counts_toward_baseline: true,
                            variants: [
                                { id: 'loan_approved', label: 'Recommended: approve', expect: { requires_capabilities: ['applications_set_recommendation'] } },
                                { id: 'underwriter_decision_applied', label: 'Decision applied', expect: { max_actions: 2, max_model_calls: 0 } },
                            ],
                        },
                        {
                            id: 'no_applications_available', label: 'Nothing to screen', counts_toward_baseline: false,
                            expect: { max_model_calls: 0, max_cost_usd: 0 },
                        },
                        { id: 'broken entry without a flag', label: 'x' },
                    ],
                    dependencies: [
                        {
                            // External, so the closure needs no system-name lookup.
                            role: 'external', depends_on_system_id: null, agent_card_id: 'card-fraud',
                            criticality: 'critical', contributes_to_decision: true, fallback: null,
                            separability_rationale: null, assumptions: {},
                            expected_outcomes: ['screening_completed.clear', 'screening_completed.hit'],
                        },
                    ],
                },
            }),
        );
        render(<ContractsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('closure-toggle-v2'));
        const section = await screen.findByTestId('closure-outcomes');
        const items = within(section).getAllByTestId('closure-outcome-item');
        // An entry the gateway cannot read declares nothing.
        expect(items).toHaveLength(2);
        expect(items[0]).toHaveTextContent('application_processed.loan_approved');
        expect(items[0]).toHaveTextContent('requires applications_set_recommendation');
        expect(items[0]).toHaveTextContent('≤ 2 actions · no model calls');
        expect(items[0]).toHaveTextContent('counts');
        expect(items[1]).toHaveTextContent('Nothing to screen');
        expect(items[1]).toHaveTextContent('no work');
        expect(items[1]).toHaveTextContent('costs nothing');
        expect(screen.getByTestId('closure-dependency-expected-outcomes')).toHaveTextContent(
            'screening_completed.clear · screening_completed.hit',
        );
    });

    it('shows no outcomes section where the key holds no outcome: the Control Plane seals the key only with outcomes in it', async () => {
        for (const outcomes of [null, []]) {
            mockVersion.mockResolvedValue(contract({ resolved_closure: { ...CLOSURE, outcomes } }));
            const { unmount } = render(<ContractsPanel aiSystemId="sys-1" />);
            fireEvent.click(await screen.findByTestId('closure-toggle-v2'));
            await screen.findByTestId('closure-v2');
            expect(screen.queryByTestId('closure-outcomes')).toBeNull();
            expect(screen.queryByText(/None declared — every outcome reported is judged undeclared/)).toBeNull();
            unmount();
        }
    });

    it('omits the section for a closure without the key', async () => {
        mockVersion.mockResolvedValue(contract({ resolved_closure: CLOSURE }));
        render(<ContractsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('closure-toggle-v2'));
        await screen.findByTestId('closure-v2');
        await waitFor(() => expect(mockVersion).toHaveBeenCalled());
        expect(screen.queryByTestId('closure-outcomes')).toBeNull();
    });
});
