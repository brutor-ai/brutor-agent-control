/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * TypeScript shapes for the `LlmProvider` admin REST surface
 * (`/v1/admin/providers`). Mirror the Pydantic models in
 * `brutor-gateway-control/app/api/v1/models/admin/llm_provider.py`.
 *
 * Providers are first-class entities. The admin UI has no
 * hardcoded provider list: it fetches this one at mount via
 * `gatewayApiClient.listProviders()` so adding/renaming/disabling
 * a provider in the DB shows up everywhere with no rebuild.
 */

export type LlmProviderCategory =
    | 'chat'
    | 'image'
    | 'audio'
    | 'video'
    | 'embeddings'
    | 'self_hosted'
    | 'internal';

export type LlmProviderAuthType =
    | 'api_key'
    | 'oauth2'
    | 'gcp_service_account'
    | 'aws_iam'
    | 'azure_ad'
    | 'none';

export interface LlmProviderResponse {
    id: string;
    tenant_id: string;
    /** Lowercase slug — what the Rust proxy adapter dispatch matches on. */
    key: string;
    display_name: string;
    category: LlmProviderCategory | null;
    developer_url: string | null;
    api_key_url: string | null;
    pricing_url: string | null;
    /** Human-facing docs page about this provider's models
     *  (e.g. https://platform.openai.com/docs/models). The admin UI
     *  renders it as a click-out anchor on the provider row. Never
     *  read by the proxy. NOT to be confused with `models_api_url`. */
    model_catalog_url: string | null;
    /** Machine-callable /models API endpoint
     *  (e.g. https://api.openai.com/v1/models). The proxy's
     *  catalog-probe health check issues `GET <models_api_url>` with
     *  the model's API key. NULL → falls back to a real-inference
     *  probe. NOT a docs link — see `model_catalog_url`. */
    models_api_url: string | null;
    icon_url: string | null;
    default_api_url: string | null;
    default_auth_type: LlmProviderAuthType | null;
    /** Provider-wide request-body parameter drop list.
     *  Inherited at runtime by every model under this provider; the
     *  proxy unions this with each model's own additional_drop_params
     *  before forwarding upstream. Edited from the provider admin
     *  page; the LLModelForm renders it as read-only "Inherited" chips
     *  next to the model's own list. */
    additional_drop_params: string[] | null;
    notes: string | null;
    metadata: Record<string, unknown> | null;
    enabled: boolean;
    /** True for providers seeded by `init_llm_providers.py` — cannot
     * be deleted via the admin API; can be edited / soft-disabled. */
    is_system: boolean;
    /** When true, the GET icon endpoint has bytes to
     *  serve (`/v1/admin/providers/{id}/icon`). The blob itself is
     *  never inlined in JSON. */
    has_icon?: boolean;
    icon_mime_type?: string | null;
    icon_size?: number | null;
    /** ISO timestamp of the last icon upload — used to bust browser
     *  cache when the operator replaces the icon. */
    icon_uploaded_at?: string | null;
    /** Geographic region this provider serves from ('eu-west-1', 'global', …).
     *  Read by the residency check in the proxy, so an unset region is a
     *  provider the check cannot refuse. */
    region?: string | null;
    /** Operator-authored label, edited on the Audit Info tab. */
    version?: string | null;
    created_by?: string | null;
    created_at: string | null;
    updated_by?: string | null;
    updated_at: string | null;
    /** Populated only when the list endpoint is called with
     * `include_model_count=true`, or always on the detail endpoint. */
    model_count?: number;
}

export interface LlmProviderListResponse {
    providers: LlmProviderResponse[];
    total: number;
}

export interface LlmProviderCreateRequest {
    /** Geographic region this provider serves from ('eu-west-1', 'global', …).
     *  Read by the residency check in the proxy, so an unset region is a
     *  provider the check cannot refuse. */
    region?: string | null;
    /** Operator-authored label. */
    version?: string;
    key: string;
    display_name: string;
    category?: LlmProviderCategory;
    developer_url?: string;
    api_key_url?: string;
    pricing_url?: string;
    model_catalog_url?: string;
    models_api_url?: string;
    icon_url?: string;
    default_api_url?: string;
    default_auth_type?: LlmProviderAuthType;
    additional_drop_params?: string[];
    notes?: string;
    metadata?: Record<string, unknown>;
    enabled?: boolean;
}

export interface LlmProviderUpdateRequest {
    /** Geographic region this provider serves from ('eu-west-1', 'global', …).
     *  Read by the residency check in the proxy, so an unset region is a
     *  provider the check cannot refuse. */
    region?: string | null;
    /** Operator-authored label. */
    version?: string;
    display_name?: string;
    category?: LlmProviderCategory;
    developer_url?: string | null;
    api_key_url?: string | null;
    pricing_url?: string | null;
    model_catalog_url?: string | null;
    models_api_url?: string | null;
    icon_url?: string | null;
    default_api_url?: string | null;
    default_auth_type?: LlmProviderAuthType;
    additional_drop_params?: string[] | null;
    notes?: string | null;
    metadata?: Record<string, unknown> | null;
    enabled?: boolean;
}

/** Optional query params for the list endpoint. */
export interface LlmProviderListOptions {
    category?: LlmProviderCategory;
    enabled?: boolean;
    includeModelCount?: boolean;
}

/**
 * A single entry in the GLOBAL provider catalog
 * (`GET /v1/admin/provider-catalog`).
 *
 * Unlike `LlmProviderResponse` (the per-tenant *configured* providers in
 * `llm_providers`), the catalog is the full browse list of every provider
 * the platform knows about — used to populate the model-form provider
 * dropdown so any provider is pickable even when not yet configured. The
 * backend auto-imports the chosen provider into `llm_providers` when a
 * model is created with its `key`.
 *
 * The `id` is the synthetic `provcat-{key}` identifier; there is no
 * per-tenant enabled state and no uploadable icon row here (icons are the
 * static `icon_url` brand mark).
 */
export interface LlmProviderCatalogEntry {
    /** Synthetic catalog id (`provcat-{key}`). */
    id: string;
    /** Lowercase slug — what gets sent as `provider` on model create. */
    key: string;
    display_name: string;
    category: LlmProviderCategory | null;
    /** Static brand-mark URL, when the catalog row has one. */
    icon_url: string | null;
}

export interface LlmProviderCatalogListResponse {
    providers: LlmProviderCatalogEntry[];
    total: number;
}
