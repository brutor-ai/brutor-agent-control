/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The release chip every surface shows: `name@version`, its
 * trust (verified | declared: anything shown to a human says which)
 * and its approval. Wording comes from `lib/agentRelease.ts`.
 */

import { CircleDashed, ShieldCheck } from 'lucide-react';

import type { AgentReleaseTrust } from '../../lib/agentIdentityTypes';
import {
    APPROVAL_EXPLANATIONS,
    APPROVAL_LABELS,
    TRUST_EXPLANATIONS,
    TRUST_LABELS,
    approvalState,
    releaseLabel,
    shortBuild,
} from '../../lib/agentRelease';

const TRUST_CLASSES: Record<AgentReleaseTrust, string> = {
    verified: 'bg-green-100 text-green-800',
    declared: 'bg-slate-100 text-slate-700',
};

export function TrustBadge({ trust, testId }: { trust: AgentReleaseTrust; testId?: string }) {
    const Icon = trust === 'verified' ? ShieldCheck : CircleDashed;
    return (
        <span
            className={`inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium ${TRUST_CLASSES[trust]}`}
            title={TRUST_EXPLANATIONS[trust]}
            data-testid={testId ?? 'trust-badge'}
            data-trust={trust}
        >
            <Icon className="h-3 w-3" aria-hidden="true" />
            {TRUST_LABELS[trust]}
        </span>
    );
}

/** The simple graph's marker: an icon only, the word for screen readers. */
export function TrustMarker({ trust }: { trust: AgentReleaseTrust }) {
    const Icon = trust === 'verified' ? ShieldCheck : CircleDashed;
    return (
        <span title={TRUST_EXPLANATIONS[trust]} data-testid="trust-marker" data-trust={trust}>
            <Icon
                className={`inline h-3 w-3 align-[-2px] ${trust === 'verified' ? 'text-green-700' : 'text-muted'}`}
                aria-hidden="true"
            />
            <span className="sr-only">{TRUST_LABELS[trust]}</span>
        </span>
    );
}

const APPROVAL_CLASSES = {
    approved: 'bg-green-100 text-green-800',
    unapproved: 'bg-amber-100 text-amber-800',
    observe_only: 'bg-slate-100 text-slate-600',
} as const;

export function ApprovalBadge({ approved, testId }: { approved: boolean | null | undefined; testId?: string }) {
    const state = approvalState(approved);
    return (
        <span
            className={`inline-flex rounded px-1.5 py-0.5 text-[10px] font-medium ${APPROVAL_CLASSES[state]}`}
            title={APPROVAL_EXPLANATIONS[state]}
            data-testid={testId ?? 'approval-badge'}
            data-approval={state}
        >
            {APPROVAL_LABELS[state]}
        </span>
    );
}

/** `name@version` + build + trust + approval. `approved` undefined hides
 *  the approval badge (a run's own release carries no approval). */
export function AgentReleaseChip({
    release,
    approved,
    showApproval = true,
    testId,
}: {
    release: { name?: string | null; version?: string | null; build?: string | null; trust?: AgentReleaseTrust | null };
    approved?: boolean | null;
    showApproval?: boolean;
    testId?: string;
}) {
    const build = shortBuild(release.build);
    return (
        <span className="inline-flex flex-wrap items-center gap-1" data-testid={testId ?? 'agent-release'}>
            <code className="font-mono text-xs text-default" data-testid="agent-release-label">
                {releaseLabel(release)}
            </code>
            {build && (
                <span className="font-mono text-[10px] text-muted" title={release.build ?? undefined}>
                    {build}
                </span>
            )}
            {release.trust && <TrustBadge trust={release.trust} />}
            {showApproval && <ApprovalBadge approved={approved} />}
        </span>
    );
}

export function NoRelease({ testId }: { testId?: string }) {
    return (
        <span
            className="text-[11px] italic text-muted"
            title="This agent has not declared a release name and version on any run yet."
            data-testid={testId ?? 'agent-release-none'}
        >
            no release declared
        </span>
    );
}
