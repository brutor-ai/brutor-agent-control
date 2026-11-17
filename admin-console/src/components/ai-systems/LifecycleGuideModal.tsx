/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The AI System Lifecycle Guide — the one-stop explanation an operator
 * reads before working this page. Every claim here is backed by shipped
 * machinery: the lifecycle gate, contracts + conformance,
 * replay, and the assurance rules. Keep the copy honest — this guide
 * describes what the platform enforces, not what a brochure promises.
 */

import { useEffect } from 'react';
import { BookOpen, X } from 'lucide-react';

interface LifecycleGuideModalProps {
    onClose: () => void;
}

const STAGES: { stage: string; asserts: string; color: string }[] = [
    { stage: 'Proposed', asserts: 'The system exists on paper. Declarations (owner, intended use, risk tier) are being filled in; nothing is asserted about readiness.', color: 'bg-slate-100 text-slate-700' },
    { stage: 'Approved', asserts: 'Somebody accountable has signed off the declared design. The gate checks the governance declarations are complete before allowing this move.', color: 'bg-blue-100 text-blue-800' },
    { stage: 'Active', asserts: 'The system is serving real traffic under a promoted contract. Assurance holds it to that contract from here on.', color: 'bg-emerald-100 text-emerald-800' },
    { stage: 'Deprecated', asserts: 'Still running, but on the way out — new dependents should not be added.', color: 'bg-amber-100 text-amber-800' },
    { stage: 'Retired', asserts: 'No longer expected to run. A retired system that still produces runs is a finding, not noise.', color: 'bg-red-100 text-red-800' },
];

export function LifecycleGuideModal({ onClose }: LifecycleGuideModalProps) {
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onClose]);

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
            onClick={onClose}
            data-testid="lifecycle-guide-modal"
        >
            <div
                className="bg-surface rounded-xl shadow-xl max-w-3xl w-full max-h-[85vh] flex flex-col"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div className="flex items-center gap-2">
                        <BookOpen className="w-5 h-5 text-primary-600" />
                        <h2 className="text-lg font-semibold text-default">AI System Lifecycle Guide</h2>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close guide"
                        className="p-1 rounded hover:bg-surface-raised text-muted"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                <div className="overflow-y-auto px-6 py-5 space-y-6 text-sm text-secondary leading-relaxed">
                    <section>
                        <h3 className="text-sm font-semibold text-default mb-2">What an AI System is here</h3>
                        <p>
                            An AI System is the unit you govern and assure: the agent, assistant or
                            integration as a whole, with the AI models, MCP servers, skills and agent
                            identities it uses <em>bound</em> to it. Those bindings are its runtime
                            access control — what it may call — and every run it produces is
                            attributed back to it, which is what makes the tabs on this page possible.
                        </p>
                    </section>

                    <section>
                        <h3 className="text-sm font-semibold text-default mb-2">The stages, and what each one asserts</h3>
                        <div className="space-y-2">
                            {STAGES.map((s) => (
                                <div key={s.stage} className="flex items-start gap-3">
                                    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium shrink-0 mt-0.5 w-24 justify-center ${s.color}`}>
                                        {s.stage}
                                    </span>
                                    <p className="text-sm">{s.asserts}</p>
                                </div>
                            ))}
                        </div>
                    </section>

                    <section>
                        <h3 className="text-sm font-semibold text-default mb-2">Moving between stages is gated</h3>
                        <p>
                            A stage is an assertion about readiness, so each transition carries
                            evidence requirements — and a refusal names <em>every</em> missing piece
                            at once, not just the first. The requirement table is per-tenant
                            configurable, with one exception that is not: a system whose EU AI Act
                            risk tier is <strong>high</strong> requires human approval on every
                            transition. That floor cannot be configured away.
                        </p>
                    </section>

                    <section>
                        <h3 className="text-sm font-semibold text-default mb-2">Contract, drift and replay</h3>
                        <p>
                            The <strong>Contract</strong> tab freezes what the system is allowed to
                            do; conformance then compares what it <em>actually</em> does. An active
                            system whose configuration changed keeps running — it simply carries an
                            unpromoted contract, and that is surfaced rather than silently blessed.
                            <strong> Replay</strong> re-evaluates recorded production steps under a
                            candidate contract before you promote it: it diffs authorization
                            decisions, not trajectories — your agent is never re-run.
                            <strong> Drift</strong> watches the shape of behaviour over time and
                            raises findings for a human to triage.
                        </p>
                    </section>

                    <section>
                        <h3 className="text-sm font-semibold text-default mb-2">How assurance judges it</h3>
                        <p>
                            The verdict strip at the top of this page is worst-of: one red signal
                            makes the system red. And absence of evidence is never green — a system
                            that stopped reporting shows as <em>Silent</em>, because liveness is the
                            only signal that fires when a system stops calling; every other metric
                            is a function of traffic that arrived.
                        </p>
                    </section>

                    <section>
                        <h3 className="text-sm font-semibold text-default mb-2">Where to do things</h3>
                        <ul className="list-disc pl-5 space-y-1">
                            <li>Declarations (owner, intended use, lifecycle stage, risk tier): the system&apos;s <strong>General</strong> tab.</li>
                            <li>What it may call: the resource bindings on its detail page.</li>
                            <li>Which EU AI Act obligations its declarations trigger: <strong>Compliance → EU AI Act</strong>.</li>
                            <li>Evidence for auditors: the <strong>Assurance</strong> tab&apos;s evidence links and reports.</li>
                        </ul>
                    </section>
                </div>
            </div>
        </div>
    );
}
