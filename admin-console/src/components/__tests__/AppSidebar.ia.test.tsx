/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The sidebar IA contract.
 *
 * These tests pin the *information architecture*, not the pixels:
 *   * six labeled sections + Platform, in frequency order (Compliance is a
 *     section of its own, not a part of Governance);
 *   * every tab id present exactly once — the sections decide labels
 *     and grouping, never routes;
 *   * Governance contains no inventory nouns (systems and the register
 *     belong to the AI Estate);
 *   * the AI Estate holds the four inventory concepts, adjacent;
 *   * the Platform section is system-admin-gated per item.
 */

import { describe, it, expect } from 'vitest';

import { resolveScreen } from '../../app/screens';
import { composeAdmin } from '../../app/extensions';
import { allIds, type AnyNode, findNode } from '../../test/navTree';

// The Community sidebar: the console composed with no extension. What the
// Enterprise extension places in it, and the pins that name its entries, are
// held in `enterprise/src/host/components/__tests__/AppSidebar.ia.test.tsx`.
const { screens: SCREENS, navSections: NAV_SECTIONS } = composeAdmin([]);
const NAV_ITEMS = NAV_SECTIONS.flatMap((s) => s.items);

/** The name of the screen that answers a tab id, or null: it would show the dashboard. */
function screenFor(id: string): string | null {
    return resolveScreen(SCREENS, id)?.name ?? null;
}

describe('section structure', () => {
    it('has the seven sections in frequency order', () => {
        expect(NAV_SECTIONS.map((s) => s.label)).toEqual([
            'Mission Control', 'AI Estate', 'Governance', 'Compliance', 'Operations', 'Administration', 'Platform',
        ]);
    });

    it('AI Estate holds the four inventory concepts, adjacent and in order', () => {
        const estate = NAV_SECTIONS.find((s) => s.label === 'AI Estate')!;
        expect(estate.items.map((i) => i.id)).toEqual([
            'ai-systems', 'groups', 'resources', 'ai-assets',
        ]);
    });

    it('Compliance is one item whose leaves are the console pages', () => {
        const section = NAV_SECTIONS.find((s) => s.label === 'Compliance')!;
        expect(section.items.map((i) => i.id)).toEqual(['compliance']);
        const item = section.items[0];
        expect(item.requiresTenantAdmin).toBe(true);
        expect(item.requiresPermission).toBe('compliance:read');
        expect(item.children!.map((c) => [c.id, c.label])).toEqual([
            ['compliance-overview', 'Overview'],
            ['compliance-frameworks', 'Frameworks'],
            ['compliance-profiles', 'Profiles'],
            ['compliance-obligations', 'Obligations'],
            ['compliance-evidence', 'Evidence Ledger'],
            ['compliance-oversight', 'Human Oversight'],
            ['compliance-transparency', 'Transparency'],
            ['compliance-reports', 'Reports'],
            ['compliance-incidents', 'Incidents'],
        ]);
        // Nouns and places only — no verbs, gerunds or ampersands.
        for (const c of item.children!) {
            expect(c.label, c.id).not.toMatch(/&|ing\b/);
        }
    });
});

describe('relabels are label-only', () => {
    const LABELS: Record<string, string> = {
        inbox: 'Inbox',
        'mission-control': 'Overview',
        'mission-control-metrics': 'Analytics',
        'ai-assets': 'Asset Register',
        'monitoring': 'Logs & Audit',
        'config': 'Settings',
    };
    for (const [id, label] of Object.entries(LABELS)) {
        it(`${id} reads "${label}"`, () => {
            expect(findNode(id, NAV_ITEMS as AnyNode[])!.label).toBe(label);
        });
    }

    it('no gerunds, ampersand-free except the deliberate pairs', () => {
        // "Logs & Audit" is the one deliberate pair; nothing else keeps one.
        const labels = NAV_SECTIONS.flatMap((s) => s.items.map((i) => i.label));
        const withAmp = labels.filter((l) => l.includes('&'));
        expect(withAmp).toEqual(['Logs & Audit']);
    });
});

describe('every Compliance page has a route', () => {
    // As for the System console below: every sidebar leaf id must be
    // claimed by the Compliance screen (`app/screens.tsx`). The parent
    // entry only expands, and no screen answers its id.
    it('the Compliance screen answers every compliance-* nav id, and nothing answers the parent', () => {
        const compliance = NAV_ITEMS.find((i) => i.id === 'compliance')!;
        expect((compliance.children ?? []).length).toBeGreaterThan(0);
        for (const child of compliance.children ?? []) {
            expect(screenFor(child.id), `no screen answers ${child.id}`).toBe('compliance');
        }
        expect(screenFor('compliance')).toBeNull();
    });
});

describe('every Governance leaf has a route', () => {
    // Incident Windows is a Governance leaf; a nav id that no
    // screen answers falls through to the dashboard.
    it('incident-windows has a screen and sits under Governance', () => {
        expect(screenFor('incident-windows')).toBe('incident-windows');
        const gov = NAV_SECTIONS.find((s) => s.label === 'Governance')!;
        expect(allIds(gov.items)).toContain('incident-windows');
    });
});

describe('every System sub-section has a route', () => {
    // A sidebar id with no route falls through to the default and lands on
    // the dashboard (clicking Audit Chains would open the dashboard). Every
    // sidebar child must be claimed by the System console screen.
    it('the System screen answers every system-* nav id', () => {
        const system = NAV_ITEMS.find((i) => i.id === 'system')!;
        expect((system.children ?? []).length).toBeGreaterThan(0);
        for (const child of system.children ?? []) {
            expect(screenFor(child.id), `no screen answers ${child.id}`).toBe('system');
        }
        expect(screenFor('system')).toBeNull();
    });
});

describe('every tab id appears once', () => {
    it('no id is used by two entries of the sidebar', () => {
        const ids = allIds(NAV_ITEMS);
        expect(ids.length).toBeGreaterThan(50);
        expect(ids.filter((id, i) => ids.indexOf(id) !== i)).toEqual([]);
    });
});
