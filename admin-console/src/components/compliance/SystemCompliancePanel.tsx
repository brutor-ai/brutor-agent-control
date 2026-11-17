/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI System detail → Compliance: this system's declared
 * profile, its obligations under the selected framework with the evidence
 * statements each is shown against, and how many sealed records the
 * ledger holds for it.
 */

import { useCallback, useState } from 'react';
import { ChevronDown, ChevronRight, Loader2, ScrollText } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { failureSaying } from '../../hooks/useRead';
import { errorMessage, failureSentence, getString } from '../../lib/narrow';
import {
    BASIS_CHIP,
    chipFor,
    OPERATOR_ROLE_LABEL,
    type AISystemComplianceProfile,
    type ComplianceFramework,
} from '../../lib/complianceEvidenceTypes';
import {
    EU_AI_ACT_ROLE_LABEL,
    splitObligations,
    type ObligationResult,
    type SystemObligationsResponse,
} from '../../lib/obligationTypes';
import { DEFAULT_FRAMEWORK, readStoredFramework, storeFramework } from './complianceSections';
import { EvaluatedTier } from './ComplianceUi';
import ObligationEvidenceList from './ObligationEvidenceList';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface Props {
    aiSystemId: string;
    setActiveTab?: (tab: string) => void;
}

interface PanelData {
    frameworks: ComplianceFramework[];
    /** The sentence of the failed read of the frameworks, or ''. */
    frameworksFailure: string;
    hasCatalog: boolean;
    profile: AISystemComplianceProfile | null;
    profileError: string;
    obligations: SystemObligationsResponse | null;
    recordCount: number | null;
    /** Why the obligations, or the count of records, could not be loaded; '' where they were. */
    obligationsFailure: string;
    recordCountFailure: string;
}

const STATUS_CHIP: Record<string, string> = {
    applies: 'bg-green-100 text-green-800',
    undetermined: 'bg-amber-100 text-amber-800',
    not_applicable: 'bg-surface-raised text-secondary',
};

export default function SystemCompliancePanel({ aiSystemId, setActiveTab }: Props) {
    const { gatewayClient, fetchSystemObligations } = useGateway();
    const [framework, setFramework] = useState<string>(() => readStoredFramework() ?? DEFAULT_FRAMEWORK);
    const [open, setOpen] = useState<Set<string>>(new Set());

    // One load per (system, framework). The registry comes first because it
    // says whether the framework has a catalog to compute obligations from;
    // the three sources after it fail independently — a missing profile is
    // the panel's error, the other two degrade to "—".
    const loader = useCallback(async (): Promise<PanelData> => {
        const offered = await gatewayClient
            .listComplianceFrameworks()
            // Offered frameworks only (verified against their published text).
            .then((r) => ({ items: r.items.filter((f) => f.offered), failure: '' }))
            .catch((error: unknown) => ({
                items: [] as ComplianceFramework[],
                failure: failureSentence('Failed to load the compliance frameworks; obligations cannot be computed', error),
            }));
        const frameworks = offered.items;
        const hasCatalog = (frameworks.find((f) => f.id === framework)?.obligation_count ?? 0) > 0;
        const [p, o, r] = await Promise.allSettled([
            gatewayClient.getAISystemComplianceProfile(aiSystemId),
            hasCatalog ? fetchSystemObligations(aiSystemId, framework) : Promise.resolve(null),
            gatewayClient.listEvidenceRecords({ ai_system_id: aiSystemId, limit: 1 }),
        ]);
        return {
            frameworks,
            frameworksFailure: offered.failure,
            hasCatalog,
            profile: p.status === 'fulfilled' ? p.value : null,
            profileError: p.status === 'rejected' ? errorMessage(p.reason, 'Failed to load the compliance profile') : '',
            obligations: o.status === 'fulfilled' ? o.value : null,
            obligationsFailure: o.status === 'rejected' ? failureSentence('Failed to load the obligations', o.reason) : '',
            recordCount: r.status === 'fulfilled' ? r.value.total : null,
            recordCountFailure: r.status === 'rejected' ? failureSentence('Failed to count the evidence records', r.reason) : '',
        };
    }, [gatewayClient, fetchSystemObligations, aiSystemId, framework]);
    const { data, loading, reload } = useKeyedLoad(`${aiSystemId}:${framework}`, loader);
    const frameworks = data?.frameworks ?? [];
    const registryEntry = frameworks.find((f) => f.id === framework);
    const hasCatalog = data?.hasCatalog ?? false;
    const profile = data?.profile ?? null;
    const error = data?.profileError ?? '';
    const obligations = data?.obligations ?? null;
    const recordCount = data?.recordCount ?? null;

    const chooseFramework = (id: string) => {
        setFramework(id);
        storeFramework(id);
    };

    if (loading && !data) {
        return (
            <div className="flex items-center gap-2 justify-center py-12 text-muted text-sm" data-testid="system-compliance-loading">
                <Loader2 className="w-4 h-4 animate-spin" /> Loading compliance…
            </div>
        );
    }

    const core = profile?.core;
    const block = profile?.frameworks ? (profile.frameworks as Record<string, unknown>)[framework] : undefined;
    const operatorRole = getString(core, 'operator_role');
    const euRole = getString(block, 'role');
    const euTier = getString(block, 'risk_tier');
    const { inForce, upcoming } = obligations ? splitObligations(obligations.obligations) : { inForce: [], upcoming: [] };

    const renderObligation = (o: ObligationResult) => {
        const expanded = open.has(o.key);
        return (
            <div key={o.key} className="border border-default rounded-lg bg-surface" data-testid={`system-obligation-${o.key}`}>
                <button
                    type="button"
                    className="w-full flex items-center gap-3 p-2.5 text-left"
                    aria-expanded={expanded}
                    onClick={() =>
                        setOpen((prev) => {
                            const next = new Set(prev);
                            if (next.has(o.key)) next.delete(o.key);
                            else next.add(o.key);
                            return next;
                        })
                    }
                >
                    {expanded ? <ChevronDown className="w-4 h-4 text-muted flex-none" /> : <ChevronRight className="w-4 h-4 text-muted flex-none" />}
                    <div className="min-w-0 flex-1 min-h-0">
                        <span className="text-sm font-medium text-default">{o.title}</span>{' '}
                        <span className="text-xs text-muted">{o.reference}</span>
                    </div>
                    <span className="text-xs text-secondary flex-none">{o.in_force ? 'in force' : `from ${o.applies_from}`}</span>
                    <span
                        className={`flex-none px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${STATUS_CHIP[o.status] ?? STATUS_CHIP.not_applicable}`}
                        title={o.reason}
                        data-testid={`system-obligation-status-${o.key}`}
                    >
                        {o.status.replace('_', ' ')}
                    </span>
                </button>
                {expanded && (
                    <div className="border-t border-default p-3 space-y-2">
                        <p className="text-xs text-secondary">{o.summary}</p>
                        <p className="text-xs text-muted">{o.reason}</p>
                        <ObligationEvidenceList evidence={o.evidence} testId={`system-obligation-evidence-${o.key}`} />
                    </div>
                )}
            </div>
        );
    };

    return (
        <div className="space-y-4" data-testid="system-compliance-panel">
            <ReadFailureNotice failure={failureSaying(data?.frameworksFailure)} onRetry={reload} />
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <label className="flex items-center gap-2 text-sm">
                    <span className="text-xs text-muted uppercase tracking-wide">Framework</span>
                    <select
                        value={framework}
                        onChange={(e) => chooseFramework(e.target.value)}
                        className="rounded-md border border-default bg-surface px-2 py-1 text-sm"
                        data-testid="system-compliance-framework"
                    >
                        {frameworks.length === 0 && <option value={framework}>{framework}</option>}
                        {frameworks.map((f) => (
                            <option key={f.id} value={f.id}>
                                {f.title}
                            </option>
                        ))}
                    </select>
                </label>
                {setActiveTab && (
                    <button
                        type="button"
                        className="text-xs text-primary-600 hover:underline"
                        onClick={() => setActiveTab('compliance-profiles')}
                        data-testid="system-compliance-edit"
                    >
                        Edit profile in Compliance → Profiles →
                    </button>
                )}
            </div>

            {error && (
                <p className="p-3 rounded-lg border border-red-300 bg-red-50 text-sm text-red-800" data-testid="system-compliance-error">
                    {error}
                </p>
            )}

            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="rounded-lg border border-default bg-surface p-3" data-testid="system-compliance-profile">
                    <h4 className="text-xs font-semibold text-muted uppercase tracking-wide mb-1">Profile</h4>
                    {profile?.exists ? (
                        <dl className="text-xs space-y-0.5">
                            <div className="flex justify-between gap-2">
                                <dt className="text-muted">Operator role</dt>
                                <dd className="text-default">{operatorRole ? OPERATOR_ROLE_LABEL[operatorRole as keyof typeof OPERATOR_ROLE_LABEL] ?? operatorRole : 'not declared'}</dd>
                            </div>
                            {framework === 'eu-ai-act' && (
                                <>
                                    <div className="flex justify-between gap-2">
                                        <dt className="text-muted">Regulated role</dt>
                                        <dd className="text-default">{euRole ? EU_AI_ACT_ROLE_LABEL[euRole] ?? euRole : 'not declared'}</dd>
                                    </div>
                                    <div className="flex justify-between gap-2">
                                        <dt className="text-muted">Risk tier</dt>
                                        <dd className="text-default text-right">
                                            <EvaluatedTier
                                                declared={euTier ?? obligations?.eu_ai_act_risk_tier ?? null}
                                                evaluated={obligations?.risk_tier_evaluated}
                                                sources={obligations?.effective_tier_sources}
                                                testId="system-compliance-tier"
                                            />
                                        </dd>
                                    </div>
                                </>
                            )}
                            {profile.declaration_record_id && (
                                <div className="flex justify-between gap-2">
                                    <dt className="text-muted">Sealed declaration</dt>
                                    <dd className="text-default font-mono truncate" title={profile.declaration_record_id}>
                                        {profile.declaration_record_id}
                                    </dd>
                                </div>
                            )}
                        </dl>
                    ) : (
                        <p className="text-xs text-amber-800" data-testid="system-compliance-undeclared">
                            Nothing declared yet
                            {euTier || euRole ? ` — the EU AI Act block is prefilled from the group (${euTier ?? 'no tier'}, ${euRole ? EU_AI_ACT_ROLE_LABEL[euRole] ?? euRole : 'no role'})` : ''}.
                        </p>
                    )}
                </div>
                <div className="rounded-lg border border-default bg-surface p-3" data-testid="system-compliance-obligations-summary">
                    <h4 className="text-xs font-semibold text-muted uppercase tracking-wide mb-1">Obligations</h4>
                    {hasCatalog && obligations ? (
                        <p className="text-sm text-default">
                            {inForce.length} in force · {upcoming.length} upcoming
                            <span className="block text-xs text-muted">as of {obligations.as_of}</span>
                            {obligations.risk_tier_evaluated &&
                                obligations.risk_tier_evaluated !== obligations.eu_ai_act_risk_tier && (
                                    <span
                                        className="block text-xs text-secondary mt-1"
                                        data-testid="system-compliance-evaluated-at"
                                    >
                                        Evaluated at the effective tier <strong>{obligations.risk_tier_evaluated}</strong>
                                        , not the declared {obligations.eu_ai_act_risk_tier ?? 'tier'}.
                                    </span>
                                )}
                        </p>
                    ) : data?.obligationsFailure ? (
                        <ReadFailureNotice compact failure={failureSaying(data.obligationsFailure)} onRetry={reload} />
                    ) : (
                        <p className="text-xs text-muted">
                            {registryEntry?.title ?? framework} has no obligation catalog yet.
                        </p>
                    )}
                </div>
                <div className="rounded-lg border border-default bg-surface p-3" data-testid="system-compliance-records">
                    <h4 className="text-xs font-semibold text-muted uppercase tracking-wide mb-1">Sealed records</h4>
                    <p className="text-sm text-default flex items-center gap-1.5">
                        <ScrollText className="w-4 h-4 text-primary-600" />
                        {recordCount === null ? '—' : recordCount.toLocaleString()}
                    </p>
                    <ReadFailureNotice compact failure={failureSaying(data?.recordCountFailure)} onRetry={reload} />
                    {setActiveTab && (
                        <button
                            type="button"
                            className="text-xs text-primary-600 hover:underline mt-1"
                            onClick={() => setActiveTab('compliance-evidence')}
                        >
                            Open the ledger →
                        </button>
                    )}
                </div>
            </div>

            {hasCatalog && obligations && (
                <div className="space-y-3">
                    <div>
                        <h4 className="text-xs font-semibold text-muted uppercase tracking-wide mb-1">In force ({inForce.length})</h4>
                        <div className="space-y-1.5">{inForce.map(renderObligation)}</div>
                    </div>
                    <div>
                        <h4 className="text-xs font-semibold text-muted uppercase tracking-wide mb-1">Upcoming ({upcoming.length})</h4>
                        <div className="space-y-1.5">{upcoming.map(renderObligation)}</div>
                    </div>
                </div>
            )}

            <p className="text-[10px] text-muted">
                Basis chips:{' '}
                {(['recomputed', 'judged', 'indicator', 'declared'] as const).map((b) => (
                    <span key={b} className={`ml-1 px-1 py-0.5 rounded uppercase font-semibold ${chipFor(BASIS_CHIP, b)}`}>
                        {b}
                    </span>
                ))}
            </p>
        </div>
    );
}
