/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Transparency.
 *
 * Left: the AI-interaction notice configuration — the tenant default and
 * one override per AI System (text per portal locale with digests,
 * surface, enabled, exemption assertion with basis and rationale). Right:
 * what the platform can say about transparency for the chosen scope over a
 * window — notice evidence as `n of m` conversations (recomputed from
 * rendered-notice records), the notice text statement, and the
 * synthetic-content declaration — each with its basis and limit. The
 * limits the CP states are printed as they come: content-level marking
 * (watermarking, C2PA) is not provided.
 */

import { useCallback, useMemo, useState } from 'react';
import { Bell } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import { errorMessage, failureSentence } from '../../../lib/narrow';
import type { EffectiveNoticeConfig, NoticeConfig, TransparencySummary } from '../../../lib/complianceEvidenceTypes';
import NoticeConfigEditor from '../NoticeConfigEditor';
import RecordDialog from '../RecordDialog';
import StatementList from '../StatementList';
import { ErrorNote, Loading, Section, WindowPicker } from '../ComplianceUi';
import { formatDay, INPUT_CLASS, windowFor, type WindowPreset } from '../complianceFormat';
import type { CompliancePageProps } from '../complianceSections';

interface PageData {
    configs: NoticeConfig[];
    configsError: string;
    effective: EffectiveNoticeConfig | null;
    effectiveError: string;
    summary: TransparencySummary | null;
    summaryError: string;
}

export default function TransparencyPage({ openObligation }: CompliancePageProps) {
    const { gatewayClient, fetchAISystems } = useGateway();
    const [scope, setScope] = useState<string>(''); // '' = tenant default
    const [preset, setPreset] = useState<WindowPreset>('30d');
    const [openRecord, setOpenRecord] = useState<string | null>(null);

    const systemsLoader = useCallback(() => fetchAISystems({ limit: 200 }), [fetchAISystems]);
    const systems = useKeyedLoad('systems', systemsLoader).data?.systems ?? [];

    // Fixed until the preset changes. Recomputing it on every render would move
    // the window (and the load key) each time the clock crosses a minute, so
    // any re-render would reload the page and drop what the operator has open.
    const window = useMemo(() => windowFor(preset), [preset]);
    const loader = useCallback(async (): Promise<PageData> => {
        const [c, e, s] = await Promise.allSettled([
            gatewayClient.listNoticeConfigs(),
            scope ? gatewayClient.getEffectiveNoticeConfig(scope) : Promise.resolve(null),
            gatewayClient.getComplianceTransparency({ ai_system_id: scope || undefined, from: window.from, to: window.to }),
        ]);
        return {
            configs: c.status === 'fulfilled' ? c.value.items : [],
            configsError: c.status === 'rejected' ? errorMessage(c.reason, 'Failed to load notice configs') : '',
            effective: e.status === 'fulfilled' ? e.value : null,
            effectiveError: e.status === 'rejected' ? failureSentence('Failed to load the notice this system resolves to', e.reason) : '',
            summary: s.status === 'fulfilled' ? s.value : null,
            summaryError: s.status === 'rejected' ? errorMessage(s.reason, 'Failed to load the transparency summary') : '',
        };
    }, [gatewayClient, scope, window.from, window.to]);
    const { data, loading, reload } = useKeyedLoad(`${scope}:${window.from}:${window.to}`, loader);

    if (loading && !data) return <Loading label="Loading notices…" testId="transparency-loading" />;
    if (!data) return null;

    const config = data.configs.find((c) => (scope ? c.ai_system_id === scope : c.ai_system_id === null)) ?? null;
    const effective = scope ? data.effective : (data.summary?.config ?? null);
    const scopeLabel = scope ? (systems.find((s) => s.id === scope)?.display_name ?? systems.find((s) => s.id === scope)?.name ?? scope) : 'tenant default';
    const summary = data.summary;
    const statements = summary
        ? [summary.notice_evidence, summary.notice_text, summary.synthetic_content].filter((s): s is NonNullable<typeof s> => !!s)
        : [];

    return (
        <div className="space-y-4" data-testid="compliance-transparency">
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <p className="text-sm text-muted max-w-3xl">
                    The portal shows the notice the effective config says, and seals a record each time it does; the
                    evidence below counts those records against conversations. Content-level marking of outputs is not
                    provided — outputs carry an X-Brutor-AI-Generated header only.
                </p>
                <div className="flex items-center gap-3 flex-wrap">
                    <label className="inline-flex items-center gap-2 text-xs">
                        <span className="text-muted uppercase tracking-wide">Scope</span>
                        <select value={scope} onChange={(e) => setScope(e.target.value)} className={INPUT_CLASS} data-testid="transparency-scope">
                            <option value="">tenant default</option>
                            {systems.map((s) => (
                                <option key={s.id} value={s.id}>
                                    {s.display_name || s.name}
                                    {data.configs.some((c) => c.ai_system_id === s.id) ? ' (override)' : ''}
                                </option>
                            ))}
                        </select>
                    </label>
                    <WindowPicker value={preset} onChange={setPreset} testId="transparency-window" />
                </div>
            </div>

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div className="rounded-lg border border-default bg-surface p-4">
                    <ErrorNote message={data.configsError} testId="transparency-configs-error" />
                    <ErrorNote message={data.effectiveError} testId="transparency-effective-error" />
                    <NoticeConfigEditor
                        key={`${scope}:${config?.updated_at ?? ''}:${config?.id ?? ''}`}
                        aiSystemId={scope || null}
                        scopeLabel={scopeLabel}
                        config={config}
                        effective={effective}
                        onSaved={reload}
                        onOpenRecord={setOpenRecord}
                    />
                </div>

                <div className="space-y-4">
                    <Section
                        title={
                            <span className="inline-flex items-center gap-1.5">
                                <Bell className="w-4 h-4 text-primary-600" /> Transparency evidence — {scopeLabel}
                            </span>
                        }
                        subtitle={summary ? `window ${formatDay(summary.window_start)} → ${formatDay(summary.window_end)}` : undefined}
                        testId="transparency-evidence"
                    >
                        <ErrorNote message={data.summaryError} testId="transparency-summary-error" />
                        {summary && (
                            <div className="space-y-3">
                                <p className="text-xs text-secondary" data-testid="transparency-flags">
                                    Interacts with natural persons:{' '}
                                    <strong className="text-default">{flag(summary.interacts_with_natural_persons)}</strong> · generates synthetic
                                    content: <strong className="text-default">{flag(summary.generates_synthetic_content)}</strong>
                                    {scope ? '' : ' (across the estate)'} — declared on the compliance profile.
                                </p>
                                <StatementList
                                    statements={statements}
                                    onOpenRecord={setOpenRecord}
                                    onOtherFramework={openObligation}
                                    testId="transparency-statements"
                                    emptyText="No transparency statement is computed for this scope."
                                />
                            </div>
                        )}
                    </Section>

                    {summary && (summary.limits ?? []).length > 0 && (
                        <Section title="Stated limits" subtitle="What this page does not show." testId="transparency-limits">
                            <ul className="list-disc pl-4 space-y-1 text-xs text-amber-800">
                                {(summary.limits ?? []).map((l) => (
                                    <li key={l}>{l}</li>
                                ))}
                            </ul>
                        </Section>
                    )}

                    <Section title="Configured scopes" subtitle="The tenant default and every per-system override." testId="transparency-configs">
                        {data.configs.length === 0 ? (
                            <p className="text-xs text-muted" data-testid="transparency-configs-empty">
                                Nothing stored yet — the portal shows its built-in notice text on both surfaces.
                            </p>
                        ) : (
                            <ul className="divide-y divide-default text-xs">
                                {data.configs.map((c) => (
                                    <li key={c.id ?? c.ai_system_id ?? 'tenant'} className="py-1.5 flex items-center gap-3 flex-wrap" data-testid={`transparency-config-${c.ai_system_id ?? 'tenant'}`}>
                                        <button type="button" className="font-medium text-primary-600 hover:underline" onClick={() => setScope(c.ai_system_id ?? '')}>
                                            {c.ai_system_id
                                                ? (systems.find((s) => s.id === c.ai_system_id)?.display_name ?? systems.find((s) => s.id === c.ai_system_id)?.name ?? c.ai_system_id)
                                                : 'tenant default'}
                                        </button>
                                        <span className="text-secondary">
                                            {c.enabled ? `shown · ${c.surface}` : 'not shown'} · {Object.keys(c.texts ?? {}).length} locale
                                            {Object.keys(c.texts ?? {}).length === 1 ? '' : 's'}
                                        </span>
                                        {c.exemption?.asserted && <span className="text-amber-800">exemption declared</span>}
                                        {c.record_id ? <span className="text-muted">sealed</span> : <span className="text-amber-800">not sealed</span>}
                                    </li>
                                ))}
                            </ul>
                        )}
                    </Section>
                </div>
            </div>

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </div>
    );
}

function flag(v: boolean | null | undefined): string {
    if (v === true) return 'yes';
    if (v === false) return 'no';
    return 'not declared';
}
