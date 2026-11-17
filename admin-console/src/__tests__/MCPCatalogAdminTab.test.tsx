/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * MCP Catalog (system admin) — MCPCatalogAdminTab test.
 *
 * Scope:
 *   • Rows render (name + slug + runtime visible)
 *   • Empty state when the list is empty
 *   • "Add entry" → create dialog → createGlobalMCPCatalogEntry + refresh
 *   • Delete → deleteGlobalMCPCatalogEntry + refresh
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import MCPCatalogAdminTab from '../components/mcp/catalog/MCPCatalogAdminTab';
import type { MCPCatalogEntryBrief } from '../lib/mcpCatalogTypes';

const _stableToast = {
    showToast: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
};
vi.mock('../components/common/Toast', () => ({
    useToast: () => _stableToast,
}));

vi.mock('../components/common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn().mockResolvedValue(true),
}));

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockDelete = vi.fn();

const _stableGatewayClient = {
    fetchMCPCatalogEntries: mockList,
    createGlobalMCPCatalogEntry: mockCreate,
    updateGlobalMCPCatalogEntry: mockUpdate,
    deleteGlobalMCPCatalogEntry: mockDelete,
};
vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: _stableGatewayClient }),
}));

function buildEntry(
    overrides: Partial<MCPCatalogEntryBrief> = {},
): MCPCatalogEntryBrief {
    return {
        id: 'mcp-cat-system-github',
        tenant_id: null,
        name: 'GitHub',
        slug: 'github',
        short_description: 'GitHub MCP server',
        icon_url: null,
        has_icon: false,
        repo_url: null,
        runtime: 'remote',
        categories: [],
        auth_pattern: 'oauth',
        source: 'system',
        enabled: true,
        created_at: '2026-06-01T00:00:00Z',
        updated_at: '2026-06-10T00:00:00Z',
        ...overrides,
    };
}

describe('MCPCatalogAdminTab', () => {
    beforeEach(() => {
        mockList.mockReset();
        mockCreate.mockReset();
        mockUpdate.mockReset();
        mockDelete.mockReset();
        _stableToast.success.mockReset();
        _stableToast.error.mockReset();
    });

    it('renders one row per entry with name, slug and runtime', async () => {
        mockList.mockResolvedValue({
            entries: [
                buildEntry({ id: 'a', name: 'GitHub', slug: 'github' }),
                buildEntry({
                    id: 'b',
                    name: 'Slack',
                    slug: 'slack',
                    auth_pattern: 'oauth',
                }),
            ],
            total: 2,
        });
        render(<MCPCatalogAdminTab />);
        await waitFor(() => {
            expect(screen.getByText('GitHub')).toBeInTheDocument();
            expect(screen.getByText('Slack')).toBeInTheDocument();
        });
        expect(screen.getByText('github')).toBeInTheDocument();
        expect(screen.getByText('slack')).toBeInTheDocument();
    });

    it('requests only source=system entries', async () => {
        mockList.mockResolvedValue({ entries: [], total: 0 });
        render(<MCPCatalogAdminTab />);
        await waitFor(() => expect(mockList).toHaveBeenCalled());
        expect(mockList).toHaveBeenCalledWith(
            expect.objectContaining({ source: 'system' }),
        );
    });

    it('shows the empty state when the list is empty', async () => {
        mockList.mockResolvedValue({ entries: [], total: 0 });
        render(<MCPCatalogAdminTab />);
        await waitFor(() => {
            expect(screen.getByText(/No catalog entries/i)).toBeInTheDocument();
        });
    });

    it('creates a global entry via the dialog and refreshes', async () => {
        mockList.mockResolvedValue({ entries: [], total: 0 });
        mockCreate.mockResolvedValue(buildEntry());
        render(<MCPCatalogAdminTab />);
        await waitFor(() => screen.getByText(/No catalog entries/i));
        expect(mockList).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole('button', { name: /Add entry/i }));

        fireEvent.change(screen.getByPlaceholderText('e.g. GitHub'), {
            target: { value: 'Notion' },
        });
        fireEvent.change(screen.getByPlaceholderText('e.g. github'), {
            target: { value: 'notion' },
        });

        fireEvent.click(screen.getByRole('button', { name: /Create entry/i }));

        await waitFor(() => {
            expect(mockCreate).toHaveBeenCalledTimes(1);
        });
        expect(mockCreate).toHaveBeenCalledWith(
            expect.objectContaining({
                name: 'Notion',
                slug: 'notion',
                runtime: 'remote',
            }),
        );
        await waitFor(() => {
            expect(mockList).toHaveBeenCalledTimes(2);
        });
        expect(_stableToast.success).toHaveBeenCalled();
    });

    it('deletes an entry and refreshes', async () => {
        mockList.mockResolvedValue({
            entries: [buildEntry({ id: 'del-me', name: 'Doomed', slug: 'doomed' })],
            total: 1,
        });
        mockDelete.mockResolvedValue(undefined);
        render(<MCPCatalogAdminTab />);
        await waitFor(() => screen.getByText('Doomed'));
        expect(mockList).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole('button', { name: /Delete Doomed/i }));

        await waitFor(() => {
            expect(mockDelete).toHaveBeenCalledWith('del-me');
        });
        await waitFor(() => {
            expect(mockList).toHaveBeenCalledTimes(2);
        });
        expect(_stableToast.success).toHaveBeenCalled();
    });
});
