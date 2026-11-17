/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import ComplexityTierEditor from '../ComplexityTierEditor';
import type { RoutingGroupTierAssignment } from '../../../../lib/llmTypes';

const MODELS = [
    { id: 'm-cheap', name: 'gpt-4o-mini' },
    { id: 'm-big', name: 'claude-sonnet' },
];

function setup(overrides: Partial<React.ComponentProps<typeof ComplexityTierEditor>> = {}) {
    const onChange = vi.fn();
    const onFloorTierChange = vi.fn();
    const props = {
        groupModels: MODELS,
        value: [] as RoutingGroupTierAssignment[],
        onChange,
        floorTier: null,
        onFloorTierChange,
        ...overrides,
    };
    render(<ComplexityTierEditor {...props} />);
    return { onChange, onFloorTierChange };
}

describe('ComplexityTierEditor', () => {
    it('explains that scoring costs no extra model call', () => {
        // The operator's first question is "does this add latency or spend?".
        // The panel must answer it without them having to ask.
        setup();
        expect(screen.getByText(/no extra model call/i)).toBeInTheDocument();
        expect(screen.getByText(/deterministic/i)).toBeInTheDocument();
    });

    it('states the English-keyword limitation rather than hiding it', () => {
        setup();
        expect(
            screen.getByText(/never as a security or policy control/i)
        ).toBeInTheDocument();
    });

    it('offers every tier a model slot', () => {
        setup();
        for (const tier of ['simple', 'medium', 'complex', 'reasoning']) {
            expect(screen.getByTestId(`tier-model-${tier}`)).toBeInTheDocument();
        }
    });

    it('maps a model to a tier', () => {
        const { onChange } = setup();
        fireEvent.change(screen.getByTestId('tier-model-simple'), {
            target: { value: 'm-cheap' },
        });
        expect(onChange).toHaveBeenCalledWith([
            { tier: 'simple', llm_model_id: 'm-cheap', position: 0 },
        ]);
    });

    it('clearing a tier removes it from the map rather than storing a blank', () => {
        const { onChange } = setup({
            value: [{ tier: 'simple', llm_model_id: 'm-cheap', position: 0 }],
        });
        fireEvent.change(screen.getByTestId('tier-model-simple'), {
            target: { value: '' },
        });
        expect(onChange).toHaveBeenCalledWith([]);
    });

    it('replaces rather than duplicates when a tier is remapped', () => {
        const { onChange } = setup({
            value: [{ tier: 'medium', llm_model_id: 'm-cheap', position: 0 }],
        });
        fireEvent.change(screen.getByTestId('tier-model-medium'), {
            target: { value: 'm-big' },
        });
        expect(onChange).toHaveBeenCalledWith([
            { tier: 'medium', llm_model_id: 'm-big', position: 0 },
        ]);
    });

    it('sets and clears the governance floor tier', () => {
        const { onFloorTierChange } = setup();
        const floor = screen.getByTestId('complexity-floor-tier');
        fireEvent.change(floor, { target: { value: 'medium' } });
        expect(onFloorTierChange).toHaveBeenCalledWith('medium');

        fireEvent.change(floor, { target: { value: '' } });
        expect(onFloorTierChange).toHaveBeenCalledWith(null);
    });

    it('tells the operator nothing changes until a tier is mapped', () => {
        // The whole feature is opt-in and inert; an operator who selects the
        // strategy and saves must not be left wondering what just happened.
        setup({ value: [] });
        expect(screen.getByTestId('complexity-fallback-note')).toHaveTextContent(
            /No tiers mapped yet/i
        );
        expect(screen.getByTestId('complexity-fallback-note')).toHaveTextContent(
            /Nothing changes until you map a tier/i
        );
    });

    it('explains the fallback once tiers exist', () => {
        setup({ value: [{ tier: 'simple', llm_model_id: 'm-cheap', position: 0 }] });
        expect(screen.getByTestId('complexity-fallback-note')).toHaveTextContent(
            /falls back to ordinary usage-based balancing/i
        );
    });

    it('directs the operator to add models first when the group is empty', () => {
        setup({ groupModels: [] });
        expect(
            screen.getByText(/Add models to this routing group first/i)
        ).toBeInTheDocument();
        expect(screen.queryByTestId('tier-model-simple')).not.toBeInTheDocument();
    });
});
