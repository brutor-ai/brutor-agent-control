/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Refused-credential rows, as Proxy Logs receives them.
 *
 * The values are the ones the gateway's own test asserts
 * (`brutor-gateway-core/tests/integration/refused_agent_credential.rs`):
 * the row is a row of the surface it was refused on,
 * subtype `agent_authz_block`, decision `deny`, status 401, with the route
 * (`refused_route`), the number of refusals it stands for (`refusals`) and
 * the kind of credential (`refused_credential`). In `proxy_logs` the three
 * are in `custom_metadata` (`src/services/refused_agent_credential.rs`,
 * `META_ROUTE`, `META_REFUSALS`, `META_CREDENTIAL`), the method is `AUTH`,
 * the endpoint is the route, and the error message is the recorder's.
 *
 * The four families of the test: an API key on the LLM, MCP and A2A routes
 * (`a_refused_agent_key_is_a_sealed_denied_record_on_every_family`: 3, 1 and
 * 2 refusals) and a workload token on the LLM route (4 refusals).
 */

import type { UnifiedProxyLogEntry } from '../../../../lib/types';

type Surface = UnifiedProxyLogEntry['proxy_type'];

function row(
    id: string,
    surface: Surface,
    agent: string,
    route: string,
    refusals: number,
    credential: 'api_key' | 'workload_token',
): UnifiedProxyLogEntry {
    return {
        id,
        tenant_id: 'tenant-1',
        timestamp: '2026-11-12T10:00:00Z',
        proxy_type: surface,
        proxy_subtype: 'agent_authz_block',
        session_id: '',
        http_method: 'AUTH',
        endpoint: route,
        request: { method: 'AUTH', endpoint: route },
        http_status_code: 401,
        response: { status_code: 401, duration_ms: 0 },
        success: false,
        error_message:
            'agent_credential_refused: the agent identity is disabled, revoked, expired or not active ' +
            `(${credential}; ${refusals} refusal${refusals === 1 ? '' : 's'} since the previous record)`,
        actor_agent_id: agent,
        agent_authz_decision: 'deny',
        custom_metadata: { refusals, refused_credential: credential, refused_route: route },
    };
}

export const REFUSED_KEY_LLM = row(
    'log-refused-llm', 'llm', 'agent-sealed-llm-611204993', '/v1/proxy/llm/chat/completions', 3, 'api_key',
);
export const REFUSED_KEY_MCP = row(
    'log-refused-mcp', 'mcp', 'agent-sealed-mcp-611204994', '/v1/proxy/mcp/{}', 1, 'api_key',
);
export const REFUSED_KEY_A2A = row(
    'log-refused-a2a', 'a2a', 'agent-sealed-a2a-611204995', '/a2a/{}/{}/message:send', 2, 'api_key',
);
export const REFUSED_TOKEN_LLM = row(
    'log-refused-token', 'llm', 'agent-wl-sealed-611204996', '/v1/proxy/llm/chat/completions', 4, 'workload_token',
);

export const REFUSED_ROWS = [REFUSED_KEY_LLM, REFUSED_KEY_MCP, REFUSED_KEY_A2A, REFUSED_TOKEN_LLM];
