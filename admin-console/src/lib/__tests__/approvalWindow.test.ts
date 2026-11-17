/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console mirrors the control plane's approval-window rules:
 * 60 s .. 7 days, and a group may only shorten the window it inherits.
 */

import { describe, it, expect } from 'vitest';

import {
    APPROVAL_WINDOW_MAX_SECONDS,
    APPROVAL_WINDOW_MIN_SECONDS,
    PLATFORM_DEFAULT_APPROVAL_WINDOW_SECONDS,
    effectiveText,
    windowProblem,
    windowText,
} from '../approvalWindow';

const W = {
    group_id: 'g',
    group_name: 'g',
    approval_window_seconds: null,
    inherited_window_seconds: null,
    inherited_from_group_id: null,
    inherited_from_group_name: null,
    tenant_default_seconds: null,
    platform_default_seconds: 300,
    effective_seconds: 300,
    effective_source: 'platform',
    min_seconds: 60,
    max_seconds: 604800,
};

describe('approval window rules', () => {
    it('bounds are the control plane’s', () => {
        expect(APPROVAL_WINDOW_MIN_SECONDS).toBe(60);
        expect(APPROVAL_WINDOW_MAX_SECONDS).toBe(604800);
        expect(PLATFORM_DEFAULT_APPROVAL_WINDOW_SECONDS).toBe(300);
    });

    it('null inherits; out of range and longer-than-inherited are refused', () => {
        expect(windowProblem(null, 3600)).toBeNull();
        expect(windowProblem(30, null)).toMatch(/between/);
        expect(windowProblem(604801, null)).toMatch(/between/);
        expect(windowProblem(7200, 3600)).toMatch(/only shorten/);
        expect(windowProblem(3600, 3600)).toBeNull();
        expect(windowProblem(86400, null)).toBeNull();
    });

    it('explains where the effective window comes from', () => {
        expect(effectiveText(W)).toMatch(/the platform default\.$/);
        expect(effectiveText({ ...W, effective_seconds: 7200, effective_source: 'tenant' })).toMatch(/2 h 00 m — the tenant default/);
        expect(
            effectiveText({ ...W, approval_window_seconds: 86400, effective_seconds: 86400, effective_source: 'resource_group' }),
        ).toMatch(/set on this group\.$/);
        expect(windowText(86400)).toBe('1 d 0 h');
    });
});
