/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The probe-type badge qualifies the health badge sitting next to it. Its whole
 * job is to stop an operator reading a green "Healthy" as "verified reachable"
 * when the only thing that happened was Brutor-side validation.
 *
 * What must hold:
 *   - each probe kind names itself, and the tooltip carries the caveat;
 *   - an unprobed model renders nothing at all rather than an empty chip;
 *   - a probe kind the backend added after this build renders nothing rather
 *     than crashing on `config.label`.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import LLModelProbeTypeBadge from '../LLModelProbeTypeBadge';
import type { ProbeType } from '../../../../lib/llmTypes';

describe('LLModelProbeTypeBadge', () => {
    it.each([
        ['catalog', 'Catalog Probe'],
        ['endpoint', 'Endpoint Probe'],
        ['synthetic', 'Synthetic Probe'],
    ])('labels the %s probe as "%s"', (probe, label) => {
        render(<LLModelProbeTypeBadge probeType={probe as ProbeType} />);
        expect(screen.getByText(label)).toBeInTheDocument();
    });

    it('spells out that a synthetic probe never reached the provider', () => {
        // This is the caveat the whole component exists for.
        const { container } = render(<LLModelProbeTypeBadge probeType="synthetic" />);
        const chip = container.querySelector('[title]');
        expect(chip?.getAttribute('title')).toMatch(/never reached/i);
        expect(chip?.getAttribute('title')).toMatch(/tentative/i);
    });

    it('says a catalog probe verified the model exists provider-side', () => {
        const { container } = render(<LLModelProbeTypeBadge probeType="catalog" />);
        expect(container.querySelector('[title]')?.getAttribute('title')).toMatch(
            /verified in the listing/i
        );
    });

    it('renders nothing for a model that has never been probed', () => {
        const { container } = render(<LLModelProbeTypeBadge probeType={null} />);
        expect(container).toBeEmptyDOMElement();
    });

    it('renders nothing when the prop is omitted entirely', () => {
        const { container } = render(<LLModelProbeTypeBadge />);
        expect(container).toBeEmptyDOMElement();
    });

    it('renders nothing — rather than crashing — for an unrecognised probe kind', () => {
        const { container } = render(
            <LLModelProbeTypeBadge probeType={'tracer' as ProbeType} />
        );
        expect(container).toBeEmptyDOMElement();
    });

    it('drops the label but keeps the tooltip in icon-only mode', () => {
        const { container } = render(
            <LLModelProbeTypeBadge probeType="endpoint" showLabel={false} />
        );
        expect(screen.queryByText('Endpoint Probe')).not.toBeInTheDocument();
        expect(container.querySelector('[title]')).toBeInTheDocument();
    });
});
