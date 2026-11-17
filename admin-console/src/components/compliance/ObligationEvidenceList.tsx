/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The evidence statements an obligation is shown against.
 *
 * Each row is a statement, its basis chip — RECOMPUTED (the platform can
 * re-derive it from sealed records), JUDGED (a governed judge graded it),
 * INDICATOR (a signal that suggests, not proves) or DECLARED (someone said
 * so) — the resolver that produces it and the limit text that says what
 * the statement does NOT cover. Shared by the Obligations matrix and the
 * AI System Compliance panel so the two never phrase a basis differently.
 */

import type { ObligationEvidenceSpec } from '../../lib/obligationTypes';
import { BASIS_CHIP, chipFor } from '../../lib/complianceEvidenceTypes';

interface Props {
    evidence: ObligationEvidenceSpec[] | undefined;
    /** Test-id prefix so callers can pin a specific list. */
    testId?: string;
}

export default function ObligationEvidenceList({ evidence, testId = 'obligation-evidence' }: Props) {
    if (!evidence || evidence.length === 0) {
        return (
            <p className="text-xs text-muted italic" data-testid={`${testId}-empty`}>
                No evidence statements are catalogued for this obligation yet.
            </p>
        );
    }
    return (
        <ul className="space-y-1.5" data-testid={testId}>
            {evidence.map((e) => (
                <li
                    key={e.key}
                    className="rounded border border-default bg-surface-alt p-2 text-xs"
                    data-testid={`${testId}-${e.key}`}
                >
                    <div className="flex items-start gap-2">
                        <span
                            className={`flex-none px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${chipFor(BASIS_CHIP, e.basis)}`}
                            title={BASIS_TITLE[e.basis] ?? e.basis}
                        >
                            {e.basis}
                        </span>
                        <div className="min-w-0 flex-1 min-h-0">
                            <p className="text-default leading-relaxed">{e.statement}</p>
                            <p className="mt-1 text-muted">
                                resolver <code className="font-mono">{e.resolver}</code>
                            </p>
                            {e.limit && (
                                <p className="mt-1 text-amber-800" data-testid={`${testId}-${e.key}-limit`}>
                                    Limit: {e.limit}
                                </p>
                            )}
                        </div>
                    </div>
                </li>
            ))}
        </ul>
    );
}

const BASIS_TITLE: Record<string, string> = {
    recomputed: 'Recomputed from sealed records — the platform can re-derive it.',
    judged: 'Judged — a governed judge graded it against a pinned rubric.',
    indicator: 'Indicator — a signal that suggests, not a recomputation.',
    declared: 'Declared — stated by the operator in the compliance profile.',
};
