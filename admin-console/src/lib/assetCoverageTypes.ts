/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** The coverage part of the unified registry (`GET /v1/admin/agent-registry`). */

/** Tenant-wide coverage scorecard (unfiltered) from `/v1/admin/agent-registry`. */
export interface AssetCoverage {
    governed: number;
    observed: number;
    ungoverned: number;
    total_seen: number;
}

/**
 * A discovered-but-ungoverned ("shadow AI") asset surfaced by the out-of-band
 * discovery plane — observed in the environment but not flowing through / not
 * configured in the gateway. Read-only here; onboarding lives in Discovery.
 */
export interface DiscoveredUngoverned {
    id: string;
    asset_key: string;
    asset_type: string;
    provider?: string | null;
    display_name?: string | null;
    computed_confidence?: number | null;
    evidence_count?: number | null;
    last_seen?: string | null;
}

/** What `GET /v1/admin/agent-registry` answers, as far as coverage goes. */
export interface AssetCoverageResponse {
    coverage?: AssetCoverage | null;
    discovered_ungoverned?: DiscoveredUngoverned[] | null;
}

/** What the coverage panels of the Asset Register show. */
export interface AssetCoverageData {
    coverage: AssetCoverage;
    ungoverned: DiscoveredUngoverned[];
}
