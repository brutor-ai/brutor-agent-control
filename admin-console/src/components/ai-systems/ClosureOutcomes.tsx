/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The sealed closure's `outcomes` section — the declared
 * outcomes a contract version signs: what the gateway judges reported
 * outcomes against once this version is active.
 */

import { expectSummary, readCatalogue } from '../../lib/runOutcomes';
import { BaselineMarker } from './RunOutcomeBadges';

export function ClosureOutcomes({ outcomes }: { outcomes: unknown }) {
    // The Control Plane seals the key only with outcomes in it: a closure
    // without declared outcomes has no such section.
    const catalogue = readCatalogue(outcomes);
    if (catalogue.length === 0) return null;
    return (
        <div data-testid="closure-outcomes">
            <p className="text-xs font-medium text-default">How its runs may end</p>
            <p className="text-[11px] text-muted mb-1.5">
                The declared outcomes. Once this version is active, an outcome the agent reports outside
                this list is a deviation, and only outcomes that count toward the baseline are the work.
            </p>
            <ul className="space-y-1.5">
                    {catalogue.map((o) => (
                        <li
                            key={o.id}
                            className="rounded border border-default px-2 py-1.5 text-xs"
                            data-testid="closure-outcome-item"
                        >
                            <div className="flex flex-wrap items-center gap-2">
                                <code className="font-mono text-default">{o.id}</code>
                                <span className="text-secondary">{o.label}</span>
                                <BaselineMarker counts={o.counts_toward_baseline} />
                                {expectSummary(o.expect) && (
                                    <span className="text-muted">expects {expectSummary(o.expect)}</span>
                                )}
                            </div>
                            {(o.variants ?? []).length > 0 && (
                                <ul className="mt-1 ml-3 space-y-0.5">
                                    {(o.variants ?? []).map((v) => (
                                        <li key={v.id} className="flex flex-wrap items-center gap-2 text-muted">
                                            <code className="font-mono text-default">
                                                {o.id}.{v.id}
                                            </code>
                                            <span>{v.label}</span>
                                            {expectSummary(v.expect) && <span>expects {expectSummary(v.expect)}</span>}
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </li>
                    ))}
            </ul>
        </div>
    );
}
