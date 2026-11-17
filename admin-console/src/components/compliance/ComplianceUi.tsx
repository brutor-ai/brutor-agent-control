/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Small presentational pieces every Compliance page shares: the section
 * frame, loading / error states, the basis and status chips, the seal
 * note a write response carries, a record link that opens the shared
 * RecordDialog, and the evaluation-window picker.
 *
 * Exported for the AI System Compliance tab and run details too — keep the
 * props stable.
 */

import { Loader2 } from 'lucide-react';

import { getString, isRecord } from '../../lib/narrow';

import {
    BASIS_TITLE,
    basisLabel,
    statusChip,
    statusLabel,
    WINDOW_PRESETS,
    type WindowPreset,
} from './complianceFormat';
import {
    BASIS_CHIP,
    chipFor,
    shortHash,
    VERIFY_SEVERITY_CHIP,
    type EvidenceBasis,
    type EvidenceVerifyFinding,
    type SealStatus,
} from '../../lib/complianceEvidenceTypes';

export function Section({
    title,
    subtitle,
    actions,
    testId,
    children,
}: {
    title: React.ReactNode;
    subtitle?: React.ReactNode;
    actions?: React.ReactNode;
    testId?: string;
    children: React.ReactNode;
}) {
    return (
        <section className="rounded-lg border border-default bg-surface min-w-0" data-testid={testId}>
            <div className="px-4 py-3 border-b border-default flex items-start justify-between gap-3 flex-wrap">
                <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-default">{title}</h3>
                    {subtitle && <p className="text-xs text-muted mt-0.5">{subtitle}</p>}
                </div>
                {actions && <div className="flex items-center gap-2 flex-wrap">{actions}</div>}
            </div>
            <div className="p-4 min-w-0">{children}</div>
        </section>
    );
}

export function Loading({ label = 'Loading…', testId }: { label?: string; testId?: string }) {
    return (
        <div className="flex items-center gap-2 justify-center py-10 text-muted text-sm" data-testid={testId}>
            <Loader2 className="w-4 h-4 animate-spin" /> {label}
        </div>
    );
}

export function ErrorNote({ message, testId }: { message: string; testId?: string }) {
    if (!message) return null;
    return (
        <p className="p-3 rounded-lg border border-red-300 bg-red-50 text-sm text-red-800" data-testid={testId} role="alert">
            {message}
        </p>
    );
}

/** RECOMPUTED · JUDGED · INDICATOR · DECLARED. */
export function BasisChip({ basis, testId }: { basis: string | null | undefined; testId?: string }) {
    return (
        <span
            className={`inline-flex flex-none px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wide ${chipFor(BASIS_CHIP, basis)}`}
            title={(BASIS_TITLE as Record<string, string>)[basis ?? ''] ?? undefined}
            data-testid={testId}
        >
            {basisLabel(basis)}
        </span>
    );
}

export function StatusChip({ status, testId }: { status: string | null | undefined; testId?: string }) {
    return (
        <span
            className={`inline-flex flex-none px-1.5 py-0.5 rounded text-[10px] font-semibold ${statusChip(status)}`}
            data-testid={testId}
        >
            {statusLabel(status)}
        </span>
    );
}

/** A record id that opens the shared record dialog. */
export function RecordLink({
    recordId,
    onOpen,
    label,
    testId,
}: {
    recordId: string;
    onOpen: (recordId: string) => void;
    label?: string;
    testId?: string;
}) {
    return (
        <button
            type="button"
            className="font-mono text-xs text-primary-600 hover:underline"
            title={recordId}
            onClick={() => onOpen(recordId)}
            data-testid={testId ?? `record-link-${recordId}`}
        >
            {label ?? shortHash(recordId, 16)}
        </button>
    );
}

/** What a write response's `seal` block says: the record it sealed, or
 *  that the change is saved but not sealed (and why). */
export function SealNote({
    seal,
    what = 'change',
    onOpenRecord,
    testId = 'seal-note',
}: {
    seal: SealStatus | null | undefined;
    what?: string;
    onOpenRecord?: (recordId: string) => void;
    testId?: string;
}) {
    if (!seal) return null;
    if (seal.sealed && seal.record_id) {
        return (
            <p className="text-xs text-secondary" data-testid={testId}>
                The {what} is sealed as record{' '}
                {onOpenRecord ? (
                    <RecordLink recordId={seal.record_id} onOpen={onOpenRecord} />
                ) : (
                    <code className="font-mono">{shortHash(seal.record_id, 16)}</code>
                )}
                .
            </p>
        );
    }
    return (
        <p className="text-xs text-amber-800 bg-amber-100 rounded px-2 py-1" data-testid={testId}>
            The {what} is saved but not sealed{seal.seal_error ? ` (${seal.seal_error})` : ''} — no record exists for it
            yet.{seal.seal_detail ? ` ${seal.seal_detail}` : ''}
        </p>
    );
}

export function WindowPicker({
    value,
    onChange,
    testId = 'window-picker',
}: {
    value: WindowPreset;
    onChange: (v: WindowPreset) => void;
    testId?: string;
}) {
    return (
        <label className="inline-flex items-center gap-2 text-xs">
            <span className="text-muted uppercase tracking-wide">Window</span>
            <select
                value={value}
                onChange={(e) => onChange(e.target.value as WindowPreset)}
                className="rounded-md border border-default bg-surface px-2 py-1.5 text-xs text-default"
                data-testid={testId}
                aria-label="Evaluation window"
            >
                {WINDOW_PRESETS.map((p) => (
                    <option key={p.id} value={p.id}>
                        {p.label}
                    </option>
                ))}
            </select>
        </label>
    );
}

/** The four basis chips with one line each — a legend. */
export function BasisLegend() {
    const bases: EvidenceBasis[] = ['recomputed', 'judged', 'indicator', 'declared'];
    return (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted" data-testid="basis-legend">
            {bases.map((b) => (
                <span key={b} className="inline-flex items-center gap-1.5">
                    <BasisChip basis={b} />
                    {BASIS_TITLE[b].split(' — ')[1] ?? ''}
                </span>
            ))}
        </div>
    );
}

// ─── Verifier findings ────────────────────────────────────────────────

const SEVERITY_ORDER: EvidenceVerifyFinding['severity'][] = ['error', 'warning', 'info'];

/** Findings grouped by severity. An error gates the result; warnings and
 *  infos do not — the group heading says which. Shared by the record
 *  dialog and the Verify panel so a finding reads the same in both. */
export function VerifyFindings({ findings, testIdPrefix }: { findings: EvidenceVerifyFinding[]; testIdPrefix: string }) {
    const grouped = SEVERITY_ORDER.map((severity) => ({
        severity,
        findings: findings.filter((f) => f.severity === severity),
    })).filter((g) => g.findings.length > 0);
    if (grouped.length === 0) return <p className="text-xs text-muted mt-1">No findings.</p>;
    return (
        <>
            {grouped.map((g) => (
                <div key={g.severity} className="mt-2" data-testid={`${testIdPrefix}-findings-${g.severity}`}>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${VERIFY_SEVERITY_CHIP[g.severity]}`}>
                        {g.severity} · {g.findings.length}
                        {g.severity === 'error' ? ' — gates the result' : ' — does not gate'}
                    </span>
                    <ul className="mt-1 space-y-1">
                        {g.findings.map((f, i) => (
                            <li key={`${f.check}-${f.code}-${i}`} className="text-xs text-default">
                                <code className="font-mono">
                                    {f.check}/{f.code}
                                </code>{' '}
                                {f.detail}
                            </li>
                        ))}
                    </ul>
                </div>
            ))}
        </>
    );
}

/**
 * The tier the registry EVALUATED a system at, next to the
 * tier it DECLARED. Under a composite-classification framework (the EU AI
 * Act) a contributing dependency is evaluated at its caller's tier, so the
 * two differ; the line then names the systems that lifted it. Every
 * compliance surface that shows a tier renders it through here, so none can
 * show the declared tier while the obligations were evaluated at another.
 *
 * `sources` accepts the typed `EffectiveTierSource` rows and the overview's
 * untyped ones (read through `narrow`).
 */
export function EvaluatedTier({
    declared,
    evaluated,
    sources,
    testId = 'evaluated-tier',
}: {
    declared: string | null | undefined;
    evaluated: string | null | undefined;
    sources?: readonly unknown[] | null;
    testId?: string;
}) {
    const lifted = Boolean(evaluated && evaluated !== declared);
    if (!lifted) {
        return (
            <span data-testid={testId} data-lifted="false">
                {declared ?? evaluated ?? 'classification not declared'}
            </span>
        );
    }
    const names = (sources ?? [])
        .filter(isRecord)
        .map((src) => getString(src, 'system_name') ?? getString(src, 'system_id'))
        .filter((n): n is string => Boolean(n));
    return (
        <span
            data-testid={testId}
            data-lifted="true"
            title="Evaluated at the effective tier: a system whose decision this one contributes to is classified higher (composite classification)"
        >
            evaluated <strong className="font-semibold text-default">{evaluated}</strong>
            <span className="text-muted">
                {' '}
                (declared {declared ?? 'none'}
                {names.length ? `, via ${names.join(', ')}` : ''})
            </span>
        </span>
    );
}

/** The framework note: classification follows contributing dependencies. */
export function CompositeClassificationNote({ testId = 'composite-classification-note' }: { testId?: string }) {
    return (
        <p className="text-xs text-secondary" data-testid={testId}>
            Composite classification: a system is evaluated at its <strong>effective</strong> tier — the
            higher of its declared tier and the tier of every system whose decision it contributes to — unless a
            sealed separability rationale says it does not contribute.
        </p>
    );
}
