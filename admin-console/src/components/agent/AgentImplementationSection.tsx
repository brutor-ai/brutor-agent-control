/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The agent identity's *Implementation* section.
 *
 * The credential says WHO this agent is. This section declares WHAT
 * software must be behind it and WHICH of its releases are approved, and
 * shows every release the gateway has seen:
 *
 *   - declared name     — the implementation name the software must declare
 *   - approved versions — exact versions or `1.4.*` prefixes (validated as
 *                         the control plane validates them); empty = observe
 *   - require release   — a run with no name/version is a finding
 *   - release timeline  — first/last seen, runs, trust (verified | declared),
 *                         approval, fingerprints; an "undeclared change"
 *                         marker when one release showed >1 fingerprint
 *
 * The three declaration fields are part of the contract closure of every AI
 * System the identity is a member of: saving a change drifts those
 * contracts, and the section says so before the approver saves.
 *
 * Controlled: the dialog owns the declaration and saves it with the rest of
 * the identity.
 */

import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, FileSignature, Fingerprint, Loader2, Plus, X } from 'lucide-react';

import type { AgentImplementationDeclaration, AgentRelease } from '../../lib/agentIdentityTypes';
import {
    MAX_APPROVED_VERSIONS,
    TRUST_EXPLANATIONS,
    declarationChanged,
    hasUndeclaredChange,
    implementationNameError,
    approvalOf,
    normaliseApprovedVersions,
    versionPatternError,
} from '../../lib/agentRelease';
import { errorMessage } from '../../lib/narrow';
import { ago } from '../../lib/relativeTime';
import { useGateway } from '../../hooks/useGateway';
import { runBackground } from '../../lib/asyncSafe';
import { HelpTip } from '../ui/HelpTip';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';
import { AgentReleaseChip, ApprovalBadge, TrustBadge } from './AgentReleaseBadges';

export const IMPLEMENTATION_SECTION_ID = 'agent-implementation';

export interface AgentImplementationSectionProps {
    /** null while creating an identity: no releases can exist yet. */
    agentId: string | null;
    value: AgentImplementationDeclaration;
    /** The saved declaration — what the contract currently signs. */
    saved: AgentImplementationDeclaration;
    onChange: (next: AgentImplementationDeclaration) => void;
    /** Scroll the section into view on mount (the inbox deep link). */
    focus?: boolean;
}

export function AgentImplementationSection({
    agentId,
    value,
    saved,
    onChange,
    focus,
}: AgentImplementationSectionProps) {
    const ref = useRef<HTMLDivElement>(null);
    const [draft, setDraft] = useState('');
    const [nameText, setNameText] = useState(value.implementation_name ?? '');

    useEffect(() => {
        if (focus) ref.current?.scrollIntoView?.({ block: 'start' });
    }, [focus]);

    const nameError = implementationNameError(nameText);

    const draftError = draft.trim() ? versionPatternError(draft) : null;
    const full = value.approved_versions.length >= MAX_APPROVED_VERSIONS;
    const duplicate = value.approved_versions.includes(draft.trim());

    const addPattern = () => {
        const p = draft.trim();
        if (!p || draftError || full || duplicate) return;
        onChange({ ...value, approved_versions: normaliseApprovedVersions([...value.approved_versions, p]) });
        setDraft('');
    };

    const removePattern = (p: string) =>
        onChange({ ...value, approved_versions: value.approved_versions.filter((v) => v !== p) });

    const dirty = declarationChanged(value, saved);

    return (
        <div ref={ref} id={IMPLEMENTATION_SECTION_ID} className="space-y-3" data-testid="agent-implementation">
            <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold text-default">Implementation</h3>
                <HelpTip
                    title="Implementation and releases"
                    text="The credential decides who this agent is. This section declares which software must be behind it and which of its releases you approve. Releases are recorded from the agent's verified token claims, MCP clientInfo, OpenTelemetry baggage or the X-Brutor-Agent-* headers. They describe the agent and are watched for change; they never grant or deny access."
                />
            </div>

            <label className="block text-sm">
                <span className="text-secondary">Declared implementation name</span>
                <input
                    value={nameText}
                    onChange={(e) => {
                        setNameText(e.target.value);
                        onChange({
                            ...value,
                            implementation_name: e.target.value.trim().toLowerCase() || null,
                        });
                    }}
                    className="mt-1 w-full rounded-lg border border-strong bg-surface px-3 py-2 font-mono text-sm"
                    placeholder="brutor-demo-screening-agent"
                    aria-invalid={nameError !== null}
                    data-testid="impl-name-input"
                />
                {nameError ? (
                    <span className="mt-1 block text-xs text-red-700" data-testid="impl-name-error">
                        {nameError}
                    </span>
                ) : (
                    <span className="mt-1 block text-xs text-muted">
                        Empty = not declared. A release with another name is an <em>implementation name mismatch</em>.
                    </span>
                )}
            </label>

            <div className="text-sm">
                <span className="text-secondary">Approved versions</span>
                <div className="mt-1 flex flex-wrap gap-1" data-testid="impl-approved-versions">
                    {value.approved_versions.length === 0 ? (
                        <span className="text-xs italic text-muted" data-testid="impl-observe-only">
                            None — observe only: releases are recorded, not judged.
                        </span>
                    ) : (
                        value.approved_versions.map((p) => (
                            <span
                                key={p}
                                className="inline-flex items-center gap-1 rounded border border-default bg-surface-alt px-1.5 py-0.5 font-mono text-xs text-default"
                                data-testid={`impl-version-${p}`}
                            >
                                {p}
                                <button
                                    type="button"
                                    onClick={() => removePattern(p)}
                                    className="text-muted hover:text-red-700"
                                    aria-label={`Remove approved version ${p}`}
                                    data-testid={`impl-version-remove-${p}`}
                                >
                                    <X className="h-3 w-3" />
                                </button>
                            </span>
                        ))
                    )}
                </div>
                <div className="mt-2 flex items-center gap-2">
                    <input
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                                e.preventDefault();
                                addPattern();
                            }
                        }}
                        className="w-48 rounded-lg border border-strong bg-surface px-3 py-1.5 font-mono text-sm"
                        placeholder="1.4.2 or 1.4.*"
                        aria-label="Approved version pattern"
                        aria-invalid={draftError !== null}
                        data-testid="impl-version-input"
                    />
                    <button
                        type="button"
                        onClick={addPattern}
                        disabled={!draft.trim() || draftError !== null || full || duplicate}
                        className="flex items-center gap-1 rounded-lg border border-strong px-2.5 py-1.5 text-xs text-secondary hover:bg-surface-alt disabled:opacity-50"
                        data-testid="impl-version-add"
                    >
                        <Plus className="h-3.5 w-3.5" /> Approve
                    </button>
                </div>
                {draftError && (
                    <p className="mt-1 text-xs text-red-700" data-testid="impl-version-error">
                        {draftError}
                    </p>
                )}
                {!draftError && duplicate && draft.trim() && (
                    <p className="mt-1 text-xs text-muted">Already approved.</p>
                )}
                {full && (
                    <p className="mt-1 text-xs text-muted">At most {MAX_APPROVED_VERSIONS} patterns.</p>
                )}
                <p className="mt-1 text-xs text-muted">
                    An exact version, or a prefix ending in <code>.*</code> — <code>1.4.*</code> accepts 1.4.2 and
                    1.4.2.1, never 1.40 or 1.4 itself.
                </p>
            </div>

            <label className="flex items-center gap-2 text-sm text-secondary">
                <input
                    type="checkbox"
                    checked={value.require_release}
                    onChange={(e) => onChange({ ...value, require_release: e.target.checked })}
                    data-testid="impl-require-release"
                />
                Require a release — a run that declares no name and version is a finding
            </label>

            {dirty && (
                <div
                    className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900"
                    data-testid="impl-contract-note"
                >
                    <FileSignature className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>
                        These terms are part of the contract of every AI System this agent is a member of. Saving
                        changes those contracts: each system shows its configuration as drifted from its active
                        contract until a new contract version is minted and approved.
                    </span>
                </div>
            )}

            <ReleaseTimeline agentId={agentId} value={value} saved={saved} dirty={dirty} />
        </div>
    );
}

function ReleaseTimeline({
    agentId,
    value,
    saved,
    dirty,
}: {
    agentId: string | null;
    value: AgentImplementationDeclaration;
    saved: AgentImplementationDeclaration;
    dirty: boolean;
}) {
    const { gatewayClient } = useGateway();
    const [releases, setReleases] = useState<AgentRelease[] | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!agentId || !gatewayClient) return;
        let cancelled = false;
        runBackground(
            (async () => {
                try {
                    const r = await gatewayClient.listAgentReleases(agentId);
                    if (!cancelled) setReleases(r.releases ?? []);
                } catch (e: unknown) {
                    if (!cancelled) setError(errorMessage(e, 'Could not load releases'));
                }
            })(),
            'agent releases',
        );
        return () => {
            cancelled = true;
        };
    }, [agentId, gatewayClient]);

    const declaredName = (dirty ? value : saved).implementation_name;

    return (
        <div className="pt-1" data-testid="impl-release-timeline">
            <div className="mb-1 flex items-center gap-2">
                <h4 className="text-xs font-semibold text-default">Releases seen</h4>
                <HelpTip
                    title="Release timeline"
                    text={`Every name@version (and build) the gateway recorded for this identity, most recently seen first. ${TRUST_EXPLANATIONS.verified} ${TRUST_EXPLANATIONS.declared} "Undeclared change" marks a release that showed more than one implementation fingerprint: the same version behaving like different software.`}
                />
                {dirty && (
                    <span className="text-[11px] text-muted" data-testid="impl-approval-preview">
                        approval shown as it will be after saving
                    </span>
                )}
            </div>
            {!agentId ? (
                <p className="text-xs italic text-muted">Releases appear once the agent runs through the gateway.</p>
            ) : error ? (
                <p className="text-xs text-red-700" data-testid="impl-releases-error">
                    {error}
                </p>
            ) : releases === null ? (
                <p className="flex items-center gap-2 text-xs text-muted">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading releases…
                </p>
            ) : releases.length === 0 ? (
                <p className="text-xs italic text-muted" data-testid="impl-releases-empty">
                    No release seen yet. The agent has not declared a name and version on any run.
                </p>
            ) : (
                <div className={TABLE_CONTAINER}>
                    <table className="min-w-full text-xs">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={TABLE_TH}>Release</th>
                                <th className={TABLE_TH}>Trust</th>
                                <th className={TABLE_TH}>Approval</th>
                                <th className={TABLE_TH}>Seen</th>
                                <th className={TABLE_TH}>Runs</th>
                                <th className={TABLE_TH}>Fingerprints</th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {releases.map((r) => (
                                <ReleaseRow
                                    key={r.id}
                                    release={r}
                                    approved={dirty ? approvalOf(r.version, value.approved_versions) : r.approved}
                                    nameMismatch={Boolean(declaredName) && r.name !== declaredName}
                                />
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}

function ReleaseRow({
    release: r,
    approved,
    nameMismatch,
}: {
    release: AgentRelease;
    approved: boolean | null;
    nameMismatch: boolean;
}) {
    const changed = hasUndeclaredChange(r);
    return (
        <tr data-testid={`impl-release-${r.id}`}>
            <td className="px-4 py-2 align-top">
                {/* Trust and approval have their own columns. */}
                <AgentReleaseChip release={{ name: r.name, version: r.version, build: r.build }} showApproval={false} />
                {r.current && (
                    <span className="ml-1 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-800" data-testid="impl-release-current">
                        current
                    </span>
                )}
                {nameMismatch && (
                    <span
                        className="mt-0.5 flex items-center gap-1 text-[11px] text-amber-800"
                        data-testid="impl-release-name-mismatch"
                    >
                        <AlertTriangle className="h-3 w-3" /> name differs from the declared implementation
                    </span>
                )}
            </td>
            <td className="px-4 py-2 align-top">
                <TrustBadge trust={r.trust} />
            </td>
            <td className="px-4 py-2 align-top">
                <ApprovalBadge approved={approved} />
            </td>
            <td className="px-4 py-2 align-top text-secondary">
                <span className="block" title={r.first_seen_at ?? undefined}>
                    first {r.first_seen_at ? ago(r.first_seen_at) : '—'}
                </span>
                <span className="block text-muted" title={r.last_seen_at ?? undefined}>
                    last {r.last_seen_at ? ago(r.last_seen_at) : '—'}
                </span>
            </td>
            <td className="px-4 py-2 align-top text-secondary">{r.run_count}</td>
            <td className="px-4 py-2 align-top text-secondary">
                <span className="inline-flex items-center gap-1">
                    <Fingerprint className="h-3 w-3 text-muted" aria-hidden="true" />
                    {r.fingerprint_count}
                </span>
                {changed && (
                    <span
                        className="mt-0.5 flex items-center gap-1 text-[11px] font-medium text-red-700"
                        title="This release showed more than one implementation fingerprint: the same version behaving like different software."
                        data-testid="impl-release-undeclared-change"
                    >
                        <AlertTriangle className="h-3 w-3" /> undeclared change
                    </span>
                )}
            </td>
        </tr>
    );
}

export default AgentImplementationSection;
