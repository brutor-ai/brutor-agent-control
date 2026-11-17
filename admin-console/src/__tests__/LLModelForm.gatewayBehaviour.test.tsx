/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the model form says the gateway does with a model's settings: which
 * parameters it drops, what the per-minute limits apply to, and that a
 * model of a disabled provider is not served.
 */

import { describe, expect, it, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import type React from 'react';
import LLModelForm from '../components/llm/config/LLModelForm';
import { STORED_KEPT, STORED_WITH_REFUSED, controlPlaneRefusal } from '../test/controlPlaneRefusal';

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient: {}, groupTree: [] }) }));

const providers = vi.hoisted(() => ({
    list: [] as Array<Record<string, unknown>>,
}));
vi.mock('../hooks/useLlmProviders', () => ({ useLlmProviders: () => ({ providers: providers.list, loading: false }) }));

const baseModel = {
    id: 'llm-existing',
    tenant_id: 't1',
    name: 'Existing Model',
    description: 'desc',
    provider: 'openai',
    model_name: 'gpt-test',
    mode: 'chat',
    api_url_auto_discovered: true,
    api_url: '',
    auth_type: 'api_key',
    default_temperature: 0.7,
    default_max_tokens: 4096,
    timeout: 60,
    enabled: true,
    health_status: { status: 'unknown' },
    performance_metrics: {},
    created_at: '2026-05-13T00:00:00Z',
    updated_at: '2026-05-13T00:00:00Z',
};

type FormModel = React.ComponentProps<typeof LLModelForm>['model'];

function renderForm(over: Record<string, unknown> | null = {}) {
    const onSave = vi.fn(() => Promise.resolve(undefined));
    const model = (over === null ? null : { ...baseModel, ...over }) as unknown as FormModel;
    render(<LLModelForm model={model} onSave={onSave} onClose={vi.fn()} />);
    return { onSave };
}
const tab = (name: RegExp) => fireEvent.click(screen.getByRole('button', { name }));
const providerSwitch = () => screen.getByLabelText<HTMLInputElement>("Apply the provider's dropped-parameters list");

afterEach(() => {
    cleanup();
    providers.list = [];
});

describe('LLModelForm: dropped parameters', () => {
    it("names the switch for what it does, and says the model's own list always applies", () => {
        providers.list = [{ id: 'p1', key: 'openai', display_name: 'OpenAI', enabled: true, additional_drop_params: ['logit_bias'] }];
        renderForm();
        tab(/Param Control/);
        expect(providerSwitch()).toBeTruthy();
        expect(screen.getByTestId('drop-params-help')).toHaveTextContent(
            "On: the gateway removes the parameters on the provider's list from every request to this model. Off: the provider's list is not applied to this model. The model's own list below always applies, whatever this switch says.",
        );
        expect(screen.queryByText(/catch-all/i)).toBeNull();
        expect(screen.queryByText(/Drop unsupported parameters/)).toBeNull();
    });

    it("is on for a model that states nothing, as the gateway reads it", () => {
        renderForm({ drop_params: undefined });
        tab(/Param Control/);
        expect(providerSwitch().checked).toBe(true);
    });

    it('is on for a new model', () => {
        renderForm(null);
        tab(/Param Control/);
        expect(providerSwitch().checked).toBe(true);
    });

    it('shows a stored "off" as off, and says the provider list is then not applied', () => {
        providers.list = [{ id: 'p1', key: 'openai', display_name: 'OpenAI', enabled: true, additional_drop_params: ['logit_bias'] }];
        renderForm({ drop_params: false });
        tab(/Param Control/);
        expect(providerSwitch().checked).toBe(false);
        expect(screen.getByTestId('provider-drop-list-state')).toHaveTextContent(
            'The switch above is off, so these are not removed from requests to this model.',
        );
        fireEvent.click(providerSwitch());
        expect(screen.getByTestId('provider-drop-list-state')).toHaveTextContent(
            'The gateway removes these from every request to this model. Change the list on the provider.',
        );
    });

    it('sends the switch as it is set', async () => {
        const { onSave } = renderForm({ drop_params: undefined });
        tab(/Param Control/);
        fireEvent.click(providerSwitch());
        fireEvent.click(screen.getByRole('button', { name: /Save Model/ }));
        await waitFor(() => expect(onSave).toHaveBeenCalled());
        expect((onSave.mock.calls[0] as unknown as [Record<string, unknown>])[0].drop_params).toBe(false);
    });
});

describe('LLModelForm: per-minute limits of a model', () => {
    it('says the limits apply to every call the model takes', () => {
        renderForm();
        tab(/^API$/);
        expect(screen.getByTestId('model-rate-limit-help')).toHaveTextContent(
            'The gateway applies both limits to every call this model takes: a direct call, a call through a routing group, and a call that falls back to it. A call above a limit is refused. Empty: no limit.',
        );
    });
});

describe('LLModelForm: a model of a disabled provider', () => {
    it('says the gateway does not serve it', () => {
        providers.list = [{ id: 'p1', key: 'openai', display_name: 'OpenAI', enabled: false }];
        renderForm();
        expect(screen.getByTestId('model-not-served-banner')).toHaveTextContent(
            'The gateway does not serve this model: its provider (openai) is disabled. A model of a disabled provider takes no call.',
        );
    });

    it('says nothing of the kind when the provider is enabled', () => {
        providers.list = [{ id: 'p1', key: 'openai', display_name: 'OpenAI', enabled: true }];
        renderForm();
        expect(screen.queryByTestId('model-not-served-banner')).toBeNull();
    });
});

describe("LLModelForm: the model's circuit breaker", () => {
    const save = async (onSave: ReturnType<typeof renderForm>['onSave']) => {
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        return (onSave.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    };

    // With neither set the Control Plane and the gateway apply defaults:
    // there is always a breaker.
    it("says what applies when neither is set: the gateway's defaults, 5 failures and 30 seconds", () => {
        renderForm();
        tab(/Fallbacks/);
        const breaker = screen.getByTestId('model-circuit-breaker');
        expect(breaker).toHaveTextContent(
            "Set both, or leave both empty for the gateway's defaults (5 failures, 30 seconds).",
        );
        expect(breaker).not.toHaveTextContent('no breaker');
    });

    it('sends the failures and the open time together', async () => {
        const { onSave } = renderForm();
        tab(/Fallbacks/);
        fireEvent.change(screen.getByLabelText('Consecutive failures before it opens'), { target: { value: '3' } });
        fireEvent.change(screen.getByLabelText('Seconds it stays open'), { target: { value: '45' } });
        const sent = await save(onSave);
        expect(sent).toMatchObject({ allowed_fails: 3, cooldown_time: 45 });
    });

    it('refuses one without the other, and does not save', async () => {
        const { onSave } = renderForm();
        tab(/Fallbacks/);
        fireEvent.change(screen.getByLabelText('Consecutive failures before it opens'), { target: { value: '3' } });
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() =>
            expect(screen.getByTestId('model-circuit-breaker')).toHaveTextContent(
                'Set both, or neither: the failures before the breaker opens and how long it stays open.',
            ),
        );
        expect(onSave).not.toHaveBeenCalled();
    });

    it('shows a stored breaker, and clears it with null when both are emptied', async () => {
        const { onSave } = renderForm({ allowed_fails: 5, cooldown_time: 60 });
        tab(/Fallbacks/);
        expect(screen.getByLabelText<HTMLInputElement>('Consecutive failures before it opens').value).toBe('5');
        expect(screen.getByLabelText<HTMLInputElement>('Seconds it stays open').value).toBe('60');
        fireEvent.change(screen.getByLabelText('Consecutive failures before it opens'), { target: { value: '' } });
        fireEvent.change(screen.getByLabelText('Seconds it stays open'), { target: { value: '' } });
        const sent = await save(onSave);
        expect(sent).toMatchObject({ allowed_fails: null, cooldown_time: null });
    });
});

// A model's tenant-wide limits and policy, read with a value the
// Control Plane now refuses, are saved without it.
describe('LLModelForm and a stored setting the Control Plane refuses', () => {
    it('a save without an edit sends none of it in the limits or the policy, and is accepted', async () => {
        const onSave = vi.fn((data: { tenant_limits?: unknown; tenant_governance?: unknown }) => {
            const refusal = controlPlaneRefusal('llm_limits', data.tenant_limits) ?? controlPlaneRefusal('llm_governance', data.tenant_governance);
            return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve(undefined);
        });
        const model = { ...baseModel, tenant_limits: STORED_WITH_REFUSED.llm_limits, tenant_governance: STORED_WITH_REFUSED.llm_governance } as unknown as FormModel;
        render(<LLModelForm model={model} onSave={onSave} onClose={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: /Save Model/ }));
        await waitFor(() => expect(onSave).toHaveBeenCalled());
        await expect(onSave.mock.results[0].value).resolves.toBeUndefined();
        const sent = (onSave.mock.calls[0] as unknown as [Record<string, unknown>])[0];
        expect(sent.tenant_limits).toEqual(STORED_KEPT.llm_limits);
        expect(sent.tenant_governance).toEqual(STORED_KEPT.llm_governance);
    });
});
