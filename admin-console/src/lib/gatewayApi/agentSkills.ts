/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent skills: lifecycle, catalog, files, sources and export. (The skill
 * workflow builder is not in this release: its routes are not called.)
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import {
    AgentSkillConfig,
    AgentSkillCreateRequest,
    AgentSkillListResponse,
    AgentSkillDetailResponse,
    AgentSkillCatalogResponse,
    AgentSkillImportResponse,
    SkillValidationResponse,
    AgentSkillSearchRequest,
    AgentSkillSearchResponse,
    ExecutionSummary,
    AgentSkillResource,
    AgentSkillResourceCreateRequest,
    AgentSkillResourceListResponse,
    AgentSkillScript,
    AgentSkillScriptCreateRequest,
    AgentSkillScriptListResponse,
    AgentSkillAsset,
    AgentSkillAssetCreateRequest,
    AgentSkillAssetListResponse,
    SkillSourceResponse,
    SkillSourceListResponse,
    SkillSourceCreateRequest,
} from '../agentSkillTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly } from '../api/controlPlaneTypes';

export class AgentSkillsApi extends GatewayApiBase {

    // ============================================================================
    // Agent Skill CRUD
    // ============================================================================

    /**
     * List all agent skills for the current tenant.
     * Returns a dict keyed by skill ID (matches LLM listing pattern).
     */
    async listAgentSkills(params?: {
        status?: string;
        enabled?: boolean;
        category?: string;
        include_groups?: boolean;
        include_content?: boolean;
    }): Promise<AgentSkillListResponse> {
        const searchParams = new URLSearchParams();
        if (params?.status) searchParams.append('status', params.status);
        if (params?.enabled !== undefined) searchParams.append('enabled', String(params.enabled));
        if (params?.category) searchParams.append('category', params.category);
        if (params?.include_groups !== undefined) searchParams.append('include_groups', String(params.include_groups));
        if (params?.include_content !== undefined) searchParams.append('include_content', String(params.include_content));

        const qs = searchParams.toString();
        const url = qs ? `/v1/admin/agent-skills?${qs}` : '/v1/admin/agent-skills';
        return this.request<AgentSkillListResponse>(url, { method: 'GET' });
    }

    /**
     * Get a single agent skill by ID with full details.
     */
    async getAgentSkill(skillId: string): Promise<AgentSkillConfig> {
        const response = await this.request<AgentSkillDetailResponse>(
            `/v1/admin/agent-skills/${skillId}`,
            { method: 'GET' }
        );
        return response.skill;
    }

    /**
     * Create a new agent skill.
     *
     * Returns the full DetailResponse so callers can read the
     * `auto_bound_server_group_ids` side-effect field — the
     * backend auto-binds the system Agent Skill MCP server to any
     * group that lacks it whenever a skill→group assignment runs,
     * and the UI surfaces a toast informing the admin. Pre-bound
     * groups return an empty array / null.
     */
    async createAgentSkill(data: AgentSkillCreateRequest): Promise<AgentSkillDetailResponse> {
        return this.request<AgentSkillDetailResponse>(
            '/v1/admin/agent-skills',
            { method: 'POST', body: JSON.stringify(data) }
        );
    }

    /**
     * Update an existing agent skill (partial update).
     * Published skills cannot be updated. Returns the full
     * DetailResponse for the same reason as createAgentSkill —
     * see comment there.
     */
    async updateAgentSkill<Sent>(skillId: string, data: ApiBodyExactly<'/v1/admin/agent-skills/{skill_id}', 'patch', Sent>): Promise<AgentSkillDetailResponse> {
        return this.request<AgentSkillDetailResponse>(
            `/v1/admin/agent-skills/${skillId}`,
            { method: 'PATCH', body: JSON.stringify(data) }
        );
    }

    /**
     * Delete an agent skill.
     */
    async deleteAgentSkill(skillId: string): Promise<{ success: boolean; message: string }> {
        return this.request<{ success: boolean; message: string }>(
            `/v1/admin/agent-skills/${skillId}`,
            { method: 'DELETE' }
        );
    }

    /**
     * Bulk-delete agent skills. Published skills are skipped (returned in
     * `skipped` with a reason) rather than failing the whole call.
     */
    async bulkDeleteAgentSkills(skillIds: string[]): Promise<{
        deleted: string[];
        skipped: { skill_id: string; reason: string }[];
        deleted_count: number;
        skipped_count: number;
    }> {
        return this.request(
            `/v1/admin/agent-skills/bulk-delete`,
            { method: 'POST', body: JSON.stringify({ skill_ids: skillIds }) }
        );
    }

    // ============================================================================
    // Agent Skill Lifecycle
    // ============================================================================

    /**
     * Validate a skill against agentskills.io spec compliance.
     * Transitions: draft → validated (if valid).
     */
    async validateAgentSkill(skillId: string): Promise<SkillValidationResponse> {
        return this.request<SkillValidationResponse>(
            `/v1/admin/agent-skills/${skillId}/validate`,
            { method: 'POST', body: JSON.stringify({ skill_id: skillId }) }
        );
    }

    /**
     * Publish a skill (draft/validated → published).
     * Published skills become immutable.
     */
    async publishAgentSkill(skillId: string): Promise<AgentSkillConfig> {
        const response = await this.request<AgentSkillDetailResponse>(
            `/v1/admin/agent-skills/${skillId}/publish`,
            { method: 'POST', body: JSON.stringify({ skill_id: skillId }) }
        );
        return response.skill;
    }

    /**
     * Deprecate a published skill.
     * Still executable but marked as superseded.
     */
    async deprecateAgentSkill(skillId: string, reason: string): Promise<AgentSkillConfig> {
        const response = await this.request<AgentSkillDetailResponse>(
            `/v1/admin/agent-skills/${skillId}/deprecate`,
            { method: 'POST', body: JSON.stringify({ skill_id: skillId, reason }) }
        );
        return response.skill;
    }

    /**
     * Archive a skill (any status → archived).
     * Fully disables the skill.
     */
    async archiveAgentSkill(skillId: string): Promise<AgentSkillConfig> {
        const response = await this.request<AgentSkillDetailResponse>(
            `/v1/admin/agent-skills/${skillId}/archive`,
            { method: 'POST', body: JSON.stringify({}) }
        );
        return response.skill;
    }

    // ============================================================================
    // Agent Skill Import
    // ============================================================================

    /**
     * Import agent skills from zip file(s).
     * Each zip should contain a SKILL.md at the root directory level.
     * Skills are imported as DRAFT and assigned to the Default Resource Group.
     */
    async importAgentSkills(files: File[]): Promise<AgentSkillImportResponse> {
        const formData = new FormData();
        for (const file of files) {
            formData.append('files', file);
        }
        return this.request<AgentSkillImportResponse>(
            '/v1/admin/agent-skills/import',
            {
                method: 'POST',
                body: formData,
                headers: {} // Let browser set Content-Type for FormData
            }
        );
    }

    // ============================================================================
    // Agent Skill Catalog & Search
    // ============================================================================

    /**
     * Get lightweight skill catalog (published + enabled only).
     * ~50-100 tokens per entry. For agent discovery.
     */
    async getAgentSkillCatalog(): Promise<AgentSkillCatalogResponse> {
        return this.request<AgentSkillCatalogResponse>(
            '/v1/admin/agent-skills/catalog',
            { method: 'GET' }
        );
    }

    /**
     * Advanced search across skills with pagination and filtering.
     */
    async searchAgentSkills(request: AgentSkillSearchRequest): Promise<AgentSkillSearchResponse> {
        return this.request<AgentSkillSearchResponse>(
            '/v1/admin/agent-skills/search',
            { method: 'POST', body: JSON.stringify(request) }
        );
    }

    // ============================================================================
    // Agent Skill Group Assignments
    // ============================================================================

    /**
     * Assign skill to additional groups (merges with existing).
     */
    async assignSkillToGroups(skillId: string, groupIds: string[]): Promise<AgentSkillConfig> {
        const response = await this.request<AgentSkillDetailResponse>(
            `/v1/admin/agent-skills/${skillId}/groups`,
            { method: 'POST', body: JSON.stringify({ group_ids: groupIds }) }
        );
        return response.skill;
    }

    /**
     * Remove skill from groups.
     */
    async removeSkillFromGroups(skillId: string, groupIds: string[]): Promise<AgentSkillConfig> {
        const response = await this.request<AgentSkillDetailResponse>(
            `/v1/admin/agent-skills/${skillId}/groups`,
            { method: 'DELETE', body: JSON.stringify({ group_ids: groupIds }) }
        );
        return response.skill;
    }

    // ============================================================================
    // Agent Skill Executions
    // ============================================================================

    /**
     * List executions for a skill (with pagination).
     */
    async listAgentSkillExecutions(
        skillId: string,
        params?: { status?: string; offset?: number; limit?: number }
    ): Promise<{ executions: ExecutionSummary[]; total: number }> {
        const searchParams = new URLSearchParams();
        if (params?.status) searchParams.append('status', params.status);
        if (params?.offset !== undefined) searchParams.append('offset', String(params.offset));
        if (params?.limit !== undefined) searchParams.append('limit', String(params.limit));

        const qs = searchParams.toString();
        const url = qs
            ? `/v1/admin/agent-skills/${skillId}/executions?${qs}`
            : `/v1/admin/agent-skills/${skillId}/executions`;

        return this.request<{ executions: ExecutionSummary[]; total: number }>(url, { method: 'GET' });
    }

    /**
     * Get a single execution detail.
     */
    async getAgentSkillExecution(executionId: string): Promise<any> {
        const response = await this.request<{ execution: any }>(
            `/v1/admin/agent-skills/executions/${executionId}`,
            { method: 'GET' }
        );
        return response.execution;
    }

    // =============================================================================
    // Skill Resources (references/ directory)
    // =============================================================================

    /**
     * List all resource files for a skill (without content bodies).
     */
    async listSkillResources(skillId: string): Promise<AgentSkillResourceListResponse> {
        return this.request<AgentSkillResourceListResponse>(
            `/v1/admin/agent-skills/${skillId}/resources`,
            { method: 'GET' }
        );
    }

    /**
     * Get a single resource file including its content.
     */
    async getSkillResource(skillId: string, resourceId: string): Promise<AgentSkillResource> {
        const resp = await this.request<{ resource: AgentSkillResource }>(
            `/v1/admin/agent-skills/${skillId}/resources/${resourceId}`,
            { method: 'GET' }
        );
        return resp.resource;
    }

    /**
     * Create a new resource file for a skill.
     */
    async createSkillResource(
        skillId: string,
        data: AgentSkillResourceCreateRequest
    ): Promise<AgentSkillResource> {
        const resp = await this.request<{ resource: AgentSkillResource }>(
            `/v1/admin/agent-skills/${skillId}/resources`,
            { method: 'POST', body: JSON.stringify(data) }
        );
        return resp.resource;
    }

    /**
     * Update a resource file.
     */
    async updateSkillResource<Sent>(
        skillId: string,
        resourceId: string,
        data: ApiBodyExactly<'/v1/admin/agent-skills/{skill_id}/resources/{resource_id}', 'patch', Sent>
    ): Promise<AgentSkillResource> {
        const resp = await this.request<{ resource: AgentSkillResource }>(
            `/v1/admin/agent-skills/${skillId}/resources/${resourceId}`,
            { method: 'PATCH', body: JSON.stringify(data) }
        );
        return resp.resource;
    }

    /**
     * Delete a resource file.
     */
    async deleteSkillResource(
        skillId: string,
        resourceId: string
    ): Promise<{ success: boolean; message: string }> {
        return this.request<{ success: boolean; message: string }>(
            `/v1/admin/agent-skills/${skillId}/resources/${resourceId}`,
            { method: 'DELETE' }
        );
    }

    // =============================================================================
    // Skill Scripts (scripts/ directory)
    // =============================================================================

    /**
     * List all script files for a skill (without content bodies).
     */
    async listSkillScripts(skillId: string): Promise<AgentSkillScriptListResponse> {
        return this.request<AgentSkillScriptListResponse>(
            `/v1/admin/agent-skills/${skillId}/scripts`,
            { method: 'GET' }
        );
    }

    /**
     * Get a single script file including source code.
     */
    async getSkillScript(skillId: string, scriptId: string): Promise<AgentSkillScript> {
        const resp = await this.request<{ script: AgentSkillScript }>(
            `/v1/admin/agent-skills/${skillId}/scripts/${scriptId}`,
            { method: 'GET' }
        );
        return resp.script;
    }

    /**
     * Create a new script file for a skill.
     */
    async createSkillScript(
        skillId: string,
        data: AgentSkillScriptCreateRequest
    ): Promise<AgentSkillScript> {
        const resp = await this.request<{ script: AgentSkillScript }>(
            `/v1/admin/agent-skills/${skillId}/scripts`,
            { method: 'POST', body: JSON.stringify(data) }
        );
        return resp.script;
    }

    /**
     * Update a script file.
     */
    async updateSkillScript<Sent>(
        skillId: string,
        scriptId: string,
        data: ApiBodyExactly<'/v1/admin/agent-skills/{skill_id}/scripts/{script_id}', 'patch', Sent>
    ): Promise<AgentSkillScript> {
        const resp = await this.request<{ script: AgentSkillScript }>(
            `/v1/admin/agent-skills/${skillId}/scripts/${scriptId}`,
            { method: 'PATCH', body: JSON.stringify(data) }
        );
        return resp.script;
    }

    /**
     * Delete a script file.
     */
    async deleteSkillScript(
        skillId: string,
        scriptId: string
    ): Promise<{ success: boolean; message: string }> {
        return this.request<{ success: boolean; message: string }>(
            `/v1/admin/agent-skills/${skillId}/scripts/${scriptId}`,
            { method: 'DELETE' }
        );
    }

    // =============================================================================
    // Skill Assets (assets/ directory)
    // =============================================================================

    /**
     * List all asset files for a skill (without content bodies).
     */
    async listSkillAssets(skillId: string): Promise<AgentSkillAssetListResponse> {
        return this.request<AgentSkillAssetListResponse>(
            `/v1/admin/agent-skills/${skillId}/assets`,
            { method: 'GET' }
        );
    }

    /**
     * Get a single asset file including content.
     */
    async getSkillAsset(skillId: string, assetId: string): Promise<AgentSkillAsset> {
        const resp = await this.request<{ asset: AgentSkillAsset }>(
            `/v1/admin/agent-skills/${skillId}/assets/${assetId}`,
            { method: 'GET' }
        );
        return resp.asset;
    }

    /**
     * Create a new asset file for a skill.
     */
    async createSkillAsset(
        skillId: string,
        data: AgentSkillAssetCreateRequest
    ): Promise<AgentSkillAsset> {
        const resp = await this.request<{ asset: AgentSkillAsset }>(
            `/v1/admin/agent-skills/${skillId}/assets`,
            { method: 'POST', body: JSON.stringify(data) }
        );
        return resp.asset;
    }

    /**
     * Update an asset file.
     */
    async updateSkillAsset<Sent>(
        skillId: string,
        assetId: string,
        data: ApiBodyExactly<'/v1/admin/agent-skills/{skill_id}/assets/{asset_id}', 'patch', Sent>
    ): Promise<AgentSkillAsset> {
        const resp = await this.request<{ asset: AgentSkillAsset }>(
            `/v1/admin/agent-skills/${skillId}/assets/${assetId}`,
            { method: 'PATCH', body: JSON.stringify(data) }
        );
        return resp.asset;
    }

    /**
     * Delete an asset file.
     */
    async deleteSkillAsset(
        skillId: string,
        assetId: string
    ): Promise<{ success: boolean; message: string }> {
        return this.request<{ success: boolean; message: string }>(
            `/v1/admin/agent-skills/${skillId}/assets/${assetId}`,
            { method: 'DELETE' }
        );
    }

// ============================================================================
// Skill Sources — git-sourced Agent Skill repositories
// ============================================================================

    //
    // Operators paste a GitHub URL; the Rust scheduler in gateway-core
    // walks the repo on a 1h cadence and ingests every SKILL.md it finds.
    // Manual `refreshSkillSource()` clears `last_attempted_at` so the next
    // scheduler tick picks the row up immediately.

    async listSkillSources(params?: {
        enabled?: boolean;
        status?: 'idle' | 'syncing' | 'failed';
    }): Promise<SkillSourceListResponse> {
        const qs = new URLSearchParams();
        if (params?.enabled !== undefined) qs.append('enabled', String(params.enabled));
        if (params?.status) qs.append('status', params.status);
        const url = qs.toString()
            ? `/v1/admin/skill-sources?${qs}`
            : '/v1/admin/skill-sources';
        return this.request<SkillSourceListResponse>(url, { method: 'GET' });
    }

    async getSkillSource(id: string): Promise<SkillSourceResponse> {
        return this.request<SkillSourceResponse>(
            `/v1/admin/skill-sources/${id}`,
            { method: 'GET' },
        );
    }

    async createSkillSource(
        data: SkillSourceCreateRequest,
    ): Promise<SkillSourceResponse> {
        return this.request<SkillSourceResponse>(
            '/v1/admin/skill-sources',
            { method: 'POST', body: JSON.stringify(data) },
        );
    }

    async updateSkillSource<Sent>(
        id: string,
        data: ApiBodyExactly<'/v1/admin/skill-sources/{source_id}', 'patch', Sent>,
    ): Promise<SkillSourceResponse> {
        return this.request<SkillSourceResponse>(
            `/v1/admin/skill-sources/${id}`,
            { method: 'PATCH', body: JSON.stringify(data) },
        );
    }

    async deleteSkillSource(id: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/skill-sources/${id}`,
            { method: 'DELETE' },
        );
    }

    /**
     * Trigger an immediate sync. Idempotent — the scheduler is the
     * single writer; this endpoint just clears `last_attempted_at` so
     * the next 60s tick processes the source. Polling getSkillSource()
     * for `status === 'idle' && last_synced_at` is the wait-for-finish
     * pattern.
     */
    async refreshSkillSource(id: string): Promise<SkillSourceResponse> {
        return this.request<SkillSourceResponse>(
            `/v1/admin/skill-sources/${id}/refresh`,
            { method: 'POST' },
        );
    }

// ============================================================================
// Agent Skill Export — SKILL.md-portable ZIP for Claude Desktop, etc.
// ============================================================================

    /**
     * Download a skill as a ZIP bundle that drops cleanly into
     * `~/.claude/skills/`, `~/.cursor/skills/`, Codex, VS Code, Kiro,
     * or Goose. SKILL.md is rewritten with `brutor-skill-id`,
     * `brutor-tenant-id`, `brutor-bundle-sha256` provenance keys so
     * the bundle is traceable back to its origin.
     */
    async exportAgentSkill(skillId: string): Promise<Blob> {
        return await this.request<Blob>(
            `/v1/admin/agent-skills/${skillId}/export`,
            { method: 'POST' },
        );
    }
}
