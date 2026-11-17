/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The proxy-log filter envelope — the one mapping from 16 filter states to
 * the body that /search, /facets and the export send.
 *
 * Tested directly, not through the 1900-line component that calls it, to
 * pin the parts that are easy to get subtly wrong: the tri-state status
 * facet (two selections mean *no* filter, not "both"), the string→int
 * coercion HTTP status needs because the column is an int, and the rule that
 * an empty filter must be omitted rather than sent as an empty list — a
 * `server_ids: []` on the wire reads as "match nothing" and empties the table.
 */

import { describe, it, expect } from 'vitest';

import type { ApiRequestBody } from '../../../lib/api/controlPlaneTypes';
import {
    buildProxyLogFilterEnvelope,
    ENTERPRISE_GATEWAY_PROXY_TYPES,
    isProxyType,
    PROXY_TYPE_VALUES,
    type ProxyLogFilterEnvelope,
    type ProxyLogFilterState,
    type ProxyType,
} from '../proxyLogFilterEnvelope';

const base = (over: Partial<ProxyLogFilterState> = {}): ProxyLogFilterState => ({
    timeRange: { kind: 'preset', minutes: 60 * 24 },
    proxyTypesFilter: [],
    successFilter: [],
    serverFilter: [],
    gatewayUserFilter: [],
    idpUserFilter: [],
    targetUrlFilter: [],
    capabilityFilter: [],
    correlationIdFilter: '',
    sessionIdFilter: '',
    minDuration: '',
    maxDuration: '',
    complianceTagFilter: [],
    regionFilter: [],
    httpStatusFilter: [],
    freeText: '',
    ...over,
});

describe('buildProxyLogFilterEnvelope', () => {
    it('omits every unset filter rather than sending empty lists', () => {
        const env = buildProxyLogFilterEnvelope(base());
        // Only the time range survives an otherwise-empty filter set.
        const set = Object.entries(env).filter(([, v]) => v !== undefined);
        expect(Object.fromEntries(set)).toEqual({ hours_ago: 24 });
    });

    it('treats the status facet as tri-state', () => {
        expect(buildProxyLogFilterEnvelope(base({ successFilter: ['true'] })).success).toBe(true);
        expect(buildProxyLogFilterEnvelope(base({ successFilter: ['false'] })).success).toBe(false);
        // Both selected is the same scope as neither — it must not filter.
        expect(buildProxyLogFilterEnvelope(base({ successFilter: ['true', 'false'] })).success)
            .toBeUndefined();
        expect(buildProxyLogFilterEnvelope(base({ successFilter: [] })).success).toBeUndefined();
    });

    it('coerces HTTP status to ints and drops unparseable values', () => {
        const env = buildProxyLogFilterEnvelope(
            base({ httpStatusFilter: ['200', '503', 'not-a-status'] }),
        );
        expect(env.http_status_codes).toEqual([200, 503]);
    });

    it('parses duration bounds as numbers and omits blanks', () => {
        const env = buildProxyLogFilterEnvelope(base({ minDuration: '150.5', maxDuration: '' }));
        expect(env.min_duration_ms).toBe(150.5);
        expect(env.max_duration_ms).toBeUndefined();
    });

    it('sends compliance tags under the plural OR-overlap key', () => {
        const env = buildProxyLogFilterEnvelope(base({ complianceTagFilter: ['gdpr', 'hipaa'] }));
        expect(env.compliance_tags_any).toEqual(['gdpr', 'hipaa']);
        expect('compliance_tag' in env).toBe(false);
    });

    it('carries a sub-hour preset as explicit start/end, not a floored hours_ago', () => {
        // The backend floors hours_ago to 1, so a 15-minute window sent that
        // way would silently widen to an hour.
        const env = buildProxyLogFilterEnvelope(
            base({ timeRange: { kind: 'preset', minutes: 15 } }),
        );
        expect(env.hours_ago).toBeUndefined();
        expect(env.start_time).toBeDefined();
        expect(env.end_time).toBeDefined();
    });

    it('maps the remaining facets onto their wire names', () => {
        const env = buildProxyLogFilterEnvelope(base({
            proxyTypesFilter: ['llm'],
            serverFilter: ['srv_1'],
            gatewayUserFilter: ['usr_1'],
            idpUserFilter: ['alice'],
            targetUrlFilter: ['https://api.example'],
            capabilityFilter: ['tools/call'],
            correlationIdFilter: 'corr_1',
            sessionIdFilter: 'sess_1',
            regionFilter: ['eu-west-1'],
            freeText: 'timeout',
        }));
        expect(env).toMatchObject({
            proxy_types: ['llm'],
            server_ids: ['srv_1'],
            gateway_user_ids: ['usr_1'],
            idp_user_usernames: ['alice'],
            target_urls: ['https://api.example'],
            mcp_capability_names: ['tools/call'],
            correlation_id: 'corr_1',
            session_id: 'sess_1',
            region_routed_to: ['eu-west-1'],
            q: 'timeout',
        });
    });

    it('rejects proxy_type values the control plane does not accept', () => {
        // A saved view can carry `virtual_mcp`, and the column an empty
        // string; letting one through makes the control plane refuse the
        // whole search. (`skill` is accepted: the gateway writes it.)
        expect(isProxyType('llm')).toBe(true);
        expect(isProxyType('virtual_mcp')).toBe(false);
        expect(isProxyType('')).toBe(false);
    });
});

describe('the export of the filter envelope', () => {
    it('is the body of the list: every key of the envelope is a field of the export body', () => {
        type ExportBody = ApiRequestBody<'/v1/admin/proxy-logs/export', 'post'>;
        // Compiles only while no key of the envelope is outside the export's body.
        const everyKeyInBody: [Exclude<keyof ProxyLogFilterEnvelope, keyof ExportBody>] extends [never] ? true : false = true;
        expect(everyKeyInBody).toBe(true);
        const envelope = buildProxyLogFilterEnvelope(
            base({ serverFilter: ['srv-a', 'srv-b'], freeText: 'timeout', httpStatusFilter: ['502'] }),
        );
        expect(envelope).toMatchObject({ server_ids: ['srv-a', 'srv-b'], q: 'timeout', http_status_codes: [502] });
    });
});

describe('the proxy types of the filter', () => {
    // What the gateway writes in `proxy_logs.proxy_type`, in its order
    // (`proxy::ALL`, brutor-gateway-core `src/constants.rs`), and what the
    // control plane's filter accepts.
    const WRITTEN = ['mcp', 'virtual-mcp', 'llm', 'skill', 'oauth', 'a2a', 'batch', 'portal'];

    it('are exactly the ones the gateway writes', () => {
        expect([...PROXY_TYPE_VALUES]).toEqual(WRITTEN);
    });

    it('are the ones the control plane takes in a search: its own type says so', () => {
        // Fails to compile when the two differ, in either direction.
        const taken: Array<NonNullable<NonNullable<ApiRequestBody<'/v1/admin/proxy-logs/query', 'post'>['proxy_types']>[number]>> = [
            ...PROXY_TYPE_VALUES,
        ];
        const ours: ProxyType[] = taken;
        expect(ours).toEqual(WRITTEN);
    });

    it('take a skill row and a virtual server row; the name nothing writes is not one', () => {
        expect(isProxyType('skill')).toBe(true);
        expect(isProxyType('virtual-mcp')).toBe(true);
        expect(isProxyType('virtual_mcp')).toBe(false);
        expect(isProxyType('')).toBe(false);
    });

    it('names the two only an Enterprise gateway writes: a Community gateway writes the other six', () => {
        expect([...ENTERPRISE_GATEWAY_PROXY_TYPES]).toEqual(['batch', 'portal']);
        expect(PROXY_TYPE_VALUES.filter((type) => !(ENTERPRISE_GATEWAY_PROXY_TYPES as readonly string[]).includes(type))).toEqual([
            'mcp', 'virtual-mcp', 'llm', 'skill', 'oauth', 'a2a',
        ]);
    });
});
