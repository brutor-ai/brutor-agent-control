/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * "N argument policies also gate this tool".
 *
 * Capability filters are the unconditional switch; argument policies are the
 * conditional one. They meet at one decision, so an operator setting a
 * filter has to see the bands that also apply — otherwise someone loosens
 * the filter, the band still bites, and nobody can find why.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

import ArgumentPolicyGateBadge from '../components/groups/subtabs/ArgumentPolicyGateBadge';
import { policyAppliesToGroup, policyTargets } from '../lib/argumentPolicyGates';
import type { ArgumentPolicy } from '../lib/argumentPolicyTypes';

function policy(over: Partial<ArgumentPolicy> = {}): ArgumentPolicy {
    return {
        id: 'ap-1',
        tenant_id: 't',
        name: 'payments-threshold',
        surface: 'mcp_input',
        target: 'transfer_funds',
        argument_key: '*',
        analyzer: 'json',
        rules: [],
        enabled: true,
        severity: 'warn',
        group_ids: ['rg-1'],
        ...over,
    };
}

describe('policyTargets', () => {
    it('matches the named tool', () => {
        expect(policyTargets(policy(), 'transfer_funds')).toBe(true);
        expect(policyTargets(policy(), 'send_email')).toBe(false);
    });

    it('treats a wildcard target as matching anything', () => {
        expect(policyTargets(policy({ target: '*' }), 'anything')).toBe(true);
    });

    it('ignores disabled policies — they gate nothing', () => {
        expect(policyTargets(policy({ enabled: false }), 'transfer_funds')).toBe(false);
    });

    it('ignores policies on another surface', () => {
        expect(
            policyTargets(policy({ surface: 'llm_output_tool_calls' }), 'transfer_funds'),
        ).toBe(false);
    });
});

describe('policyAppliesToGroup', () => {
    it('matches a bound group', () => {
        expect(policyAppliesToGroup(policy(), 'rg-1')).toBe(true);
        expect(policyAppliesToGroup(policy(), 'rg-2')).toBe(false);
    });

    it('treats an unbound policy as tenant-wide', () => {
        expect(policyAppliesToGroup(policy({ group_ids: [] }), 'rg-anything')).toBe(true);
    });
});

describe('ArgumentPolicyGateBadge', () => {
    it('says how many policies also gate the tool', () => {
        render(
            <ArgumentPolicyGateBadge
                capabilityName="transfer_funds"
                groupId="rg-1"
                policies={[policy(), policy({ id: 'ap-2', name: 'currency-allowlist' })]}
            />,
        );
        expect(screen.getByTestId('argument-policy-gate-transfer_funds')).toHaveTextContent('2 argument policies also gate this');
    });

    it('uses the singular for one', () => {
        render(<ArgumentPolicyGateBadge capabilityName="transfer_funds" groupId="rg-1" policies={[policy()]} />);
        expect(screen.getByTestId('argument-policy-gate-transfer_funds')).toHaveTextContent('1 argument policy also gates this');
    });

    it('names the policies on hover so the operator can go find them', () => {
        render(<ArgumentPolicyGateBadge capabilityName="transfer_funds" groupId="rg-1" policies={[policy()]} />);
        expect(screen.getByTestId('argument-policy-gate-transfer_funds').getAttribute('title')).toContain('payments-threshold');
    });

    it('renders nothing when no policy gates the tool', () => {
        const { container } = render(
            <ArgumentPolicyGateBadge capabilityName="transfer_funds" groupId="rg-1" policies={[policy({ target: 'other' })]} />,
        );
        expect(container).toBeEmptyDOMElement();
    });
});
