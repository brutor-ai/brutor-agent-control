/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The page shown in place of a screen whose feature is off for the tenant.
 *
 * On its own the console knows only that the tenant's flag is off, and says
 * so. A composition that knows why (the slot `feature-disabled.reason`) is
 * asked, with the flag, and what it answers is shown instead; until it has
 * an answer, and when it has none, the page says what it says on its own.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import { SlotProvider } from '../../../app/SlotProvider';
import type { SlotRegistry } from '../../../app/slots';
import { FeatureDisabled } from '../FeatureDisabled';

afterEach(cleanup);

const page = (slots: SlotRegistry = {}) =>
    render(
        <SlotProvider slots={slots}>
            <FeatureDisabled flag="guardrails" featureName="Guardrails" />
        </SlotProvider>,
    );

describe('FeatureDisabled', () => {
    it('says the feature is not enabled for the tenant when nothing knows more', () => {
        page();
        expect(screen.getByRole('heading')).toHaveTextContent('Guardrails is not enabled');
        expect(screen.getByText(/This feature is not enabled for your tenant/)).toBeInTheDocument();
    });

    it('asks whoever knows why, with the flag and the name, and shows the answer in place of its own words', () => {
        const reason = vi.fn(() => <p>Guardrails is not in the license.</p>);
        page({ 'feature-disabled.reason': [{ id: 'license', render: reason }] });
        expect(reason).toHaveBeenCalledWith(expect.objectContaining({ flag: 'guardrails', featureName: 'Guardrails' }));
        expect(screen.getByText('Guardrails is not in the license.')).toBeInTheDocument();
        expect(screen.queryByText(/This feature is not enabled for your tenant/)).not.toBeInTheDocument();
    });

    it('hands over its own words, for the time the reason is not known', () => {
        page({ 'feature-disabled.reason': [{ id: 'license', render: ({ fallback }) => <>{fallback}</> }] });
        expect(screen.getByRole('heading')).toHaveTextContent('Guardrails is not enabled');
    });
});
