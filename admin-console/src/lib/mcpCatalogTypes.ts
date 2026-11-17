/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCP Server Catalog Types
 */

export interface CatalogHeader {
    name: string;
    description?: string;
    key: string;
    required: boolean;
    sensitive: boolean;
}

export interface CatalogEnvVar {
    key: string;
    name: string;
    required: boolean;
    sensitive: boolean;
    description: string;
}

export interface CatalogToolPreview {
    name: string;
    description: string;
    params: Record<string, string>;
}

export interface CatalogRemoteConfig {
    fixedURL?: string;
    hostname?: string;
    headers?: CatalogHeader[];
}

export interface CatalogContainerizedConfig {
    image: string;
    port: number;
    path: string;
    args?: string[];
}

export interface CatalogMetadataExtra {
    allow_multiple?: boolean;
    unsupported_tools?: string[];
}

/**
 * Trust tier values mirror the A2A agent-card vocabulary.
 *   - `unsigned`        — default; no signature applied
 *   - `tenant_signed`   — signed by this tenant's Ed25519 key
 *   - `partner_signed`  — reserved for cross-tenant trust (future)
 *   - `revoked`         — terminal; entry cannot be re-signed
 */
export type CatalogTrustTier = 'unsigned' | 'tenant_signed' | 'partner_signed' | 'revoked';

export interface MCPCatalogEntryBrief {
    id: string;
    /** The tenant a custom entry belongs to; null on an entry of the system catalog, which belongs to none. */
    tenant_id?: string | null;
    name: string;
    slug: string;
    short_description: string | null;
    icon_url: string | null;
    has_icon: boolean;
    repo_url: string | null;
    runtime: 'remote' | 'containerized';
    categories: string[];
    auth_pattern: 'oauth' | 'api_key' | 'header' | 'none';
    source: 'system' | 'custom';
    enabled: boolean;
    // Trust attestation metadata
    trust_tier?: CatalogTrustTier;
    signed_by?: string | null;
    signed_at?: string | null;
    signature_algorithm?: string | null;
    created_at: string | null;
    updated_at: string | null;
}

export interface MCPCatalogEntry extends MCPCatalogEntryBrief {
    description: string | null;
    remote_config: CatalogRemoteConfig | null;
    containerized_config: CatalogContainerizedConfig | null;
    env_vars: CatalogEnvVar[];
    tool_preview: CatalogToolPreview[];
    metadata_extra: CatalogMetadataExtra | null;
}

export interface CatalogEntryListResponse {
    entries: MCPCatalogEntryBrief[];
    total: number;
}

export interface CategoryCount {
    name: string;
    count: number;
}

export interface CatalogCategoriesResponse {
    categories: CategoryCount[];
}

export interface CatalogImportCredentials {
    client_id?: string;
    client_secret?: string;
    api_key?: string;
    header_values?: Record<string, string>;
    env_values?: Record<string, string>;
}

export interface CatalogImportRequest {
    name_override?: string;
    credentials?: CatalogImportCredentials;
    deploy_container?: boolean;
    enabled?: boolean;
}

export interface CatalogImportResponse {
    mcp_server_id: string;
    idp_server_id: string | null;
    oauth_client_id: string | null;
    server_config_id: string;
    docker_deployment_id: string | null;
    message: string;
    // Post-deploy probe outcome. Empty when the import is clean.
    // Populated when the MCP server is reachable but failing — e.g.
    // wrapped binary crashed because a required CLI flag wasn't set.
    // The import modal renders these as an amber banner so the
    // operator doesn't leave thinking everything is fine.
    deployment_warnings?: string[];
    // Tail of the container log captured during the failed probe —
    // raw evidence for operators who want to debug further.
    container_log_excerpt?: string | null;
}

export interface CatalogEntryCreateRequest {
    name: string;
    slug: string;
    short_description?: string;
    description?: string;
    icon_url?: string;
    repo_url?: string;
    runtime: 'remote' | 'containerized';
    remote_config?: CatalogRemoteConfig;
    containerized_config?: CatalogContainerizedConfig;
    env_vars?: CatalogEnvVar[];
    tool_preview?: CatalogToolPreview[];
    categories?: string[];
    metadata_extra?: CatalogMetadataExtra;
    auth_pattern?: string;
    enabled?: boolean;
}

export interface CatalogEntryUpdateRequest {
    name?: string;
    slug?: string;
    short_description?: string;
    description?: string;
    icon_url?: string;
    repo_url?: string;
    runtime?: 'remote' | 'containerized';
    remote_config?: CatalogRemoteConfig;
    containerized_config?: CatalogContainerizedConfig;
    env_vars?: CatalogEnvVar[];
    tool_preview?: CatalogToolPreview[];
    categories?: string[];
    metadata_extra?: CatalogMetadataExtra;
    auth_pattern?: string;
    enabled?: boolean;
}

export interface CatalogListParams {
    search?: string;
    runtime?: string;
    auth_pattern?: string;
    category?: string;
    source?: string;
    enabled_only?: boolean;
    offset?: number;
    limit?: number;
}
