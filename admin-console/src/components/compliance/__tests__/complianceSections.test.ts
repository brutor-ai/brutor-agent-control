/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console catalogue: nine pages in order, each under its own route id.
 */

import { describe, expect, it } from 'vitest';

import { COMPLIANCE_SECTIONS, COMPLIANCE_SECTION_BY_ROUTE, resolveComplianceRoute } from '../complianceSections';

describe('complianceSections catalogue', () => {
    it('has the nine pages in order, with route ids matching the sidebar', () => {
        expect(COMPLIANCE_SECTIONS.map((s) => s.routeId)).toEqual([
            'compliance-overview',
            'compliance-frameworks',
            'compliance-profiles',
            'compliance-obligations',
            'compliance-evidence',
            'compliance-oversight',
            'compliance-transparency',
            'compliance-reports',
            'compliance-incidents',
        ]);
        // Every page is live: nothing in the catalogue marks a phase.
        for (const s of COMPLIANCE_SECTIONS) expect(s).not.toHaveProperty('phase');
        // Sidebar IA: nouns and places only — no ampersands, no gerunds.
        for (const s of COMPLIANCE_SECTIONS) expect(s.label).not.toMatch(/&|ing\b/);
    });

    it('resolves each route id to its page, and any other id to Overview', () => {
        for (const section of COMPLIANCE_SECTIONS) expect(resolveComplianceRoute(section.routeId)).toBe(section);
        expect(resolveComplianceRoute('compliance-evidence').id).toBe('evidence');
        for (const other of ['nonsense', 'compliance', 'eu-ai-act', 'iso42001']) {
            expect(resolveComplianceRoute(other)).toBe(COMPLIANCE_SECTION_BY_ROUTE['compliance-overview']);
        }
    });
});
