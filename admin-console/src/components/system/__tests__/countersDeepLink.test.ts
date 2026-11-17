/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import {
    buildCountersDeepLinkUrl,
    deepLinkInputFromProxyLogRow,
    deepLinkInputFromSystemAlert,
    deepLinkInputFromUsageAlert,
    shouldShowCountersLink,
    shouldShowCountersLinkForAlert,
    shouldShowCountersLinkForUsageAlert,
} from '../countersDeepLink';

describe('buildCountersDeepLinkUrl', () => {
    it('produces a bare path when no inputs are set', () => {
        expect(buildCountersDeepLinkUrl({})).toBe('/admin/system/counters');
    });

    it('includes scope + subject when present', () => {
        const url = buildCountersDeepLinkUrl({
            scope: 'model',
            subject: 'gpt-4o',
        });
        expect(url).toContain('cnt-scope=model');
        expect(url).toContain('cnt-subject=gpt-4o');
    });

    it('encodes special chars in the subject id', () => {
        // MCP server ids in particular can carry slashes / colons —
        // URLSearchParams must encode them so the page receives the
        // raw value back via decodeURIComponent.
        const url = buildCountersDeepLinkUrl({
            scope: 'mcp_server',
            subject: 'mcp/server-1:foo',
        });
        expect(url).toContain(
            'cnt-subject=mcp%2Fserver-1%3Afoo',
        );
    });

    it('threads optional kind + group + drawer params', () => {
        const url = buildCountersDeepLinkUrl({
            scope: 'model',
            subject: 'gpt-4o',
            kind: 'llm_request_rate',
            groupId: 'grp_red',
            openDrawer: true,
        });
        expect(url).toContain('cnt-kind=llm_request_rate');
        expect(url).toContain('cnt-group=grp_red');
        expect(url).toContain('cnt-drawer=open');
    });

    it('omits the drawer param when openDrawer is false', () => {
        const url = buildCountersDeepLinkUrl({
            scope: 'model',
            subject: 'gpt-4o',
            openDrawer: false,
        });
        expect(url).not.toContain('cnt-drawer');
    });
});

describe('shouldShowCountersLink', () => {
    it('returns true for HTTP 429 rows', () => {
        expect(
            shouldShowCountersLink({
                response: { status_code: 429 },
            }),
        ).toBe(true);
    });

    it('returns true when enforced_limit_type is populated', () => {
        // The proxy stamps enforced_limit_type on
        // any rate-limit rejection, even when the HTTP code isn't
        // 429 (e.g. concurrency-slot rejections surface as 503).
        // The forensic link still belongs on those rows.
        expect(
            shouldShowCountersLink({
                response: { status_code: 503 },
                enforced_limit_type: 'llm_concurrent',
            }),
        ).toBe(true);
    });

    it('returns false for 2xx success rows', () => {
        expect(
            shouldShowCountersLink({
                response: { status_code: 200 },
            }),
        ).toBe(false);
    });

    it('returns false for non-rate-limit 4xx rows', () => {
        expect(
            shouldShowCountersLink({
                response: { status_code: 403 },
            }),
        ).toBe(false);
    });

    it('returns false when response is missing', () => {
        expect(shouldShowCountersLink({})).toBe(false);
    });
});

describe('deepLinkInputFromProxyLogRow', () => {
    it('maps an LLM 429 to a model-scoped deep link', () => {
        const input = deepLinkInputFromProxyLogRow({
            proxy_type: 'llm',
            llm: { model_used: 'gpt-4o', model_requested: null },
            response: { status_code: 429 },
            enforced_limit_type: 'llm_request_rate',
            resource_group_id: 'grp_red',
        });
        expect(input).not.toBeNull();
        expect(input).toEqual({
            scope: 'model',
            subject: 'gpt-4o',
            kind: 'llm_request_rate',
            groupId: 'grp_red',
            openDrawer: true,
        });
    });

    it('falls back to model_requested when model_used is null', () => {
        // When the gateway rejects upstream-side (e.g. cooldown
        // armed on a model) the request never got an answer, so
        // model_used is null but model_requested carries the user's
        // intent. Use the latter so the deep link still lands.
        const input = deepLinkInputFromProxyLogRow({
            proxy_type: 'llm',
            llm: { model_used: null, model_requested: 'claude-opus-4' },
            response: { status_code: 429 },
        });
        expect(input?.subject).toBe('claude-opus-4');
    });

    it('falls back to server_id for LLM rows with no llm block', () => {
        // The contract makes a row's llm block optional: server_id keeps
        // the link for an LLM row answered without one.
        const input = deepLinkInputFromProxyLogRow({
            proxy_type: 'llm',
            server_id: 'srv_1',
            response: { status_code: 429 },
        });
        expect(input?.subject).toBe('srv_1');
    });

    it('maps an MCP 429 to an mcp_server-scoped deep link', () => {
        const input = deepLinkInputFromProxyLogRow({
            proxy_type: 'mcp',
            server_id: 'mcp_github',
            response: { status_code: 429 },
            enforced_limit_type: 'mcp_request_rate',
        });
        expect(input).toEqual({
            scope: 'mcp_server',
            subject: 'mcp_github',
            kind: 'mcp_request_rate',
            groupId: undefined,
            openDrawer: true,
        });
    });

    it('returns null when proxy_type is a scope-less family (a2a, skill)', () => {
        // Future deep-link scopes will cover these — for now we
        // return null so the link doesn't render. Operators
        // navigate via the sidebar; the page handles those scopes
        // with subject_id alone.
        expect(
            deepLinkInputFromProxyLogRow({
                proxy_type: 'a2a',
                server_id: 'agent-1',
                response: { status_code: 429 },
            }),
        ).toBeNull();
        expect(
            deepLinkInputFromProxyLogRow({
                proxy_type: 'skill',
                server_id: 'skill-1',
                response: { status_code: 429 },
            }),
        ).toBeNull();
    });

    it('returns null when no subject can be derived', () => {
        expect(
            deepLinkInputFromProxyLogRow({
                proxy_type: 'llm',
                response: { status_code: 429 },
            }),
        ).toBeNull();
    });
});

describe('shouldShowCountersLinkForAlert', () => {
    it('returns true for usage_rate_limit alerts', () => {
        expect(
            shouldShowCountersLinkForAlert({
                error_code: 'USAGE_RATE_LIMIT',
                resource_type: 'llm_model',
                resource_id: 'mdl_1',
            }),
        ).toBe(true);
    });

    it('does not read context.kind: no raiser of this release writes it', () => {
        expect(
            shouldShowCountersLinkForAlert({
                error_code: 'system_error',
                resource_type: 'mcp_server',
                resource_id: 'mcp_1',
                context: { kind: 'concurrency_saturation' },
            }),
        ).toBe(false);
        expect(
            deepLinkInputFromSystemAlert({
                error_code: 'usage_rate_limit',
                resource_type: 'llm_model',
                resource_id: 'mdl_1',
                context: { kind: 'concurrency_saturation' },
            })?.kind,
        ).toBe('usage_rate_limit');
    });

    it('returns true when enforced_limit_type is populated in context', () => {
        expect(
            shouldShowCountersLinkForAlert({
                error_code: null,
                resource_type: 'llm_model',
                resource_id: 'mdl_1',
                context: { enforced_limit_type: 'llm_itpm' },
            }),
        ).toBe(true);
    });

    it('returns false for downstream-IDP alerts', () => {
        expect(
            shouldShowCountersLinkForAlert({
                error_code: 'IDP_UNAVAILABLE',
                resource_type: 'idp',
                resource_id: 'idp_1',
            }),
        ).toBe(false);
    });

    it('returns false when resource_id is missing', () => {
        expect(
            shouldShowCountersLinkForAlert({
                error_code: 'USAGE_RATE_LIMIT',
                resource_type: 'llm_model',
                resource_id: null,
            }),
        ).toBe(false);
    });

    it('normalises case + hyphens so SCREAMING_SNAKE matches lower_snake', () => {
        expect(
            shouldShowCountersLinkForAlert({
                error_code: 'COOLDOWN-ARMED',
                resource_type: 'llm_model',
                resource_id: 'mdl_1',
            }),
        ).toBe(true);
    });
});

describe('deepLinkInputFromSystemAlert', () => {
    it('maps an llm_model rate-limit alert to a model-scoped input', () => {
        const input = deepLinkInputFromSystemAlert({
            error_code: 'usage_rate_limit',
            resource_type: 'llm_model',
            resource_id: 'mdl_gpt4o',
            context: {
                enforced_limit_type: 'llm_request_rate',
                resource_group_id: 'grp_red',
            },
        });
        expect(input).toEqual({
            scope: 'model',
            subject: 'mdl_gpt4o',
            kind: 'llm_request_rate',
            groupId: 'grp_red',
            openDrawer: true,
        });
    });

    it('maps an mcp_server alert to an mcp_server-scoped input', () => {
        const input = deepLinkInputFromSystemAlert({
            error_code: 'rate_limited',
            resource_type: 'mcp_server',
            resource_id: 'mcp_github',
        });
        expect(input?.scope).toBe('mcp_server');
        expect(input?.subject).toBe('mcp_github');
    });

    it.each(['llm', 'mcp', 'model'])('maps only the resource types the raisers write, not %s', (resource_type) => {
        expect(
            deepLinkInputFromSystemAlert({ error_code: 'usage_rate_limit', resource_type, resource_id: 'r1' }),
        ).toBeNull();
    });

    it('passes the error_code along as kind where the alert names no limit type', () => {
        // The Counters page tolerates a kind it does not know (it does
        // not narrow further).
        const input = deepLinkInputFromSystemAlert({
            error_code: 'cooldown_armed',
            resource_type: 'llm_model',
            resource_id: 'mdl_1',
        });
        expect(input?.kind).toBe('cooldown_armed');
    });

    it('returns null when resource_type is unmappable', () => {
        // Infra-health alerts (database / redis / qdrant) carry their
        // own resource_type tokens we don't bridge to a scope.
        expect(
            deepLinkInputFromSystemAlert({
                error_code: 'CACHE_DOWN',
                resource_type: 'cache_layer',
                resource_id: 'group_cache',
            }),
        ).toBeNull();
    });
});

describe('shouldShowCountersLinkForUsageAlert', () => {
    it('returns true when group_id is set', () => {
        expect(
            shouldShowCountersLinkForUsageAlert({
                resource_type: 'llm',
                group_id: 'grp_red',
                metric: 'requests',
            }),
        ).toBe(true);
    });

    it('returns false without group_id', () => {
        expect(
            shouldShowCountersLinkForUsageAlert({
                resource_type: 'llm',
                group_id: null,
            }),
        ).toBe(false);
    });
});

describe('deepLinkInputFromUsageAlert', () => {
    it('maps a quota alert to a group-scoped input', () => {
        const input = deepLinkInputFromUsageAlert({
            resource_type: 'llm',
            group_id: 'grp_red',
            metric: 'requests',
        });
        expect(input).toEqual({
            scope: 'group',
            subject: 'grp_red',
            kind: 'requests',
            groupId: 'grp_red',
            openDrawer: false,
        });
    });

    it('returns null without group_id', () => {
        expect(
            deepLinkInputFromUsageAlert({
                resource_type: 'llm',
                group_id: null,
            }),
        ).toBeNull();
    });
});
