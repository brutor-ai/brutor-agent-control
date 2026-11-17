/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GroupType and SystemKind.
 *
 * These are drift guards. application / agent / assistant / service are
 * not GroupType members: they would express as a *group type*
 * what is really a *kind of AI System*; if any of them appears the
 * platform has two ways of saying "this is an agent" and the
 * lifecycle engine has to guess which one an operator meant.
 *
 * The enum values are also wire values — the control plane validates
 * against exactly these strings — so a rename here is an API change, not
 * a refactor.
 */

import { describe, it, expect } from 'vitest';

import {
    GroupType,
    GROUP_TYPE_LABELS,
    SystemKind,
    SYSTEM_KIND_LABELS,
    SYSTEM_KIND_DESCRIPTIONS,
    systemKindLabel,
} from '../groupTypes';
import {
    GROUP_TYPE_DEFAULT_ICONS,
    SYSTEM_KIND_DEFAULT_ICONS,
    resolveGroupIcon,
    getIconEntry,
} from '../lucideIconMap';

const REMOVED_GROUP_TYPES = ['application', 'agent', 'assistant', 'service'];

describe('GroupType', () => {
    it('no longer carries the AI/technical member types', () => {
        const values = Object.values(GroupType) as string[];
        for (const removed of REMOVED_GROUP_TYPES) {
            expect(values).not.toContain(removed);
        }
    });

    it('still carries ai_system and custom', () => {
        expect(Object.values(GroupType)).toContain('ai_system');
        expect(Object.values(GroupType)).toContain('custom');
    });

    it('has a label for every remaining member', () => {
        for (const type of Object.values(GroupType)) {
            expect(GROUP_TYPE_LABELS[type]).toBeTruthy();
        }
    });

    it('has no orphan labels for removed members', () => {
        const labelled = Object.keys(GROUP_TYPE_LABELS);
        for (const removed of REMOVED_GROUP_TYPES) {
            expect(labelled).not.toContain(removed);
        }
    });

    it('has no orphan default icons for removed members', () => {
        for (const removed of REMOVED_GROUP_TYPES) {
            expect(GROUP_TYPE_DEFAULT_ICONS[removed]).toBeUndefined();
        }
    });
});

describe('SystemKind', () => {
    it('pins the wire values the control plane validates against', () => {
        expect(Object.values(SystemKind).sort()).toEqual([
            'agent',
            'application',
            'assistant',
            'integration',
            'service',
            'workflow',
        ]);
    });

    it('has a label and a description for every kind', () => {
        for (const kind of Object.values(SystemKind)) {
            expect(SYSTEM_KIND_LABELS[kind]).toBeTruthy();
            expect(SYSTEM_KIND_DESCRIPTIONS[kind]).toBeTruthy();
        }
    });

    it('maps every kind to an icon that actually exists in the registry', () => {
        for (const kind of Object.values(SystemKind)) {
            const iconKey = SYSTEM_KIND_DEFAULT_ICONS[kind];
            expect(iconKey, `no default icon for kind ${kind}`).toBeTruthy();
            expect(
                getIconEntry(iconKey),
                `icon key ${iconKey} for kind ${kind} is not registered`
            ).toBeDefined();
        }
    });

    it('falls back to the raw value for an unknown kind', () => {
        expect(systemKindLabel('agent')).toBe('Agent');
        expect(systemKindLabel('something-new')).toBe('something-new');
        expect(systemKindLabel(null)).toBe('');
        expect(systemKindLabel(undefined)).toBe('');
    });
});

describe('resolveGroupIcon with system kinds', () => {
    it('prefers the kind icon over the generic ai_system icon', () => {
        const generic = resolveGroupIcon(undefined, GroupType.AI_SYSTEM);
        const agent = resolveGroupIcon(undefined, GroupType.AI_SYSTEM, SystemKind.AGENT);
        expect(agent).not.toBe(generic);
    });

    it('still honours an explicit custom icon over the kind', () => {
        const custom = resolveGroupIcon('rocket', GroupType.AI_SYSTEM, SystemKind.AGENT);
        const kindOnly = resolveGroupIcon(undefined, GroupType.AI_SYSTEM, SystemKind.AGENT);
        expect(custom).not.toBe(kindOnly);
    });

    it('ignores a kind that has no icon mapping and falls back to the group type', () => {
        const fallback = resolveGroupIcon(undefined, GroupType.AI_SYSTEM, 'not-a-kind');
        const generic = resolveGroupIcon(undefined, GroupType.AI_SYSTEM);
        expect(fallback).toBe(generic);
    });

    it('is unaffected for non-AI-System group types', () => {
        const before = resolveGroupIcon(undefined, GroupType.TEAM);
        const after = resolveGroupIcon(undefined, GroupType.TEAM, undefined);
        expect(before).toBe(after);
    });
});
