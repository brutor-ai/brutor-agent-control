/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two state machines on an AI System, drawn separately because they are
 * separate — and, since the Lifecycle tab, drawn *live*.
 *
 *   * `StageDiagram` — the **system's stage** (proposed → approved → active →
 *     deprecated → retired), where every arrow is a gate with named
 *     requirements. It takes the gate's actual state from
 *     `GET /ai-systems/{id}/lifecycle`: the current stage is marked, the
 *     gate leaving it shows each requirement as met or unmet, and every
 *     unmet requirement that has a single fix carries a button that does
 *     the fix (mint the contract, set the owner, open the evidence tab…).
 *   * `ContractStatesDiagram` — one **contract version** (draft → active →
 *     superseded, or rejected), with the sentence that resolves the
 *     confusion these two share: promoting a contract is not a lifecycle
 *     transition.
 *
 * Everything drawn here mirrors `ai_system_lifecycle_gate.DEFAULT_REQUIREMENTS`
 * and `ai_system_contract_service`. If either changes, change this too — a
 * diagram that lies about the gate is worse than no diagram. The requirement
 * *keys* are the control plane's; only the short labels are ours.
 *
 * Colours come from the theme's CSS variables plus alpha-tinted semantic
 * fills, so it reads on both the light and dark themes without a second
 * palette.
 */

import { useState } from 'react';
import { Check, ChevronDown, ChevronRight, HelpCircle, X } from 'lucide-react';

import type { GateRequirement, TransitionOption } from '../../lib/aiSystemTypes';
import {
    GATE_ACTIONS,
    REQUIREMENT_SHORT,
    backwardTransition,
    forwardTransition,
    type Stage,
} from '../../lib/lifecycleGates';
import { OffendingCredentials } from './OffendingCredentials';

/** Alpha tints survive both grounds; flat pastels do not. */
const AMBER_FILL = 'rgba(245,158,11,0.16)';
const AMBER_LINE = 'rgba(245,158,11,0.65)';
const GREEN_FILL = 'rgba(34,197,94,0.16)';
const GREEN_LINE = 'rgba(34,197,94,0.65)';
const RED_FILL = 'rgba(239,68,68,0.13)';
const RED_LINE = 'rgba(239,68,68,0.55)';

const STAGES: Array<{ x: number; w: number; label: Stage }> = [
    { x: 16, w: 124, label: 'proposed' },
    { x: 244, w: 124, label: 'approved' },
    { x: 472, w: 124, label: 'active' },
    { x: 700, w: 124, label: 'deprecated' },
    { x: 928, w: 96, label: 'retired' },
];

/** Where each gate's requirement list is centred, and what it lists when
 *  the gate is not the live one. Keys are the control plane's. */
const GATES: Array<{ cx: number; to: Stage; keys: string[] }> = [
    { cx: 192, to: 'approved', keys: ['contract_minted', 'owner_set', 'intended_use_set'] },
    {
        cx: 420,
        to: 'active',
        keys: ['contract_minted', 'approver_recorded', 'liveness_expectation_set', 'replay_passed', 'workload_identity_enforced'],
    },
    { cx: 648, to: 'deprecated', keys: ['successor_named'] },
    { cx: 876, to: 'retired', keys: ['no_recent_runs'] },
];

function shortLabel(key: string): string {
    return REQUIREMENT_SHORT[key] ?? key.replace(/_/g, ' ');
}

interface StageDiagramProps {
    /** The system's current stage; unset systems mark nothing. */
    currentStage?: string | null;
    /** The reachable transitions with their live checklists. */
    transitions?: TransitionOption[];
    /** `high` draws the EU AI Act floor as applying, not as a footnote. */
    riskTier?: string | null;
    /** An operator clicked the operation that opens a gate. */
    onAction?: (requirementKey: string, toStage: string) => void;
    /** The action currently in flight, so its button reads as such. */
    busy?: string;
}

export function StageDiagram({
    currentStage,
    transitions,
    riskTier,
    onAction,
    busy,
}: StageDiagramProps) {
    const forward = forwardTransition(currentStage, transitions);
    const backward = backwardTransition(currentStage, transitions);
    const high = riskTier === 'high';

    // The live gate's requirements come from the API; every other gate is
    // drawn from the pinned defaults so the whole road stays visible.
    const liveByKey = new Map<string, GateRequirement>(
        (forward?.requirements ?? []).map((r) => [r.key, r]),
    );
    // The rail grows with the live gate: a high-risk system lists five
    // requirements under one arrow, which a fixed height would clip.
    const gateTop = currentStage ? 100 : 92;
    const maxLines = Math.max(
        3,
        ...GATES.map((g) => (forward?.to_stage === g.to ? forward.requirements.length : g.keys.length)),
    );
    const viewHeight = gateTop + maxLines * 13 + 6;

    return (
        <div data-testid="stage-diagram">
            <svg
                viewBox={`0 0 1040 ${viewHeight}`}
                className="w-full h-auto"
                role="img"
                aria-label={
                    "The system's stage moves proposed, approved, active, deprecated, retired, " +
                    'and each arrow is a gate. Proposed to approved requires a minted contract, an ' +
                    'owner and an intended use. Approved to active requires a recorded approver, ' +
                    'liveness set, and the bound replay suite to have passed. Active to deprecated ' +
                    'requires a named successor. Deprecated to retired requires no recent runs. ' +
                    'Moving backwards is always allowed.' +
                    (currentStage ? ` This system is ${currentStage}.` : '')
                }
            >
                <defs>
                    <marker id="clArrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
                        <path d="M0,0 L8,4 L0,8 Z" fill="var(--color-text-muted, #9ca3af)" />
                    </marker>
                    <marker id="clBack" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
                        <path d="M0,0 L8,4 L0,8 Z" fill="var(--color-border-strong, #d1d5db)" />
                    </marker>
                    <style>{`
                      .clh{font:800 10px ui-monospace,'JetBrains Mono',monospace;letter-spacing:1px;fill:var(--color-text-muted, #9ca3af)}
                      .clstage{font:700 13px Inter,system-ui,sans-serif;fill:var(--color-text, #111827)}
                      .clgate{font:600 9px ui-monospace,'JetBrains Mono',monospace;fill:var(--color-text-secondary, #4b5563)}
                      .clgatedim{font:600 9px ui-monospace,'JetBrains Mono',monospace;fill:var(--color-text-muted, #9ca3af)}
                      .clgateok{font:700 9px ui-monospace,'JetBrains Mono',monospace;fill:#15803d}
                      .clgateno{font:800 9px ui-monospace,'JetBrains Mono',monospace;fill:#b45309}
                      .clsmall{font:500 9.5px Inter,system-ui,sans-serif;fill:var(--color-text-muted, #9ca3af)}
                    `}</style>
                </defs>

                <text x="16" y="20" className="clh">
                    THE SYSTEM&rsquo;S STAGE — EVERY ARROW IS A GATE
                </text>

                {STAGES.map((s) => {
                    const current = s.label === currentStage;
                    return (
                        <g key={s.label} data-testid={`stage-${s.label}`} data-current={current ? 'true' : 'false'}>
                            <rect
                                x={s.x}
                                y={40}
                                width={s.w}
                                height={34}
                                rx={8}
                                fill={current ? GREEN_FILL : 'var(--color-surface, #ffffff)'}
                                stroke={current ? GREEN_LINE : 'var(--color-border-strong, #d1d5db)'}
                                strokeWidth={current ? 2.4 : 1.4}
                            />
                            <text x={s.x + s.w / 2} y={62} className="clstage" textAnchor="middle">
                                {s.label}
                            </text>
                            {current && (
                                <text x={s.x + s.w / 2} y={84} className="clgateok" textAnchor="middle">
                                    ▲ this system
                                </text>
                            )}
                        </g>
                    );
                })}

                {GATES.map((g, i) => {
                    const from = STAGES[i].x + STAGES[i].w;
                    const to = STAGES[i + 1].x;
                    const live = forward?.to_stage === g.to;
                    // The live gate lists what the API says it needs (the
                    // high-risk floor adds requirements the defaults omit);
                    // the rest show the defaults, dimmed.
                    const keys = live ? (forward?.requirements ?? []).map((r) => r.key) : g.keys;
                    return (
                        <g key={g.to} data-testid={`gate-${g.to}`} data-live={live ? 'true' : 'false'}>
                            <path
                                d={`M${from + 8},57 L${to - 10},57`}
                                stroke={live ? 'var(--color-text, #111827)' : 'var(--color-text-muted, #9ca3af)'}
                                strokeWidth={live ? 2.2 : 1.6}
                                fill="none"
                                markerEnd="url(#clArrow)"
                            />
                            {keys.map((k, j) => {
                                const req = live ? liveByKey.get(k) : undefined;
                                const cls = !live
                                    ? 'clgatedim'
                                    : req?.satisfied
                                      ? 'clgateok'
                                      : 'clgateno';
                                const mark = !live ? '' : req?.satisfied ? '✓ ' : '✗ ';
                                return (
                                    <text
                                        key={k}
                                        x={g.cx}
                                        y={(currentStage ? 100 : 92) + j * 13}
                                        className={cls}
                                        textAnchor="middle"
                                    >
                                        {mark}
                                        {shortLabel(k)}
                                    </text>
                                );
                            })}
                        </g>
                    );
                })}

                {/* Backwards is always allowed — the escape hatch. Routed ABOVE
                    the boxes so it never crosses a gate's requirement list. */}
                <path
                    d="M534,36 C500,14 340,14 306,36"
                    fill="none"
                    stroke="var(--color-border-strong, #d1d5db)"
                    strokeWidth="1.3"
                    strokeDasharray="4 4"
                    markerEnd="url(#clBack)"
                />
                <text x="700" y="28" className="clsmall">
                    going back is always allowed — that is how you correct a mistake
                </text>
            </svg>

            {/* The live gate's operations. HTML rather than SVG so they are
                real buttons — focusable, labelled, and styled like every other
                creative action in the console. */}
            {forward && (
                <div
                    className="mt-2 border border-default rounded-lg bg-surface p-3 space-y-2"
                    data-testid={`gate-panel-${forward.to_stage}`}
                >
                    <div className="flex items-center justify-between gap-3 flex-wrap">
                        <p className="text-xs font-semibold text-default">
                            Gate: {currentStage} → {forward.to_stage}
                            {forward.allowed ? (
                                <span className="ml-2 text-green-700 font-medium">open</span>
                            ) : (
                                <span className="ml-2 text-amber-700 font-medium">
                                    {forward.unmet.length} requirement
                                    {forward.unmet.length === 1 ? '' : 's'} outstanding
                                </span>
                            )}
                        </p>
                        {backward && onAction && (
                            <button
                                type="button"
                                onClick={() => onAction('__back__', backward.to_stage)}
                                disabled={busy === `stage:${backward.to_stage}`}
                                className="text-xs text-secondary hover:text-default underline disabled:opacity-50"
                                data-testid={`gate-back-${backward.to_stage}`}
                            >
                                Move back to {backward.to_stage}
                            </button>
                        )}
                    </div>
                    <ul className="space-y-1.5">
                        {forward.requirements.map((r) => (
                            <li
                                key={r.key}
                                className="flex items-center gap-2 text-xs flex-wrap"
                                data-testid={`gate-req-${r.key}`}
                            >
                                {r.satisfied ? (
                                    <Check className="w-3.5 h-3.5 text-green-600 shrink-0" />
                                ) : (
                                    <X className="w-3.5 h-3.5 text-amber-600 shrink-0" />
                                )}
                                <span className={r.satisfied ? 'text-muted' : 'text-default'}>
                                    {r.label}
                                    {!r.satisfied && r.detail && (
                                        <span className="text-muted"> — {r.detail}</span>
                                    )}
                                </span>
                                {!r.satisfied && onAction && GATE_ACTIONS[r.key] && (
                                    <button
                                        type="button"
                                        onClick={() => onAction(r.key, forward.to_stage)}
                                        disabled={busy === `action:${r.key}`}
                                        className="px-2.5 py-1 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                                        data-testid={`gate-action-${r.key}`}
                                    >
                                        {GATE_ACTIONS[r.key]}
                                    </button>
                                )}
                                <OffendingCredentials requirement={r} testId={`offending-gate-${forward.to_stage}`} className="basis-full" />
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {/* The floor. Drawn always, so an operator on a minimal-risk system
                still learns it exists; emphasised when it applies. */}
            <div
                className={`mt-2 border rounded-lg p-3 ${
                    high ? 'border-red-300 bg-red-50' : 'border-default bg-surface'
                }`}
                data-testid="high-risk-floor"
                data-applies={high ? 'true' : 'false'}
            >
                <p className="text-[10px] font-extrabold tracking-wider font-mono text-amber-700">
                    HIGH-RISK (EU AI ACT) — A FLOOR NO TENANT POLICY CAN LOWER
                    {high && <span className="ml-2 text-red-700">· APPLIES TO THIS SYSTEM</span>}
                </p>
                <p className="mt-1 text-xs text-default">
                    A named approver on <strong>every</strong> transition, plus an unexpired impact
                    assessment at proposed → approved.
                </p>
                <p className="mt-1 text-xs text-muted">
                    Only at the approval step: that is the review where a document like an Article 27
                    FRIA is actually read. Demanding it again later would be evidence theatre.
                </p>
            </div>
        </div>
    );
}

/** One frozen version of the rules, and the button that is not a gate. */
export function ContractStatesDiagram() {
    const [open, setOpen] = useState(true);

    return (
        <div
            className="border border-default rounded-lg bg-surface-alt"
            data-testid="contract-states-diagram"
        >
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                className="w-full flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-primary-700 hover:underline"
                aria-expanded={open}
                data-testid="contract-states-toggle"
            >
                {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                <HelpCircle className="w-3.5 h-3.5" />
                How a contract version moves — and why promoting one is not a lifecycle transition
            </button>

            {open && (
                <div className="px-3 pb-3 max-w-4xl">
                    <svg
                        viewBox="0 0 1040 80"
                        className="w-full h-auto"
                        role="img"
                        aria-label={
                            'A contract is one frozen version of the rules and goes draft, then active ' +
                            'when promoted, then superseded when the next version is promoted; a draft ' +
                            'can instead be rejected. Minting freezes the live configuration and hashes ' +
                            'it, approving records who signed it while the status stays draft, and ' +
                            'promoting makes it the governing version; minting is called new version in the console. Promoting a contract is not a ' +
                            'lifecycle transition; the stage and its gates are on the Lifecycle tab.'
                        }
                    >
                        <defs>
                            <marker id="csArrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
                                <path d="M0,0 L8,4 L0,8 Z" fill="var(--color-text-muted, #9ca3af)" />
                            </marker>
                            <style>{`
                              .csh{font:800 10px ui-monospace,'JetBrains Mono',monospace;letter-spacing:1px;fill:var(--color-text-muted, #9ca3af)}
                              .csgate{font:600 9px ui-monospace,'JetBrains Mono',monospace;fill:var(--color-text-secondary, #4b5563)}
                              .csnote{font:500 10px Inter,system-ui,sans-serif;fill:var(--color-text-secondary, #4b5563)}
                              .csnoteb{font:700 11px Inter,system-ui,sans-serif;fill:var(--color-text, #111827)}
                              .cssmall{font:500 9.5px Inter,system-ui,sans-serif;fill:var(--color-text-muted, #9ca3af)}
                              .csbox{font:700 11px Inter,system-ui,sans-serif;fill:var(--color-text, #111827)}
                            `}</style>
                        </defs>

                        <text x="16" y="18" className="csh">
                            THE CONTRACT — ONE FROZEN VERSION OF THOSE RULES
                        </text>

                        {/* One row: rejected sits left of draft so the row reads
                            as a line rather than an L, and the notes take the
                            space to the right. */}
                        <rect x="16" y="32" width="100" height="34" rx="8" fill={RED_FILL} stroke={RED_LINE} strokeWidth="1.4" />
                        <text x="66" y="54" className="csbox" textAnchor="middle">rejected</text>

                        <path d="M176,49 L128,49" stroke="var(--color-text-muted, #9ca3af)" strokeWidth="1.4" fill="none" markerEnd="url(#csArrow)" />
                        <text x="152" y="39" className="cssmall" textAnchor="middle">reject</text>

                        <rect x="186" y="32" width="100" height="34" rx="8" fill={AMBER_FILL} stroke={AMBER_LINE} strokeWidth="1.6" />
                        <text x="236" y="54" className="csbox" textAnchor="middle">draft</text>

                        <path d="M294,49 L336,49" stroke="var(--color-text-muted, #9ca3af)" strokeWidth="1.6" fill="none" markerEnd="url(#csArrow)" />
                        <text x="316" y="39" className="cssmall" textAnchor="middle">promote</text>

                        <rect x="346" y="32" width="100" height="34" rx="8" fill={GREEN_FILL} stroke={GREEN_LINE} strokeWidth="1.6" />
                        <text x="396" y="54" className="csbox" textAnchor="middle">active</text>

                        <path d="M454,49 L496,49" stroke="var(--color-text-muted, #9ca3af)" strokeWidth="1.6" fill="none" markerEnd="url(#csArrow)" />
                        <text x="475" y="39" className="cssmall" textAnchor="middle">v+1</text>

                        <rect x="506" y="32" width="118" height="34" rx="8" fill="var(--color-surface-raised, #f3f4f6)" stroke="var(--color-border-strong, #d1d5db)" strokeWidth="1.4" />
                        <text x="565" y="54" className="csbox" textAnchor="middle">superseded</text>

                        <text x="660" y="30" className="csnote">new version — freeze the live config into a version, and hash it</text>
                        <text x="660" y="48" className="csnote">approve — record who signed it. The status stays draft.</text>
                        <text x="660" y="66" className="csnote">promote — make it the governing version</text>
                    </svg>
                </div>
            )}
        </div>
    );
}

export default StageDiagram;
