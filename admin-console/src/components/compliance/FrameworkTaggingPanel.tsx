/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Frameworks: request-log tagging for the selected framework. What the core stamps on request-log rows while the framework is
 * enabled, how many rows carry each tag in the window, and — where the
 * evidence model is a per-call list — the auditor CSV export.
 *
 * The enable switch is the registry table's; this panel never declares
 * anything, it reports what the switch produced.
 */

import { useState } from 'react';
import { Download } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import { downloadBlob } from '../../lib/csvExport';
import { errorMessage } from '../../lib/narrow';
import { TABLE_TBODY } from '../ui/tableStyles';
import { ErrorNote, Loading, Section, WindowPicker } from './ComplianceUi';
import { BUTTON_SECONDARY, type WindowPreset } from './complianceFormat';
import {
    FRAMEWORK_TAGGING_GUIDE,
    NO_CSV_EXPORT_NOTE,
    TAGGING_SUBTITLE,
    type TaggingCard,
} from './frameworkTagging';

interface Props {
    title: string;
    tagKey: string | null | undefined;
    enabled: boolean;
    card: TaggingCard | undefined;
    loading: boolean;
    error: string;
    preset: WindowPreset;
    onPreset: (p: WindowPreset) => void;
    window: { from: string; to: string };
}

export default function FrameworkTaggingPanel({
    title, tagKey, enabled, card, loading, error, preset, onPreset, window,
}: Props) {
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const [exporting, setExporting] = useState(false);
    const guide = tagKey ? FRAMEWORK_TAGGING_GUIDE[tagKey] : undefined;
    const total = card?.total_tagged_rows ?? 0;

    const exportCsv = async () => {
        if (!guide?.csvExportRegime || !tagKey) return;
        setExporting(true);
        try {
            const blob = await gatewayClient.downloadComplianceExportCsv(guide.csvExportRegime, window);
            downloadBlob(`${tagKey}-${window.from.slice(0, 10)}-${window.to.slice(0, 10)}.csv`, blob);
        } catch (e: unknown) {
            toast.error(`Export failed: ${errorMessage(e)}`);
        } finally {
            setExporting(false);
        }
    };

    return (
        <Section
            title={`${title} — request-log tagging`}
            subtitle={TAGGING_SUBTITLE}
            actions={tagKey ? <WindowPicker value={preset} onChange={onPreset} testId="tagging-window" /> : undefined}
            testId="framework-tagging"
        >
            {!tagKey ? (
                <p className="text-xs text-muted" data-testid="framework-tagging-none">
                    This framework does not tag request-log rows. Its evidence is the obligations and records on the
                    other Compliance pages.
                </p>
            ) : (
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 text-xs">
                    <div className="space-y-3" data-testid="framework-tagging-counts">
                        {error ? (
                            <ErrorNote message={error} testId="framework-tagging-error" />
                        ) : loading && !card ? (
                            <Loading />
                        ) : (
                            <>
                                <div>
                                    <div className="text-2xl font-semibold text-default" data-testid="framework-tagging-total">
                                        {total.toLocaleString()}
                                    </div>
                                    <div className="text-muted">tagged request-log rows in the window</div>
                                </div>
                                {!enabled && (
                                    <p className="text-amber-800 bg-amber-100 rounded px-2 py-1" data-testid="framework-tagging-off">
                                        {total > 0
                                            ? 'The framework is off: these rows were tagged while it was on, and no new rows are being tagged.'
                                            : 'The framework is off: no rows are being tagged.'}
                                    </p>
                                )}
                                <table className="w-full">
                                    <tbody className={TABLE_TBODY}>
                                        {Object.entries(card?.by_tag ?? {}).map(([tag, count]) => (
                                            <tr key={tag}>
                                                <td className="py-1 font-mono text-secondary">{tag}</td>
                                                <td className="py-1 text-right text-default">{count.toLocaleString()}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </>
                        )}
                        {guide?.csvExportRegime ? (
                            <div className="space-y-1">
                                <button
                                    type="button"
                                    className={BUTTON_SECONDARY}
                                    onClick={() => void exportCsv()}
                                    disabled={exporting || total === 0}
                                    title={total === 0 ? 'No tagged rows in the selected window' : guide.csvExportNote}
                                    data-testid="framework-tagging-export"
                                >
                                    <Download className="w-3.5 h-3.5" />
                                    Export for auditor (CSV)
                                </button>
                                <p className="text-muted">{guide.csvExportNote}</p>
                            </div>
                        ) : (
                            guide && <p className="text-muted" data-testid="framework-tagging-no-export">{NO_CSV_EXPORT_NOTE}</p>
                        )}
                    </div>
                    {guide && (
                        <dl className="lg:col-span-2 space-y-3" data-testid="framework-tagging-guide">
                            {(
                                [
                                    ['What it is', guide.whatItIs],
                                    ['Who needs it', guide.whoNeedsIt],
                                    ['What gets tagged', guide.whatGetsTagged],
                                    ['Audit evidence model', guide.auditEvidence],
                                    ['Per resource group', guide.perGroupField],
                                ] as const
                            )
                                .filter(([, text]) => !!text)
                                .map(([label, text]) => (
                                    <div key={label}>
                                        <dt className="text-muted uppercase tracking-wide">{label}</dt>
                                        <dd className="mt-1 text-secondary leading-relaxed">{text}</dd>
                                    </div>
                                ))}
                        </dl>
                    )}
                </div>
            )}
        </Section>
    );
}
