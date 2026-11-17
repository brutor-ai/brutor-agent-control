/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The hierarchy tree must say where limits and policy actually live.
 *
 * With counts for members, servers, models and keys alone, a department
 * that caps every model beneath it looks exactly like one that governs
 * nothing. A filled mark means this group
 * carries a document of its own; an outline means it only inherits; nothing
 * means it is ungoverned on that axis, which is itself the thing worth
 * seeing at a glance.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

import GovernanceTreeBadges from '../components/groups/GovernanceTreeBadges';

describe('GovernanceTreeBadges', () => {
    it('marks a group that sets both of its own', () => {
        render(<GovernanceTreeBadges node={{ sets_own_limits: true, sets_own_policy: true }} />);
        expect(screen.getByTestId('tree-limits-own')).toBeInTheDocument();
        expect(screen.getByTestId('tree-policy-own')).toBeInTheDocument();
    });

    it('marks a group that only inherits differently from one that sets its own', () => {
        render(
            <GovernanceTreeBadges
                node={{ inherits_limits: true, sets_own_policy: true }}
            />,
        );
        const inherited = screen.getByTestId('tree-limits-inherited');
        const own = screen.getByTestId('tree-policy-own');
        expect(inherited).toBeInTheDocument();
        expect(own).toBeInTheDocument();
        expect(inherited.className).not.toEqual(own.className);
    });

    it('shows nothing on an axis where nothing governs the group', () => {
        render(<GovernanceTreeBadges node={{ sets_own_limits: true }} />);
        expect(screen.getByTestId('tree-limits-own')).toBeInTheDocument();
        expect(screen.queryByTestId('tree-policy-own')).not.toBeInTheDocument();
        expect(screen.queryByTestId('tree-policy-inherited')).not.toBeInTheDocument();
    });

    it('prefers "sets its own" over "inherits" when both are true', () => {
        render(
            <GovernanceTreeBadges
                node={{ sets_own_limits: true, inherits_limits: true }}
            />,
        );
        expect(screen.getByTestId('tree-limits-own')).toBeInTheDocument();
        expect(screen.queryByTestId('tree-limits-inherited')).not.toBeInTheDocument();
    });

    it('renders nothing at all for an ungoverned group', () => {
        const { container } = render(<GovernanceTreeBadges node={{}} />);
        expect(container).toBeEmptyDOMElement();
    });

    it('explains each mark in words for hover and screen readers', () => {
        render(
            <GovernanceTreeBadges
                node={{ sets_own_limits: true, inherits_policy: true }}
            />,
        );
        expect(screen.getByLabelText('Sets its own limits')).toBeInTheDocument();
        expect(screen.getByLabelText('Inherits policy from a parent group')).toBeInTheDocument();
    });

    it('marks nothing on a node that states no flag', () => {
        const { container } = render(
            <GovernanceTreeBadges node={{ sets_own_limits: undefined }} />,
        );
        expect(container).toBeEmptyDOMElement();
    });
});
