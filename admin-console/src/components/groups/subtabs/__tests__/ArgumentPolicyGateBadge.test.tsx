/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * "N argument policies also gate this tool".
 *
 * The badge exists so the two surfaces that meet at one decision cannot
 * disagree silently: someone loosens an unconditional capability filter, a
 * conditional argument band still bites, and nobody can find why. So the
 * matching has to be exactly right in both directions — it must catch the
 * wildcard target and the tenant-wide (no group) binding, and it must NOT
 * claim a disabled policy or one bound to a different group gates anything.
 *
 * It reads nothing itself: the dialog reads the policies once and hands
 * every row the answer. With no match it renders nothing, and so it does
 * while the policies are not known; a read that failed is the dialog's to
 * say, once, above the rows.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import ArgumentPolicyGateBadge from '../ArgumentPolicyGateBadge';
import type { ArgumentPolicy } from '../../../../lib/argumentPolicyTypes';

function policy(overrides: Partial<ArgumentPolicy> = {}): ArgumentPolicy {
    return {
        id: 'ap-1',
        tenant_id: 't1',
        name: 'Large refunds need approval',
        surface: 'mcp_input',
        target: 'refund',
        argument_key: 'amount',
        analyzer: 'json',
        rules: [],
        enabled: true,
        severity: 'approval_required',
        group_ids: ['rg-1'],
        ...overrides,
    };
}

/** The badge of a row, given the policies its dialog read (null: not known). */
const renderBadge = (policies: ArgumentPolicy[] | null, capability = 'refund', groupId = 'rg-1') =>
    render(<ArgumentPolicyGateBadge capabilityName={capability} groupId={groupId} policies={policies} />);

const badge = (capability = 'refund') => screen.queryByTestId(`argument-policy-gate-${capability}`);

describe('ArgumentPolicyGateBadge — what it counts', () => {
    it('names a single gate in the singular', () => {
        renderBadge([policy()]);
        expect(screen.getByTestId('argument-policy-gate-refund')).toHaveTextContent('1 argument policy also gates this');
    });

    it('counts several, and names them all in the tooltip', () => {
        renderBadge([policy(), policy({ id: 'ap-2', name: 'No prod schema' })]);
        const el = screen.getByTestId('argument-policy-gate-refund');
        expect(el).toHaveTextContent('2 argument policies also gate this');
        expect(el).toHaveAttribute('title', expect.stringContaining('Large refunds need approval, No prod schema'));
    });

    it('a wildcard target gates every tool', () => {
        renderBadge([policy({ target: '*' })], 'anything_at_all');
        expect(screen.getByTestId('argument-policy-gate-anything_at_all')).toBeInTheDocument();
    });

    it('a tenant-wide policy (no groups) gates this group too', () => {
        renderBadge([policy({ group_ids: [] })], 'refund', 'rg-99');
        expect(screen.getByTestId('argument-policy-gate-refund')).toBeInTheDocument();
    });
});

describe('ArgumentPolicyGateBadge — what it must not claim', () => {
    it('says nothing when no policy targets this tool', () => {
        renderBadge([policy({ target: 'other_tool' })]);
        expect(badge()).toBeNull();
    });

    it('a disabled policy gates nothing', () => {
        renderBadge([policy({ enabled: false })]);
        expect(badge()).toBeNull();
    });

    it('a policy bound to another group does not gate this one', () => {
        renderBadge([policy({ group_ids: ['rg-other'] })], 'refund', 'rg-1');
        expect(badge()).toBeNull();
    });

    it('says nothing while the policies are not known: the dialog says a failed read once, above the rows', () => {
        const { container } = renderBadge(null);
        expect(container).toBeEmptyDOMElement();
    });
});

describe('ArgumentPolicyGateBadge — follows the subject', () => {
    it('re-evaluates for a different capability', () => {
        const policies = [policy({ target: 'refund' })];
        const { rerender } = renderBadge(policies, 'refund');
        expect(screen.getByTestId('argument-policy-gate-refund')).toBeInTheDocument();

        rerender(<ArgumentPolicyGateBadge capabilityName="lookup" groupId="rg-1" policies={policies} />);
        expect(badge('refund')).toBeNull();
        expect(badge('lookup')).toBeNull();
    });
});
