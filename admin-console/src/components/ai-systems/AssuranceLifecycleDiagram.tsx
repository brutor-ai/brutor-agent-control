/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where this system is in the **assurance lifecycle** — the continuous loop,
 * not the governance stage (that rail lives on the Lifecycle tab and is a
 * different state machine with a shared word).
 *
 *   governed calls → run ledger → learned normal → seven signals →
 *   one worst-of verdict → a human decides → a signed report,
 *   and the verdict feeds back into response, replay and the inbox.
 *
 * The loop runs all the time; what moves is how far the evidence has got.
 * A system with no runs is stuck at the ledger, one whose baselines are
 * still learning is stuck at normal, one with open findings is waiting on
 * a person. The first node that is not yet satisfied is marked — the same
 * convention as the stage rail — and every node states the number it rests
 * on, so "you are here" is a fact, not a decoration.
 */

import { loopNodes, type AssuranceLoopFacts } from '../../lib/assuranceLoop';

const GREEN_FILL = 'rgba(34,197,94,0.16)';
const GREEN_LINE = 'rgba(34,197,94,0.65)';
const AMBER_FILL = 'rgba(245,158,11,0.16)';
const AMBER_LINE = 'rgba(245,158,11,0.65)';

const X0 = 16;
const W = 150;
const GAP = 22;

export default function AssuranceLifecycleDiagram({
    facts,
    onOpenInbox,
}: {
    facts: AssuranceLoopFacts;
    /** "N findings open" becomes a button that takes the person to the Inbox. */
    onOpenInbox?: () => void;
}) {
    const nodes = loopNodes(facts);
    const openFindings = facts.findingsOpen ?? 0;
    const here = nodes.find((n) => n.state === 'here');

    return (
        <div
            className="border border-default rounded-lg bg-surface-alt px-3 pt-2 pb-3 max-w-4xl"
            data-testid="assurance-lifecycle-diagram"
            data-here={here?.id ?? 'running'}
        >
            <svg
                viewBox="0 0 1040 132"
                className="w-full h-auto"
                role="img"
                aria-label={
                    'The assurance lifecycle: governed calls become evidence in the run ledger, ' +
                    'evidence becomes learned normal, the signals collapse into one health ' +
                    'verdict, a human decides on the findings, and a signed report is captured; ' +
                    'the verdict feeds back into response and replay. ' +
                    (here ? `This system is at: ${here.label} — ${here.fact}.` : 'Every step is satisfied; the loop is running.')
                }
            >
                <defs>
                    <marker id="alArrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
                        <path d="M0,0 L8,4 L0,8 Z" fill="var(--color-text-muted, #9ca3af)" />
                    </marker>
                    <marker id="alBack" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
                        <path d="M0,0 L8,4 L0,8 Z" fill="var(--color-border-strong, #d1d5db)" />
                    </marker>
                    <style>{`
                      .alh{font:800 10px ui-monospace,'JetBrains Mono',monospace;letter-spacing:1px;fill:var(--color-text-muted, #9ca3af)}
                      .alnode{font:700 12px Inter,system-ui,sans-serif;fill:var(--color-text, #111827)}
                      .alfact{font:600 9px ui-monospace,'JetBrains Mono',monospace;fill:var(--color-text-secondary, #4b5563)}
                      .alfactdim{font:600 9px ui-monospace,'JetBrains Mono',monospace;fill:var(--color-text-muted, #9ca3af)}
                      .alhere{font:800 9px ui-monospace,'JetBrains Mono',monospace;fill:#b45309}
                      .alsmall{font:500 9.5px Inter,system-ui,sans-serif;fill:var(--color-text-muted, #9ca3af)}
                    `}</style>
                </defs>

                <text x="16" y="18" className="alh">
                    THE ASSURANCE LIFECYCLE — EVIDENCE RE-EARNS TRUST CONTINUOUSLY
                </text>

                {nodes.map((n, i) => {
                    const x = X0 + i * (W + GAP);
                    const fill =
                        n.state === 'done' ? GREEN_FILL : n.state === 'here' ? AMBER_FILL : 'var(--color-surface, #ffffff)';
                    const line =
                        n.state === 'done' ? GREEN_LINE : n.state === 'here' ? AMBER_LINE : 'var(--color-border-strong, #d1d5db)';
                    return (
                        <g key={n.id} data-testid={`loop-${n.id}`} data-state={n.state}>
                            <rect x={x} y={40} width={W} height={34} rx={8} fill={fill} stroke={line} strokeWidth={n.state === 'here' ? 2.4 : 1.4} />
                            <text x={x + W / 2} y={62} className="alnode" textAnchor="middle">
                                {n.label}
                            </text>
                            {n.state === 'here' && (
                                <text x={x + W / 2} y={86} className="alhere" textAnchor="middle">
                                    ▲ this system is here
                                </text>
                            )}
                            {n.id === 'review' && openFindings > 0 && onOpenInbox ? (
                                // The one node a person can act on from here: the
                                // count is the button, and it lands in the Inbox.
                                <g
                                    role="button"
                                    tabIndex={0}
                                    onClick={onOpenInbox}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter' || e.key === ' ') onOpenInbox();
                                    }}
                                    className="cursor-pointer"
                                    data-testid="loop-review-open"
                                    aria-label={`${n.fact} — open the Inbox`}
                                >
                                    <rect
                                        x={x + 14}
                                        y={(n.state === 'here' ? 100 : 90) - 12}
                                        width={W - 28}
                                        height={18}
                                        rx={6}
                                        fill="var(--color-surface, #ffffff)"
                                        stroke="var(--color-border-strong, #d1d5db)"
                                        strokeWidth="1"
                                    />
                                    <text
                                        x={x + W / 2}
                                        y={n.state === 'here' ? 100 : 90}
                                        className="alfact"
                                        textAnchor="middle"
                                        style={{ textDecoration: 'underline' }}
                                    >
                                        {n.fact} →
                                    </text>
                                </g>
                            ) : (
                                <text
                                    x={x + W / 2}
                                    y={n.state === 'here' ? 100 : 90}
                                    className={n.state === 'ahead' ? 'alfactdim' : 'alfact'}
                                    textAnchor="middle"
                                >
                                    {n.fact}
                                </text>
                            )}
                            {i < nodes.length - 1 && (
                                <path
                                    d={`M${x + W + 4},57 L${x + W + GAP - 6},57`}
                                    stroke="var(--color-text-muted, #9ca3af)"
                                    strokeWidth="1.6"
                                    fill="none"
                                    markerEnd="url(#alArrow)"
                                />
                            )}
                        </g>
                    );
                })}

                {/* The feedback: the verdict drives response, replay and the
                    inbox, which change the traffic the ledger records next. */}
                <path
                    d={`M${X0 + 3 * (W + GAP) + W / 2},36 C${X0 + 3 * (W + GAP)},12 ${X0 + W},12 ${X0 + W / 2},36`}
                    fill="none"
                    stroke="var(--color-border-strong, #d1d5db)"
                    strokeWidth="1.3"
                    strokeDasharray="4 4"
                    markerEnd="url(#alBack)"
                />
                <text x={X0 + 4 * (W + GAP) + 10} y="28" className="alsmall">
                    the verdict feeds back: response, replay, inbox
                </text>
                <text x="16" y="126" className="alsmall">
                    Not the governance stage — that rail is on the Lifecycle tab. This loop runs at every stage; what moves is how far the evidence has got.
                </text>
            </svg>
        </div>
    );
}
