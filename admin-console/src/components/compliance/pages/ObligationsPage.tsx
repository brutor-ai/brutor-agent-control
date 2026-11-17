/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Obligations.
 *
 * One load — `GET /compliance/obligations?include_matrix=true` — feeds every
 * view of the selected framework over the chosen window; picking a system
 * filters what was loaded, it does not ask again:
 *
 *   * ONE AI SYSTEM (the selector): its obligations grouped in force /
 *     upcoming / not applicable, each with the worst statement status and
 *     `n of m` per basis; a row opens its evaluated evidence in place;
 *   * ALL SYSTEMS, BOARD (the default): one dated row per obligation
 *     with the systems in scope and the catalogued statements;
 *   * ALL SYSTEMS, MATRIX: system × obligation, the cross-system comparison;
 *     a cell or a column opens the drill-in.
 *
 * The drill-in lists the evaluated statements with their basis chip,
 * status, `n of m`, limit, catalog version and citations; judged rows carry
 * their human agreement; "show this evidence under other frameworks"
 * switches the console's framework and opens that obligation (the
 * `focusObligation` prop is how the console hands it back).
 */

import { useCallback, useMemo, useState } from 'react';
import { LayoutGrid, Library, List } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import type { ComplianceObligationsResponse } from '../../../lib/complianceEvidenceTypes';
import ObligationBoard from '../ObligationBoard';
import ObligationDrillIn from '../ObligationDrillIn';
import ObligationMatrix, { type MatrixSelection } from '../ObligationMatrix';
import RecordDialog from '../RecordDialog';
import SystemObligations from '../SystemObligations';
import { BasisLegend, ErrorNote, Loading, WindowPicker } from '../ComplianceUi';
import { formatDay, INPUT_CLASS, NOT_LEGAL_ADVICE, windowFor, type WindowPreset } from '../complianceFormat';
import type { CompliancePageProps } from '../complianceSections';

type View = 'board' | 'matrix';

export default function ObligationsPage({
    framework,
    frameworks,
    setActiveTab,
    onTabChange,
    focusObligation,
    openObligation,
}: CompliancePageProps) {
    const { gatewayClient } = useGateway();
    const [view, setView] = useState<View>('board');
    // `null` is every system; an id that is not in the loaded matrix (after a
    // framework switch) reads as every system too.
    const [systemId, setSystemId] = useState<string | null>(null);
    const [preset, setPreset] = useState<WindowPreset>('30d');
    const [selection, setSelection] = useState<MatrixSelection | null>(
        focusObligation ? { obligationKey: focusObligation, aiSystemId: null } : null,
    );
    const [appliedFocus, setAppliedFocus] = useState<string | null>(focusObligation ?? null);
    const [openRecord, setOpenRecord] = useState<string | null>(null);

    // A new cross-framework focus arriving while mounted opens that
    // obligation (derived from the prop, applied once per value).
    if (focusObligation && focusObligation !== appliedFocus) {
        setAppliedFocus(focusObligation);
        setSelection({ obligationKey: focusObligation, aiSystemId: null });
    }

    const registryEntry = frameworks.find((f) => f.id === framework);
    const hasCatalog = (registryEntry?.obligation_count ?? 0) > 0;
    // Fixed until the preset changes. Recomputing it on every render would move
    // the window (and the load key) each time the clock crosses a minute, so
    // any re-render would reload the page and drop what the operator has open.
    const window = useMemo(() => windowFor(preset), [preset]);

    const loader = useCallback(
        (): Promise<ComplianceObligationsResponse | null> =>
            hasCatalog
                ? gatewayClient.getComplianceObligations({ framework, include_matrix: true, from: window.from, to: window.to })
                : Promise.resolve(null),
        [gatewayClient, framework, hasCatalog, window.from, window.to],
    );
    const { data: board, error, loading } = useKeyedLoad(
        `${framework}:${hasCatalog}:${window.from}:${window.to}`,
        loader,
        'Failed to compute obligations',
    );

    if (!hasCatalog) {
        return (
            <div className="rounded-lg border border-default bg-surface p-8 text-center" data-testid="obligations-no-catalog">
                <Library className="w-8 h-8 text-muted mx-auto mb-3" />
                <h3 className="text-sm font-semibold text-default">{registryEntry?.title ?? framework} has no obligation catalog yet</h3>
                <p className="text-xs text-secondary mt-1 max-w-md mx-auto">
                    The registry knows this framework
                    {registryEntry ? ` (${registryEntry.reference}, catalog ${registryEntry.catalog_version})` : ''}, but its
                    obligations and evidence statements are not available yet. Nothing is being computed for it,
                    and nothing on this page pretends otherwise.
                </p>
                <button type="button" className="mt-3 text-xs text-primary-600 hover:underline" onClick={() => onTabChange('compliance-frameworks')}>
                    Open the registry →
                </button>
            </div>
        );
    }

    if (loading && !board) return <Loading label="Computing obligations…" testId="obligations-loading" />;
    if (error) return <ErrorNote message={error} testId="obligations-error" />;
    if (!board) return null;

    const matrix = board.matrix ?? null;
    const system = matrix?.systems.find((x) => x.ai_system_id === systemId) ?? null;
    const openSystem = setActiveTab ? (id: string) => setActiveTab(`ai-systems:${id}:compliance`) : undefined;
    const selectedObligation = selection ? board.obligations.find((o) => o.key === selection.obligationKey) : null;
    // On the one-system view and the board the evidence opens under its row;
    // the matrix, and an obligation that has no row (a cross-framework
    // alias), show it at the foot.
    const inline = !!selectedObligation && (!!system || view === 'board');

    const drillIn = selection && (
        <ObligationDrillIn
            key={`${framework}:${selection.obligationKey}:${system?.ai_system_id ?? selection.aiSystemId ?? '*'}`}
            framework={framework}
            obligationKey={selection.obligationKey}
            aiSystemId={system?.ai_system_id ?? selection.aiSystemId}
            window={window}
            onClose={() => setSelection(null)}
            onOpenRecord={setOpenRecord}
            onOtherFramework={openObligation}
            onOpenSystem={openSystem}
        />
    );

    return (
        <div className="space-y-4" data-testid="compliance-obligations">
            <div className="rounded-lg border border-default bg-surface px-4 py-3 flex items-center justify-between gap-3 flex-wrap">
                <label className="inline-flex items-center gap-2 text-xs">
                    <span className="text-muted uppercase tracking-wide">AI System</span>
                    <select
                        value={system?.ai_system_id ?? ''}
                        onChange={(e) => {
                            setSystemId(e.target.value || null);
                            setSelection(null);
                        }}
                        className={`${INPUT_CLASS} min-w-[16rem]`}
                        data-testid="obligations-system"
                        aria-label="AI System"
                    >
                        <option value="">All systems ({board.system_count})</option>
                        {(matrix?.systems ?? []).map((x) => (
                            <option key={x.ai_system_id} value={x.ai_system_id}>
                                {x.name}
                            </option>
                        ))}
                    </select>
                </label>
                <div className="flex items-center gap-3 flex-wrap">
                    <WindowPicker value={preset} onChange={setPreset} testId="obligations-window" />
                    {!system && (
                        <div className="inline-flex rounded-md border border-default overflow-hidden" role="tablist" aria-label="View">
                            <ViewButton current={view} value="board" icon={List} label="Board" onClick={setView} />
                            <ViewButton current={view} value="matrix" icon={LayoutGrid} label="Matrix" onClick={setView} />
                        </div>
                    )}
                </div>
            </div>

            <p className="text-xs text-muted">
                {board.framework.reference} · evaluated as of {board.as_of}
                {matrix ? ` · window ${formatDay(matrix.window.from)} → ${formatDay(matrix.window.to)}` : ''}. Applicability derives
                from each system{'’'}s declared facts — dated, never a checkbox. Undetermined is a to-do: declare the missing
                fact and the row recomputes. {NOT_LEGAL_ADVICE}
            </p>

            {matrix && matrix.catalog_versions && matrix.catalog_versions.filter(Boolean).length > 1 && (
                <p className="text-xs text-amber-800 bg-amber-100 rounded px-2 py-1" data-testid="obligations-version-span">
                    The window spans catalog versions {matrix.catalog_versions.filter(Boolean).join(', ')} — each statement names the
                    version it was evaluated under.
                </p>
            )}

            {system ? (
                <>
                    <SystemObligations
                        system={system}
                        obligations={board.obligations}
                        selectedKey={selection?.obligationKey ?? null}
                        onSelect={(key) => setSelection(key ? { obligationKey: key, aiSystemId: system.ai_system_id } : null)}
                        detail={inline ? drillIn : undefined}
                        onOpenSystem={openSystem}
                        onEditProfile={() => onTabChange('compliance-profiles')}
                    />
                    <BasisLegend />
                </>
            ) : view === 'matrix' ? (
                matrix ? (
                    <>
                        <BasisLegend />
                        <ObligationMatrix matrix={matrix} obligations={board.obligations} selection={selection} onSelect={setSelection} />
                    </>
                ) : (
                    <p className="text-xs text-muted" data-testid="matrix-missing">
                        The matrix was not computed for this framework.
                    </p>
                )
            ) : (
                <ObligationBoard
                    obligations={board.obligations}
                    onOpenSystem={openSystem}
                    onOpenEvidence={(key) => setSelection({ obligationKey: key, aiSystemId: null })}
                    selectedKey={selection?.obligationKey ?? null}
                    detail={inline ? drillIn : undefined}
                />
            )}

            {!inline && drillIn}
            {selection && !selectedObligation && (
                <p className="text-xs text-muted" data-testid="obligations-focus-note">
                    {selection.obligationKey} is not on this framework{'’'}s board in the window; the evidence above resolves it
                    through the catalog{'’'}s aliases where it can.
                </p>
            )}

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </div>
    );
}

function ViewButton({
    current,
    value,
    icon: Icon,
    label,
    onClick,
}: {
    current: View;
    value: View;
    icon: React.FC<{ className?: string }>;
    label: string;
    onClick: (v: View) => void;
}) {
    const active = current === value;
    return (
        <button
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onClick(value)}
            className={`inline-flex items-center gap-1 px-2.5 py-1.5 text-xs ${active ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/20' : 'text-secondary hover:bg-surface-hover'}`}
            data-testid={`obligations-view-${value}`}
        >
            <Icon className="w-3.5 h-3.5" /> {label}
        </button>
    );
}
