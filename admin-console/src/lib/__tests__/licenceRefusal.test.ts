/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The license and quota refusals of the control plane, as the API client
 * turns them into text (`licenceRefusal.ts`, `gatewayApi/base.ts`).
 *
 * The bodies are the control plane's own
 * (`enterprise/brutor_enterprise/licence/features.py`, `limits.py`,
 * `app/services/tenant/tenant_service.py`). Each is given to the real client,
 * and what it throws is what a page catches.
 */

import { describe, expect, it, vi } from 'vitest';

import { GatewayAPIError } from '../gatewayApiClient';
import { licenceRefusal, licenceRefusalText, onLicenceRefusal, quotaRefusalText } from '../licenceRefusal';
import { errorMessage } from '../narrow';
import { thrownByClient } from '../../test/serverAnswers';

const NOT_LICENSED = {
    detail: {
        error: 'feature_not_licensed',
        feature: 'policies',
        message:
            "The licence of this installation does not include the feature 'policies'. Contact Brutor AI to add it to your licence.",
    },
};
const NOT_ENABLED = {
    detail: {
        error: 'feature_not_enabled_for_tenant',
        feature: 'knowledge_base',
        message:
            "The feature 'knowledge_base' is not enabled for tenant 'acme'. A system administrator can enable it in the tenant's features.",
    },
};
const LIMIT_REACHED = {
    detail: {
        error: 'licence_limit_reached',
        limit: 'max_llm_models',
        maximum: 5,
        current: 9,
        message:
            'The licence of this installation allows 5 enabled models per tenant (max_llm_models); there are 9. Contact Brutor AI to raise the limit of your licence.',
    },
};
const EXPIRED_ON = '2026-10-01';
const NOT_VALID = {
    detail: { error: 'licence_not_valid', message: `License fully expired on ${EXPIRED_ON}. Contact Brutor AI to renew your license.` },
};

describe('a license refusal, as the API client throws it', () => {
    it('names the feature that is not in the license, by the name the console gives it', async () => {
        const thrown = await thrownByClient(403, NOT_LICENSED);
        expect(thrown).toBeInstanceOf(GatewayAPIError);
        expect(errorMessage(thrown)).toBe(
            'Policy as Code is not in the license of this installation. Contact Brutor AI to add it to your license.',
        );
        expect(licenceRefusal(thrown)).toEqual({ code: 'feature_not_licensed', feature: 'policies' });
    });

    it('says a feature is switched off for the tenant, and who can switch it on', async () => {
        const thrown = await thrownByClient(403, NOT_ENABLED);
        expect(errorMessage(thrown)).toBe(
            "Knowledgebase (RAG) is not enabled for this tenant. A system administrator can enable it in the tenant's features.",
        );
    });

    it('says what the license allows and how many there are', async () => {
        const thrown = await thrownByClient(403, LIMIT_REACHED);
        expect(errorMessage(thrown)).toBe(
            'The license allows 5 enabled models per tenant, and there are 9. Nothing was changed. Remove or disable one, or contact Brutor AI to raise the limit of your license.',
        );
        expect(licenceRefusal(thrown)).toEqual({ code: 'licence_limit_reached', limit: 'max_llm_models', maximum: 5, current: 9 });
    });

    it('counts tenants in the installation, not per tenant', () => {
        expect(licenceRefusalText({ code: 'licence_limit_reached', limit: 'max_tenants', maximum: 3, current: 3 })).toBe(
            'The license allows 3 tenants in this installation, and there are 3. Nothing was changed. Remove or disable one, or contact Brutor AI to raise the limit of your license.',
        );
    });

    it('says the installation has no valid license', async () => {
        const thrown = await thrownByClient(403, NOT_VALID);
        expect(errorMessage(thrown)).toBe(
            'This installation has no valid license: it has expired or was not accepted. Contact Brutor AI to renew your license.',
        );
    });

    it.each([
        ['feature_not_licensed', { detail: { error: 'feature_not_licensed', feature: 'mcp_registry' } }, /^MCP Registry is not in the license/],
        ['licence_limit_reached', { detail: { error: 'licence_limit_reached', limit: 'max_mcp_servers', maximum: 2, current: 2 } }, /allows 2 enabled MCP servers per tenant/],
    ])('is readable without the server\'s sentence: %s', async (_code, body, expected) => {
        // An object `detail` with no `message` must not be shown as "HTTP 403: ".
        const thrown = await thrownByClient(403, body);
        const text = errorMessage(thrown);
        expect(text).toMatch(expected);
        expect(text).not.toContain('[object Object]');
        expect(text).not.toMatch(/^HTTP 403/);
    });

    it('never shows a flag or a limit by its key', async () => {
        for (const body of [NOT_LICENSED, NOT_ENABLED, LIMIT_REACHED, NOT_VALID]) {
            const text = errorMessage(await thrownByClient(403, body));
            expect(text).not.toMatch(/knowledge_base|max_llm_models|feature_not|licence_/);
        }
    });

    it('names a flag it has no name for by its words', () => {
        expect(licenceRefusalText({ code: 'feature_not_licensed', feature: 'new_thing' })).toMatch(/^new thing is not in the license/);
    });

    it('tells whoever listens, with the feature', async () => {
        const heard = vi.fn();
        const stop = onLicenceRefusal(heard);
        await thrownByClient(403, NOT_LICENSED);
        expect(heard).toHaveBeenCalledWith({ code: 'feature_not_licensed', feature: 'policies' });
        stop();
        await thrownByClient(403, NOT_ENABLED);
        expect(heard).toHaveBeenCalledTimes(1);
    });

    it('leaves every other refusal as it was', async () => {
        const heard = vi.fn();
        const stop = onLicenceRefusal(heard);
        const other = await thrownByClient(403, { detail: { error: 'feature_disabled', message: 'Switched off.' } });
        expect(errorMessage(other)).toBe('Switched off.');
        expect(licenceRefusal(other)).toBeNull();
        expect(errorMessage(await thrownByClient(403, { detail: 'Not allowed' }))).toBe('Not allowed');
        expect(heard).not.toHaveBeenCalled();
        stop();
    });
});

describe('a tenant quota that is full (429)', () => {
    const QUOTA = {
        detail: {
            error: 'tenant_quota_reached',
            limit: 'max_llm_models',
            maximum: 5,
            current: 5,
            message: 'Quota exceeded for llm models. Current: 5, Maximum: 5. Please upgrade your plan.',
        },
    };

    it('says how many the quota allows and who can raise it', async () => {
        const thrown = await thrownByClient(429, QUOTA);
        expect(errorMessage(thrown)).toBe(
            "This tenant's quota allows 5 enabled models, and there are 5. Nothing was changed. Remove or disable one, or ask a system administrator to raise the tenant's quota.",
        );
    });

    it('names what is counted by the limit, not by the words of the sentence', () => {
        expect(
            quotaRefusalText({ detail: { error: 'tenant_quota_reached', limit: 'max_mcp_servers', maximum: 3, current: 3, message: 'Anything.' } }),
        ).toMatch(/allows 3 enabled MCP servers, and there are 3/);
    });

    it('is readable when the limit has no name or no numbers', () => {
        expect(quotaRefusalText({ detail: { error: 'tenant_quota_reached', limit: 'max_widgets', maximum: 2, current: 2 } })).toMatch(
            /allows 2 widgets, and there are 2/,
        );
        expect(quotaRefusalText({ detail: { error: 'tenant_quota_reached', limit: null, maximum: null, current: 7 } })).toBe(
            "This tenant's quota is reached. Nothing was changed. Remove or disable one, or ask a system administrator to raise the tenant's quota.",
        );
    });

    it('is the same sentence under any status, because paths that create on behalf of someone else refuse with their own', async () => {
        expect(errorMessage(await thrownByClient(403, QUOTA))).toMatch(/^This tenant's quota allows 5 enabled models/);
    });

    it('is not read into another answer, and the old sentence alone is not taken apart', () => {
        expect(quotaRefusalText({ detail: 'Too many requests' })).toBeNull();
        expect(quotaRefusalText({ detail: 'Quota exceeded for llm models. Current: 5, Maximum: 5. Please upgrade your plan.' })).toBeNull();
        expect(quotaRefusalText({ detail: { error: 'something_else', limit: 'max_llm_models' } })).toBeNull();
    });
});
