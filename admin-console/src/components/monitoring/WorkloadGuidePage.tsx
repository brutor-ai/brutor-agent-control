/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Workload classes — T(n·k·a) guide as a full sub-page.
 *
 * Promoted from a modal: the guide is the one long-form read in Mission
 * Control, and a modal both cramped it and lost the growth-curve chart
 * that makes Big-T click. Content still comes from the control plane
 * (single source of truth for every tokenomics term); this page owns
 * only the presentation — article typography, the curves diagram, and
 * a class chip on ladder headings.
 */

import React, { useEffect } from 'react';
import { ArrowLeft } from 'lucide-react';

import type { WorkloadGuide } from '../../lib/missionControlCardTypes';
import bigTCurves from '../../assets/big-t-growth-curves.svg';

interface WorkloadGuidePageProps {
    guide: WorkloadGuide;
    onBack: () => void;
}

/** Headings that ARE a ladder rung get their notation rendered as a
 *  chip, so the ladder scans as a ladder. */
function headingParts(title?: string): { chip?: string; rest?: string } {
    if (!title) return {};
    const m = title.match(/^(T\([^)]*\))\s*[—–-]?\s*(.*)$/);
    if (!m) return { rest: title };
    return { chip: m[1], rest: m[2] };
}

export const WorkloadGuidePage: React.FC<WorkloadGuidePageProps> = ({
    guide,
    onBack,
}) => {
    // A sub-page starts at the top, and Escape goes back — the two
    // behaviours operators expect after living in the modal version.
    useEffect(() => {
        window.scrollTo(0, 0);
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onBack();
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onBack]);

    return (
        <div className="max-w-3xl mx-auto" data-testid="mc-guide-page">
            <button
                type="button"
                onClick={onBack}
                data-testid="mc-guide-back"
                className="flex items-center gap-2 text-sm text-secondary hover:text-default mb-4"
            >
                <ArrowLeft className="w-4 h-4" />
                Mission Control
            </button>

            <h1 className="text-2xl font-bold text-default mb-2">{guide.title}</h1>
            <p className="text-sm text-muted mb-6">
                How each AI System&apos;s cost grows as usage grows — measured from
                its run ledger over the 30-day window, never estimated.
            </p>

            {/* The chart that makes the notation click: six growth shapes
                on one pair of axes. */}
            <figure className="bg-surface border border-default rounded-lg p-4 mb-8">
                <img
                    src={bigTCurves}
                    alt="Big-T notation: six growth curves on one chart — from T(1), barely growing with usage, to T(∞), unbounded"
                    className="w-full h-auto"
                />
                <figcaption className="mt-2 text-xs text-muted text-center">
                    Six classes, one chart. The violet T(n·k·a) curve runs below
                    T(n·k) for most of the range and only overtakes it late —
                    which is why agent workloads are easy to underestimate in a
                    pilot.
                </figcaption>
            </figure>

            <div className="space-y-8">
                {guide.sections.map((sec, i) => {
                    const { chip, rest } = headingParts(sec.title);
                    return (
                        <section key={sec.title ?? i}>
                            {sec.title && (
                                <h2 className="flex items-center gap-2 text-base font-semibold text-default mb-2">
                                    {chip && (
                                        <span className="inline-flex px-2 py-0.5 rounded bg-primary-50 text-primary-800 dark:bg-primary-900/40 dark:text-primary-300 font-mono text-sm">
                                            {chip}
                                        </span>
                                    )}
                                    {rest}
                                </h2>
                            )}
                            {sec.body && (
                                <p className="text-sm text-secondary leading-relaxed whitespace-pre-line">
                                    {sec.body}
                                </p>
                            )}
                            {sec.points && sec.points.length > 0 && (
                                <ul className="mt-3 space-y-2">
                                    {sec.points.map((pt) => (
                                        <li
                                            key={pt}
                                            className="flex items-start gap-2 text-sm text-secondary leading-relaxed"
                                        >
                                            <span className="mt-[7px] w-1.5 h-1.5 rounded-full bg-primary-400 shrink-0" />
                                            <span>{pt}</span>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </section>
                    );
                })}
            </div>
        </div>
    );
};

export default WorkloadGuidePage;
