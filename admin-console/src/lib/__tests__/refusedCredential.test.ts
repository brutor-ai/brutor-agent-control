/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { refusedCredentialOf, refusedCredentialSentence } from '../refusedCredential';
import {
    REFUSED_KEY_A2A,
    REFUSED_KEY_LLM,
    REFUSED_KEY_MCP,
    REFUSED_ROWS,
    REFUSED_TOKEN_LLM,
} from '../../components/monitoring/__tests__/fixtures/refusedCredentialRows';

describe('refusedCredentialOf', () => {
    it('reads the kind of credential, the route and the number of refusals', () => {
        expect(refusedCredentialOf(REFUSED_KEY_LLM)).toEqual({
            credential: 'api_key', route: '/v1/proxy/llm/chat/completions', refusals: 3,
        });
        expect(refusedCredentialOf(REFUSED_KEY_MCP)).toEqual({
            credential: 'api_key', route: '/v1/proxy/mcp/{}', refusals: 1,
        });
        expect(refusedCredentialOf(REFUSED_KEY_A2A)).toEqual({
            credential: 'api_key', route: '/a2a/{}/{}/message:send', refusals: 2,
        });
        expect(refusedCredentialOf(REFUSED_TOKEN_LLM)).toEqual({
            credential: 'workload_token', route: '/v1/proxy/llm/chat/completions', refusals: 4,
        });
    });

    it('does not depend on the row being of any one surface', () => {
        for (const row of REFUSED_ROWS) {
            for (const surface of ['llm', 'mcp', 'a2a', 'skill', 'oauth']) {
                const onSurface = { ...row, proxy_type: surface };
                expect(refusedCredentialOf(onSurface)).not.toBeNull();
            }
        }
    });

    it('is null for the agent gate\'s own denials, which carry the same subtype and no refused credential', () => {
        expect(refusedCredentialOf({ proxy_subtype: 'agent_authz_block', custom_metadata: undefined })).toBeNull();
        expect(refusedCredentialOf({ proxy_subtype: 'agent_authz_block', custom_metadata: { other: 1 } })).toBeNull();
    });

    it('is null for a row of another subtype', () => {
        expect(refusedCredentialOf({ ...REFUSED_KEY_LLM, proxy_subtype: 'engine_failure' })).toBeNull();
        expect(refusedCredentialOf({ ...REFUSED_KEY_LLM, proxy_subtype: undefined })).toBeNull();
    });

    it('keeps a kind it does not know as it is, and a missing route or count as null', () => {
        // A refusal on a route of no governed surface has no `refused_route`
        // (`refused_agent_credential.rs`: such a row keeps endpoint `authentication`).
        expect(refusedCredentialOf({
            proxy_subtype: 'agent_authz_block',
            custom_metadata: { refused_credential: 'client_certificate' },
        })).toEqual({ credential: 'client_certificate', route: null, refusals: null });
    });
});

describe('refusedCredentialSentence', () => {
    it('says which credential was refused, on which route, and how many refusals the row stands for', () => {
        expect(refusedCredentialSentence(refusedCredentialOf(REFUSED_KEY_LLM)!)).toBe(
            'The gateway refused the API key of an agent that is disabled, expired or revoked, ' +
            'on /v1/proxy/llm/chat/completions. This row stands for 3 refusals.',
        );
        expect(refusedCredentialSentence(refusedCredentialOf(REFUSED_TOKEN_LLM)!)).toBe(
            'The gateway refused the workload token of an agent that is disabled, expired or revoked, ' +
            'on /v1/proxy/llm/chat/completions. This row stands for 4 refusals.',
        );
    });

    it('says "1 refusal"', () => {
        expect(refusedCredentialSentence(refusedCredentialOf(REFUSED_KEY_MCP)!)).toMatch(/stands for 1 refusal\.$/);
    });

    it('leaves out what the row does not state', () => {
        expect(refusedCredentialSentence({ credential: 'client_certificate', route: null, refusals: null })).toBe(
            'The gateway refused the credential (client_certificate) of an agent that is disabled, expired or revoked.',
        );
    });
});
