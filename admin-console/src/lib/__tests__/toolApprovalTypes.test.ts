/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { approvalDecision, skillHold, timeLeft, timeSince } from '../toolApprovalTypes';

const NOW = Date.parse('2026-11-10T12:00:00Z');
const at = (minutes: number) => new Date(NOW + minutes * 60_000).toISOString();

describe('approvalDecision', () => {
    it('is the server’s decision when it is one this console knows', () => {
        for (const d of ['pending', 'approved', 'rejected', 'expired']) {
            expect(approvalDecision({ decision: d })).toBe(d);
        }
    });

    it('is unknown otherwise, and never the raw status', () => {
        expect(approvalDecision({ decision: 'escalated' })).toBe('unknown');
        expect(approvalDecision({ decision: null })).toBe('unknown');
        expect(approvalDecision({})).toBe('unknown');
        // a lapsed row: status pending, no decision sent — not actionable
        expect(approvalDecision({ status: 'pending' } as { decision?: string })).toBe('unknown');
    });
});

describe('timeLeft', () => {
    it('counts down in the unit a person reads', () => {
        expect(timeLeft(at(0.5), NOW)).toBe('under a minute');
        expect(timeLeft(at(8), NOW)).toBe('8 min');
        expect(timeLeft(at(125), NOW)).toBe('2 h 5 min');
        expect(timeLeft(at(60 * 24 * 3), NOW)).toBe('3 days');
    });

    it('is null once the deadline has passed, or without one', () => {
        expect(timeLeft(at(0), NOW)).toBeNull();
        expect(timeLeft(at(-1), NOW)).toBeNull();
        expect(timeLeft(null, NOW)).toBeNull();
        expect(timeLeft('not a date', NOW)).toBeNull();
    });
});

describe('timeSince', () => {
    it('says how long ago', () => {
        expect(timeSince(at(-0.2), NOW)).toBe('just now');
        expect(timeSince(at(-2), NOW)).toBe('2 min ago');
        expect(timeSince(at(-180), NOW)).toBe('3 h ago');
        expect(timeSince(at(-60 * 24 * 4), NOW)).toBe('4 days ago');
        expect(timeSince(null, NOW)).toBe('');
        expect(timeSince('nope', NOW)).toBe('');
    });
});

describe('skillHold: a held skill execution, as a skill', () => {
    const hold = (over: Record<string, unknown> = {}) => ({
        capability_type: 'skill_exec',
        capability_name: 'sk-3',
        server_name: 'sk-3',
        requester_group_id: 'rg-loans',
        request_metadata: { surface: 'skill', agent_id: null, execution_mode: { required_by: ['group', 'assignment'] } },
        ...over,
    });

    it('is null for a hold that is not a skill execution', () => {
        expect(skillHold(hold({ capability_type: 'tool' }))).toBeNull();
        expect(skillHold(hold({ capability_type: 'a2a_call' }))).toBeNull();
    });

    it('has the skill\'s id, the Resource Group it is to run in, and which settings ask for a person', () => {
        expect(skillHold(hold())).toEqual({
            id: 'sk-3',
            name: null,
            groupId: 'rg-loans',
            requiredBy: ["the Resource Group's skill governance", "the skill's assignment to a Resource Group"],
        });
    });

    it('has a name only when the request carries one that is not the id', () => {
        expect(skillHold(hold())?.name).toBeNull();
        expect(skillHold(hold({ capability_name: 'quarterly-report' }))?.name).toBe('quarterly-report');
        expect(skillHold(hold({ capability_name: 'quarterly-report', server_name: null }))).toMatchObject({ id: null, name: 'quarterly-report' });
    });

    it.each([
        ['group', "the Resource Group's skill governance"],
        ['skill', "the skill's own setting"],
        ['assignment', "the skill's assignment to a Resource Group"],
        ['tenant', 'tenant'],
    ])('reads the source %s as "%s"', (source, words) => {
        expect(skillHold(hold({ request_metadata: { execution_mode: { required_by: [source] } } }))?.requiredBy).toEqual([words]);
    });

    it.each([
        [undefined],
        [null],
        [{}],
        [{ execution_mode: null }],
        [{ execution_mode: { required_by: 'group' } }],
        [{ execution_mode: { required_by: [] } }],
        [{ band: 'high' }],
    ])('names no setting when the metadata (%j) states none: the agent\'s grant or an argument policy asked', (request_metadata) => {
        expect(skillHold(hold({ request_metadata }))?.requiredBy).toEqual([]);
    });

    it('leaves out what is not a name of a source', () => {
        expect(skillHold(hold({ request_metadata: { execution_mode: { required_by: ['skill', 7, null, ''] } } }))?.requiredBy).toEqual(["the skill's own setting"]);
    });
});
