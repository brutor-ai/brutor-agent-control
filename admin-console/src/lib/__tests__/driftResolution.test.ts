/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The pure words and arithmetic every drift-resolution surface
 * shares. The classes of bug these guard:
 *
 *   - a close request that carries no kind, no note, or window fields on a
 *     kind that has no window (the control plane refuses them);
 *   - a cause_fixed close whose window bounds are not the ones the operator
 *     saw and edited;
 *   - an epoch lookup that disagrees with the control plane's
 *     `epoch_covers` (wrong metric / basis / caller);
 *   - a closed finding whose effect line names the wrong effect;
 *   - chart shading on the wrong buckets;
 *   - a report section guessed into existence on a report without it.
 */

import { describe, it, expect } from 'vitest';

import {
    closeRequestOf,
    closedEffect,
    epochInForce,
    epochText,
    excludedHoldsText,
    fromLocalInputValue,
    incidentShading,
    incidentWindowCause,
    incidentWindowCauseText,
    proposedWindow,
    readExcludedPeriod,
    readJudgementExclusions,
    toLocalInputValue,
    type CloseDraft,
} from '../driftResolution';
import type { BaselineEpoch, IncidentWindow } from '../driftResolutionTypes';

function epoch(overrides: Partial<BaselineEpoch> = {}): BaselineEpoch {
    return {
        id: 'ep-1',
        ai_system_id: 'sys-1',
        metric_key: 'action_count',
        basis: 'runs',
        caller_system_id: 'all',
        bucket: 'all',
        starts_at: '2026-09-26T06:06:00Z',
        kind: 'accepted_change',
        drift_event_id: 'drift-1',
        reason: 'retune',
        declared_by: 'bob',
        declared_at: '2026-09-28T07:00:00Z',
        record_id: 'rec-1',
        sealed_at: '2026-09-28T07:00:01Z',
        ...overrides,
    };
}

function window_(overrides: Partial<IncidentWindow> = {}): IncidentWindow {
    return {
        id: 'iw-1',
        ai_system_id: 'sys-1',
        tenant_wide: false,
        starts_at: '2026-09-27T21:00:00Z',
        ends_at: '2026-09-28T06:40:00Z',
        cause_category: 'provider_outage',
        reason: 'credits ran out',
        drift_event_id: 'drift-2',
        declared_by: 'alice',
        runs_excluded: 14,
        ...overrides,
    };
}

const draft = (overrides: Partial<CloseDraft> = {}): CloseDraft => ({
    kind: null,
    note: '',
    windowStart: toLocalInputValue('2026-09-27T21:00:00Z'),
    windowEnd: toLocalInputValue('2026-09-28T06:40:00Z'),
    causeCategory: 'provider_outage',
    tenantWide: false,
    ...overrides,
});

describe('closeRequestOf — a close needs a kind and a note', () => {
    it('refuses a draft with no kind, even with a note', () => {
        expect(closeRequestOf(draft({ note: 'fixed' }))).toEqual({ error: expect.any(String) as string });
    });

    it('refuses a blank note on every kind', () => {
        for (const kind of ['accepted_change', 'cause_fixed', 'false_positive'] as const) {
            expect(closeRequestOf(draft({ kind, note: '   ' }))).toEqual({ error: 'A note is required.' });
        }
    });

    it('sends no window fields on accepted_change / false_positive', () => {
        expect(closeRequestOf(draft({ kind: 'accepted_change', note: ' retune ', tenantWide: true }))).toEqual({
            request: { kind: 'accepted_change', note: 'retune' },
        });
        expect(closeRequestOf(draft({ kind: 'false_positive', note: 'noise' }))).toEqual({
            request: { kind: 'false_positive', note: 'noise' },
        });
    });

    it('cause_fixed sends the window bounds the operator saw, the category and the scope', () => {
        expect(closeRequestOf(draft({ kind: 'cause_fixed', note: 'outage', tenantWide: true }))).toEqual({
            request: {
                kind: 'cause_fixed',
                note: 'outage',
                window_starts_at: '2026-09-27T21:00:00.000Z',
                window_ends_at: '2026-09-28T06:40:00.000Z',
                cause_category: 'provider_outage',
                tenant_wide: true,
            },
        });
    });

    it('cause_fixed refuses an empty or inverted window', () => {
        expect('error' in closeRequestOf(draft({ kind: 'cause_fixed', note: 'x', windowEnd: '' }))).toBe(true);
        expect(
            'error' in
                closeRequestOf(
                    draft({ kind: 'cause_fixed', note: 'x', windowStart: draft().windowEnd, windowEnd: draft().windowStart }),
                ),
        ).toBe(true);
    });
});

describe('proposedWindow', () => {
    const now = new Date('2026-09-28T07:00:00Z');

    it('runs from the change point to now', () => {
        expect(proposedWindow([{ change_point_at: '2026-09-27T21:00:00Z', detected_at: '2026-09-28T01:00:00Z' }], now)).toEqual({
            startsAt: '2026-09-27T21:00:00.000Z',
            endsAt: '2026-09-28T07:00:00.000Z',
        });
    });

    it('falls back to the detection time, and takes the earliest member of a group', () => {
        expect(
            proposedWindow(
                [
                    { change_point_at: null, detected_at: '2026-09-28T01:00:00Z' },
                    { change_point_at: '2026-09-27T23:00:00Z', detected_at: '2026-09-28T02:00:00Z' },
                ],
                now,
            ).startsAt,
        ).toBe('2026-09-27T23:00:00.000Z');
    });

    it('round-trips through the datetime-local input at minute precision', () => {
        expect(fromLocalInputValue(toLocalInputValue('2026-09-27T21:00:00Z'))).toBe('2026-09-27T21:00:00.000Z');
    });
});

describe('epochInForce — the control plane epoch_covers rule', () => {
    const epochs = [
        epoch({ id: 'old', starts_at: '2026-09-20T00:00:00Z' }),
        epoch({ id: 'new', starts_at: '2026-09-26T06:06:00Z' }),
        epoch({ id: 'other-metric', metric_key: 'cost_per_run', starts_at: '2026-09-27T00:00:00Z' }),
        epoch({ id: 'segments', basis: 'segments', starts_at: '2026-09-27T00:00:00Z' }),
        epoch({ id: 'caller-x', caller_system_id: 'sys-x', starts_at: '2026-09-27T12:00:00Z' }),
    ];

    it('picks the newest epoch on the same metric and basis', () => {
        expect(epochInForce(epochs, 'action_count', 'runs')?.id).toBe('new');
    });

    it("the aggregate baseline honours only 'all' epochs; a per-caller one honours both", () => {
        expect(epochInForce(epochs, 'action_count', 'runs', 'all')?.id).toBe('new');
        expect(epochInForce(epochs, 'action_count', 'runs', 'sys-x')?.id).toBe('caller-x');
    });

    it('basis defaults to runs and never crosses to segments', () => {
        expect(epochInForce(epochs, 'action_count', undefined)?.id).toBe('new');
        expect(epochInForce(epochs, 'action_count', 'segments')?.id).toBe('segments');
        expect(epochInForce(epochs, 'tool_mix', 'runs')).toBeNull();
    });

    it('says re-learning since … — accepted change by …', () => {
        expect(epochText(epoch())).toMatch(/^re-learning since .+ — accepted change by bob$/);
        expect(epochText(epoch({ kind: 'false_positive' }))).toMatch(/marked not drift by bob$/);
    });

    it('names a platform measurement change and its reason — never a person', () => {
        const text = epochText(
            epoch({
                kind: 'measurement_change',
                declared_by: 'platform',
                measurement_change_id: 'm0001_client_label_product',
                reason: 'The client label became the product name.',
            }),
        );
        expect(text).toMatch(
            /^re-learning since .+ — measurement changed by the platform: The client label became the product name\.$/,
        );
        expect(text).not.toMatch(/accepted change|marked not drift/);
        expect(epochText(epoch({ kind: 'measurement_change', reason: null }))).toMatch(
            /measurement changed by the platform$/,
        );
    });
});

describe('closedEffect — a closed finding names the effect its kind had', () => {
    const base = {
        id: 'drift-1',
        status: 'resolved' as const,
        change_point_at: '2026-09-26T06:06:00Z',
        detected_at: '2026-09-28T07:27:00Z',
    };

    it('accepted change → baseline re-learning since the epoch', () => {
        const r = closedEffect({ ...base, resolution_kind: 'accepted_change' }, [epoch()], []);
        expect(r.label).toBe('Accepted change');
        expect(r.effect).toMatch(/^baseline re-learning since /);
    });

    it('cause fixed → the incident window it declared', () => {
        const r = closedEffect({ ...base, id: 'drift-2', resolution_kind: 'cause_fixed' }, [], [window_()]);
        expect(r.label).toBe('Cause fixed');
        expect(r.effect).toMatch(/^incident window .+ → .+ — its runs are not judged$/);
    });

    it('cause fixed attached to a window another member declared names that window', () => {
        const r = closedEffect(
            { ...base, id: 'drift-3', resolution_kind: 'cause_fixed' },
            [],
            [window_({ drift_event_ids: ['drift-2', 'drift-3'] })],
        );
        expect(r.effect).toMatch(/^incident window .+ — its runs are not judged$/);
    });

    it('cause fixed whose window was withdrawn says so', () => {
        const r = closedEffect({ ...base, resolution_kind: 'cause_fixed' }, [], []);
        expect(r.effect).toMatch(/withdrawn/);
    });

    it('false positive → evidence up to detection is not read again', () => {
        const r = closedEffect({ ...base, status: 'false_positive', resolution_kind: 'false_positive' }, [], []);
        expect(r.label).toBe('False positive');
        expect(r.effect).toMatch(/^evidence up to .+ is not read again/);
    });

    it('a finding closed before the kinds existed claims no effect', () => {
        expect(closedEffect({ ...base, resolution_kind: null }, [epoch()], [])).toEqual({ label: 'Resolved', effect: null });
    });
});

describe('incidentWindowCause — the incident_window attribution', () => {
    it('reads the window the core named', () => {
        const c = incidentWindowCause({
            incident_window_id: 'iw-1',
            tenant_wide: true,
            starts_at: '2026-09-27T21:00:00Z',
            ends_at: null,
            cause_category: 'provider_outage',
            reason: 'credits ran out',
        });
        expect(c).not.toBeNull();
        expect(incidentWindowCauseText(c!)).toMatch(/^Provider outage, tenant-wide · .+ → still open · credits ran out$/);
    });

    it('is null when the evidence names no window', () => {
        expect(incidentWindowCause({ summary: 'x' })).toBeNull();
        expect(incidentWindowCause(null)).toBeNull();
    });
});

describe('incidentShading', () => {
    const hours = ['2026-09-27T20:00:00Z', '2026-09-27T21:00:00Z', '2026-09-27T22:00:00Z', '2026-09-27T23:00:00Z'].map(
        (bucket_start) => ({ bucket_start }),
    );

    it('shades every bucket the half-open window overlaps', () => {
        expect(
            incidentShading([window_({ starts_at: '2026-09-27T21:30:00Z', ends_at: '2026-09-27T23:00:00Z' })], hours, 'hour'),
        ).toEqual([{ id: 'iw-1', x1: '2026-09-27T21:00:00Z', x2: '2026-09-27T22:00:00Z' }]);
    });

    it('an open window runs to the end of the chart; one outside it is dropped', () => {
        expect(incidentShading([window_({ starts_at: '2026-09-27T22:10:00Z', ends_at: null })], hours, 'hour')).toEqual([
            { id: 'iw-1', x1: '2026-09-27T22:00:00Z', x2: '2026-09-27T23:00:00Z' },
        ]);
        expect(
            incidentShading([window_({ starts_at: '2026-09-29T00:00:00Z', ends_at: '2026-09-29T02:00:00Z' })], hours, 'hour'),
        ).toEqual([]);
    });

    it('day buckets are a day wide', () => {
        const days = [{ bucket_start: '2026-09-27T00:00:00Z' }, { bucket_start: '2026-09-28T00:00:00Z' }];
        expect(incidentShading([window_()], days, 'day')).toEqual([
            { id: 'iw-1', x1: '2026-09-27T00:00:00Z', x2: '2026-09-28T00:00:00Z' },
        ]);
    });
});

describe('the report section (spec 1.8)', () => {
    it('reads judgement_exclusions and the coverage statement', () => {
        const report = {
            judgement_exclusions: {
                incident_windows: [{ ...window_(), tenant_wide: undefined, ai_system_id: null }],
                baseline_epochs: [epoch()],
                excluded_hours: 9.7,
                excluded_runs: 14,
            },
            coverage: {
                excluded_period: { incident_windows: 1, excluded_hours: 9.7, excluded_runs: 14, statement: 'unmeasured, not passed' },
            },
        };
        const ex = readJudgementExclusions(report)!;
        expect(ex.incident_windows[0].runs_excluded).toBe(14);
        // A window with no system is tenant-wide even if the flag is absent.
        expect(ex.incident_windows[0].tenant_wide).toBe(true);
        expect(ex.baseline_epochs[0].id).toBe('ep-1');
        expect(readExcludedPeriod(report)?.statement).toBe('unmeasured, not passed');
    });

    it('reads the approval holds a window excluded (spec 1.10) and says what they mean', () => {
        const section = { incident_windows: [], baseline_epochs: [], excluded_hours: 9.7, excluded_runs: 14 };
        expect(readJudgementExclusions({ judgement_exclusions: { ...section, excluded_approval_requests: 250 } })?.excluded_approval_requests).toBe(250);
        // A 1.8/1.9 report has no count — null, never a guessed zero.
        expect(readJudgementExclusions({ judgement_exclusions: section })?.excluded_approval_requests).toBeNull();
        expect(excludedHoldsText(250)).toBe(
            '250 approval request(s) raised inside the windows: recorded, not judged by the oversight rate. ' +
                'Holds raised outside every window count, however they ended.',
        );
        expect(excludedHoldsText(0)).toBeNull();
        expect(excludedHoldsText(null)).toBeNull();
    });

    it('is null on a report without the section', () => {
        expect(readJudgementExclusions({ coverage: {} })).toBeNull();
        expect(readExcludedPeriod({ coverage: { basis: 'x' } })).toBeNull();
    });
});

describe('catalogue_change epochs', () => {
    const changed = epoch({
        id: 'cat', kind: 'catalogue_change', bucket: 'application_processed.loan_approved',
        starts_at: '2026-10-26T09:00:00Z', drift_event_id: null, declared_by: null,
    });

    it('describes a catalogue change as the contract changing what an outcome means', () => {
        expect(epochText(changed)).toMatch(/^re-learning for outcome application_processed\.loan_approved since .* — the declared outcomes changed in a new contract$/);
        expect(epochText({ ...changed, bucket: 'all' })).toMatch(/^re-learning since .* — the declared outcomes changed in a new contract$/);
    });

    it('covers only its own bucket; an epoch on bucket all covers every bucket', () => {
        const all = epoch({ id: 'all', starts_at: '2026-10-01T00:00:00Z' });
        expect(epochInForce([all, changed], 'action_count', 'runs')?.id).toBe('all');
        expect(epochInForce([all, changed], 'action_count', 'runs', null, 'application_processed.loan_approved')?.id).toBe('cat');
        expect(epochInForce([all, changed], 'action_count', 'runs', null, 'application_processed.loan_rejected')?.id).toBe('all');
        expect(epochInForce([changed], 'action_count', 'runs')).toBeNull();
    });
});
