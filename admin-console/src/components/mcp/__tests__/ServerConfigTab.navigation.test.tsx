/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * An MCP server that virtual servers use cannot be deleted, and the refusal
 * offers the way to them. The page owns the sub-tabs, so it is the page that
 * gives the servers tab that way: taking it shows the virtual servers.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('@/components/mcp/config/server/ServersTab', () => ({
    default: ({ onNavigateToVirtualServers }: { onNavigateToVirtualServers?: () => void }) => (
        <div>
            <span>servers tab</span>
            {onNavigateToVirtualServers && <button onClick={onNavigateToVirtualServers}>to virtual servers</button>}
        </div>
    ),
}));
vi.mock('@/components/mcp/virtual/VirtualServersTab', () => ({ default: () => <span>virtual servers tab</span> }));
vi.mock('@/components/mcp/config/server-access/ServerAccessTab', () => ({ default: () => <span>server access tab</span> }));
vi.mock('@/components/mcp/catalog/MCPCatalogTab', () => ({ default: () => <span>catalog tab</span> }));

import ServerConfigTab from '../ServerConfigTab';

describe('ServerConfigTab', () => {
    it('gives the servers tab the way to the virtual servers, and taking it shows them', () => {
        render(<ServerConfigTab />);
        expect(screen.getByText('servers tab')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'to virtual servers' }));
        expect(screen.getByText('virtual servers tab')).toBeInTheDocument();
        expect(screen.queryByText('servers tab')).toBeNull();
    });
});
