/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuthAppClientRow "via DCR" badge
 *
 * The badge is the load-bearing piece of UX for inbound DCR
 * governance. Operators need to spot self-registered clients
 * (created via RFC 7591) at a glance so they can revoke unexpected
 * entries.
 *
 * Two invariants:
 *   1. `auto_registered: true` → badge renders with "via DCR".
 *   2. `auto_registered: false / undefined` → no badge.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import OAuthAppClientRow from '../components/mcp/config/server-access/oauth_app/OAuthAppClientRow';
import type { OAuthAppClient } from '../lib/types';


function makeClient(overrides: Partial<OAuthAppClient> = {}): OAuthAppClient {
    return {
        id: 'appc-test',
        name: 'Test Client',
        app_type: 'webapp',
        idp_id: 'idp-1',
        client_id: 'app-abc123',
        redirect_uri: 'https://x.example.com/cb',
        scopes: [],
        client_type: 'confidential',
        supports_pkce: true,
        allowed_grant_types: ['authorization_code'],
        registered_at: '2026-05-13T00:00:00Z',
        is_active: true,
        ...overrides,
    };
}


describe('OAuthAppClientRow — "via DCR" badge', () => {
    it('renders the badge when auto_registered is true', () => {
        render(
            <OAuthAppClientRow
                client={makeClient({ auto_registered: true })}
                idpName="Test IdP"
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />,
        );
        expect(screen.getByText(/via DCR/i)).toBeInTheDocument();
    });

    it('does NOT render the badge when auto_registered is false', () => {
        render(
            <OAuthAppClientRow
                client={makeClient({ auto_registered: false })}
                idpName="Test IdP"
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />,
        );
        expect(screen.queryByText(/via DCR/i)).not.toBeInTheDocument();
    });

    it('does NOT render the badge when auto_registered is undefined', () => {
        // Defensive: client rows created before the column existed
        // will have NULL in the column, which TypeScript treats as
        // undefined. The badge must not render for these.
        render(
            <OAuthAppClientRow
                client={makeClient()}
                idpName="Test IdP"
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />,
        );
        expect(screen.queryByText(/via DCR/i)).not.toBeInTheDocument();
    });
});
