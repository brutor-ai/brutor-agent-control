/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Contracts for one AI System.
 *
 * Three sub-tabs, one subject: what the system is signed off to do. **Active**
 * is the version list with the stored closure and the diff; **Operating
 * envelope** is the declared-bounds half of the same signature; **External
 * evidence** is what a third party attached. The lifecycle gate is not on
 * this panel — it has its own tab (`LifecyclePanel`), because promoting a
 * contract is not a lifecycle transition and putting both sets of buttons
 * on one screen would say otherwise.
 *
 * Three things this panel is deliberate about.
 *
 * **Widened permissions lead.** A diff is presented as "what this version
 * lets the system do that it could not before", with narrowings underneath
 * as context. A flat change list sorted by field name is how a reviewer
 * learns to click approve without reading, and the whole point of the
 * phase is that the approval means something.
 *
 * **The gate shows its checklist before you click.** Every reachable stage
 * lists what it needs and what is missing, so an operator is never told
 * "no" by a toast after the fact. A gate you can only discover by failing
 * it is a gate people resent and route around.
 *
 * **Drift from the active contract is stated, not implied.** "This system
 * is running configuration nobody approved" is the conformance
 * question, and it is the one an auditor asks first.
 */

import { useCallback, useEffect, useState } from 'react';
import {
    AlertTriangle,
    ChevronDown,
    ChevronRight,
    Download as DownloadIcon,
    FileText,
    Printer,
    RefreshCw,
    Stamp,
} from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import { ContractStatesDiagram } from './ContractLifecycleDiagram';
import EnvelopePanel from './EnvelopePanel';
import OutcomesPanel from './OutcomesPanel';
import { ClosureOutcomes } from './ClosureOutcomes';
import EvidencePanel from './EvidencePanel';
import { ClosureDependencies, DiffEntryLine } from './ContractDependencies';

import { useGateway } from '../../hooks/useGateway';
import { useAdminUsername } from '../../hooks/useAdminUsername';
import { downloadJson } from '../../lib/downloadJson';
import { printContractDocument } from '../../lib/printableDocuments';
import { declarationOf, declarationTerms } from '../../lib/agentRelease';
import { isDependencyContractChange } from '../../lib/composition';
import {
    AISystemContract,
    AUTONOMY_CLASSES,
    AUTONOMY_LABELS,
    AutonomyLevel,
    AUTONOMY_HELP,
    AUTONOMY_RESOLUTION_HELP,
    ClosureDependency,
    CONTRACT_STATUS_CLASSES,
    ContractListResponse,
    LifecycleResponse,
} from '../../lib/aiSystemTypes';
import { errorKind } from '../../lib/narrow';

export type ContractSubTab = 'active' | 'outcomes' | 'envelope' | 'evidence';

const SUB_TABS: { id: ContractSubTab; label: string }[] = [
    { id: 'active', label: 'Active' },
    // How a run may end, signed off with the rest.
    { id: 'outcomes', label: 'Outcomes' },
    { id: 'envelope', label: 'Operating envelope' },
    { id: 'evidence', label: 'External evidence' },
];

interface ContractsPanelProps {
    aiSystemId: string;
    /** Which sub-tab to open on; the Lifecycle tab deep-links into
     *  `evidence` when a gate wants an impact assessment attached. */
    initialSubTab?: ContractSubTab;
    /** The envelope and the evidence are part of what the gate and the
     *  closure render; the host re-queries them after a change. */
    onChanged?: () => void;
}

export function ContractsPanel({ aiSystemId, initialSubTab, onChanged }: ContractsPanelProps) {
    const {
        fetchAISystemContracts,
        fetchAISystemLifecycle,
        mintAISystemContract,
        approveContract,
        rejectContract,
        promoteContract,
        exportContractDocument,
    } = useGateway();

    const [subTab, setSubTab] = useState<ContractSubTab>(initialSubTab ?? 'active');
    const currentAdmin = useAdminUsername();
    useEffect(() => {
        if (initialSubTab) setSubTab(initialSubTab);
    }, [initialSubTab]);

    const [contracts, setContracts] = useState<ContractListResponse | null>(null);
    const [lifecycle, setLifecycle] = useState<LifecycleResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const [c, l] = await Promise.all([
                fetchAISystemContracts(aiSystemId),
                fetchAISystemLifecycle(aiSystemId),
            ]);
            setContracts(c);
            setLifecycle(l);
        } catch (err: unknown) {
            console.error('Failed to load contracts:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load contracts');
            setContracts(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchAISystemContracts, fetchAISystemLifecycle]);

    useEffect(() => {
        void load();
    }, [load]);

    const run = async (key: string, fn: () => Promise<unknown>, after?: string) => {
        try {
            setBusy(key);
            setError('');
            setNotice('');
            await fn();
            if (after) setNotice(after);
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Operation failed');
        } finally {
            setBusy('');
        }
    };

    const mint = () =>
        run('mint', async () => {
            const result = await mintAISystemContract(aiSystemId);
            if (!result.created) {
                // Not an error — saying so plainly stops an operator from
                // clicking repeatedly wondering why nothing happened.
                setNotice(
                    `Configuration already matches contract v${result.contract.version}; no new version was created.`
                );
            }
        });

    // Re-attestation is a different act from minting, so it gets its own
    // button rather than a checkbox on this one. Minting captures a change;
    // re-attesting captures a *signature* on config that has not changed —
    // a new owner taking responsibility, or a periodic review. The version
    // it produces has an empty diff on purpose.
    const reattest = () =>
        run('reattest', async () => {
            const result = await mintAISystemContract(aiSystemId, { force: true });
            setNotice(
                `Version v${result.contract.version} minted with no configuration change — approve it to record who now stands behind it.`
            );
        });

    const autonomy = lifecycle?.autonomy_level as AutonomyLevel | undefined;

    // A new version records a change; with nothing changed there is nothing to
    // record, and a button that mints an identical hash teaches people to
    // click it "just in case".
    const nothingChanged = !!contracts && contracts.total > 0 && !contracts.config_drifted;
    // Re-attestation is a *different* signature on the same rules. The admin
    // whose name is already on the governing version has nothing to add.
    const governing =
        contracts?.contracts.find((c) => c.is_active) ?? contracts?.contracts[0];
    const alreadyAttested =
        !!governing?.approved_by && !!currentAdmin && governing.approved_by === currentAdmin;

    return (
        <div className="space-y-4" data-testid="contracts-panel">
            <div className="border-b border-default" data-testid="contract-subtabs">
                <nav className="flex gap-1 -mb-px" role="tablist" aria-label="Contract">
                    {SUB_TABS.map((t) => (
                        <button
                            key={t.id}
                            type="button"
                            role="tab"
                            aria-selected={subTab === t.id}
                            onClick={() => setSubTab(t.id)}
                            data-testid={`contract-subtab-${t.id}`}
                            className={`px-3 py-1.5 text-sm border-b-2 ${
                                subTab === t.id
                                    ? 'border-primary-600 text-primary-900 font-medium'
                                    : 'border-transparent text-secondary hover:text-default'
                            }`}
                        >
                            {t.label}
                        </button>
                    ))}
                </nav>
            </div>

            {subTab === 'outcomes' && (
                <OutcomesPanel aiSystemId={aiSystemId} onChanged={() => { void load(); onChanged?.(); }} />
            )}
            {subTab === 'envelope' && (
                <EnvelopePanel aiSystemId={aiSystemId} onChanged={onChanged} />
            )}
            {subTab === 'evidence' && (
                <EvidencePanel aiSystemId={aiSystemId} onChanged={onChanged} />
            )}

            {subTab === 'active' && (
            <div className="space-y-4" data-testid="contract-active">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Contract</h3>
                    <HelpTip text="A hash-pinned snapshot of every control that applies to this AI System, generated from live configuration. Stamped on every run, so a historical run can be tied to the exact rules that governed it." />
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        disabled={busy === 'mint' || nothingChanged}
                        onClick={() => void mint()}
                        title={
                            nothingChanged
                                ? `Live configuration matches v${contracts?.active_version ?? governing?.version ?? ''} — nothing new to record`
                                : 'Freeze the live configuration into a new version and hash it'
                        }
                        className="flex items-center gap-2 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                        data-testid="mint-button"
                    >
                        <FileText className="w-4 h-4" />
                        New version from current config
                    </button>
                    <button
                        type="button"
                        disabled={busy === 'reattest' || alreadyAttested}
                        onClick={() => void reattest()}
                        title={
                            alreadyAttested
                                ? `v${governing?.version} is already attested by ${currentAdmin}; re-attestation is for a different approver`
                                : 'Mint a version with no configuration change, so a new approver can sign the current rules'
                        }
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                        data-testid="reattest-button"
                    >
                        <Stamp className="w-4 h-4" />
                        Re-attest
                    </button>
                    <button
                        type="button"
                        onClick={() => void load()}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                </div>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            {notice && !error && (
                <p className="text-sm text-muted" data-testid="contract-notice">
                    {notice}
                </p>
            )}

            {/* The conformance question, stated rather than implied. */}
            {contracts?.config_drifted && !error && (
                <div
                    className="flex items-start gap-2 p-3 border border-amber-200 bg-amber-50 rounded"
                    data-testid="config-drifted"
                >
                    <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                    <div className="text-sm text-amber-900">
                        <p className="font-medium">
                            Running configuration nobody approved
                        </p>
                        <p className="mt-1">
                            Live configuration no longer matches the active contract
                            {contracts.active_version
                                ? ` (v${contracts.active_version})`
                                : ''}
                            . Mint a new version so the runs on record can be tied to
                            the rules that actually governed them.
                        </p>
                    </div>
                </div>
            )}

            {autonomy && (
                <div className="flex items-center gap-2" data-testid="autonomy-level">
                    <span className="text-xs text-muted">Autonomy</span>
                    <HelpTip
                        title={AUTONOMY_LABELS[autonomy]}
                        text={`${AUTONOMY_HELP[autonomy]} ${AUTONOMY_RESOLUTION_HELP}`}
                        ariaLabel="What does this autonomy level mean?"
                    />
                    <span
                        className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${AUTONOMY_CLASSES[autonomy]}`}
                    >
                        {AUTONOMY_LABELS[autonomy]}
                    </span>
                    {autonomy === 'suspended' && (
                        <span className="text-xs text-red-700">
                            Every actor in this system is denied.
                        </span>
                    )}
                </div>
            )}

            {/* The one fact operators get wrong, stated where the buttons are:
                promoting a version here is not a stage transition — that lives
                on the Lifecycle tab. */}
            {!error && <ContractStatesDiagram />}

            {contracts && !error && contracts.total === 0 && (
                <div className="p-6 text-center border border-dashed border-strong rounded-lg">
                    <p className="text-sm text-default font-medium">
                        No contract yet
                    </p>
                    <p className="text-sm text-muted mt-1">
                        This system&apos;s runs are governed by live configuration that
                        has never been pinned. Mint one to make what it is allowed to do
                        provable.
                    </p>
                </div>
            )}

            {contracts?.contracts.map((c) => (
                <ContractRow
                    key={c.id}
                    aiSystemId={aiSystemId}
                    contract={c}
                    busy={busy === c.id}
                    onApprove={() =>
                        void run(c.id, () => approveContract(c.id), 'Approval recorded.')
                    }
                    onReject={(reason) =>
                        void run(c.id, () => rejectContract(c.id, reason))
                    }
                    onPromote={() =>
                        void run(c.id, () => promoteContract(c.id), 'Contract promoted.')
                    }
                    onPrint={() =>
                        void run(`pdf-${c.id}`, async () => {
                            const doc = await exportContractDocument(
                                aiSystemId,
                                c.version
                            );
                            printContractDocument(doc);
                        })
                    }
                    onDownload={() =>
                        void run(`dl-${c.id}`, async () => {
                            // The canonical ai.brutor/ai-system-contract
                            // document — closure, hash, approval trail —
                            // downloaded verbatim so the file matches what
                            // a third party can validate against the schema.
                            const doc = await exportContractDocument(
                                aiSystemId,
                                c.version
                            );
                            downloadJson(
                                `ai-system-contract-${aiSystemId}-v${c.version}.json`,
                                doc
                            );
                        })
                    }
                />
            ))}
            </div>
            )}
        </div>
    );
}

/** One entry in the stamped closure. Every collection shares this head. */
interface ClosureItem {
    id?: string;
    name?: string | null;
    inherited?: boolean;
    [k: string]: unknown;
}

/** The closure groups, in the order an approver reads them: what it may
 *  touch, what constrains it, then how much rope it has. */
const CLOSURE_GROUPS: Array<{
    title: string;
    blurb: string;
    keys: Array<[string, string]>;
}> = [
    {
        title: 'Resources it may use',
        blurb: 'Bound at mint time. Anything not listed here was not granted.',
        keys: [
            ['llm_models', 'Models'],
            ['mcp_servers', 'MCP servers'],
            ['skills', 'Agent skills'],
            ['agent_identities', 'Agent identities'],
        ],
    },
    {
        title: 'Policies in force',
        blurb: 'Guardrails and policies flow down the group chain unconditionally.',
        keys: [
            ['guardrails', 'Guardrails'],
            ['argument_policies', 'Argument policies'],
            ['cache_configs', 'Cache configs'],
        ],
    },
];

/** Each member agent's implementation terms as the
 *  contract signs them: the name its software must declare, the approved
 *  releases, and whether a run without a release is a finding. Identities
 *  that declare nothing are listed as observe-only, so an approver sees the
 *  absence as a term too. */
function ClosureAgentImplementation({ agents }: { agents: ClosureItem[] }) {
    if (agents.length === 0) return null;
    return (
        <div data-testid="closure-agent-implementation">
            <p className="text-xs font-medium text-default">Agent implementation</p>
            <p className="text-[11px] text-muted mb-1.5">
                Which software each member agent must be and which of its releases are approved. Changing these
                drifts this contract.
            </p>
            <ul className="space-y-1">
                {agents.map((a, i) => {
                    const terms = declarationTerms(declarationOf(a));
                    return (
                        <li
                            key={`${a.id ?? i}`}
                            className="text-xs text-secondary"
                            data-testid={`closure-agent-implementation-${a.id ?? i}`}
                        >
                            <span className="font-medium text-default">{itemLabel(a)}</span>
                            {' — '}
                            {terms.length ? terms.join('; ') : <span className="italic text-muted">observe only (no releases approved)</span>}
                        </li>
                    );
                })}
            </ul>
        </div>
    );
}

function itemLabel(it: ClosureItem): string {
    return (it.name as string) || (it.id as string) || 'unnamed';
}

/** One grant as the closure stores it. */
interface ClosureGrant {
    agent_id?: string;
    agent_name?: string | null;
    action_type?: string;
    target?: string;
    effect?: string;
    constraints?: Record<string, unknown> | null;
    expires_at?: string | null;
}

const SURFACE_LABEL: Record<string, string> = {
    llm: 'LLM',
    mcp: 'MCP tools',
    skill: 'Skills',
    media: 'Media',
};

/** `max_requests_per_day` → `max requests per day`. The closure keys are the
 *  column names; an approver should not have to read snake_case. */
const humanKey = (k: string) => k.replace(/_/g, ' ');

/**
 * Flatten a limit blob to `path → value` leaves.
 *
 * The blobs are nested by group — `{budget: {daily_limit_usd: 50}}` — so a
 * single-level render would print the inner object as JSON. Recursing gives
 * an approver "budget · daily limit usd  50", which is the actual term.
 */
function flattenLimits(
    blob: unknown,
    prefix = '',
    out: Array<[string, string]> = [],
): Array<[string, string]> {
    if (blob === null || blob === undefined || blob === '') return out;
    if (typeof blob !== 'object') {
        if (blob === false) return out; // an unset flag is not a term
        out.push([prefix, String(blob)]);
        return out;
    }
    if (Array.isArray(blob)) {
        if (blob.length > 0) out.push([prefix, blob.join(', ')]);
        return out;
    }
    for (const [k, v] of Object.entries(blob as Record<string, unknown>)) {
        flattenLimits(v, prefix ? `${prefix} · ${humanKey(k)}` : humanKey(k), out);
    }
    return out;
}

/**
 * Render a limit/governance blob as its actual values.
 *
 * The blob's NAME alone — `llm`, `mcp`, `media`, `skill` — tells an
 * approver that a limit exists but never what it is, and since `limits`
 * and `governance` use the identical four keys, spreading the two into one
 * list would show every chip twice. A contract page whose whole job is
 * "this is what was signed" cannot show the word `llm` and call that a
 * signed limit.
 */
function LimitValues({ blob }: { blob: unknown }) {
    const entries = flattenLimits(blob);
    if (entries.length === 0) return null;
    return (
        <span className="flex flex-wrap gap-1">
            {entries.map(([k, v]) => (
                <span
                    key={k}
                    className="px-1.5 py-0.5 rounded bg-surface-alt border border-default text-xs text-default"
                    data-testid="closure-limit-value"
                >
                    <span className="text-muted">{k}</span>{' '}
                    <span className="font-mono">{v}</span>
                </span>
            ))}
        </span>
    );
}

const EFFECT_STYLE: Record<string, string> = {
    allow: 'bg-green-50 text-green-800 border-green-300',
    approval_required: 'bg-amber-50 text-amber-800 border-amber-300',
    deny: 'bg-red-50 text-red-800 border-red-300',
};

const EFFECT_LABEL: Record<string, string> = {
    allow: 'allow',
    approval_required: 'approval',
    deny: 'deny',
};

/** A sealed list of strings as "a, b" — or null when empty/absent. */
function listText(v: unknown): string | null {
    if (!Array.isArray(v)) return null;
    const items = v.filter((x): x is string => typeof x === 'string' && x.length > 0);
    return items.length ? items.join(', ') : null;
}

/** Human labels for the sealed `ai_system` block. Order is the order an
 *  approver reads them: who owns it, what it is for, how it is classified. */
const OWNERSHIP_ROWS: Array<[string, string, (v: unknown) => string | null]> = [
    ['owner', 'Owner', (v) => (typeof v === 'string' && v ? v : null)],
    ['intended_use', 'Intended use', (v) => (typeof v === 'string' && v ? v : null)],
    ['intended_client_labels', 'Intended client labels', listText],
    ['intended_client_systems', 'Intended client systems', listText],
    ['system_kind', 'System kind', (v) => (typeof v === 'string' && v ? v : null)],
    ['eu_ai_act_role', 'EU AI Act role', (v) => (typeof v === 'string' && v ? v : null)],
    ['eu_ai_act_risk_tier', 'EU AI Act risk tier', (v) => (typeof v === 'string' && v ? v : null)],
    ['phi_resource', 'Handles PHI', (v) => (v === true ? 'yes' : null)],
    ['baseline_opt_out', 'Baseline opt-out', (v) => (v === true ? 'yes' : null)],
    [
        'run_idle_timeout_seconds',
        'Run idle timeout',
        (v) => (typeof v === 'number' && v > 0 ? `${v} s` : null),
    ],
];

/**
 * Ownership & purpose as the contract sealed it.
 *
 * The same declarations a group's General → Ownership & Purpose tab shows,
 * but from the stamped closure — so an approver reads who owned the system
 * and what it was for *at signing*, not whatever the tab says today.
 * Lifecycle stage and the estate-rollup exclusion are not contract terms
 * (the closure leaves them out on purpose) and stay on the group tab.
 */
function OwnershipBlock({
    system,
    closure,
}: {
    system: Record<string, unknown>;
    closure: Record<string, unknown>;
}) {
    const rows = OWNERSHIP_ROWS.map(([key, label, fmt]) => [label, fmt(system[key])] as const).filter(
        (r): r is readonly [string, string] => r[1] !== null,
    );
    const chain = Array.isArray(closure.inheritance_chain)
        ? (closure.inheritance_chain as string[]).length
        : null;
    return (
        <div data-testid="closure-ownership">
            <p className="text-xs font-medium text-default">Ownership &amp; purpose</p>
            <p className="text-[11px] text-muted mb-1.5">
                As declared when this version was sealed. Lifecycle stage is not a contract term.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1">
                {rows.map(([label, value]) => (
                    <div key={label} className="flex items-start gap-2 text-xs">
                        <span className="text-muted shrink-0 w-32">{label}</span>
                        <span className="text-default break-words">{value}</span>
                    </div>
                ))}
                <div className="flex items-start gap-2 text-xs" data-testid="closure-autonomy">
                    <span className="text-muted shrink-0 w-32">Autonomy</span>
                    <span className="text-default">
                        {(closure.autonomy_level as string) ?? 'not set'}
                        {chain !== null && (
                            <span className="text-muted"> · inherited through {chain} group(s)</span>
                        )}
                    </span>
                </div>
            </div>
            {rows.length === 0 && (
                <p className="text-xs text-muted italic mt-1">No ownership declarations were on file at signing.</p>
            )}
        </div>
    );
}

/**
 * What a contract version actually froze.
 *
 * The version list deliberately omits the closure, and the diff only exists
 * from v2 onwards — so without this view, an approver promoting v1 would sign
 * a hash-pinned document whose entire body is invisible unless they
 * download the JSON. Lazy-loaded on expand: closures are large and most
 * rows are never opened.
 */
function ClosureView({
    aiSystemId,
    contract,
}: {
    aiSystemId: string;
    contract: AISystemContract;
}) {
    const { fetchContractVersion } = useGateway();
    const [open, setOpen] = useState(false);
    const [closure, setClosure] = useState<Record<string, unknown> | null>(
        (contract.resolved_closure as Record<string, unknown> | undefined) ?? null,
    );
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState('');

    const toggle = async () => {
        const next = !open;
        setOpen(next);
        if (!next || closure) return;
        try {
            setLoading(true);
            setErr('');
            const full = await fetchContractVersion(aiSystemId, contract.version);
            setClosure((full.resolved_closure as Record<string, unknown>) ?? {});
        } catch (e: unknown) {
            setErr(e instanceof Error ? e.message : 'Could not load the closure');
        } finally {
            setLoading(false);
        }
    };

    const system = (closure?.ai_system ?? {}) as Record<string, unknown>;
    const composition = (closure?.composition ?? {}) as Record<string, ClosureItem[]>;
    const responsePolicies = (closure?.response_policies ?? []) as ClosureItem[];
    const limits = (closure?.limits ?? {}) as Record<string, unknown>;
    const governance = (closure?.governance ?? {}) as Record<string, unknown>;
    const grants = (closure?.agent_grants ?? []) as ClosureGrant[];
    // Conditional key: a closure has it only when the system declares dependencies.
    const dependencies = (closure?.dependencies ?? []) as ClosureDependency[];
    // `limits` and `governance` are keyed by the SAME four surfaces, so they
    // are merged per surface rather than concatenated — concatenating is what
    // produced "llm mcp media skill llm mcp media skill".
    // An empty blob is an UNDECLARED limit, not a limit of nothing — a system
    // that sets none serialises as `{llm:{}, mcp:{}, …}`, which would
    // otherwise print four rows saying nothing.
    const surfaces = Array.from(
        new Set([...Object.keys(limits), ...Object.keys(governance)]),
    ).filter(
        (s) =>
            flattenLimits(limits[s]).length > 0 ||
            flattenLimits(governance[s]).length > 0,
    );

    return (
        <div className="border-t border-default pt-3">
            <button
                type="button"
                onClick={() => void toggle()}
                className="flex items-center gap-1.5 text-xs font-medium text-primary-700 hover:underline"
                data-testid={`closure-toggle-v${contract.version}`}
                aria-expanded={open}
            >
                {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                What this version covers
            </button>

            {open && (
                <div className="mt-3 space-y-4" data-testid={`closure-v${contract.version}`}>
                    {loading && <p className="text-xs text-muted">Loading the stamped closure…</p>}
                    {err && <p className="text-xs text-red-700">{err}</p>}

                    {!loading && !err && closure && (
                        <>
                            <p className="text-[11px] text-muted">
                                Frozen at mint time and pinned by hash{' '}
                                <span className="font-mono">{contract.short_hash}</span> — this is
                                what was signed, not today&apos;s live configuration.
                            </p>

                            <OwnershipBlock system={system} closure={closure} />

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                            {CLOSURE_GROUPS.map((group) => {
                                const present = group.keys.filter(
                                    ([key]) => (composition[key] ?? []).length > 0,
                                );
                                return (
                                    <div key={group.title} data-testid={`closure-group-${group.title.split(' ')[0].toLowerCase()}`}>
                                        <p className="text-xs font-medium text-default">{group.title}</p>
                                        <p className="text-[11px] text-muted mb-1.5">{group.blurb}</p>
                                        {present.length === 0 ? (
                                            <p className="text-xs text-muted italic">None bound.</p>
                                        ) : (
                                            <div className="space-y-1.5">
                                                {present.map(([key, label]) => (
                                                    <div key={key} className="flex items-start gap-2 text-xs">
                                                        <span className="text-muted shrink-0 w-32">{label}</span>
                                                        <span className="flex flex-wrap gap-1">
                                                            {(composition[key] ?? []).map((it, i) => (
                                                                <span
                                                                    key={`${it.id ?? i}`}
                                                                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-surface-alt border border-default text-default"
                                                                    data-testid={`closure-${key}-item`}
                                                                >
                                                                    {itemLabel(it)}
                                                                    {it.inherited && (
                                                                        <span
                                                                            className="text-[10px] text-muted"
                                                                            title="Bound on an ancestor group and inherited, not set on this system"
                                                                        >
                                                                            inherited
                                                                        </span>
                                                                    )}
                                                                </span>
                                                            ))}
                                                        </span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                            </div>

                            <ClosureAgentImplementation agents={composition.agent_identities ?? []} />

                            {/* Conditional key: a closure has it only when the system declares outcomes. */}
                            <ClosureOutcomes outcomes={closure?.outcomes} />

                            <ClosureDependencies dependencies={dependencies} />

                            {responsePolicies.length > 0 && (
                                <div data-testid="closure-response-policies">
                                    <p className="text-xs font-medium text-default">Automatic responses armed</p>
                                    <span className="flex flex-wrap gap-1 mt-1">
                                        {responsePolicies.map((p, i) => (
                                            <span
                                                key={`${p.id ?? i}`}
                                                className="px-1.5 py-0.5 rounded bg-surface-alt border border-default text-xs text-default"
                                            >
                                                {itemLabel(p)}
                                            </span>
                                        ))}
                                    </span>
                                </div>
                            )}

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                            {grants.length > 0 && (
                                <div data-testid="closure-grants">
                                    <p className="text-xs font-medium text-default">What its agents may do</p>
                                    <p className="text-[11px] text-muted mb-1.5">
                                        Agent policies, per action. Anything not granted is denied.
                                    </p>
                                    <div className="space-y-1">
                                        {grants.map((g, i) => (
                                            <div
                                                key={`${g.agent_id}-${g.action_type}-${g.target}-${i}`}
                                                className="flex items-center gap-2 text-xs"
                                                data-testid="closure-grant-item"
                                            >
                                                <span
                                                    className={`px-1.5 py-0.5 rounded border font-mono shrink-0 w-20 text-center ${
                                                        EFFECT_STYLE[g.effect ?? ''] ??
                                                        'bg-surface-alt border-default text-default'
                                                    }`}
                                                >
                                                    {EFFECT_LABEL[g.effect ?? ''] ?? g.effect}
                                                </span>
                                                <span className="font-mono text-default">
                                                    {g.action_type}
                                                </span>
                                                <span className="text-muted">&rarr;</span>
                                                <span className="font-mono text-default break-all">
                                                    {g.target}
                                                </span>
                                                {g.constraints && (
                                                    <span
                                                        className="text-[10px] text-muted font-mono"
                                                        title={JSON.stringify(g.constraints)}
                                                    >
                                                        {Object.keys(g.constraints).join(' · ')}
                                                    </span>
                                                )}
                                                {g.expires_at && (
                                                    <span className="text-[10px] text-muted">
                                                        until {g.expires_at.slice(0, 10)}
                                                    </span>
                                                )}
                                                <span className="text-[10px] text-muted ml-auto shrink-0">
                                                    {g.agent_name || g.agent_id}
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            <div data-testid="closure-limits">
                                <p className="text-xs font-medium text-default">Limits &amp; governance</p>
                                <p className="text-[11px] text-muted mb-1.5">
                                    Merged restrictively down the group chain — the strictest value wins.
                                </p>
                                {surfaces.length === 0 ? (
                                    <p className="text-xs text-muted italic">None declared.</p>
                                ) : (
                                    <div className="space-y-1.5">
                                        {surfaces.map((surface) => (
                                            <div key={surface} className="flex items-start gap-2 text-xs">
                                                <span className="text-muted shrink-0 w-32">
                                                    {SURFACE_LABEL[surface] ?? surface}
                                                </span>
                                                <span className="flex flex-col gap-1">
                                                    <LimitValues blob={limits[surface]} />
                                                    <LimitValues blob={governance[surface]} />
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                            </div>
                        </>
                    )}
                </div>
            )}
        </div>
    );
}

function ContractRow({
    aiSystemId,
    contract,
    busy,
    onApprove,
    onReject,
    onPromote,
    onDownload,
    onPrint,
}: {
    aiSystemId: string;
    contract: AISystemContract;
    busy: boolean;
    onApprove: () => void;
    onReject: (reason: string) => void;
    onPromote: () => void;
    onDownload: () => void;
    onPrint: () => void;
}) {
    const [rejecting, setRejecting] = useState(false);
    const [reason, setReason] = useState('');
    const diff = contract.diff_summary;

    return (
        <div
            className="bg-surface border border-default rounded-lg p-4 space-y-3"
            data-testid={`contract-v${contract.version}`}
        >
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <p className="text-sm font-medium text-default">
                        v{contract.version}
                        <span className="ml-2 font-mono text-xs text-muted">
                            {contract.short_hash}
                        </span>
                    </p>
                    <p className="text-xs text-muted mt-1">
                        {contract.created_by ? `Minted by ${contract.created_by}` : 'Minted'}
                        {contract.approved_by ? ` · approved by ${contract.approved_by}` : ''}
                        {contract.promoted_at
                            ? ` · promoted ${new Date(contract.promoted_at).toLocaleString()}`
                            : ''}
                    </p>
                </div>
                <div className="shrink-0 flex items-center gap-2">
                    <span
                        className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                            CONTRACT_STATUS_CLASSES[contract.status] ?? 'bg-slate-100 text-slate-700'
                        }`}
                    >
                        {contract.status}
                    </span>
                    <button
                        type="button"
                        disabled={busy}
                        onClick={onDownload}
                        title="Download the canonical AI System Contract document (JSON)"
                        className="p-1.5 text-muted hover:text-default disabled:opacity-50"
                        data-testid={`download-contract-v${contract.version}`}
                    >
                        <DownloadIcon className="w-4 h-4" />
                    </button>
                    <button
                        type="button"
                        disabled={busy}
                        onClick={onPrint}
                        title="Print / save as PDF — a readable rendering of the same canonical document"
                        className="p-1.5 text-muted hover:text-default disabled:opacity-50"
                        data-testid={`print-contract-v${contract.version}`}
                    >
                        <Printer className="w-4 h-4" />
                    </button>
                </div>
            </div>

            {contract.rejection_reason && (
                <p className="text-xs text-red-700" data-testid="rejection-reason">
                    Rejected: {contract.rejection_reason}
                </p>
            )}

            {/* What the version CONTAINS, above what it CHANGED. v1 has no
                predecessor and therefore no diff, so without this an approver
                promoting the first contract sees nothing at all about what
                they are signing. */}
            <ClosureView aiSystemId={aiSystemId} contract={contract} />

            {diff && (
                <div className="space-y-2" data-testid={`diff-v${contract.version}`}>
                    {/* Widening leads. Everything else is context. */}
                    {diff.widened.length > 0 ? (
                        <div data-testid="widened">
                            <p className="text-xs font-medium text-amber-800">
                                Grants this system more than v{contract.diff_from_version}
                            </p>
                            <ul className="mt-1 space-y-0.5">
                                {diff.widened.map((e, i) => (
                                    <DiffEntryLine key={`${e.kind}-${i}`} entry={e} tone="widened" />
                                ))}
                            </ul>
                        </div>
                    ) : (
                        <p className="text-xs text-muted" data-testid="no-widening">
                            Grants nothing new.
                        </p>
                    )}

                    {diff.narrowed.length > 0 && (
                        <div data-testid="narrowed">
                            <p className="text-xs text-muted">Tightened</p>
                            <ul className="mt-1 space-y-0.5">
                                {diff.narrowed.map((e, i) => (
                                    <DiffEntryLine key={`${e.kind}-${i}`} entry={e} tone="narrowed" />
                                ))}
                            </ul>
                        </div>
                    )}

                    {diff.changed.length > 0 && (
                        <p className="text-xs text-muted" data-testid="changed-count">
                            {diff.changed.length} documentation change(s)
                        </p>
                    )}
                    {/* An explicit identity policy set or removed that moves no
                        field is still named: what the contract records changed. */}
                    {diff.changed.some((e) => e.kind === 'identity_policy') && (
                        <ul className="space-y-0.5" data-testid="changed-identity-policy">
                            {diff.changed
                                .filter((e) => e.kind === 'identity_policy')
                                .map((e, i) => (
                                    <DiffEntryLine key={`${e.kind}-${i}`} entry={e} tone="changed" />
                                ))}
                        </ul>
                    )}
                    {/* A dependency's contract that moved without widening or
                        narrowing (a re-attestation, a documentation change)
                        is still named: the approver relies on that service. */}
                    {diff.changed.some(isDependencyContractChange) && (
                        <ul className="space-y-0.5" data-testid="changed-dependencies">
                            {diff.changed.filter(isDependencyContractChange).map((e, i) => (
                                <DiffEntryLine key={`${e.kind}-${i}`} entry={e} tone="changed" />
                            ))}
                        </ul>
                    )}
                </div>
            )}

            {contract.status === 'draft' && (
                <div className="flex flex-wrap gap-2 pt-1">
                    {!contract.is_approved && (
                        <button
                            type="button"
                            disabled={busy}
                            onClick={onApprove}
                            className="px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                            data-testid="approve-button"
                        >
                            Approve
                        </button>
                    )}
                    <button
                        type="button"
                        disabled={busy}
                        onClick={onPromote}
                        className="px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                        data-testid="promote-button"
                    >
                        Promote to active
                    </button>
                    {!rejecting ? (
                        <button
                            type="button"
                            disabled={busy}
                            onClick={() => setRejecting(true)}
                            className="px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                            data-testid="reject-button"
                        >
                            Reject
                        </button>
                    ) : (
                        <div className="flex items-center gap-2">
                            <input
                                type="text"
                                value={reason}
                                onChange={(e) => setReason(e.target.value)}
                                placeholder="Why is this refused?"
                                aria-label="Rejection reason"
                                className="px-2 py-1 text-xs border border-strong rounded"
                                data-testid="reject-reason"
                            />
                            <button
                                type="button"
                                disabled={busy || !reason.trim()}
                                onClick={() => onReject(reason.trim())}
                                className="px-3 py-1.5 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                                data-testid="reject-confirm"
                            >
                                Confirm rejection
                            </button>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

export default ContractsPanel;
