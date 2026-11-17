/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The canonical way a Resource Group is named everywhere in the console.
 * The type suffix is dropped when the payload does not carry one — a group
 * labelled with the wrong type reads as a different governance object.
 */

import { describe, it, expect } from 'vitest';
import {
    GROUP_TYPE_LABELS,
    GroupType,
    LIFECYCLE_STAGE_LABELS,
    ROLLUP_EXCLUSION_REASON_LABELS,
    ROLLUP_EXCLUSION_TEMPORARY,
    SYSTEM_KIND_DESCRIPTIONS,
    SYSTEM_KIND_LABELS,
    SystemKind,
    formatGroupLabel,
    groupTypeLabel,
    systemKindLabel,
} from '../groupTypes';

describe('systemKindLabel', () => {
    it('labels every known kind', () => {
        expect(systemKindLabel('agent')).toBe('Agent');
        expect(systemKindLabel(SystemKind.INTEGRATION)).toBe('Integration');
    });

    it('echoes an unknown kind rather than rendering "undefined"', () => {
        expect(systemKindLabel('copilot')).toBe('copilot');
    });

    it('renders nothing at all when there is no kind', () => {
        expect(systemKindLabel(null)).toBe('');
        expect(systemKindLabel(undefined)).toBe('');
        expect(systemKindLabel('')).toBe('');
    });
});

describe('groupTypeLabel', () => {
    it('labels every known group type', () => {
        expect(groupTypeLabel('ai_system')).toBe('AI System');
        expect(groupTypeLabel(GroupType.BUSINESS_UNIT)).toBe('Business Unit');
    });

    it('echoes an unknown type', () => {
        expect(groupTypeLabel('squad')).toBe('squad');
    });

    it('returns null — not an empty string — when there is no type', () => {
        // The caller uses null to decide whether to render the suffix at all.
        expect(groupTypeLabel(null)).toBeNull();
        expect(groupTypeLabel(undefined)).toBeNull();
        expect(groupTypeLabel('')).toBeNull();
    });
});

describe('formatGroupLabel', () => {
    it('prefers the display name and appends the type', () => {
        expect(
            formatGroupLabel({ name: 'fin-ops', display_name: 'Finance Ops', group_type: 'team' }),
        ).toBe('Finance Ops (Team)');
    });

    it('falls back to the raw name when there is no display name', () => {
        expect(formatGroupLabel({ name: 'fin-ops', group_type: 'team' })).toBe('fin-ops (Team)');
        expect(formatGroupLabel({ name: 'fin-ops', display_name: '', group_type: 'team' })).toBe(
            'fin-ops (Team)',
        );
    });

    it('omits the suffix when the payload does not carry a type', () => {
        expect(formatGroupLabel({ display_name: 'Finance Ops' })).toBe('Finance Ops');
        expect(formatGroupLabel({ display_name: 'Finance Ops', group_type: null })).toBe(
            'Finance Ops',
        );
    });

    it('returns an empty string for a missing group rather than throwing', () => {
        expect(formatGroupLabel(null)).toBe('');
        expect(formatGroupLabel(undefined)).toBe('');
        expect(formatGroupLabel({})).toBe('');
    });

    it('still shows the type when only the type is known', () => {
        expect(formatGroupLabel({ group_type: 'ai_system' })).toBe(' (AI System)');
    });
});

describe('label tables', () => {
    it('labels and describes every system kind', () => {
        expect(Object.values(SystemKind).filter((k) => !SYSTEM_KIND_LABELS[k])).toEqual([]);
        expect(Object.values(SystemKind).filter((k) => !SYSTEM_KIND_DESCRIPTIONS[k])).toEqual([]);
    });

    it('labels every group type', () => {
        expect(Object.values(GroupType).filter((t) => !GROUP_TYPE_LABELS[t])).toEqual([]);
    });

    it('gives every lifecycle stage a label', () => {
        const unlabelled = Object.entries(LIFECYCLE_STAGE_LABELS)
            .filter(([, label]) => !label)
            .map(([stage]) => stage);
        expect(unlabelled).toEqual([]);
    });
});

describe('rollup exclusions', () => {
    it('labels every exclusion reason', () => {
        const unlabelled = Object.entries(ROLLUP_EXCLUSION_REASON_LABELS)
            .filter(([, label]) => !label)
            .map(([reason]) => reason);
        expect(unlabelled).toEqual([]);
    });

    it('marks only a subset of reasons as temporary, and all of them are known reasons', () => {
        expect(ROLLUP_EXCLUSION_TEMPORARY.length).toBeGreaterThan(0);
        expect(ROLLUP_EXCLUSION_TEMPORARY.length).toBeLessThan(
            Object.keys(ROLLUP_EXCLUSION_REASON_LABELS).length,
        );
        expect(
            ROLLUP_EXCLUSION_TEMPORARY.filter((r) => !ROLLUP_EXCLUSION_REASON_LABELS[r]),
        ).toEqual([]);
    });
});
