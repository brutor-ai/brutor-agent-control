/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A delete refused because the row is in use: a 409 whose `detail` is the
 * sentence (`message`) and what uses the row, by kind (`used_by`). The
 * console lists what uses it from the structure, never from the sentence.
 */

import { describe, expect, it } from 'vitest';

import { thrownByClient } from '../../test/serverAnswers';
import { inUseRefusalOf } from '../inUseRefusal';

describe('inUseRefusalOf', () => {
    it('reads what uses the row, by kind, in the order the console lists the kinds', async () => {
        const error = await thrownByClient(409, {
            detail: {
                message: "Identity Provider 'ADFS' is in use.",
                used_by: {
                    agent_identities: [{ id: 'a1', name: 'nightly-agent' }],
                    mcp_servers: [{ id: 'm1', name: 'Jira' }, { id: 'm2', name: 'Wiki' }],
                },
            },
        });
        expect(inUseRefusalOf(error)).toEqual({
            message: "Identity Provider 'ADFS' is in use.",
            groups: [
                { kind: 'mcp_servers', label: 'MCP servers', rows: [{ id: 'm1', name: 'Jira' }, { id: 'm2', name: 'Wiki' }] },
                { kind: 'agent_identities', label: 'Agent identities', rows: [{ id: 'a1', name: 'nightly-agent' }] },
            ],
        });
    });

    it.each([
        ['virtual_servers', 'Virtual servers'],
        ['server_configs', 'Server configurations'],
        ['administrators', 'Administrators'],
        ['oauth_app_clients', 'OAuth app clients'],
    ])('names the kind %s', async (kind, label) => {
        const error = await thrownByClient(409, { detail: { message: 'in use', used_by: { [kind]: [{ id: 'x', name: 'X' }] } } });
        expect(inUseRefusalOf(error)?.groups).toEqual([{ kind, label, rows: [{ id: 'x', name: 'X' }] }]);
    });

    it('lists a kind it does not know under its own key, after the known ones', async () => {
        const error = await thrownByClient(409, {
            detail: { message: 'in use', used_by: { widgets: [{ id: 'w', name: 'W' }], administrators: [{ id: 'u', name: 'U' }] } },
        });
        expect(inUseRefusalOf(error)?.groups.map((g) => g.label)).toEqual(['Administrators', 'widgets']);
    });

    it('is null for any other refusal', async () => {
        expect(inUseRefusalOf(await thrownByClient(409, { detail: 'The server is being deployed.' }))).toBeNull();
        expect(inUseRefusalOf(await thrownByClient(400, { detail: { message: 'x', used_by: {} } }))).toBeNull();
        expect(inUseRefusalOf(new Error('boom'))).toBeNull();
    });
});
