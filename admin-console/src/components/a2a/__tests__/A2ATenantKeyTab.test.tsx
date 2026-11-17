/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tenant's Ed25519 signing key, as an external verifier needs to consume
 * it. Two facts on this screen are load-bearing:
 *
 *   - the JWKS URL, which is what a third party actually fetches. It is derived
 *     from the tenant id, so a wrong id here means every downstream signature
 *     check fails against a 404;
 *   - the promise that the private key never leaves the gateway. If that
 *     sentence ever quietly disappears, operators will assume the opposite.
 *
 * Rotation is deliberately absent (deferred to a later phase); the test pins
 * that so nobody "restores" a destructive button that has no backend.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const getA2ATenantPublicKey = vi.fn();
const stubClient = { getA2ATenantPublicKey };

vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ gatewayClient: stubClient }),
}));

import A2ATenantKeyTab from '../A2ATenantKeyTab';

const KEY = {
    tenant_id: 'acme',
    public_key: 'MCowBQYDK2VwAyEAabcdef0123456789',
    key_algorithm: 'Ed25519',
    created_at: '2026-03-04T09:00:00Z',
};

beforeEach(() => {
    vi.clearAllMocks();
    getA2ATenantPublicKey.mockResolvedValue(KEY);
});

describe('A2ATenantKeyTab', () => {
    it('renders the key material once loaded', async () => {
        render(<A2ATenantKeyTab />);
        expect(
            await screen.findByText('MCowBQYDK2VwAyEAabcdef0123456789')
        ).toBeInTheDocument();
        expect(screen.getByText('Ed25519')).toBeInTheDocument();
    });

    it('derives the JWKS URL from the tenant id', async () => {
        render(<A2ATenantKeyTab />);
        expect(
            await screen.findByText('/.well-known/a2a-jwks/acme')
        ).toBeInTheDocument();
    });

    it('makes the JWKS URL a real link a verifier can follow', async () => {
        render(<A2ATenantKeyTab />);
        const link = await screen.findByRole('link', {
            name: '/.well-known/a2a-jwks/acme',
        });
        expect(link).toHaveAttribute('href', '/.well-known/a2a-jwks/acme');
    });

    it('states that the private key stays on the gateway', async () => {
        render(<A2ATenantKeyTab />);
        await screen.findByText('Ed25519');
        expect(screen.getByText(/private key lives encrypted/i)).toBeInTheDocument();
        expect(screen.getByText(/never leaves it/i)).toBeInTheDocument();
    });

    it('offers no rotate or delete action', async () => {
        // Rotation is deferred; a button here would invalidate every signed
        // card with no way to re-sign them.
        render(<A2ATenantKeyTab />);
        await screen.findByText('Ed25519');
        expect(screen.queryByRole('button', { name: /rotate/i })).not.toBeInTheDocument();
        expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
    });

    it('shows the rotation timestamp only once a rotation has happened', async () => {
        render(<A2ATenantKeyTab />);
        await screen.findByText('Ed25519');
        expect(screen.queryByText('Last rotated')).not.toBeInTheDocument();

        getA2ATenantPublicKey.mockResolvedValue({
            ...KEY,
            rotated_at: '2026-06-01T09:00:00Z',
        });
        fireEvent.click(screen.getByTitle('Refresh'));
        expect(await screen.findByText('Last rotated')).toBeInTheDocument();
    });

    it('shows the failure instead of an empty panel when the key cannot be read', async () => {
        getA2ATenantPublicKey.mockRejectedValue(new Error('no key provisioned'));
        render(<A2ATenantKeyTab />);

        expect(await screen.findByText('no key provisioned')).toBeInTheDocument();
        expect(screen.queryByText('Public key')).not.toBeInTheDocument();
    });

    it('recovers from an error on refresh', async () => {
        getA2ATenantPublicKey.mockRejectedValueOnce(new Error('boom'));
        render(<A2ATenantKeyTab />);
        await screen.findByText('boom');

        fireEvent.click(screen.getByTitle('Refresh'));
        expect(await screen.findByText('Ed25519')).toBeInTheDocument();
        expect(screen.queryByText('boom')).not.toBeInTheDocument();
    });

    it('refetches rather than re-rendering cached key material', async () => {
        render(<A2ATenantKeyTab />);
        await screen.findByText('Ed25519');
        expect(getA2ATenantPublicKey).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByTitle('Refresh'));
        await waitFor(() => expect(getA2ATenantPublicKey).toHaveBeenCalledTimes(2));
    });

    it('copies the public key and confirms it did', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText },
        });

        render(<A2ATenantKeyTab />);
        await screen.findByText('Ed25519');

        // Public key and JWKS URL are copyable; the algorithm and dates are not.
        const copyButtons = screen.getAllByTitle('Copy to clipboard');
        expect(copyButtons).toHaveLength(2);

        fireEvent.click(copyButtons[0]);
        await waitFor(() =>
            expect(writeText).toHaveBeenCalledWith('MCowBQYDK2VwAyEAabcdef0123456789')
        );
    });
});
