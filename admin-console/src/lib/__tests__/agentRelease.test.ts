/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * The console's one wording of a release, and the approved-
 * version rules it mirrors from the control plane.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { siblingForTest } from '../../../scripts/sibling-repositories.mjs';
import {
    AGENT_FINDING_LABELS,
    MAX_APPROVED_VERSIONS,
    NAME_PATTERN,
    VERSION_PATTERN,
    WILDCARD_SUFFIX,
    agentFindingOfMetric,
    agentImplementationLink,
    approvalOf,
    approvalState,
    declarationChanged,
    declarationOf,
    declarationTerms,
    diffEntryText,
    hasUndeclaredChange,
    implementationNameError,
    newReleaseOf,
    normaliseApprovedVersions,
    releaseChangeOf,
    releaseChangeText,
    releaseLabel,
    releaseLongLabel,
    runRelease,
    shortBuild,
    versionMatches,
    versionPatternError,
} from '../agentRelease';
import { driftMetricLabel } from '../aiSystemTypes';
import {
    INBOX_CATEGORIES,
    INBOX_CATEGORY_LABELS,
    agentInboxLinks,
    inboxCategoryLabel,
} from '../assuranceInboxTypes';

// The sibling Control Plane (scripts/sibling-repositories.mjs): its root, or
// null where it is not beside this repository, and its two tests are skipped.
const CP = siblingForTest('control-plane', 'approved-version patterns and the agent finding codes mirror the control plane');
const CP_IMPL = CP === null ? null : resolve(CP, 'app/services/agent_implementation.py');
const CP_CHECKS = CP === null ? null : resolve(CP, 'app/services/assurance_checks/agent.py');

describe('approved-version patterns mirror the control plane', () => {
    it.each(['1.4.2', '1.4.*', 'v2', '1.0.0+build.1', '1.0-rc_1', '2.*', 'a'.repeat(64)])('accepts %s', (p) => {
        expect(versionPatternError(p)).toBeNull();
    });

    it.each(['', '*', '.*', '1.*.2', '1.4*', '1 4', 'a'.repeat(65), '1.4.**', '1/4'])('refuses %j', (p) => {
        expect(versionPatternError(p)).not.toBeNull();
    });

    it('1.4.* accepts 1.4.2 and 1.4.2.1, never 1.40 or 1.4 itself', () => {
        expect(versionMatches('1.4.2', '1.4.*')).toBe(true);
        expect(versionMatches('1.4.2.1', '1.4.*')).toBe(true);
        expect(versionMatches('1.40', '1.4.*')).toBe(false);
        expect(versionMatches('1.4', '1.4.*')).toBe(false);
        expect(versionMatches('1.4.', '1.4.*')).toBe(false);
        expect(versionMatches('1.4.2', '1.4.2')).toBe(true);
        expect(versionMatches('1.4.20', '1.4.2')).toBe(false);
        expect(versionMatches(null, '1.4.*')).toBe(false);
    });

    it('approval is null (observe only) when nothing is approved', () => {
        expect(approvalOf('1.4.2', [])).toBeNull();
        expect(approvalOf('1.4.2', ['1.4.*'])).toBe(true);
        expect(approvalOf('1.5.0', ['1.4.*'])).toBe(false);
        expect(approvalState(null)).toBe('observe_only');
        expect(approvalState(true)).toBe('approved');
        expect(approvalState(false)).toBe('unapproved');
    });

    it('stores a set: trimmed, de-duplicated, sorted (the contract hash never depends on order)', () => {
        expect(normaliseApprovedVersions([' 1.5.0', '1.4.*', '1.5.0', ''])).toEqual(['1.4.*', '1.5.0']);
    });

    it('implementation names follow the name bounds after lower-casing', () => {
        expect(implementationNameError('Brutor-Demo')).toBeNull();
        expect(implementationNameError('')).toBeNull();
        expect(implementationNameError('has space')).not.toBeNull();
        expect(implementationNameError('x'.repeat(65))).not.toBeNull();
    });

    // Class-level guard: the console validates BEFORE the server does, so a
    // bound that drifts on either side lets the UI accept what the server
    // refuses (or refuse what it accepts). Read the constants from the
    // control plane itself when the repo is a sibling.
    it.skipIf(CP_IMPL === null)('uses the same bounds as app/services/agent_implementation.py', () => {
        const py = readFileSync(CP_IMPL ?? '', 'utf8');
        const pattern = (name: string) => {
            const m = new RegExp(`${name} = re\\.compile\\(r"([^"]+)"\\)`).exec(py);
            expect(m, `${name} not found in agent_implementation.py`).not.toBeNull();
            return m![1];
        };
        expect(NAME_PATTERN.source).toBe(pattern('NAME_PATTERN'));
        expect(VERSION_PATTERN.source).toBe(pattern('VERSION_PATTERN'));
        expect(py).toContain(`WILDCARD_SUFFIX = "${WILDCARD_SUFFIX}"`);
        expect(py).toContain(`MAX_APPROVED_VERSIONS = ${MAX_APPROVED_VERSIONS}`);
    });
});

describe('release wording', () => {
    it('name@version, with the build shortened in the long form', () => {
        expect(releaseLabel({ name: 'loan-intake', version: '1.4.2' })).toBe('loan-intake@1.4.2');
        expect(releaseLabel({ name: null, version: '1.4.2' })).toBe('unnamed@1.4.2');
        const digest = `sha256:${'ab'.repeat(32)}`;
        expect(shortBuild(digest)).toBe('sha256:abababababab');
        expect(shortBuild('0123456789abcdef0123')).toBe('0123456789ab');
        expect(shortBuild('abc123')).toBe('abc123');
        expect(releaseLongLabel({ name: 'a', version: '1', build: digest })).toBe('a@1 (sha256:abababababab)');
        expect(releaseLongLabel({ name: 'a', version: '1', build: null })).toBe('a@1');
    });

    it('marks an undeclared change only when a release showed more than one fingerprint', () => {
        expect(hasUndeclaredChange({ fingerprint_count: 1 })).toBe(false);
        expect(hasUndeclaredChange({ fingerprint_count: 2 })).toBe(true);
        expect(hasUndeclaredChange({ fingerprint_count: 0 })).toBe(false);
    });

    it('reads a run release from agent_impl_*; none when the run declared nothing', () => {
        expect(runRelease({ agent_impl_name: 'a', agent_impl_version: '1', agent_impl_trust: 'verified' })).toEqual({
            name: 'a',
            version: '1',
            build: null,
            trust: 'verified',
        });
        expect(runRelease({ agent_impl_name: null, agent_impl_version: null })).toBeNull();
        expect(runRelease({ agent_impl_name: 'a', agent_impl_version: '1', agent_impl_trust: 'bogus' })?.trust).toBeNull();
    });
});

describe('declarations as contract terms', () => {
    it('reads a closure entry with conditional keys, and words each term', () => {
        const d = declarationOf({ id: 'a', name: 'Intake', implementation_name: 'loan-intake', approved_versions: ['1.5.0', '1.4.*'], require_release: true });
        expect(declarationTerms(d)).toEqual([
            'must declare implementation “loan-intake”',
            'approved releases: 1.4.*, 1.5.0',
            'runs without a declared release are a finding',
        ]);
        // An identity that declares nothing carries none of the keys.
        expect(declarationTerms(declarationOf({ id: 'b', name: 'Scorer' }))).toEqual([]);
    });

    it('a change in any signed term is a change; order is not', () => {
        const base = { implementation_name: 'x', approved_versions: ['1.4.*', '1.5.0'], require_release: false };
        expect(declarationChanged(base, { ...base, approved_versions: ['1.5.0', '1.4.*'] })).toBe(false);
        expect(declarationChanged(base, { ...base, approved_versions: ['1.4.*'] })).toBe(true);
        expect(declarationChanged(base, { ...base, require_release: true })).toBe(true);
        expect(declarationChanged(base, { ...base, implementation_name: null })).toBe(true);
    });

    it('diff entries of kind agent_implementation name the agent', () => {
        expect(
            diffEntryText({ kind: 'agent_implementation', agent: 'Intake', field: 'approved_versions', detail: 'release 1.5.0 approved' } as never),
        ).toBe('Agent Intake: release 1.5.0 approved');
        expect(diffEntryText({ kind: 'model_added', detail: 'model x added' })).toBe('model x added');
    });
});

describe('the agent_release drift cause', () => {
    const coreTransition = {
        agent_id: 'agent-1',
        from: { name: 'screening-agent', version: '1.4.2', build: null },
        to: { name: 'screening-agent', version: '1.5.0', build: 'sha256:beef' },
        changed_at: '2026-09-27T14:00:00Z',
        version_change: true,
        from_fingerprint: 'sha256:aa',
        to_fingerprint: 'sha256:bb',
        trust: 'declared',
    };

    it('names both releases of a transition (the core ladder evidence)', () => {
        const c = releaseChangeOf(coreTransition)!;
        expect(c.versionChanged).toBe(true);
        expect(c.trust).toBe('declared');
        expect(releaseChangeText(c)).toBe('screening-agent@1.4.2 → screening-agent@1.5.0 (sha256:beef)');
    });

    it('says "no version change, new fingerprint" for the undeclared change', () => {
        const c = releaseChangeOf({
            ...coreTransition,
            from: coreTransition.to,
            version_change: false,
            from_fingerprint: `sha256:${'1'.repeat(64)}`,
            to_fingerprint: `sha256:${'2'.repeat(64)}`,
        })!;
        expect(c.versionChanged).toBe(false);
        expect(releaseChangeText(c)).toBe(
            'No version change: still screening-agent@1.5.0 (sha256:beef), but a new implementation fingerprint ' +
                '(sha256:111111111111 → sha256:222222222222).',
        );
    });

    it('reads the control plane agent finding shape (code + release + message)', () => {
        const c = releaseChangeOf({
            code: 'agent.undeclared_change',
            agent_id: 'agent-1',
            agent_name: 'Screening',
            release: { name: 'screening-agent', version: '1.5.0', build: null, trust: 'verified' },
            message: 'Screening still declares release …',
            no_version_change: true,
            established_fingerprint: 'sha256:aa',
            new_fingerprint: 'sha256:bb',
        })!;
        expect(c.code).toBe('agent.undeclared_change');
        expect(c.versionChanged).toBe(false);
        expect(c.trust).toBe('verified');
        expect(c.agentName).toBe('Screening');
        expect(c.fromFingerprint).toBe('sha256:aa');
        expect(releaseChangeOf({ summary: 'nothing to do with releases' })).toBeNull();
    });

    it('agent finding metric keys read as the finding, not as a slug', () => {
        expect(agentFindingOfMetric('agent.unapproved_release:agent-1')).toBe('agent.unapproved_release');
        expect(agentFindingOfMetric('cost_per_run')).toBeNull();
        expect(driftMetricLabel('agent.unapproved_release:agent-1')).toBe('Unapproved release');
        expect(driftMetricLabel('agent.undeclared_change:agent-1')).toBe('Undeclared implementation change');
    });

    // Class-level: the finding vocabulary is written by the control plane;
    // a code it adds that the console does not name would render as a slug.
    it.skipIf(CP_CHECKS === null)('names exactly the codes the control plane checks raise', () => {
        const py = readFileSync(CP_CHECKS ?? '', 'utf8');
        const codes = [...py.matchAll(/^FINDING_[A-Z_]+ = "(agent\.[a-z_]+)"$/gm)].map((m) => m[1]).sort();
        expect(codes.length).toBeGreaterThan(0);
        expect(Object.keys(AGENT_FINDING_LABELS).sort()).toEqual(codes);
    });
});

describe('the agent.new_release lifecycle event', () => {
    it('reads the release an event records, and nothing from a stage transition', () => {
        const ev = {
            event_type: 'agent.new_release',
            details: { agent_id: 'agent-1', agent_name: 'Intake', name: 'loan-intake', version: '1.5.0', build: null, trust: 'verified', approved: false },
        };
        expect(newReleaseOf(ev)).toEqual({
            agentId: 'agent-1',
            agentName: 'Intake',
            release: { name: 'loan-intake', version: '1.5.0', build: null, trust: 'verified', approved: false, last_seen_at: null },
        });
        expect(newReleaseOf({ event_type: 'stage_transition', details: null })).toBeNull();
    });
});

describe('inbox: the agent category', () => {
    it('is appended LAST (the control plane pins the order) and labelled "Agent"', () => {
        expect(INBOX_CATEGORIES[INBOX_CATEGORIES.length - 1]).toBe('agent');
        expect(INBOX_CATEGORY_LABELS.agent).toBe('Agent');
        expect(inboxCategoryLabel('agent')).toBe('Agent');
        expect(inboxCategoryLabel('something_new')).toBe('something_new');
    });

    it('every category has a label (none renders as its slug)', () => {
        for (const c of INBOX_CATEGORIES) expect(INBOX_CATEGORY_LABELS[c]).toBeTruthy();
    });

    it('an agent finding links to the identity’s Implementation section, then the system', () => {
        expect(agentInboxLinks('agent', { agent_id: 'agent-1' }, 'sys-1')).toEqual([
            { label: 'View agent implementation', tab: agentImplementationLink('agent-1') },
            { label: 'View composition', tab: 'ai-systems:sys-1:composition' },
        ]);
        expect(agentImplementationLink('agent-1')).toBe('a2a-identities:agent-1:implementation');
        expect(agentInboxLinks('drift', { agent_id: 'agent-1' }, 'sys-1')).toEqual([]);
    });
});
