/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * An approval timeout is what the Control Plane accepts: a whole number of
 * seconds from 1 to 604800 (7 days). The forms say so beside the field
 * before a save is refused for it.
 */

import { describe, expect, it } from 'vitest';

import { APPROVAL_TIMEOUT_MAX_SECONDS, APPROVAL_TIMEOUT_MIN_SECONDS, APPROVAL_TIMEOUT_RANGE, approvalTimeoutProblem, governanceApprovalTimeoutProblem } from '../approvalTimeout';

describe('approvalTimeoutProblem', () => {
    it('has the bounds of the contract', () => {
        expect(APPROVAL_TIMEOUT_MIN_SECONDS).toBe(1);
        expect(APPROVAL_TIMEOUT_MAX_SECONDS).toBe(604800);
        expect(APPROVAL_TIMEOUT_RANGE).toBe('An approval timeout is a whole number of seconds from 1 to 604800 (7 days).');
    });

    it('accepts no timeout, and every whole number of seconds within the bounds', () => {
        for (const value of [undefined, null, 1, 300, 604800]) expect(approvalTimeoutProblem(value)).toBeNull();
    });

    it('refuses zero, a negative number, a fraction, more than seven days, and what is not a number', () => {
        for (const value of [0, -5, 1.5, 604801, Number.NaN, '300']) expect(approvalTimeoutProblem(value)).toBe(APPROVAL_TIMEOUT_RANGE);
    });
});

describe('governanceApprovalTimeoutProblem', () => {
    it('finds the timeout wherever a policy holds one: at the top, or in a section', () => {
        expect(governanceApprovalTimeoutProblem({ approval_timeout_seconds: 0 })).toBe(APPROVAL_TIMEOUT_RANGE);
        expect(governanceApprovalTimeoutProblem({ capability: { default_access: 'approval_required', approval_timeout_seconds: 604801 } })).toBe(APPROVAL_TIMEOUT_RANGE);
        expect(governanceApprovalTimeoutProblem({ execution_mode: 'approval_required', approval_timeout_seconds: 1.5 })).toBe(APPROVAL_TIMEOUT_RANGE);
    });

    it('has nothing to say of a timeout in range, or of a policy without one', () => {
        expect(governanceApprovalTimeoutProblem({ approval_timeout_seconds: 300 })).toBeNull();
        expect(governanceApprovalTimeoutProblem({ capability: { approval_timeout_seconds: 604800 } })).toBeNull();
        expect(governanceApprovalTimeoutProblem({ capability: { approval_timeout_seconds: null } })).toBeNull();
        expect(governanceApprovalTimeoutProblem({ remote: { allowed_globs: ['x'] } })).toBeNull();
        expect(governanceApprovalTimeoutProblem({})).toBeNull();
        expect(governanceApprovalTimeoutProblem(null)).toBeNull();
        expect(governanceApprovalTimeoutProblem(undefined)).toBeNull();
    });
});
