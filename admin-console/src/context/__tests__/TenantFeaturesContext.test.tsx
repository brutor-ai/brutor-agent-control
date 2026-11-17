/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';

const getCurrentTenant = vi.fn();
const request = vi.fn();
vi.mock('../../hooks/useAuth', () => ({
    useAuth: () => ({ isAuthenticated: true, principal: null, gatewayClient: client }),
}));
const client = { getCurrentTenant, request };

import { TenantFeaturesProvider, useTenantFeatures } from '../TenantFeaturesContext';

function Probe() {
    const { displayName, isFeatureEnabled, loaded, failure, reload } = useTenantFeatures();
    return (
        <div>
            <span data-testid="loaded">{String(loaded)}</span>
            <span data-testid="name">{displayName ?? ''}</span>
            <span data-testid="kb">{String(isFeatureEnabled('knowledge_base'))}</span>
            <span data-testid="failure">{failure?.sentence ?? ''}</span>
            <button type="button" onClick={reload}>again</button>
        </div>
    );
}

beforeEach(() => {
    vi.clearAllMocks();
});

describe('TenantFeaturesProvider', () => {
    it('reads the tenant through the typed client method', async () => {
        getCurrentTenant.mockResolvedValue({ display_name: 'Acme', features: { knowledge_base: false } });
        render(<TenantFeaturesProvider><Probe /></TenantFeaturesProvider>);
        await waitFor(() => expect(screen.getByTestId('name')).toHaveTextContent('Acme'));
        expect(screen.getByTestId('kb')).toHaveTextContent('false');
        expect(screen.getByTestId('failure')).toHaveTextContent('');
        expect(request).not.toHaveBeenCalled();
    });

    it('a tenant that could not be read is held as a failure, and reload asks again', async () => {
        getCurrentTenant.mockRejectedValue(new Error('The gateway is not reachable'));
        render(<TenantFeaturesProvider><Probe /></TenantFeaturesProvider>);
        await waitFor(() =>
            expect(screen.getByTestId('failure')).toHaveTextContent(
                'Failed to load the features of this tenant; every feature is shown: The gateway is not reachable',
            ),
        );
        expect(screen.getByTestId('loaded')).toHaveTextContent('true');

        getCurrentTenant.mockResolvedValue({ display_name: 'Acme', features: {} });
        act(() => screen.getByRole('button', { name: 'again' }).click());
        await waitFor(() => expect(screen.getByTestId('name')).toHaveTextContent('Acme'));
        expect(screen.getByTestId('failure')).toHaveTextContent('');
    });
});
