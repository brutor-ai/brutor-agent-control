/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A secret of a guardrail provider's `config` in the form.
 *
 * The Control Plane returns no secret, only whether one is stored. The
 * field therefore holds nothing but what is typed: an empty field leaves
 * the key out of the save (the stored value is kept), a typed value
 * replaces it, and `null` removes it.
 */

import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient: null }) }));

import { ProviderConfigSecret } from '../GuardrailForm';

afterEach(cleanup);

const seen: Array<Record<string, unknown>> = [];

function Harness({ stored, removable = false }: { stored: boolean; removable?: boolean }) {
    const [provider, setProvider] = useState<{ provider: string; config: Record<string, unknown>; config_secrets_stored: Record<string, boolean> }>({
        provider: 'bedrock',
        config: { guardrail_id: 'gr-1' },
        config_secrets_stored: { aws_session_token: stored },
    });
    return (
        <ProviderConfigSecret
            provider={provider}
            onChange={(updated) => {
                seen.push(updated.config);
                setProvider((prev) => ({ ...prev, config: updated.config }));
            }}
            field="aws_session_token"
            label="AWS session token"
            removable={removable}
        />
    );
}

const field = () => screen.getByLabelText<HTMLInputElement>('AWS session token');
/** What the field last handed up. */
const last = () => seen[seen.length - 1];

describe('ProviderConfigSecret', () => {
    it('says a value is stored and never shows one', () => {
        render(<Harness stored />);
        expect(screen.getByTestId('guardrail-secret-stored-aws_session_token')).toHaveTextContent('A value is stored. It is never shown.');
        expect(field().value).toBe('');
        expect(field().type).toBe('password');
    });

    it('says no value is stored', () => {
        render(<Harness stored={false} />);
        expect(screen.getByTestId('guardrail-secret-stored-aws_session_token')).toHaveTextContent('No value is stored.');
    });

    it('puts what is typed in the config, and leaves the key out again when the field is cleared', () => {
        seen.length = 0;
        render(<Harness stored />);
        fireEvent.change(field(), { target: { value: 'tok' } });
        expect(last()).toEqual({ guardrail_id: 'gr-1', aws_session_token: 'tok' });
        fireEvent.change(field(), { target: { value: '' } });
        expect(last()).toEqual({ guardrail_id: 'gr-1' });
    });

    it('removes a stored value with null, and takes that back', () => {
        seen.length = 0;
        render(<Harness stored removable />);
        const remove = screen.getByRole('checkbox', { name: 'Remove the stored value when the policy is saved' });
        fireEvent.click(remove);
        expect(last()).toEqual({ guardrail_id: 'gr-1', aws_session_token: null });
        expect(field().disabled).toBe(true);
        fireEvent.click(remove);
        expect(last()).toEqual({ guardrail_id: 'gr-1' });
    });

    it('offers no removal when nothing is stored', () => {
        render(<Harness stored={false} removable />);
        expect(screen.queryByRole('checkbox')).toBeNull();
    });
});
