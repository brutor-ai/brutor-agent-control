/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Composition tab: it renders location, members, inherited resources,
 * declared and observed edges and findings from a fixture payload, and the
 * list view matches the diagram.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { NOT_SOURCE_DIRS, sourceRoots } from '../../../test/sourceRoots';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockFetchComposition = vi.fn();
const mockFetchAISystems = vi.fn();
const mockListAgentCards = vi.fn();
const mockFetchContracts = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockDelete = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemComposition: mockFetchComposition,
        fetchAISystems: mockFetchAISystems,
        listAgentCards: mockListAgentCards,
        fetchAISystemContracts: mockFetchContracts,
        createAISystemDependency: mockCreate,
        updateAISystemDependency: mockUpdate,
        deleteAISystemDependency: mockDelete,
    }),
}));

import CompositionTab from '../CompositionTab';
import { buildCompositionModel, COMPOSITION_CATEGORIES, modelNodeKeys } from '../../../lib/composition';
import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';
import { CONTRIBUTED_LINKS } from '../../../app/links';
import { SlotProvider } from '../../../app/SlotProvider';

const setActiveTab = vi.fn();

/** Renders the tab and opens the detailed Diagram (the default view is the
 *  Simple graph — see CompositionSimpleGraph.test.tsx). */
async function renderTab() {
    render(<CompositionTab aiSystemId="sys-demo" windowHours={168} setActiveTab={setActiveTab} />);
    const tab = await screen.findByTestId('composition-tab');
    fireEvent.click(screen.getByTestId('composition-view-diagram'));
    return tab;
}

const node = (key: string) => screen.getByTestId(`composition-node-${key}`);

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    mockFetchComposition.mockResolvedValue(COMPOSITION_PAYLOAD);
    mockFetchAISystems.mockResolvedValue({
        systems: [
            { id: 'sys-fraud', name: 'fraud', display_name: 'Fraud Screener' },
            { id: 'sys-kyc', name: 'kyc', display_name: 'KYC Checker' },
        ],
    });
    mockListAgentCards.mockResolvedValue({ cards: [{ id: 'card-bureau', name: 'Credit Bureau Agent' }], total: 1 });
    mockFetchContracts.mockResolvedValue({ contracts: [], total: 0, config_drifted: false });
    mockDelete.mockResolvedValue(undefined);
});

describe('CompositionTab — layout', () => {
    it('fetches the composition for the system and window', async () => {
        await renderTab();
        expect(mockFetchComposition).toHaveBeenCalledWith('sys-demo', 168);
    });

    it('top: the location breadcrumb root-first, ending with the system, and the system facts', async () => {
        await renderTab();
        const crumbs = within(screen.getByTestId('composition-location')).getAllByRole('listitem');
        expect(crumbs.map((c) => c.textContent)).toEqual(['Nordica', 'Lending', 'Brutor Demo System']);

        const facts = screen.getByTestId('composition-facts');
        expect(facts).toHaveTextContent('Credit Risk');
        expect(facts).toHaveTextContent('Active');
        expect(facts).toHaveTextContent('Approval required');
        expect(screen.getByTestId('composition-fact-risk')).toHaveTextContent('high');
        expect(screen.getByTestId('composition-fact-contract')).toHaveTextContent('v3');
    });

    it('risk shows declared → effective with its source when a caller lifts it', async () => {
        mockFetchComposition.mockResolvedValue({
            ...COMPOSITION_PAYLOAD,
            system: {
                ...COMPOSITION_PAYLOAD.system,
                risk_tier_declared: 'minimal',
                risk_tier_effective: 'high',
                effective_tier_sources: [{ system_id: 'sys-x', system_name: 'Loan Desk', tier: 'high' }],
                separability_declared: true,
            },
        });
        await renderTab();
        const risk = screen.getByTestId('composition-fact-risk');
        expect(risk).toHaveTextContent('minimal → high via Loan Desk');
        expect(risk).toHaveTextContent('separability declared');
    });

    it('left: member agents (neutral) and, below them, callers', async () => {
        await renderTab();
        const left = screen.getByTestId('composition-column-left');
        expect(within(left).getByTestId('composition-node-agent:agent-intake')).toHaveAttribute('data-category', 'neutral');
        expect(within(left).getByTestId('composition-node-agent:agent-scorer')).toBeInTheDocument();
        expect(within(left).getByTestId('composition-node-caller:sys-portal')).toBeInTheDocument();
        expect(within(left).getByTestId('composition-node-client:Loan Officer Portal')).toBeInTheDocument();
        // Agents come before callers.
        const sections = within(left).getAllByRole('region').map((r) => r.getAttribute('aria-label'));
        expect(sections).toEqual(['Member agents', 'Used by']);
    });

    it('centre: ONE "Brutor Gateway" rectangle holding policies (guardrails included) and limits', async () => {
        await renderTab();
        const gateways = screen.getAllByTestId('composition-gateway');
        expect(gateways).toHaveLength(1);
        const gw = gateways[0];
        expect(gw).toHaveTextContent('Brutor Gateway');
        const guardrail = within(gw).getByTestId('composition-node-policy:guardrail:gr-pii');
        expect(guardrail).toHaveAttribute('data-category', 'policy');
        expect(guardrail).toHaveTextContent('Guardrail');
        expect(within(gw).getByTestId('composition-node-policy:argument_policy:ap-amount')).toBeInTheDocument();
        const budget = within(gw).getByTestId('composition-node-limit:budget:monthly_budget_usd');
        expect(budget).toHaveAttribute('data-category', 'limit');
        expect(budget).toHaveTextContent('$500 / month');
        // Inherited settings show their source group.
        expect(screen.getByTestId('inherited-policy:guardrail:gr-pii')).toHaveTextContent('inherited from Nordica');
        expect(screen.getByTestId('inherited-limit:budget:monthly_budget_usd')).toHaveTextContent('inherited from Lending');
        expect(within(gw).queryByTestId('composition-node-model:llm-sonnet')).toBeNull();
    });

    it('right: AI resources (models, MCP, skills, KBs) with inheritance', async () => {
        await renderTab();
        const right = screen.getByTestId('composition-column-right');
        for (const key of ['model:llm-sonnet', 'model:llm-mini', 'mcp:mcp-core', 'skill:skill-affordability', 'kb:kb-policy']) {
            expect(within(right).getByTestId(`composition-node-${key}`)).toHaveAttribute('data-category', 'ai_resource');
        }
        expect(screen.getByTestId('inherited-model:llm-mini')).toHaveTextContent('Lending');
        expect(screen.getByTestId('inherited-kb:kb-policy')).toHaveTextContent('Nordica');
    });

    it('right: delegation agents with role, criticality, contribution, contract, health, calls and outcome mix', async () => {
        await renderTab();
        const fraud = node('dep:dep-fraud');
        expect(fraud).toHaveAttribute('data-category', 'delegation_agent');
        expect(fraud).toHaveTextContent('Shared service');
        expect(fraud).toHaveTextContent('Critical');
        expect(fraud).toHaveTextContent('contributes to decision');
        expect(fraud).toHaveTextContent('following active v2');
        expect(fraud).toHaveTextContent('Healthy');
        expect(fraud).toHaveTextContent('11 answered · 1 failed');

        const address = node('dep:dep-address');
        expect(address).toHaveTextContent('pinned to v2');
        expect(address).toHaveTextContent('separable');
        expect(address).toHaveTextContent('Degradable');
    });
});

describe('CompositionTab — declared vs observed', () => {
    it('declared and observed is solid; declared never observed is outlined', async () => {
        await renderTab();
        expect(node('dep:dep-fraud')).toHaveAttribute('data-state', 'declared_observed');
        expect(node('dep:dep-address')).toHaveAttribute('data-state', 'declared_unobserved');
        expect(node('dep:dep-address')).toHaveTextContent('Declared · not observed');
        expect(node('caller:sys-portal')).toHaveAttribute('data-state', 'declared_observed');
    });

    it('external delegates are dashed and labelled "assumed, not tested"; shared services are not', async () => {
        await renderTab();
        const bureau = node('dep:dep-bureau');
        expect(bureau).toHaveAttribute('data-dashed', 'true');
        expect(bureau).toHaveTextContent('Assumed, not tested');
        expect(node('dep:dep-fraud')).toHaveAttribute('data-dashed', 'false');
    });

    it('observed-undeclared is highlighted with its finding and a Declare action', async () => {
        await renderTab();
        const kyc = node('undeclared:sys-kyc');
        expect(kyc).toHaveAttribute('data-state', 'observed_undeclared');
        expect(kyc).toHaveTextContent('Called KYC Checker 3 times with no declared dependency.');

        const shadow = node('caller:sys-shadow');
        expect(shadow).toHaveAttribute('data-state', 'observed_undeclared');
        expect(shadow).toHaveTextContent('not an intended client');

        fireEvent.click(screen.getByTestId('declare-undeclared:sys-kyc'));
        const dialog = await screen.findByTestId('dependency-dialog');
        await waitFor(() => expect(within(dialog).getByTestId('dependency-target')).toHaveValue('sys-kyc'));
        expect(within(dialog).getByTestId('dependency-role-shared_service')).toBeChecked();
    });

    it('a card the grants reach with no declared edge is highlighted and declarable as external', async () => {
        await renderTab();
        const weather = node('dep:card:card-weather');
        expect(weather).toHaveAttribute('data-state', 'granted_undeclared');
        expect(weather).toHaveTextContent('Granted · not declared');
        expect(weather.className).toContain('ring-amber-500');
        expect(screen.queryByTestId('edit-dep:card:card-weather')).toBeNull();

        fireEvent.click(screen.getByTestId('declare-dep:card:card-weather'));
        const dialog = await screen.findByTestId('dependency-dialog');
        expect(within(dialog).getByTestId('dependency-role-external')).toBeChecked();
        expect(within(dialog).getByTestId('dependency-card')).toHaveValue('card-weather');
    });

    it('lists every finding, most severe first', async () => {
        await renderTab();
        const list = screen.getByTestId('composition-findings');
        // The heading and its count, side by side.
        expect(list).toHaveTextContent(/Findings\s*3/);
        expect(within(list).getAllByRole('listitem').map((li) => li.getAttribute('data-testid'))).toEqual([
            'composition-finding-composition.undeclared_dependency',
            'composition-finding-composition.unaccepted_caller',
            'composition-finding-composition.declared_never_observed',
        ]);
        expect(list).toHaveTextContent('Undeclared dependency');
    });
});

describe('CompositionTab — colour coding', () => {
    it('the legend carries exactly the four categories, each with a label', async () => {
        await renderTab();
        const legend = screen.getByTestId('composition-legend');
        const cats = COMPOSITION_CATEGORIES.map((c) => c.id);
        expect(cats).toEqual(['ai_resource', 'policy', 'limit', 'delegation_agent']);
        for (const c of COMPOSITION_CATEGORIES) {
            expect(within(legend).getByTestId(`legend-${c.id}`)).toHaveTextContent(c.label);
        }
        expect(legend.querySelectorAll('[data-testid^="legend-"]:not([data-testid="legend-neutral"])')).toHaveLength(4);
    });

    it('colour is never the only signal: every categorised node names its category in text', async () => {
        await renderTab();
        const labels = Object.fromEntries(COMPOSITION_CATEGORIES.map((c) => [c.id, c.label]));
        const categorised = screen
            .getAllByTestId(/^composition-node-/)
            .filter((n) => n.getAttribute('data-category') !== 'neutral');
        expect(categorised.length).toBeGreaterThan(0);
        const missing = categorised
            .filter((n) => !(n.textContent ?? '').includes(labels[n.getAttribute('data-category')!]))
            .map((n) => n.getAttribute('data-testid'));
        expect(missing).toEqual([]);
    });

    it('the four tokens are defined once, in globals.css, with light and dark values and no !important', () => {
        const src = resolve(__dirname, '../../..');
        const css = readFileSync(join(src, 'styles/globals.css'), 'utf8');
        for (const token of ['ai-resource', 'policy', 'limit', 'delegation-agent']) {
            const defs = css.match(new RegExp(`--category-${token}\\s*:`, 'g')) ?? [];
            expect({ token, defs: defs.length }).toEqual({ token, defs: 2 }); // :root + dark
        }
        expect(css.match(/--category-[a-z-]+\s*:/g)).toHaveLength(8);
        expect(css).toMatch(/\[data-theme="dark"\][^{]*\{[^}]*--category-ai-resource/);
        expect(css).not.toContain('!important');

        // …and nowhere else in the app source.
        const offenders: string[] = [];
        const walk = (dir: string) => {
            for (const name of readdirSync(dir)) {
                const p = join(dir, name);
                if (statSync(p).isDirectory()) {
                    if (name !== '__tests__' && name !== 'generated' && !NOT_SOURCE_DIRS.has(name)) walk(p);
                } else if (/\.(css|ts|tsx)$/.test(name) && p !== join(src, 'styles/globals.css')) {
                    if (/--category-[a-z-]+\s*:/.test(readFileSync(p, 'utf8'))) offenders.push(p);
                }
            }
        };
        // …of both editions.
        for (const root of sourceRoots()) walk(root);
        expect(offenders).toEqual([]);
    });
});

describe('CompositionTab — list view', () => {
    it('the accessible list view renders exactly the diagram’s nodes, with the same states', async () => {
        await renderTab();
        const diagram = screen.getAllByTestId(/^composition-node-/).map((n) => [
            n.getAttribute('data-testid')!.slice('composition-node-'.length),
            n.getAttribute('data-state'),
        ]);

        fireEvent.click(screen.getByTestId('composition-view-list'));
        expect(screen.queryByTestId('composition-diagram')).toBeNull();
        const list = screen.getAllByTestId(/^composition-list-row-/).map((r) => [
            r.getAttribute('data-testid')!.slice('composition-list-row-'.length),
            r.getAttribute('data-state'),
        ]);

        expect(list).toEqual(diagram);
        expect(list.map(([k]) => k)).toEqual(modelNodeKeys(buildCompositionModel(COMPOSITION_PAYLOAD)));
        // Spot check: the list states the external role in words.
        expect(screen.getByTestId('composition-list-row-dep:dep-bureau')).toHaveTextContent('Assumed, not tested');
    });
});

describe('CompositionTab — links and edge actions', () => {
    it('every node links to its page', async () => {
        await renderTab();
        const cases: [string, string][] = [
            ['dep:dep-fraud', 'ai-systems:sys-fraud:composition'],
            ['dep:dep-bureau', 'a2a-agent-cards'],
            ['model:llm-sonnet', 'llm-models:llm-sonnet'],
            ['mcp:mcp-core', 'mcp-config'],
            ['policy:guardrail:gr-pii', 'guardrails'],
            ['caller:sys-portal', 'ai-systems:sys-portal:composition'],
            // A member agent opens its identity's Implementation.
            ['agent:agent-intake', 'a2a-identities:agent-intake:implementation'],
            ['limit:run_cap:max_actions_per_run', 'groups:sys-demo:governance'],
        ];
        const opened = cases.map(([key]) => {
            setActiveTab.mockClear();
            fireEvent.click(within(node(key)).getAllByRole('button')[0]);
            return [key, setActiveTab.mock.calls[0]?.[0] as string | undefined];
        });
        expect(opened).toEqual(cases);
        // Inherited source links to the group; breadcrumb crumbs link to groups.
        setActiveTab.mockClear();
        fireEvent.click(within(screen.getByTestId('inherited-model:llm-mini')).getByRole('button'));
        expect(setActiveTab).toHaveBeenCalledWith('groups:grp-lending');
        fireEvent.click(screen.getByTestId('composition-location-grp-tenant'));
        expect(setActiveTab).toHaveBeenLastCalledWith('groups:grp-tenant');
    });

    it('a node whose page this console does not have is plain text, in the Diagram and in the List', async () => {
        // Knowledge bases are managed on a page another distribution
        // contributes (`app/links.ts`): with nothing contributed there is
        // no such page, and the node names the knowledge base without a link.
        await renderTab();
        const kb = node('kb:kb-policy');
        expect(kb).toHaveTextContent('Credit Policy Handbook');
        expect(within(kb).queryByRole('button', { name: 'Credit Policy Handbook' })).toBeNull();
        // Every other node of the diagram still links somewhere: its label is a button.
        const unlinked = screen
            .getAllByTestId(/^composition-node-/)
            .filter((el) => el.querySelector('.truncate')?.tagName !== 'BUTTON')
            .map((el) => el.getAttribute('data-testid'));
        expect(unlinked).toEqual(['composition-node-kb:kb-policy']);

        fireEvent.click(screen.getByTestId('composition-view-list'));
        const row = screen.getByTestId('composition-list-row-kb:kb-policy');
        expect(row).toHaveTextContent('Credit Policy Handbook');
        expect(within(row).queryByRole('button', { name: 'Credit Policy Handbook' })).toBeNull();
        expect(setActiveTab).not.toHaveBeenCalled();
    });

    it('a contributed page is linked to: the node opens the tab the contribution names', async () => {
        render(
            <SlotProvider slots={{ 'app.links': [{ id: CONTRIBUTED_LINKS.knowledgeBases, tab: 'fake-kb-page' }] }}>
                <CompositionTab aiSystemId="sys-demo" windowHours={168} setActiveTab={setActiveTab} />
            </SlotProvider>,
        );
        await screen.findByTestId('composition-tab');
        fireEvent.click(screen.getByTestId('composition-view-diagram'));
        fireEvent.click(within(node('kb:kb-policy')).getAllByRole('button')[0]);
        expect(setActiveTab).toHaveBeenCalledWith('fake-kb-page');
    });

    it('Remove deletes the edge and reloads the composition', async () => {
        await renderTab();
        fireEvent.click(screen.getByTestId('delete-dep:dep-fraud'));
        fireEvent.click(await screen.findByTestId('dependency-delete-confirm'));
        await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('sys-demo', 'dep-fraud'));
        await waitFor(() => expect(mockFetchComposition).toHaveBeenCalledTimes(2));
        expect(screen.queryByTestId('dependency-delete-dialog')).toBeNull();
    });

    it('Edit opens the dialog on the edge with role and target locked', async () => {
        await renderTab();
        fireEvent.click(screen.getByTestId('edit-dep:dep-bureau'));
        const dialog = await screen.findByTestId('dependency-dialog');
        expect(within(dialog).getByTestId('dependency-role-external')).toBeChecked();
        expect(within(dialog).getByTestId('dependency-role-external')).toBeDisabled();
        expect(within(dialog).getByTestId('dependency-fallback')).toHaveValue('Route to manual underwriting');
    });

    it('a load failure is shown, not an empty diagram', async () => {
        mockFetchComposition.mockRejectedValue(new Error('control plane unreachable'));
        render(<CompositionTab aiSystemId="sys-demo" />);
        expect(await screen.findByTestId('composition-error')).toHaveTextContent('control plane unreachable');
        expect(screen.queryByTestId('composition-diagram')).toBeNull();
    });
});

describe('CompositionTab — findings and pins', () => {
    const GRANT_AND_PIN_FINDINGS = [
        {
            code: 'composition.grant_without_edge',
            severity: 'warning' as const,
            message: 'A member agent of Brutor Demo System may call agent card Weather Agent (a2a_call grant), but no dependency covers it.',
            subject_id: 'card-weather',
        },
        {
            code: 'composition.edge_without_grant',
            severity: 'warning' as const,
            message: 'Brutor Demo System declares a dependency on Fraud Screener, but no member agent holds an a2a_call grant for it.',
            subject_id: 'sys-fraud',
        },
        {
            code: 'composition.dependency_pin_stale',
            severity: 'warning' as const,
            message: "Brutor Demo System is pinned to Address Validator's contract v2, but Address Validator now runs under v3.",
            subject_id: 'sys-address',
        },
    ];

    it('labels the grant and pin findings and attaches them to their nodes', async () => {
        mockFetchComposition.mockResolvedValue({ ...COMPOSITION_PAYLOAD, findings: GRANT_AND_PIN_FINDINGS });
        await renderTab();
        const findings = screen.getByTestId('composition-findings');
        expect(within(findings).getByTestId('composition-finding-composition.grant_without_edge')).toHaveTextContent(
            'Grant without a declared dependency',
        );
        expect(within(findings).getByTestId('composition-finding-composition.edge_without_grant')).toHaveTextContent(
            'Dependency without a grant',
        );
        expect(within(findings).getByTestId('composition-finding-composition.dependency_pin_stale')).toHaveTextContent(
            'Pinned contract is behind',
        );
        // Subject ids resolve to nodes: card, shared-service target.
        expect(node('dep:card:card-weather')).toHaveTextContent('no dependency covers it');
        expect(node('dep:dep-fraud')).toHaveTextContent('no member agent holds an a2a_call grant');
        expect(node('dep:dep-address')).toHaveTextContent('now runs under v3');
    });

    it('shows a pinned edge against the target’s active contract', async () => {
        mockFetchContracts.mockImplementation((target: string) =>
            Promise.resolve(
                target === 'sys-address'
                    ? { contracts: [], total: 2, active_version: 3, config_drifted: false }
                    : { contracts: [], total: 0, config_drifted: false },
            ),
        );
        await renderTab();
        await waitFor(() =>
            expect(node('dep:dep-address')).toHaveTextContent('pinned to v2 · target active v3 — behind'),
        );
        // Only pinned targets are asked; a followed edge already names its version.
        expect(mockFetchContracts).toHaveBeenCalledWith('sys-address');
        expect(mockFetchContracts).not.toHaveBeenCalledWith('sys-fraud');
        expect(node('dep:dep-fraud')).toHaveTextContent('following active v2');
    });
});
