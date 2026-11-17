/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Catalog trust badge
 *
 * The Verified / Revoked badges are the operator's at-a-glance
 * signal that a catalog entry has cryptographic attestation. They
 * MUST render correctly for each trust_tier value and MUST NOT
 * render for unsigned entries (the catalog has hundreds of unsigned
 * entries by default; we don't want every row noisy).
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import MCPCatalogEntryCard from '../components/mcp/catalog/MCPCatalogEntryCard';
import type { MCPCatalogEntryBrief } from '../lib/mcpCatalogTypes';


function makeEntry(overrides: Partial<MCPCatalogEntryBrief> = {}): MCPCatalogEntryBrief {
    return {
        id: 'mcp-cat-test',
        tenant_id: 't1',
        name: 'Test Server',
        slug: 'test-server',
        short_description: 'Demo entry',
        icon_url: null,
        has_icon: false,
        repo_url: null,
        runtime: 'remote',
        categories: [],
        auth_pattern: 'none',
        source: 'system',
        enabled: true,
        trust_tier: 'unsigned',
        signed_by: null,
        signed_at: null,
        signature_algorithm: null,
        created_at: null,
        updated_at: null,
        ...overrides,
    };
}


describe('MCPCatalogEntryCard — trust badge', () => {
    it('renders Verified badge for tenant_signed entries', () => {
        render(
            <MCPCatalogEntryCard
                entry={makeEntry({
                    trust_tier: 'tenant_signed',
                    signed_at: '2026-05-13T10:00:00Z',
                    signature_algorithm: 'ed25519',
                })}
                onClick={vi.fn()}
            />,
        );
        expect(screen.getByText(/Verified/i)).toBeInTheDocument();
        expect(screen.queryByText(/Revoked/i)).not.toBeInTheDocument();
    });

    it('renders Revoked badge for revoked entries', () => {
        render(
            <MCPCatalogEntryCard
                entry={makeEntry({ trust_tier: 'revoked' })}
                onClick={vi.fn()}
            />,
        );
        expect(screen.getByText(/Revoked/i)).toBeInTheDocument();
        // Revoked entries do NOT show the green Verified badge — even if
        // a stale signature is still in the DB.
        expect(screen.queryByText(/✓ Verified/)).not.toBeInTheDocument();
    });

    it('does NOT render either badge for unsigned entries', () => {
        // Most entries in a default tenant are unsigned. The badges
        // would be noise.
        render(
            <MCPCatalogEntryCard
                entry={makeEntry({ trust_tier: 'unsigned' })}
                onClick={vi.fn()}
            />,
        );
        expect(screen.queryByText(/Verified/i)).not.toBeInTheDocument();
        expect(screen.queryByText(/Revoked/i)).not.toBeInTheDocument();
    });

    it('does NOT render Verified for partner_signed (reserved, not yet meaningful)', () => {
        // partner_signed is reserved for a future cross-tenant CA
        // story. Until that ships, treat it like unsigned in the UI
        // rather than misleading operators with a Verified badge.
        render(
            <MCPCatalogEntryCard
                entry={makeEntry({ trust_tier: 'partner_signed' })}
                onClick={vi.fn()}
            />,
        );
        expect(screen.queryByText(/Verified/i)).not.toBeInTheDocument();
    });

    it('handles an entry with no trust_tier', () => {
        // The contract gives trust_tier a default ('unsigned'), so the
        // field is optional: a card given none must render without
        // crashing.
        const entry = makeEntry();
        delete (entry as Partial<MCPCatalogEntryBrief>).trust_tier;
        render(
            <MCPCatalogEntryCard entry={entry} onClick={vi.fn()} />,
        );
        expect(screen.getByText('Test Server')).toBeInTheDocument();
    });
});
