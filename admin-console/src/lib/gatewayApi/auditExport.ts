/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Scheduled audit-log export destinations.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import { GatewayApiBase } from './base';
import type { ApiBodyExactly } from '../api/controlPlaneTypes';

export class AuditExportApi extends GatewayApiBase {

    // =============================================================================
    // Audit Export Destinations — scheduled proxy_log export
    //
    // Backend: app/api/v1/endpoints/admin/audit_export_destinations.py
    // Docs:    trial_bundle/docs/reference/export_proxy_log_s3.md
    //
    // The secrets of a destination (`secret_access_key`, `bearer_token`,
    // `token`, a webhook's `headers`) are never returned: a response says
    // whether each is stored (`has_secret_access_key`, `has_bearer_token`,
    // `has_token`, `has_headers`). On an update, a secret the `config`
    // does not name stays as stored, a value replaces it, and an empty
    // string or null removes it.
    // =============================================================================

    async fetchAuditExportDestinations(): Promise<import('../types').AuditExportDestinationListResponse> {
        try {
            return await this.request('/v1/admin/audit-export-destinations');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_audit_export_destinations',
                resource: 'audit_export_destinations',
            });
            throw error;
        }
    }

    async fetchAuditExportDestination(destinationId: string): Promise<import('../types').AuditExportDestination> {
        try {
            return await this.request(
                `/v1/admin/audit-export-destinations/${encodeURIComponent(destinationId)}`,
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_audit_export_destination',
                resource: 'audit_export_destination',
                destinationId,
            });
            throw error;
        }
    }

    async createAuditExportDestination(data: {
        name: string;
        destination_type: 's3' | 'webhook' | 'splunk_hec';
        enabled?: boolean;
        config: Record<string, unknown>;
        field_allowlist?: string[] | null;
        interval_seconds?: number;
    }): Promise<import('../types').AuditExportDestination> {
        try {
            return await this.request('/v1/admin/audit-export-destinations', {
                method: 'POST',
                body: JSON.stringify(data),
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_audit_export_destination',
                resource: 'audit_export_destination',
                name: data.name,
            });
            throw error;
        }
    }

    async updateAuditExportDestination<Sent>(
        destinationId: string,
        updates: ApiBodyExactly<'/v1/admin/audit-export-destinations/{destination_id}', 'patch', Sent>,
    ): Promise<import('../types').AuditExportDestination> {
        try {
            return await this.request(
                `/v1/admin/audit-export-destinations/${encodeURIComponent(destinationId)}`,
                { method: 'PATCH', body: JSON.stringify(updates) },
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_audit_export_destination',
                resource: 'audit_export_destination',
                destinationId,
            });
            throw error;
        }
    }

    async deleteAuditExportDestination(destinationId: string): Promise<{ deleted: boolean }> {
        try {
            return await this.request(
                `/v1/admin/audit-export-destinations/${encodeURIComponent(destinationId)}`,
                { method: 'DELETE' },
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_audit_export_destination',
                resource: 'audit_export_destination',
                destinationId,
            });
            throw error;
        }
    }

    async runAuditExportDestinationNow(destinationId: string): Promise<import('../types').AuditExportRunResult> {
        try {
            return await this.request(
                `/v1/admin/audit-export-destinations/${encodeURIComponent(destinationId)}/run-now`,
                { method: 'POST' },
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'run_audit_export_destination_now',
                resource: 'audit_export_destination',
                destinationId,
            });
            throw error;
        }
    }
}
