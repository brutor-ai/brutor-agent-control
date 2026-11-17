/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for `bucketRequestsBySurface` — the helper that rolls raw
 * `proxy_metrics.proxy_type` counts into the four display buckets
 * (LLM / MCP / Portal / Other) used by Mission Control's
 * "Total Requests" KPI subtitle and the "Requests by Surface"
 * pie chart. A misclassification here breaks both views, so the
 * mapping is pinned with explicit cases for every known subtype.
 */

import { describe, it, expect } from 'vitest';
import { bucketRequestsBySurface } from '../proxyTypeBuckets';


describe('bucketRequestsBySurface', () => {
    it('returns all-zero buckets for null/undefined input', () => {
        expect(bucketRequestsBySurface(null)).toEqual({
            llm: 0, mcp: 0, portal: 0, other: 0,
        });
        expect(bucketRequestsBySurface(undefined)).toEqual({
            llm: 0, mcp: 0, portal: 0, other: 0,
        });
        expect(bucketRequestsBySurface({})).toEqual({
            llm: 0, mcp: 0, portal: 0, other: 0,
        });
    });

    it('rolls llm + batch + skill into the LLM bucket', () => {
        const out = bucketRequestsBySurface({ llm: 50, batch: 10, skill: 3 });
        expect(out.llm).toBe(63);
        expect(out.mcp).toBe(0);
        expect(out.portal).toBe(0);
        expect(out.other).toBe(0);
    });

    it('rolls portal + oauth into the Portal bucket', () => {
        // The 27-row gap that prompted this work: oauth token
        // exchanges (MCP server auth) plus portal session refreshes
        // both write proxy_metrics but no usage_logs, so they
        // appear in tenant total but not per-group totals. The
        // pie + KPI surface them as a single "Portal" slice.
        const out = bucketRequestsBySurface({ portal: 15, oauth: 13 });
        expect(out.portal).toBe(28);
        expect(out.llm).toBe(0);
        expect(out.mcp).toBe(0);
        expect(out.other).toBe(0);
    });

    it('puts mcp on its own', () => {
        const out = bucketRequestsBySurface({ mcp: 17 });
        expect(out.mcp).toBe(17);
        expect(out.llm).toBe(0);
    });

    it('routes unknown subtypes to Other so nothing silently disappears', () => {
        // A future Rust-side `proxy_type` we don't yet recognise
        // (say "agent" if A2A starts writing proxy_metrics) must
        // land in the Other bucket — never get dropped from the
        // total. Pin that here.
        const out = bucketRequestsBySurface({ llm: 5, agent: 2, ws: 1 });
        expect(out.llm).toBe(5);
        expect(out.other).toBe(3);
    });

    it('ignores zero-valued and missing keys', () => {
        const out = bucketRequestsBySurface({ llm: 10, batch: 0 });
        expect(out.llm).toBe(10);
        expect(out.other).toBe(0);
    });

    it('matches the user-reported example shape (LLM 63 / MCP 17 / Portal 28)', () => {
        // Pin the headline scenario. Proves the helper produces
        // the breakdown the KPI subtitle is documented to show.
        const out = bucketRequestsBySurface({
            llm: 60, batch: 2, skill: 1,   // 63
            mcp: 17,
            portal: 28,
        });
        expect(out).toEqual({ llm: 63, mcp: 17, portal: 28, other: 0 });
    });
});
