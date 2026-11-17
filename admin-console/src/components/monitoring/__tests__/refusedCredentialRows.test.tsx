/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Proxy Logs: a refused credential of a disabled, expired or revoked agent.
 *
 * The gateway records it as a row of the surface it was refused on (`llm`,
 * `mcp`, `a2a`, `skill`), subtype `agent_authz_block`, status 401, and not
 * under `oauth`. The row has to say what it is, and nothing the console
 * ships may depend on it being `oauth`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const mockFetchProxyLogs = vi.fn();
const stubClient = {
    fetchProxyLogFacets: vi.fn().mockResolvedValue({ facets: {} }),
    fetchProxyLogFacetSuggestions: vi.fn().mockResolvedValue({ values: [] }),
    listSavedLogViews: vi.fn().mockResolvedValue({ views: [] }),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchProxyLogs: mockFetchProxyLogs,
        exportProxyLogs: vi.fn(),
        gatewayClient: stubClient,
    }),
}));
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ user: { id: 'adm_1', username: 'admin' }, isAuthenticated: true }),
}));
vi.mock('../SavedViewMenu', () => ({ default: () => null }));
vi.mock('../FacetSidebar', () => ({ default: () => null }));

import ProxyLogsViewer from '../ProxyLogsViewer';
import ProxyLogDetailsModal from '../ProxyLogDetailsModal';
import { buildProxyLogFilterEnvelope, PROXY_TYPE_VALUES } from '../proxyLogFilterEnvelope';
import { REFUSED_KEY_A2A, REFUSED_KEY_MCP, REFUSED_ROWS, REFUSED_TOKEN_LLM } from './fixtures/refusedCredentialRows';

vi.setConfig({ testTimeout: 20_000 });

function page(logs: unknown[]) {
    return { logs, pagination: { total: logs.length, offset: 0, limit: 50, returned: logs.length, has_more: false } };
}

describe('Proxy Logs: refused-credential rows', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        stubClient.fetchProxyLogFacets.mockResolvedValue({ facets: {} });
        stubClient.listSavedLogViews.mockResolvedValue({ views: [] });
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('marks each such row in the list with the kind of credential and the count', async () => {
        mockFetchProxyLogs.mockResolvedValue(page(REFUSED_ROWS));
        render(<ProxyLogsViewer />);
        const marks = await screen.findAllByTestId('refused-credential-mark');
        expect(marks.map((m) => m.textContent)).toEqual([
            'API key refused ×3',
            'API key refused',
            'API key refused ×2',
            'workload token refused ×4',
        ]);
    });

    it('says in the opened row which credential, which route and how many refusals', async () => {
        mockFetchProxyLogs.mockResolvedValue(page([REFUSED_TOKEN_LLM]));
        render(<ProxyLogsViewer />);
        fireEvent.click(await screen.findByTestId('refused-credential-mark'));
        const note = await screen.findByTestId('refused-credential-note');
        expect(note).toHaveTextContent(
            'The gateway refused the workload token of an agent that is disabled, expired or revoked, ' +
            'on /v1/proxy/llm/chat/completions. This row stands for 4 refusals.',
        );
    });

    it('does not mark a row that is not one', async () => {
        const denial = { ...REFUSED_KEY_MCP, id: 'log-gate-denial', custom_metadata: undefined };
        const plain = { ...REFUSED_KEY_MCP, id: 'log-plain', proxy_subtype: undefined, custom_metadata: undefined };
        mockFetchProxyLogs.mockResolvedValue(page([denial, plain]));
        render(<ProxyLogsViewer />);
        await waitFor(() => expect(screen.getAllByText('AUTH').length).toBe(2));
        expect(screen.queryByTestId('refused-credential-mark')).toBeNull();
    });

    it('says the same in the details view', () => {
        render(<ProxyLogDetailsModal log={REFUSED_KEY_A2A} onClose={vi.fn()} />);
        const section = screen.getByTestId('refused-credential-details');
        expect(within(section).getByText('API key')).toBeInTheDocument();
        expect(within(section).getByText('/a2a/{}/{}/message:send')).toBeInTheDocument();
        expect(within(section).getByText('2')).toBeInTheDocument();
        expect(section).toHaveTextContent(/This row stands for 2 refusals\./);
    });

    it('asks for no proxy type by default: no built-in view narrows to oauth', async () => {
        mockFetchProxyLogs.mockResolvedValue(page([]));
        render(<ProxyLogsViewer />);
        await waitFor(() => expect(mockFetchProxyLogs).toHaveBeenCalled());
        for (const [body] of mockFetchProxyLogs.mock.calls as Array<[Record<string, unknown>]>) {
            expect(body.proxy_types).toBeUndefined();
            expect(body.proxy_subtype).toBeUndefined();
            expect(body.proxy_subtypes).toBeUndefined();
            expect(JSON.stringify(body)).not.toContain('oauth');
        }
    });

    it('can still be asked for the surfaces the rows are now on', () => {
        // `skill` too, since the control plane's `proxy_types` filter takes
        // every type the gateway writes.
        for (const surface of ['llm', 'mcp', 'a2a', 'skill'] as const) {
            expect(PROXY_TYPE_VALUES).toContain(surface);
            const body = buildProxyLogFilterEnvelope({
                timeRange: { kind: 'preset', minutes: 60 },
                proxyTypesFilter: [surface],
                successFilter: [], serverFilter: [], gatewayUserFilter: [], idpUserFilter: [],
                targetUrlFilter: [], capabilityFilter: [], correlationIdFilter: '', sessionIdFilter: '',
                minDuration: '', maxDuration: '', complianceTagFilter: [], regionFilter: [],
                httpStatusFilter: [], freeText: '',
            });
            expect(body.proxy_types).toEqual([surface]);
        }
    });
});
