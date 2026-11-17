/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two governance marks on a tree node.
 *
 * The tree answers "where does governance live", so each axis has exactly
 * three readings and they must not blur into each other: a filled mark
 * (this group carries a document of its own), an outline (it only inherits
 * one), and nothing at all (ungoverned on that axis — itself the fact an
 * operator is scanning for). "Own" outranks "inherits": a group that sets
 * its own limits AND inherits some still reads as a setter, because that is
 * where an operator would go to change them.
 *
 * The title/aria text is the only explanation a mark gets, so it is part of
 * the contract, not decoration.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import GovernanceTreeBadges from '../GovernanceTreeBadges';

describe('GovernanceTreeBadges', () => {
    it('an ungoverned node carries no marks at all', () => {
        const { container } = render(<GovernanceTreeBadges node={{}} />);
        expect(container).toBeEmptyDOMElement();
    });

    it('a group that sets its own limits reads as a setter on that axis only', () => {
        render(<GovernanceTreeBadges node={{ sets_own_limits: true }} />);
        expect(screen.getByTestId('tree-limits-own')).toBeInTheDocument();
        expect(screen.queryByTestId('tree-policy-own')).toBeNull();
        expect(screen.queryByTestId('tree-policy-inherited')).toBeNull();
    });

    it('an inheriting group reads as inherited, not as a setter', () => {
        render(<GovernanceTreeBadges node={{ inherits_limits: true, inherits_policy: true }} />);
        expect(screen.getByTestId('tree-limits-inherited')).toBeInTheDocument();
        expect(screen.getByTestId('tree-policy-inherited')).toBeInTheDocument();
        expect(screen.queryByTestId('tree-limits-own')).toBeNull();
    });

    it('setting your own outranks also inheriting', () => {
        render(<GovernanceTreeBadges node={{ sets_own_policy: true, inherits_policy: true }} />);
        expect(screen.getByTestId('tree-policy-own')).toBeInTheDocument();
        expect(screen.queryByTestId('tree-policy-inherited')).toBeNull();
    });

    it('the two axes are read independently', () => {
        render(<GovernanceTreeBadges node={{ sets_own_limits: true, inherits_policy: true }} />);
        expect(screen.getByTestId('tree-limits-own')).toBeInTheDocument();
        expect(screen.getByTestId('tree-policy-inherited')).toBeInTheDocument();
    });

    it('each mark explains itself for a mouse and for a screen reader', () => {
        render(<GovernanceTreeBadges node={{ sets_own_limits: true, inherits_policy: true }} />);
        expect(screen.getByLabelText('Sets its own limits')).toBeInTheDocument();
        expect(screen.getByLabelText('Inherits policy from a parent group')).toBeInTheDocument();
        expect(screen.getByTestId('tree-limits-own')).toHaveAttribute('title', 'Sets its own limits');
    });
});
