/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent implementation identity: the ONE place the console
 * words a release, its trust and its approval.
 *
 * The credential says WHO acts (the agent identity). What software acted
 * and which release of it are recorded from a fixed list of carriers and
 * shown next to the identity — never used to decide access, and
 * always with their trust level. Every surface that names a release
 * (the identity's Implementation section, the Composition tab's three
 * views, the Lifecycle tab, Runs, Drift, the contract and the assurance
 * report) goes through these helpers, so a release reads the same
 * everywhere.
 *
 * Pure; the tests pin the wording and the pattern rules (which mirror the
 * control plane's `app/services/agent_implementation.py`).
 */

import type {
    AgentImplementationDeclaration,
    AgentRelease,
    AgentReleaseRef,
    AgentReleaseTrust,
} from './agentIdentityTypes';
import { identityDiffText } from './identityPolicy';
import { getArray, getBoolean, getRecord, getString, isRecord } from './narrow';

// ─── Trust ────────────────────────────────────────────────────────────────

export const TRUST_LABELS: Record<AgentReleaseTrust, string> = {
    verified: 'verified',
    declared: 'declared',
};

/** What each trust level means — shown wherever a trust badge is. */
export const TRUST_EXPLANATIONS: Record<AgentReleaseTrust, string> = {
    verified:
        'Verified: the name and version came from claims inside the agent’s verified workload token ' +
        '(software_id / software_version), so whoever issued the token vouches for them.',
    declared:
        'Declared: the agent reported its own name and version (MCP clientInfo, OpenTelemetry baggage ' +
        'or the X-Brutor-Agent-* headers). Anyone holding the credential could send a different value — ' +
        'it describes the release, it never grants access.',
};

export function isTrust(v: unknown): v is AgentReleaseTrust {
    return v === 'verified' || v === 'declared';
}

// ─── Labels ───────────────────────────────────────────────────────────────

interface ReleaseLike {
    name?: string | null;
    version?: string | null;
    build?: string | null;
}

/** `name@version` — the short form every view uses. A missing half reads
 *  as such rather than as an empty string. */
export function releaseLabel(r: ReleaseLike): string {
    return `${r.name || 'unnamed'}@${r.version || 'unversioned'}`;
}

/** A build, shortened for display: a `sha256:` digest keeps its algorithm
 *  and 12 hex characters; a git SHA its first 12. */
export function shortBuild(build: string | null | undefined): string | null {
    if (!build) return null;
    const m = /^([a-z0-9]+):([0-9a-f]{13,})$/i.exec(build);
    if (m) return `${m[1]}:${m[2].slice(0, 12)}`;
    return build.length > 16 ? build.slice(0, 12) : build;
}

/** `name@version (build)` — the long form, with the build when there is one. */
export function releaseLongLabel(r: ReleaseLike): string {
    const b = shortBuild(r.build);
    return b ? `${releaseLabel(r)} (${b})` : releaseLabel(r);
}

// ─── Approval ─────────────────────────────────────────────────────────────

export type ApprovalState = 'approved' | 'unapproved' | 'observe_only';

/** `approved` is derived by the control plane: null when the identity
 *  approves nothing (observe only), else whether the version matches. */
export function approvalState(approved: boolean | null | undefined): ApprovalState {
    if (approved === true) return 'approved';
    if (approved === false) return 'unapproved';
    return 'observe_only';
}

export const APPROVAL_LABELS: Record<ApprovalState, string> = {
    approved: 'approved',
    unapproved: 'not approved',
    observe_only: 'observe only',
};

export const APPROVAL_EXPLANATIONS: Record<ApprovalState, string> = {
    approved: 'This version matches one of the identity’s approved versions.',
    unapproved:
        'This version matches none of the identity’s approved versions — an agent.unapproved_release finding ' +
        'until it is approved or the agent is rolled back.',
    observe_only: 'The identity approves no versions yet, so releases are recorded but not judged.',
};

// ─── Undeclared change ────────────────────────────────────────────────────

/** A release that showed more than one implementation fingerprint behaved
 *  like different software under the same version — the undeclared change. The finding itself needs ≥ 20 runs on another fingerprint;
 *  the marker shows the fact. */
export function hasUndeclaredChange(r: Pick<AgentRelease, 'fingerprint_count'>): boolean {
    return r.fingerprint_count > 1;
}

// ─── Approved-version patterns (mirror of the control plane) ─────────────

/** Name and version bounds, the control plane's. */
export const NAME_PATTERN = /^[a-z0-9._-]{1,64}$/;
export const VERSION_PATTERN = /^[A-Za-z0-9.+_-]{1,64}$/;
export const WILDCARD_SUFFIX = '.*';
export const MAX_APPROVED_VERSIONS = 32;

/** An error message, or null when `value` is a valid approved-version
 *  pattern: an exact version, or a version prefix followed by `.*`. */
export function versionPatternError(value: string): string | null {
    const pattern = value.trim();
    const base = pattern.endsWith(WILDCARD_SUFFIX) ? pattern.slice(0, -WILDCARD_SUFFIX.length) : pattern;
    if (!base || base.includes('*') || !VERSION_PATTERN.test(base)) {
        return `“${value}” must be an exact version (letters, digits and . + _ -) or a version prefix ending in “.*”, e.g. 1.4.*`;
    }
    return null;
}

/** The stored form, as the control plane stores it: trimmed,
 *  de-duplicated and sorted (a set — the contract hash never depends on
 *  typing order). */
export function normaliseApprovedVersions(values: readonly string[]): string[] {
    return Array.from(new Set(values.map((v) => v.trim()).filter(Boolean))).sort();
}

/** Lower-cased and trimmed; '' clears the declaration. Returns the error
 *  for a name outside the bounds. */
export function implementationNameError(value: string): string | null {
    const name = value.trim().toLowerCase();
    if (!name) return null;
    return NAME_PATTERN.test(name)
        ? null
        : 'The implementation name may use lower-case letters, digits and . _ - (at most 64 characters).';
}

export function normaliseImplementationName(value: string): string | null {
    const name = value.trim().toLowerCase();
    return name || null;
}

/** Whether `version` matches `pattern` — `1.4.*` accepts `1.4.2` and
 *  `1.4.2.1`, never `1.40` and never `1.4` itself. */
export function versionMatches(version: string | null | undefined, pattern: string): boolean {
    if (!version) return false;
    if (pattern.endsWith(WILDCARD_SUFFIX)) {
        const prefix = pattern.slice(0, -WILDCARD_SUFFIX.length);
        return version.startsWith(`${prefix}.`) && version.length > prefix.length + 1;
    }
    return version === pattern;
}

/** The control plane's `approval_of`: null when nothing is approved
 *  (observe only), else whether `version` matches a pattern. Used to
 *  preview approval under a declaration that is being edited. */
export function approvalOf(version: string | null | undefined, patterns: readonly string[]): boolean | null {
    if (patterns.length === 0) return null;
    return patterns.some((p) => versionMatches(version, p));
}

// ─── Declarations ─────────────────────────────────────────────────────────

/** The declaration from an identity or a closure entry (both carry the
 *  same three keys; a closure entry omits the ones not set). */
export function declarationOf(value: unknown): AgentImplementationDeclaration {
    return {
        implementation_name: getString(value, 'implementation_name') || null,
        approved_versions: (getArray(value, 'approved_versions') ?? []).filter(
            (v): v is string => typeof v === 'string',
        ),
        require_release: getBoolean(value, 'require_release') === true,
    };
}

/** Whether two declarations differ in a term the contract signs. */
export function declarationChanged(a: AgentImplementationDeclaration, b: AgentImplementationDeclaration): boolean {
    return (
        (a.implementation_name ?? null) !== (b.implementation_name ?? null) ||
        a.require_release !== b.require_release ||
        normaliseApprovedVersions(a.approved_versions).join('\n') !==
            normaliseApprovedVersions(b.approved_versions).join('\n')
    );
}

/** The declaration as contract terms, one line each — the contract view,
 *  the printed contract and the tests read the same words. Empty when the
 *  identity declares nothing. */
export function declarationTerms(d: AgentImplementationDeclaration): string[] {
    const out: string[] = [];
    if (d.implementation_name) out.push(`must declare implementation “${d.implementation_name}”`);
    if (d.approved_versions.length > 0) {
        out.push(`approved releases: ${normaliseApprovedVersions(d.approved_versions).join(', ')}`);
    }
    if (d.require_release) out.push('runs without a declared release are a finding');
    return out;
}

// ─── Links ────────────────────────────────────────────────────────────────

/** The App tab id of an identity's Implementation section. */
export const agentImplementationLink = (agentId: string) => `a2a-identities:${agentId}:implementation`;

// ─── Findings ───────────────────────────────────────────────

export type AgentFindingCode =
    | 'agent.unapproved_release'
    | 'agent.name_mismatch'
    | 'agent.release_missing'
    | 'agent.undeclared_change';

export const AGENT_FINDING_LABELS: Record<AgentFindingCode, string> = {
    'agent.unapproved_release': 'Unapproved release',
    'agent.name_mismatch': 'Implementation name mismatch',
    'agent.release_missing': 'Release not declared',
    'agent.undeclared_change': 'Undeclared implementation change',
};

export function agentFindingLabel(code: string): string {
    return AGENT_FINDING_LABELS[code as AgentFindingCode] ?? code;
}

/** Drift metric keys of agent findings are `<code>:<identity>`. */
export function agentFindingOfMetric(metricKey: string): AgentFindingCode | null {
    const code = metricKey.split(':')[0];
    return code in AGENT_FINDING_LABELS ? (code as AgentFindingCode) : null;
}

// ─── Runs ─────────────────────────────────────────────────────────────────

export interface RunRelease {
    name: string | null;
    version: string | null;
    build: string | null;
    trust: AgentReleaseTrust | null;
}

/** The release a run carried (`agent_impl_*`), or null when it carried none. */
export function runRelease(run: unknown): RunRelease | null {
    const name = getString(run, 'agent_impl_name') ?? null;
    const version = getString(run, 'agent_impl_version') ?? null;
    if (!name && !version) return null;
    const trust = getString(run, 'agent_impl_trust');
    return {
        name,
        version,
        build: getString(run, 'agent_impl_build') ?? null,
        trust: isTrust(trust) ? trust : null,
    };
}

// ─── Drift: the agent_release cause ───────────────────────────────────────

export interface ReleaseChange {
    from: ReleaseLike | null;
    to: ReleaseLike | null;
    /** false = no version change: the same release, a new fingerprint. */
    versionChanged: boolean;
    fromFingerprint: string | null;
    toFingerprint: string | null;
    trust: AgentReleaseTrust | null;
    changedAt: string | null;
    /** Agent findings only: the finding code and its message. */
    code: AgentFindingCode | null;
    message: string | null;
    agentId: string | null;
    agentName: string | null;
}

function releaseOf(v: unknown): ReleaseLike | null {
    if (!isRecord(v)) return null;
    const name = getString(v, 'name') ?? null;
    const version = getString(v, 'version') ?? null;
    if (!name && !version) return null;
    return { name, version, build: getString(v, 'build') ?? null };
}

/**
 * The two shapes of `cause_evidence` behind `agent_release`:
 *  - the core's cause ladder (a behavioural shift explained by a release
 *    transition): `{from, to, version_change, from_fingerprint,
 *    to_fingerprint, trust, changed_at}`;
 *  - the control plane's agent checks (an `agent.*` finding):
 *    `{code, release, message, no_version_change?, established_fingerprint?,
 *    new_fingerprint?}`.
 */
export function releaseChangeOf(evidence: unknown): ReleaseChange | null {
    if (!isRecord(evidence)) return null;
    const codeRaw = getString(evidence, 'code');
    const code = codeRaw && codeRaw in AGENT_FINDING_LABELS ? (codeRaw as AgentFindingCode) : null;
    const from = releaseOf(evidence.from);
    const to = releaseOf(evidence.to) ?? releaseOf(evidence.release);
    if (!from && !to && !code) return null;
    const noVersionChange =
        getBoolean(evidence, 'version_change') === false ||
        getBoolean(evidence, 'no_version_change') === true ||
        code === 'agent.undeclared_change';
    const trust = getString(evidence, 'trust') ?? getString(getRecord(evidence, 'release'), 'trust');
    return {
        from,
        to,
        versionChanged: !noVersionChange,
        fromFingerprint:
            getString(evidence, 'from_fingerprint') ?? getString(evidence, 'established_fingerprint') ?? null,
        toFingerprint: getString(evidence, 'to_fingerprint') ?? getString(evidence, 'new_fingerprint') ?? null,
        trust: isTrust(trust) ? trust : null,
        changedAt: getString(evidence, 'changed_at') ?? null,
        code,
        message: getString(evidence, 'message') ?? null,
        agentId: getString(evidence, 'agent_id') ?? null,
        agentName: getString(evidence, 'agent_name') ?? null,
    };
}

/** A fingerprint shortened for display: `sha256:` + 12 hex. */
export function shortFingerprint(fp: string | null | undefined): string {
    if (!fp) return '—';
    return fp.startsWith('sha256:') ? `sha256:${fp.slice(7, 19)}` : fp.slice(0, 19);
}

/** One sentence for the drift card's release line. */
export function releaseChangeText(c: ReleaseChange): string {
    if (!c.versionChanged) {
        const release = c.to ? releaseLongLabel(c.to) : 'the same release';
        return (
            `No version change: still ${release}, but a new implementation fingerprint ` +
            `(${shortFingerprint(c.fromFingerprint)} → ${shortFingerprint(c.toFingerprint)}).`
        );
    }
    if (c.from && c.to) return `${releaseLongLabel(c.from)} → ${releaseLongLabel(c.to)}`;
    if (c.to) return releaseLongLabel(c.to);
    return '';
}

// ─── Lifecycle: agent.new_release ────────────────────────────────────────

export const LIFECYCLE_AGENT_NEW_RELEASE = 'agent.new_release';

export interface NewReleaseEvent {
    agentId: string | null;
    agentName: string | null;
    release: AgentReleaseRef;
}

/** The release an `agent.new_release` lifecycle event records, or null for
 *  any other event. */
export function newReleaseOf(event: { event_type?: string | null; details?: unknown }): NewReleaseEvent | null {
    if (event.event_type !== LIFECYCLE_AGENT_NEW_RELEASE) return null;
    const d = event.details;
    const name = getString(d, 'name');
    const version = getString(d, 'version');
    if (!name || !version) return null;
    const trust = getString(d, 'trust');
    const approved = isRecord(d) ? d.approved : undefined;
    return {
        agentId: getString(d, 'agent_id') ?? null,
        agentName: getString(d, 'agent_name') ?? null,
        release: {
            name,
            version,
            build: getString(d, 'build') ?? null,
            trust: isTrust(trust) ? trust : 'declared',
            approved: typeof approved === 'boolean' ? approved : null,
            last_seen_at: null,
        },
    };
}

// ─── Contract diff ────────────────────────────────────────────────────────

/** Diff entries of kind `agent_implementation` name the agent, those of
 *  kind `identity_policy` the field and its values (`identityDiffText`);
 *  the rest read as the server wrote them. */
export function diffEntryText(entry: { kind: string; detail: string; agent?: unknown; field?: string; from?: unknown; to?: unknown }): string {
    const identity = identityDiffText(entry);
    if (identity !== null) return identity;
    if (entry.kind === 'agent_implementation' && typeof entry.agent === 'string' && entry.agent) {
        return `Agent ${entry.agent}: ${entry.detail}`;
    }
    return entry.detail;
}
