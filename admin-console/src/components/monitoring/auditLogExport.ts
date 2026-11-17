/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The export of the admin audit log, from the filters of the list.
 *
 * The list (`POST /v1/admin/users/audit-logs/search`) filters by several
 * administrators, actions, resource types, IP addresses and HTTP methods,
 * by the outcome, a time window and free text. The export route
 * (`GET /v1/admin/users/audit-logs/export`) takes the same filters as query
 * parameters, under the same names and with the same meaning.
 *
 * `auditLogExportParams` turns the list's filter envelope into the export's
 * parameters: every filter that is set, with every value, so the file holds
 * the rows the list shows.
 */

import type { AuditLogsExportParams } from '../../lib/types';

/** The filters of the list, as the search sends them. */
export interface AuditLogFilterEnvelope {
    hours_ago?: number;
    start_time?: string;
    end_time?: string;
    admin_ids?: readonly string[];
    actions?: readonly string[];
    resource_types?: readonly string[];
    ip_addresses?: readonly string[];
    request_methods?: readonly string[];
    success?: boolean;
    q?: string;
}

/** The filters that hold a list of exact values. */
const LIST_FILTERS = ['admin_ids', 'actions', 'resource_types', 'ip_addresses', 'request_methods'] as const;

/** The parameters of the export of what the envelope selects. A filter that is not set is not sent. */
export function auditLogExportParams(envelope: AuditLogFilterEnvelope): AuditLogsExportParams {
    const params: AuditLogsExportParams = {};
    if (envelope.hours_ago !== undefined) params.hours_ago = envelope.hours_ago;
    if (envelope.start_time) params.start_time = envelope.start_time;
    if (envelope.end_time) params.end_time = envelope.end_time;
    for (const key of LIST_FILTERS) {
        const values = envelope[key];
        if (values !== undefined && values.length > 0) params[key] = [...values];
    }
    if (envelope.success !== undefined) params.success = envelope.success;
    if (envelope.q) params.q = envelope.q;
    return params;
}
