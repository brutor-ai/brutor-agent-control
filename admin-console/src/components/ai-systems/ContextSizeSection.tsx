/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import type { ContextSignal } from '../../lib/aiSystemTypes';
import { SignalHeading, Tile } from './SignalParts';

const tokens = (v: number | null | undefined) => (v == null ? '—' : Math.round(v).toLocaleString('en-US'));
const times = (v: number | null | undefined) => (v == null ? '—' : `${v.toFixed(1)}×`);
const share = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);

const HELP = {
    peak:
        'The largest context any single model call in the run was sent, cached tokens included. A peak rather than the last call, because an agent that compacts shrinks before it finishes. Contexts of different agents are never added together.',
    growth:
        'The peak divided by the first context of the same agent in that run. If this rises while the task size holds steady, the agent changed, not the questions.',
    utilization:
        'The largest share of a model’s context window any call in the run used, measured per call against that call’s own model. Only runs whose model has a known window count.',
};

/** Context size, on the Trajectory sub-tab. Descriptive only: no threshold and
 *  no good-or-bad tone, because movement is judged against the system's own
 *  baseline by drift. This is what the gateway sent the model, not what an
 *  agent holds on its own side. */
export function ContextSizeSection({ context }: { context: ContextSignal }) {
    const runs = context.runs ?? 0;
    const measured = context.measured_runs ?? 0;
    const utilized = context.utilization_measured_runs ?? 0;
    return (
        <div className="mt-6" data-testid="signal-context">
            <SignalHeading
                title="Context size"
                hint="How large a context the work sent its models. Provider usage only: calls that report no usage are not measured, and context an agent keeps on its own side is not visible here."
            />
            {measured === 0 ? (
                <Tile
                    label="Peak context"
                    value="Not measured"
                    tone="unknown"
                    hint={`0 of ${runs} processed runs reported usage`}
                    help={HELP.peak}
                    testId="context-not-measured"
                />
            ) : (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                    <Tile
                        label="Median peak"
                        value={tokens(context.median_peak_tokens)}
                        hint={`${measured} of ${runs} processed runs measured`}
                        help={HELP.peak}
                        testId="context-median-peak"
                    />
                    <Tile
                        label="p95 peak"
                        value={tokens(context.p95_peak_tokens)}
                        hint={`max ${tokens(context.max_peak_tokens)}`}
                        help={HELP.peak}
                        testId="context-p95-peak"
                    />
                    <Tile
                        label="Median growth"
                        value={times(context.median_growth)}
                        hint={`p95 ${times(context.p95_growth)}`}
                        help={HELP.growth}
                        testId="context-growth"
                    />
                    {utilized > 0 ? (
                        <Tile
                            label="p95 window used"
                            value={share(context.p95_utilization)}
                            hint={`${utilized} of ${measured} measured runs have a known window`}
                            help={HELP.utilization}
                            testId="context-utilization"
                        />
                    ) : (
                        <Tile
                            label="Window used"
                            value="Unknown"
                            tone="unknown"
                            hint="No model in these runs has a known window"
                            help={HELP.utilization}
                            testId="context-utilization-unknown"
                        />
                    )}
                </div>
            )}
        </div>
    );
}
