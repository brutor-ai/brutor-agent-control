/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The export of the audit log: from the filters of the list to the
 * parameters of the export route.
 *
 * The export route takes the filters of the list under the same names and
 * with the same meaning: several administrators, actions, resource types,
 * IP addresses and HTTP methods, the outcome, a time window and free text.
 * Every filter the list applies is sent, so the file holds the rows the
 * list shows.
 */

import { describe, expect, it } from 'vitest';

import { auditLogExportParams } from '../auditLogExport';

describe('auditLogExportParams', () => {
    it('sends the time window of the list as the list sends it', () => {
        expect(auditLogExportParams({ hours_ago: 24 })).toEqual({ hours_ago: 24 });
        expect(auditLogExportParams({ start_time: '2026-11-01T00:00:00.000Z', end_time: '2026-11-02T00:00:00.000Z' })).toEqual({
            start_time: '2026-11-01T00:00:00.000Z',
            end_time: '2026-11-02T00:00:00.000Z',
        });
    });

    it('sends every value of every filter under the name the list sends it by', () => {
        expect(
            auditLogExportParams({
                hours_ago: 1,
                admin_ids: ['adm-1', 'adm-2'],
                actions: ['create_user', 'delete_user', 'login'],
                resource_types: ['tenant'],
                ip_addresses: ['10.0.0.1', '10.0.0.2'],
                request_methods: ['POST'],
                success: false,
                q: 'tenant',
            }),
        ).toEqual({
            hours_ago: 1,
            admin_ids: ['adm-1', 'adm-2'],
            actions: ['create_user', 'delete_user', 'login'],
            resource_types: ['tenant'],
            ip_addresses: ['10.0.0.1', '10.0.0.2'],
            request_methods: ['POST'],
            success: false,
            q: 'tenant',
        });
    });

    it('sends nothing for a filter that is not set', () => {
        expect(auditLogExportParams({ hours_ago: 24, admin_ids: [], actions: undefined, q: '', success: undefined })).toEqual({ hours_ago: 24 });
        expect(auditLogExportParams({})).toEqual({});
    });

    it('sends the outcome when it is "succeeded" as well as when it is "failed"', () => {
        expect(auditLogExportParams({ success: true })).toEqual({ success: true });
    });
});
