/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * "How Brutor learns" — the one-page explanation of what the Mission
 * Control cards are saying: the learning window, the health and verdict
 * vocabularies, the stripe-colour ladder, and the honesty rules that
 * decide them. This is documentation of JUDGMENT SEMANTICS, so every
 * claim here must match the actual rules in the assurance service and
 * `cardStripe` — if a rule changes, this page changes with it.
 */

import React, { useEffect } from 'react';
import { ArrowLeft, GraduationCap } from 'lucide-react';

interface HowBrutorLearnsPageProps {
    onBack: () => void;
}

const STRIPES: Array<{ color: string; label: string; means: string }> = [
    { color: 'bg-red-500', label: 'Red', means: 'Violations, or the system is suspended. Something the contract forbids happened — this outranks everything.' },
    { color: 'bg-orange-500', label: 'Orange', means: 'Drifting, stalled, or silent. Silent matters most: liveness is the only signal that fires when a system stops calling — every other metric needs traffic to exist.' },
    { color: 'bg-yellow-500', label: 'Yellow', means: 'Degraded health, or assured with exceptions — the verdict holds, but with named caveats you should read.' },
    { color: 'bg-blue-400', label: 'Blue', means: 'Learning. The behavioural baselines have not seen enough runs to vouch for the system yet. It can be "Assured" and still blue: watched, not yet proven.' },
    { color: 'bg-green-500', label: 'Green', means: 'Healthy AND assured. Both must hold — a green edge is earned twice, never averaged into existence.' },
    { color: 'bg-gray-300', label: 'Gray', means: 'No judgment exists at all — never computed, or a pseudo-card (unattributed / observed traffic). Absence of evidence is not health.' },
];

export const HowBrutorLearnsPage: React.FC<HowBrutorLearnsPageProps> = ({ onBack }) => {
    useEffect(() => {
        window.scrollTo(0, 0);
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onBack();
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onBack]);

    return (
        <div className="max-w-3xl mx-auto" data-testid="mc-learn-page">
            <button
                type="button"
                onClick={onBack}
                data-testid="mc-learn-back"
                className="flex items-center gap-2 text-sm text-secondary hover:text-default mb-4"
            >
                <ArrowLeft className="w-4 h-4" />
                Mission Control
            </button>

            <h1 className="flex items-center gap-2 text-2xl font-bold text-default mb-2">
                <GraduationCap className="w-6 h-6 text-primary-600" />
                How Brutor learns
            </h1>
            <p className="text-sm text-muted mb-8">
                What the cards are saying — the learning window, the colour
                ladder, and the honesty rules that decide every judgment.
            </p>

            <div className="space-y-8 text-sm text-secondary leading-relaxed">
                <section>
                    <h2 className="text-base font-semibold text-default mb-2">
                        Judgments are learned from runs, not configured
                    </h2>
                    <p>
                        Every governed call an AI System makes lands in its run
                        ledger. Brutor learns each system&apos;s <em>baselines</em> from
                        those runs — how many actions a task takes, what it costs,
                        which tools it touches, how runs end. Health, drift and the
                        workload class are all comparisons against those learned
                        baselines, which is why a brand-new system cannot be judged
                        on day one: there is nothing honest to compare it to yet.
                    </p>
                </section>

                <section>
                    <h2 className="text-base font-semibold text-default mb-2">
                        The learning window
                    </h2>
                    <p>
                        While a baseline is still accumulating evidence the system
                        reads <span className="px-1.5 py-0.5 rounded text-[11px] font-medium bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300">Learning</span>.
                        A learning system can still earn an
                        <span className="mx-1 px-1.5 py-0.5 rounded text-[11px] font-medium bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300">Assured</span>
                        verdict — nothing it did violated its contract — but its
                        card edge stays blue, not green: assured <em>so far</em> is
                        not the same as proven. The same rule powers the workload
                        class chip: below 20 finished runs no class is shown at
                        all, because a blank you can trust beats a number you
                        can&apos;t.
                    </p>
                </section>

                <section>
                    <h2 className="text-base font-semibold text-default mb-3">
                        The colour ladder
                    </h2>
                    <p className="mb-3">
                        The left edge of every card is the worst of its health and
                        its verdict — groups inherit the worst edge in their
                        subtree, never an average:
                    </p>
                    <div className="space-y-2">
                        {STRIPES.map((r) => (
                            <div key={r.label} className="flex items-start gap-3 bg-surface border border-default rounded-lg p-3">
                                <span className={`w-1.5 self-stretch rounded ${r.color} shrink-0`} aria-hidden="true" />
                                <div>
                                    <span className="text-xs font-semibold text-default">{r.label}</span>
                                    <p className="text-xs text-secondary mt-0.5">{r.means}</p>
                                </div>
                            </div>
                        ))}
                    </div>
                </section>

                <section>
                    <h2 className="text-base font-semibold text-default mb-2">
                        The two chips
                    </h2>
                    <p>
                        <strong>Health</strong> describes behaviour: healthy,
                        learning, degraded, drifting, stalled, silent, suspended.
                        <strong className="ml-2">The verdict</strong> describes
                        conformance to the contract: assured, assured with
                        exceptions, violations, or insufficient evidence. They are
                        deliberately separate — a system can behave normally while
                        violating its contract, and vice versa. Health, verdicts
                        and workload class are always computed on a fixed 30-day
                        window, whatever time range the dashboard shows: the
                        window pills scale <em>measurements</em> (spend, runs,
                        tokens), never <em>judgments</em>.
                    </p>
                </section>

                <section>
                    <h2 className="text-base font-semibold text-default mb-2">
                        The honesty rules, in one place
                    </h2>
                    <ul className="list-disc pl-5 space-y-1.5">
                        <li>Absence of evidence is never green — no data reads as unknown, not healthy.</li>
                        <li>Groups show the worst state inside them, never an average: nine healthy systems and one in violation reads &quot;Violations&quot;.</li>
                        <li>A judgment you cannot stand behind is not shown — below the evidence floor the card shows a blank, not a guess.</li>
                        <li>A silent system is a finding, not a quiet dashboard — liveness fires precisely when everything else has nothing to measure.</li>
                        <li>Judgments never follow the window selector — there is no time range in which a struggling system looks fine.</li>
                    </ul>
                </section>
            </div>
        </div>
    );
};

export default HowBrutorLearnsPage;
