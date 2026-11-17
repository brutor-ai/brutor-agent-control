/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The printable renderings of the two canonical assurance documents. The
 * JSON export is the artifact under signature; this page is only allowed to
 * *present* it, so the rules worth pinning are the ones that stop it from
 * saying more than the document does: absent facts are disclosed rather
 * than omitted, `unexplained` stays `unexplained`, and untrusted document
 * text is escaped before it reaches the page.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { printAssuranceReport, printContractDocument } from '../printableDocuments';
import { baselineValue, distributionSummary } from '../baselineFormat';

interface FakeWindow {
    title: string;
    html: string;
    printed: number;
}

let opened: FakeWindow[];

beforeEach(() => {
    opened = [];
    vi.spyOn(window, 'open').mockImplementation(() => {
        const w: FakeWindow = { title: '', html: '', printed: 0 };
        opened.push(w);
        const fake = {
            document: {
                write: (html: string) => {
                    w.html += html;
                    w.title = /<title>(.*?)<\/title>/.exec(html)?.[1] ?? '';
                },
                close: () => undefined,
            },
            // The real code defers print() by a tick; run it immediately so
            // the test can assert the dialog was asked for.
            setTimeout: (fn: () => void) => {
                fn();
                return 0;
            },
            print: () => {
                w.printed += 1;
            },
        };
        return fake as unknown as Window;
    });
});

afterEach(() => {
    vi.restoreAllMocks();
});

const html = () => opened[0].html;

// ---------------------------------------------------------------- contract

const contractDoc = () => ({
    spec: 'brutor.ai-system-contract',
    spec_version: '1.0',
    generated_at: '2026-08-04T10:00:00Z',
    tenant_id: 'acme',
    system: { id: 'sys-1', name: 'Copilot', system_kind: 'agent' },
    contract: {
        version: 3,
        status: 'active',
        hash: 'abc123',
        hash_algorithm: 'sha256',
        created_at: '2026-07-01T00:00:00Z',
        created_by: 'priya',
        approved_at: '2026-07-02T00:00:00Z',
        approved_by: 'sam',
        promoted_at: '2026-07-03T00:00:00Z',
    },
    closure: {
        autonomy_level: 'supervised',
        ai_system: { eu_ai_act_risk_tier: 'high', intended_use: 'Draft replies' },
        composition: {
            models: [{ id: 'm1', name: 'gpt-5.2' }],
            tools: [],
        },
        operating_envelope: { p95_latency_ms: 1200 },
    },
});

describe('printContractDocument', () => {
    it('opens one window, writes it and asks for the print dialog', () => {
        printContractDocument(contractDoc());
        expect(opened).toHaveLength(1);
        expect(opened[0].printed).toBe(1);
        expect(html()).toContain('<h1>AI System Contract</h1>');
    });

    it('titles the window with the system and contract version', () => {
        printContractDocument(contractDoc());
        expect(opened[0].title).toBe('AI System Contract — Copilot v3');
    });

    it('renders the identity, hash and approval facts', () => {
        printContractDocument(contractDoc());
        expect(html()).toContain('Copilot');
        expect(html()).toContain('abc123');
        expect(html()).toContain('2026-07-02T00:00:00Z by sam');
    });

    it('omits the approval row entirely when nothing was approved', () => {
        const doc = contractDoc();
        delete (doc.contract as Record<string, unknown>).approved_by;
        printContractDocument(doc);
        expect(html()).not.toContain('<th>Approved</th>');
    });

    it('says "none" for an empty composition section rather than dropping it', () => {
        // A section silently missing reads as "not part of this contract";
        // an explicit "none (0)" reads as "nothing is bound here".
        printContractDocument(contractDoc());
        expect(html()).toContain('<th>tools</th>');
        expect(html()).toContain('none');
        expect(html()).toContain('(0)');
        expect(html()).toContain('gpt-5.2');
    });

    it('drops the envelope section when the contract has no envelope', () => {
        const doc = contractDoc();
        delete (doc.closure as Record<string, unknown>).operating_envelope;
        printContractDocument(doc);
        expect(html()).not.toContain('Operating envelope');
    });

    it('renders envelope terms when there is one', () => {
        printContractDocument(contractDoc());
        expect(html()).toContain('Operating envelope');
        expect(html()).toContain('p95_latency_ms');
        expect(html()).toContain('1200');
    });

    it('survives an empty document without throwing', () => {
        expect(() => printContractDocument({})).not.toThrow();
        expect(opened).toHaveLength(1);
    });

    it('escapes document text rather than letting it become markup', () => {
        printContractDocument({ system: { name: '<script>alert(1)</script>' } });
        expect(html()).not.toContain('<script>alert(1)</script>');
        expect(html()).toContain('&lt;script&gt;');
    });

    it('renders a nested object as JSON, never as [object Object]', () => {
        printContractDocument({
            closure: { operating_envelope: { budget: { max_usd: 50 } } },
        });
        expect(html()).not.toContain('[object Object]');
        expect(html()).toContain('max_usd');
    });

    it('writes nothing when the browser blocks the popup', () => {
        vi.spyOn(window, 'open').mockReturnValue(null);
        expect(() => printContractDocument(contractDoc())).not.toThrow();
        expect(opened).toHaveLength(0);
    });
});

// ------------------------------------------------------------------ report

const reportDoc = () => ({
    spec: 'brutor.ai-system-assurance-report',
    spec_version: '1.0',
    period_hours: 720,
    generated_at: '2026-08-04T10:00:00Z',
    identity: {
        name: 'Copilot',
        owner: 'RevOps',
        system_kind: 'agent',
        lifecycle_stage: 'active',
        eu_ai_act_risk_tier: 'high',
    },
    verdict: { status: 'assured_with_exceptions', conclusion: 'Held within bounds.', failing_dependencies: [] },
    health: { health_state: 'degraded', health_score: 0.72, reasons: ['cost above baseline'] },
    contract_compliance: {
        envelope: {
            terms: [
                { label: 'p95 latency', status: 'met', detail: '900ms' },
                { label: 'cost per run', status: 'breached' },
            ],
        },
    },
    exceptions: {
        guardrail_blocks: 4,
        policy_denials: 0,
        approval_requests: 2,
        automatic_responses: 1,
    },
    changes: {
        contract_versions_minted: [2, 3],
        attributed_model_changes: 1,
        attributed_dependency_changes: 0,
    },
    not_covered: ['Model internals', 'Vendor sub-processors'],
    non_drifting: {
        events: [
            {
                metric_key: 'tool_call_rate',
                baseline_value: 1.234,
                observed_value: 4.567,
                direction: 'up',
                severity: 'high',
                status: 'active',
            },
        ],
    },
    baselines: {
        metrics: [
            { metric_key: 'latency_ms', median: 800, mad: 40, state: 'ready', sample_count: 900 },
            {
                metric_key: 'tool_mix',
                distribution: { search: 0.6, write: 0.3, other: 0.1 },
                state: 'learning',
                sample_count: 20,
            },
        ],
    },
    recommended_controls: [
        { severity: 'high', control: 'Add a cost cap', subject: 'gpt-5.2', reason: 'spend drift' },
    ],
    assurance_summary: {
        status: { code: 'assured_with_exceptions', label: 'Assured with exceptions' },
        what_we_know: 'Runs stayed inside the envelope apart from cost.',
        what_prevents_full_assurance: ['Cost term breached'],
        recommended_action: 'Set a cap',
        why_it_matters: 'Unbounded spend is an unbounded blast radius.',
        level: { scale: 'Evidence maturity', level: 2, label: 'Measured', means: 'Baselines exist' },
        dimensions: {
            operational_evidence: { status: 'ok', detail: '1200 runs' },
            contract_assurance: { status: 'partial', detail: 'one term breached' },
            independent_evidence: { status: 'missing', detail: 'no external replay' },
        },
    },
});

describe('printAssuranceReport', () => {
    it('titles itself a Report without an attestation and a Certificate with one', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('<h1>AI System Assurance Report</h1>');
        expect(html()).not.toContain('<h2>Attestation</h2>');

        opened = [];
        printAssuranceReport(reportDoc(), {
            attestation: { signed: true, report_sha256: 'deadbeef', signature: 'sig', public_key: 'pk' },
        });
        expect(html()).toContain('<h1>AI System Assurance Certificate</h1>');
        expect(html()).toContain('<h2>Attestation</h2>');
        expect(html()).toContain('deadbeef');
    });

    it('discloses an unsigned attestation with its reason instead of hiding it', () => {
        printAssuranceReport(reportDoc(), {
            attestation: { signed: false, unsigned_reason: 'no signing key configured', report_sha256: 'abc' },
        });
        expect(html()).toContain('unsigned');
        expect(html()).toContain('no signing key configured');
    });

    it('reads the verdict back without underscores and colours it', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('assured with exceptions');
        expect(html()).toContain('pill warn');
    });

    it('prints an envelope term the report does not grade as enforced, not graded', () => {
        const r = reportDoc();
        (r.contract_compliance as { envelope: { terms: Record<string, unknown>[] } }).envelope.terms.push(
            { label: 'New runs per credential in a window', status: 'not_graded', detail: 'no worst case is recorded' },
            { label: 'Tasks that complete', status: 'insufficient_evidence' },
        );
        printAssuranceReport(r);
        expect(html()).toContain('<span class="pill mut">enforced, not graded</span>');
        expect(html()).toContain('no worst case is recorded');
        expect(html()).not.toContain('not_graded');
        expect(html()).toContain('<span class="pill mut">insufficient evidence</span>');
        expect(html()).toContain('<span class="pill ok">met</span>');
        expect(html()).toContain('<span class="pill bad">breached</span>');
    });

    it('flags a verdict measured against observed history as conditional', () => {
        const r = reportDoc();
        (r.verdict as Record<string, unknown>).basis = 'observed_history';
        printAssuranceReport(r);
        expect(html()).toContain('conditional — measured against observed history');
    });

    it('does not add the conditional caveat when the basis is the contract', () => {
        printAssuranceReport(reportDoc());
        expect(html()).not.toContain('conditional — measured');
    });

    it('names the health score as an evidence score, not a bare percentage', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('degraded (evidence score 72%)');
        expect(html()).toContain('cost above baseline');
    });

    it('says "no score" rather than implying zero when health was never scored', () => {
        const r = reportDoc();
        delete (r.health as Record<string, unknown>).health_score;
        printAssuranceReport(r);
        expect(html()).toContain('(no score)');
        expect(html()).not.toContain('evidence score 0%');
    });

    it('states plainly when no envelope is under signature', () => {
        const r = reportDoc();
        delete (r.contract_compliance as Record<string, unknown>).envelope;
        printAssuranceReport(r);
        expect(html()).toContain('no operating envelope under signature');
    });

    it('renders each envelope term with its met/breached standing', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('p95 latency');
        expect(html()).toContain('pill ok">met');
        expect(html()).toContain('pill bad">breached');
    });

    it('renders baselines as a median ± MAD, or as a top-3 distribution', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('800 ± 40');
        expect(html()).toContain('search 60%');
        expect(html()).toContain('900 runs');
    });

    it('rounds drift values instead of printing raw floats', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('1.23');
        expect(html()).toContain('4.57');
        expect(html()).toContain('(up)');
    });

    it('labels an unattributed drift finding "unexplained" rather than guessing', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('unexplained');
    });

    it('renders an unmeasured drift value as an em dash, not as zero', () => {
        const r = reportDoc();
        (r.non_drifting as { events: Record<string, unknown>[] }).events[0].observed_value = null;
        printAssuranceReport(r);
        expect(html()).toContain('—');
    });

    it('omits the optional sections when the report has none', () => {
        printAssuranceReport({ identity: { name: 'Bare' }, verdict: { failing_dependencies: [] }, health: {} });
        expect(html()).not.toContain('Learned baselines');
        expect(html()).not.toContain('Drift findings');
        expect(html()).not.toContain('Recommended controls');
        expect(html()).not.toContain('Assurance status');
    });

    it('always states what the report does not cover', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('What this report does not cover');
        expect(html()).toContain('Vendor sub-processors');
        // Even with nothing listed, the heading stands.
        opened = [];
        printAssuranceReport({ identity: {}, verdict: { failing_dependencies: [] }, health: {} });
        expect(html()).toContain('What this report does not cover');
    });

    it('renders the assurance summary with its blockers and maturity level', () => {
        printAssuranceReport(reportDoc());
        expect(html()).toContain('ASSURED WITH EXCEPTIONS');
        expect(html()).toContain('Cost term breached');
        expect(html()).toContain('Evidence maturity');
        expect(html()).toContain('2 — Measured');
        // Dimensions are labelled in words, not by their wire keys.
        expect(html()).toContain('Independent evidence');
        expect(html()).not.toContain('independent_evidence');
    });

    it('falls back to the plain conclusion when the summary has no narrative', () => {
        const r = reportDoc();
        r.assurance_summary = { conclusion: 'Nothing conclusive.' } as never;
        printAssuranceReport(r);
        expect(html()).toContain('Nothing conclusive.');
        expect(html()).not.toContain('What we know');
    });

    it('escapes untrusted report text', () => {
        printAssuranceReport({
            identity: { name: '<img onerror=x>' },
            verdict: { failing_dependencies: [] },
            health: {},
        });
        expect(html()).not.toContain('<img onerror=x>');
        expect(html()).toContain('&lt;img onerror=x&gt;');
    });

    it('survives an empty report and still asks to print', () => {
        expect(() => printAssuranceReport({})).not.toThrow();
        expect(opened[0].printed).toBe(1);
    });
});

// ------------------------------------------------ Composite AI systems

describe('composite AI systems in the printed documents', () => {
    it('prints the dependency section, tested vs assumed, and the failing dependency', async () => {
        const { DEPENDENCY_HEALTH } = await import('../../components/ai-systems/__tests__/fixtures/dependencyHealthPayloads');
        printAssuranceReport({
            identity: { name: 'Brutor Demo System' },
            verdict: { status: 'assured_with_exceptions', conclusion: 'x', failing_dependencies: ['Scoring Service'] },
            signal_basis: 'runs',
            service: null,
            dependency_health: DEPENDENCY_HEALTH,
            not_covered: [],
        });
        expect(html()).toContain('critical dependencies not healthy');
        expect(html()).toContain('<h2>Dependencies</h2>');
        expect(html()).toContain('tested · Degraded');
        expect(html()).toContain('assumed, not tested');
        expect(html()).toContain('caps this system at degraded');
        expect(html()).toContain('Fallback did NOT hold');
        expect(html()).toContain('At the gateway boundary: 20 calls');
        // An external agent is never given a tested status.
        expect(html()).not.toContain('tested · Assumed');
    });

    it('prints a shared service’s basis, served calls and served-call drift', async () => {
        const { SERVICE_FACTS } = await import('../../components/ai-systems/__tests__/fixtures/dependencyHealthPayloads');
        printAssuranceReport({
            identity: { name: 'Scoring Service' },
            verdict: { status: 'assured', conclusion: 'x', failing_dependencies: [] },
            signal_basis: 'segments',
            service: SERVICE_FACTS,
            dependency_health: [],
            baselines: { metrics: [{ metric_key: 'action_count', basis: 'segments', state: 'ready', sample_count: 128, median: 4, mad: 1 }] },
            non_drifting: {
                events: [{ metric_key: 'cost_per_run', basis: 'segments', caller_system_id: 'sys-demo', severity: 'medium', status: 'active' }],
            },
            not_covered: [],
        });
        expect(html()).toContain('Assured from 128 served calls as a shared service');
        expect(html()).toContain('<h2>Served as a shared service</h2>');
        expect(html()).toContain('Actions per served call');
        expect(html()).toContain('128 served calls');
        expect(html()).toContain('Cost per served call');
        expect(html()).toContain('caller sys-demo');
        expect(html()).not.toContain('<h2>Dependencies</h2>');
    });

    it('prints the sealed dependency edges of a contract', async () => {
        const { CLOSURE_DEPENDENCIES } = await import('../../components/ai-systems/__tests__/fixtures/dependencyHealthPayloads');
        printContractDocument({
            system: { name: 'Brutor Demo System' },
            contract: { version: 4 },
            closure: { dependencies: CLOSURE_DEPENDENCIES },
        });
        expect(html()).toContain('<h2>Dependencies</h2>');
        expect(html()).toContain('pinned v2 · abcdef01');
        expect(html()).toContain('follows v2 · 98765432');
        expect(html()).toContain('agent card card-bureau');
    });

    // Report spec 1.6 — the printout carries the same
    // "Composition and dependencies" section as the report view.
    it('prints the Composition and dependencies section', async () => {
        const { COMPOSITION_SECTION } = await import('../../components/ai-systems/__tests__/fixtures/compositionReportSection');
        printAssuranceReport({ ...reportDoc(), composition: COMPOSITION_SECTION });
        const out = html();
        expect(out).toContain('<h2>Composition and dependencies</h2>');
        expect(out).toContain('limited → high via Lending Decision Engine');
        expect(out).toContain('Accepts only listed callers — enforced');
        expect(out).toContain('tested · Degraded (failing on reliability) · caps this system');
        expect(out).toContain('assumed, not tested');
        expect(out).toContain('task states: 12 completed, 2 failed, 1 input required');
        expect(out).toContain('Nordic Credit Bureau AB · supplier information on file (1)');
        expect(out).toContain('<span class="pill warn">gap</span> Group Address Services Ltd · supplier information MISSING');
        expect(out).toContain('Declares Address Validator separable — sealed as');
        expect(out).toContain('Shadow Batch Job — NOT accepted (refused at the gateway) · 2 calls');
        expect(out).toContain('Unaccepted caller: Enforced: Shadow Batch Job is not an accepted caller; 2 calls refused.');
    });

    it('a report without the composition section prints none', () => {
        printAssuranceReport(reportDoc());
        expect(html()).not.toContain('Composition and dependencies');
    });

    it('prints an edge’s supplier in the sealed contract terms', () => {
        printContractDocument({
            system: { name: 'Brutor Demo System' },
            contract: { version: 5 },
            closure: {
                dependencies: [
                    {
                        role: 'external',
                        depends_on_system_id: null,
                        agent_card_id: 'card-bureau',
                        criticality: 'degradable',
                        contributes_to_decision: true,
                        fallback: 'manual',
                        separability_rationale: null,
                        assumptions: {},
                        supplier_legal_entity: 'Nordic Credit Bureau AB',
                    },
                ],
            },
        });
        expect(html()).toContain('Supplier: Nordic Credit Bureau AB');
    });
});

// ------------------------------------------------ Agent releases

describe('agent implementation in print', () => {
    it('the contract prints each member agent’s implementation terms', () => {
        const doc = contractDoc();
        (doc.closure.composition as Record<string, unknown>).agent_identities = [
            { id: 'agent-1', name: 'Screening', implementation_name: 'screening-agent', approved_versions: ['1.5.0', '1.4.*'], require_release: true },
            { id: 'agent-2', name: 'Fraud <x>' },
        ];
        printContractDocument(doc);
        expect(html()).toContain('<h2>Agent implementation</h2>');
        expect(html()).toContain('must declare implementation “screening-agent”');
        expect(html()).toContain('approved releases: 1.4.*, 1.5.0');
        expect(html()).toContain('runs without a declared release are a finding');
        expect(html()).toContain('observe only');
        // Document text is escaped.
        expect(html()).toContain('Fraud &lt;x&gt;');
    });

    it('a contract with no member agents prints no agent implementation section', () => {
        printContractDocument(contractDoc());
        expect(html()).not.toContain('Agent implementation');
    });

    it('the report prints each member agent’s release, trust and approval', async () => {
        const { COMPOSITION_SECTION } = await import('../../components/ai-systems/__tests__/fixtures/compositionReportSection');
        printAssuranceReport({
            ...reportDoc(),
            composition: {
                ...COMPOSITION_SECTION,
                agents: [
                    {
                        id: 'agent-1', name: 'Screening', implementation_name: 'screening-agent',
                        approved_versions: ['1.4.*'], require_release: false,
                        release: { name: 'screening-agent', version: '1.5.0', build: null, trust: 'verified', approved: false, last_seen_at: null },
                    },
                    { id: 'agent-2', name: 'Fraud', implementation_name: null, approved_versions: [], require_release: false, release: null },
                ],
                agent_findings: [
                    { code: 'agent.unapproved_release', severity: 'high', agent_id: 'agent-1', agent_name: 'Screening', message: 'Screening ran release screening-agent@1.5.0 (verified), which is not approved.' },
                ],
            },
        });
        expect(html()).toContain('Member agents and their releases');
        expect(html()).toContain('Screening — screening-agent@1.5.0 · verified · not approved');
        expect(html()).toContain('Fraud — no release declared');
        expect(html()).toContain('Unapproved release: Screening ran release');
    });

    it('a section that names no agents prints no agent releases', async () => {
        const { COMPOSITION_SECTION } = await import('../../components/ai-systems/__tests__/fixtures/compositionReportSection');
        printAssuranceReport({ ...reportDoc(), composition: COMPOSITION_SECTION });
        expect(html()).not.toContain('Member agents and their releases');
    });
});

describe('excluded from judgement in print', () => {
    // Report spec 1.8 — the printed report carries the same exclusions
    // section as the panel: every window with who / why / runs excluded /
    // record, the epochs, and the coverage statement of the excluded period.
    it('prints the excluded-from-judgement section and the coverage statement', () => {
        printAssuranceReport({
            ...reportDoc(),
            non_drifting: {
                events: [
                    { metric_key: 'action_count', severity: 'high', status: 'resolved', resolution_kind: 'accepted_change' },
                ],
            },
            baselines: { metrics: [{ metric_key: 'action_count', basis: 'runs', median: 3, state: 'learning', sample_count: 12 }] },
            judgement_exclusions: {
                incident_windows: [
                    {
                        id: 'iw-1', ai_system_id: 'sys-1', ai_system_name: 'Loan <pre>', tenant_wide: false,
                        starts_at: '2026-09-27T21:00:00Z', ends_at: '2026-09-28T06:40:00Z',
                        cause_category: 'provider_outage', reason: 'credits <ran> out',
                        declared_by: 'alice', record_id: 'rec-9', runs_excluded: 14,
                    },
                ],
                baseline_epochs: [
                    {
                        id: 'ep-1', ai_system_id: 'sys-1', metric_key: 'action_count', basis: 'runs',
                        caller_system_id: 'all', starts_at: '2026-09-26T06:06:00Z', kind: 'accepted_change',
                        declared_by: 'bob', reason: 'retune', record_id: null,
                    },
                ],
                excluded_hours: 9.67,
                excluded_runs: 14,
                excluded_approval_requests: 250,
            },
            coverage: {
                excluded_period: {
                    incident_windows: 1, excluded_hours: 9.67, excluded_runs: 14,
                    statement: 'that period is unmeasured, not passed.',
                },
            },
        });
        const out = html();
        expect(out).toContain('<h2>Excluded from judgement</h2>');
        expect(out).toContain('that period is unmeasured, not passed.');
        expect(out).toContain('Provider outage · Loan &lt;pre&gt;');
        expect(out).toContain('credits &lt;ran&gt; out');
        expect(out).toContain('by alice');
        expect(out).toContain('<td>14</td>');
        expect(out).toContain('sealed record rec-9');
        expect(out).toContain('250 approval request(s) raised inside the windows: recorded, not judged by the oversight rate.');
        expect(out).toContain('accepted change by bob');
        // The baseline row carries its epoch; the closed finding its kind.
        expect(out).toMatch(/learning<\/span> 12 runs<br>re-learning since .+ — accepted change by bob/);
        expect(out).toContain('resolved — accepted change');
    });

    it('a report without the exclusions section prints none', () => {
        printAssuranceReport(reportDoc());
        expect(html()).not.toContain('Excluded from judgement');
    });

    // The bug class: the printable formats a value differently from the
    // screen it prints. Baselines once printed as raw floats
    // ("0.022198625 ± 0.002228000000000001") while the tab read "$0.0222 ± $0.0022".
    it('writes a baseline the way the Assurance tab does', () => {
        const metrics = [
            { metric_key: 'cost_per_run', state: 'ready', sample_count: 2394, median: 0.022198625, mad: 0.002228000000000001, p90: 0.0262 },
            { metric_key: 'duration_ms', state: 'ready', sample_count: 2394, median: 9904.5, mad: 3498.500000000001, p90: 13602 },
            { metric_key: 'model_mix', state: 'ready', sample_count: 2394, median: null, mad: null, p90: null, distribution: { 'gpt-5.2': 0.6667, 'gpt-5.5': 0.3333 } },
            { metric_key: 'tool_error_rate', state: 'learning', sample_count: 3, median: null, mad: null, p90: null, distribution: null },
        ];
        printAssuranceReport({ ...reportDoc(), baselines: { ready: 3, learning: 1, metrics } });
        const out = html();
        for (const m of metrics) {
            const expected = m.distribution ? distributionSummary(m.distribution) : baselineValue(m);
            expect(out).toContain(`<td>${expected} <span class="pill`);
        }
        expect(out).toContain('$0.0222 ± $0.0022, p90 $0.0262');
        expect(out).toContain('9904.5 ± 3498.5, p90 13602');
        // No number on the page carries more than four decimals.
        const baselinesHtml = out.slice(out.indexOf('<h2>Learned baselines</h2>'), out.indexOf('<h2>Exceptions in the period</h2>'));
        expect(baselinesHtml).not.toMatch(/\d\.\d{5,}/);
    });
});
