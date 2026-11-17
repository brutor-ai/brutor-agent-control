/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tenant settings — automatic assurance capture.
 *
 * The scheduler reads the setting, so it has to be writable: with no control
 * for it, scheduled capture never runs and "continuous evidence" is only
 * true when a human remembers to click.
 *
 * These tests pin the two behaviours that make it reachable:
 *
 *   (1) the current value loads into the control, so an operator can see what
 *       is configured rather than an empty box over a set value;
 *   (2) the touched-gate — the interval is sent ONLY when edited on this tab,
 *       and clearing it sends an explicit `null` rather than being dropped.
 *
 * (2) is the subtle one. `update_tenant` builds its payload with
 * `exclude_none`, so a cleared value that is not explicitly re-added silently
 * vanishes and the setting can be switched on but never off.
 */

import { describe, expect, it } from 'vitest';

/**
 * The payload rule under test, extracted so it can be asserted without
 * mounting the whole settings tab (which fans out to several endpoints).
 * Mirrors the `captureTouched` branch in TenantSettingsTab.handleSave.
 */
export function buildCapturePayload(
    captureTouched: boolean,
    captureHours: string,
): Record<string, unknown> {
    const payload: Record<string, unknown> = {};
    if (captureTouched) {
        const raw = captureHours.trim();
        payload.assurance_capture_interval_hours = raw ? Number(raw) : null;
    }
    return payload;
}

/** Mirrors the load branch: column value → control value. */
export function captureHoursFromTenant(v: number | null | undefined): string {
    return v != null ? String(v) : '';
}

describe('assurance capture — loading current state', () => {
    it('shows the configured interval', () => {
        expect(captureHoursFromTenant(24)).toBe('24');
    });

    it('shows empty when capture is off', () => {
        expect(captureHoursFromTenant(null)).toBe('');
        expect(captureHoursFromTenant(undefined)).toBe('');
    });

    it('does not confuse a valid interval with absence', () => {
        // A plain falsy check would render 0 as "off". 0 is rejected by the
        // API (ge=1) but must not be silently reinterpreted here either.
        expect(captureHoursFromTenant(0)).toBe('0');
    });
});

describe('assurance capture — the touched gate', () => {
    it('omits the field entirely when the tab was not edited', () => {
        // Saving from another sub-tab must not round-trip this value: doing so
        // masks a concurrent edit by another admin and stamps the audit log
        // with a no-op change.
        expect(buildCapturePayload(false, '24')).toEqual({});
    });

    it('sends the number when set', () => {
        expect(buildCapturePayload(true, '24')).toEqual({
            assurance_capture_interval_hours: 24,
        });
    });

    it('sends an explicit null when cleared — turning capture OFF', () => {
        // The whole point: without an explicit null the backend's
        // `exclude_none` drops the clear and the operator cannot switch
        // scheduled capture back off once enabled.
        expect(buildCapturePayload(true, '')).toEqual({
            assurance_capture_interval_hours: null,
        });
    });

    it('treats whitespace as cleared', () => {
        expect(buildCapturePayload(true, '   ')).toEqual({
            assurance_capture_interval_hours: null,
        });
    });

    it('sends a number, not a string', () => {
        // The column is Integer; a string would be coerced by Pydantic on a
        // good day and rejected on a bad one.
        const payload = buildCapturePayload(true, '168');
        expect(typeof payload.assurance_capture_interval_hours).toBe('number');
    });
});
