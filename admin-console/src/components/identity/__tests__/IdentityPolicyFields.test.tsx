/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The minimum credential level a policy may require offers the levels a
 * credential can reach. The bound key level is not one yet: a policy at it
 * would refuse every API key. A stored policy that holds it still shows it.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import IdentityPolicyFields from '../IdentityPolicyFields';
import { toDraft } from '../policyDraft';
import { DEVELOPMENT_DEFAULT } from '../../../lib/identityPolicy';

const renderFields = (min_assurance: (typeof DEVELOPMENT_DEFAULT)['min_assurance']) =>
    render(
        <IdentityPolicyFields
            draft={toDraft({ ...DEVELOPMENT_DEFAULT, min_assurance })}
            onChange={vi.fn()}
            fieldError={() => null}
            issuers={[]}
            idPrefix="t"
        />,
    );

const levelOptions = () => within(screen.getByLabelText('Minimum credential level')).getAllByRole('option').map((option) => option.getAttribute('value'));

describe('IdentityPolicyFields: the minimum credential level', () => {
    it('offers key, workload and workload with proof of possession', () => {
        renderFields('key');
        expect(levelOptions()).toEqual(['key', 'workload', 'workload_pop']);
    });

    it('shows the bound key level where the stored policy holds it', () => {
        renderFields('bound_key');
        expect(levelOptions()).toEqual(['key', 'bound_key', 'workload', 'workload_pop']);
        expect(screen.getByLabelText('Minimum credential level')).toHaveValue('bound_key');
    });
});
