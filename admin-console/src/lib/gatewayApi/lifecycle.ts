/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI System lifecycle: composition and contracts, the operating envelope, evidence attachments, response policies, data scope and record reach.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type {
    AISystemCompositionView,
    DependencyDraft,
    DependencyEdge,
    DependencyListResponse,
    AISystemContract,
    ContractListResponse,
    MintContractResponse,
    LifecycleResponse,
    TransitionRequest,
    AISystemEvidenceItem,
    EnvelopeResponse,
    EvidenceCreateRequest,
    EvidenceListResponse,
    ResponsePolicy,
    ResponsePolicyListResponse,
    PolicyPreview,
    ResponseHistory,
    DataScopeActivity,
    DataScopeConfig,
    ReachPreview,
    ReachRule,
    ReachRuleBody,
    ReachRuleList,
    BuildSuiteRequest,
    BuildSuiteResponse,
    ReplayStatus,
    RequestRunResponse,
} from '../aiSystemTypes';
import type { OutcomesResponse } from '../runOutcomes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';

export class LifecycleApi extends GatewayApiBase {

    // -----------------------------------------------------------------
    // Contracts + the lifecycle gate
    // -----------------------------------------------------------------

    /** Contract version history, plus whether live config has drifted from it. */
    /**
     * The system's composition: location, member agents, the
     * gateway's policies and limits, AI resources, dependency edges joined
     * with what the run segments observed, callers and findings. Built
     * server-side from the contract closure — one computation with the
     * contract.
     */
    async fetchAISystemComposition(
        aiSystemId: string,
        windowHours?: number
    ): Promise<AISystemCompositionView> {
        const qs = windowHours ? `?window_hours=${windowHours}` : '';
        return this.request<AISystemCompositionView>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/composition${qs}`
        );
    }

    /** Declared dependency edges of one AI System. */
    async fetchAISystemDependencies(aiSystemId: string): Promise<DependencyListResponse> {
        return this.request<DependencyListResponse>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/dependencies`
        );
    }

    /** Declare an edge. 422 on the validation rules, 409 on a duplicate. */
    async createAISystemDependency(
        aiSystemId: string,
        draft: DependencyDraft
    ): Promise<DependencyEdge> {
        return this.request<DependencyEdge>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/dependencies`,
            { method: 'POST', body: JSON.stringify(draft) }
        );
    }

    async updateAISystemDependency<Sent>(
        aiSystemId: string,
        edgeId: string,
        patch: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/dependencies/{edge_id}', 'patch', Sent>
    ): Promise<DependencyEdge> {
        return this.request<DependencyEdge>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/dependencies/${encodeURIComponent(edgeId)}`,
            { method: 'PATCH', body: JSON.stringify(patch) }
        );
    }

    async deleteAISystemDependency(aiSystemId: string, edgeId: string): Promise<void> {
        await this.request<unknown>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/dependencies/${encodeURIComponent(edgeId)}`,
            { method: 'DELETE' }
        );
    }

    async fetchAISystemContracts(aiSystemId: string): Promise<ContractListResponse> {
        return this.request<ContractListResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/contracts`
        );
    }

    /**
     * One contract version WITH the closure it stamped.
     *
     * The list endpoint omits `resolved_closure` (closures are large and the
     * list is a navigation aid), so this is how the panel shows an approver
     * what a version actually froze: models, MCP servers, skills, agents,
     * guardrails, policies and the merged limits.
     */
    async fetchContractVersion(
        aiSystemId: string,
        version: number
    ): Promise<AISystemContract> {
        return this.request<AISystemContract>(
            `/v1/admin/ai-systems/${aiSystemId}/contracts/${version}`
        );
    }

    /** The contract governing this system's runs right now. */
    async fetchCurrentContract(aiSystemId: string): Promise<AISystemContract> {
        return this.request<AISystemContract>(
            `/v1/admin/ai-systems/${aiSystemId}/contracts/current`
        );
    }

    /**
     * Mint from live config. Idempotent — returns `created: false` when
     * nothing has changed, rather than piling up identical versions.
     */
    /**
     * Mint a contract from the resolved live configuration.
     *
     * `force` re-mints even when nothing has changed, producing a version
     * with an empty diff. That is the re-attestation case: an approver can
     * only sign a *draft*, so a new owner who wants their own name against
     * unchanged config has no other route.
     */
    async mintAISystemContract(
        aiSystemId: string,
        opts: { force?: boolean } = {}
    ): Promise<MintContractResponse> {
        const qs = opts.force ? '?force=true' : '';
        return this.request<MintContractResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/contracts${qs}`,
            { method: 'POST' }
        );
    }

    async approveContract(contractId: string): Promise<AISystemContract> {
        return this.request<AISystemContract>(
            `/v1/admin/contracts/${contractId}/approve`,
            { method: 'POST', body: JSON.stringify({}) }
        );
    }

    async rejectContract(contractId: string, reason: string): Promise<AISystemContract> {
        return this.request<AISystemContract>(
            `/v1/admin/contracts/${contractId}/reject`,
            { method: 'POST', body: JSON.stringify({ reason }) }
        );
    }

    async promoteContract(contractId: string): Promise<AISystemContract> {
        return this.request<AISystemContract>(
            `/v1/admin/contracts/${contractId}/promote`,
            { method: 'POST' }
        );
    }

    /**
     * What every reachable lifecycle transition requires, and what is
     * missing — fetched so the UI can show the checklist *before* the click
     * rather than a red toast after it.
     */
    async fetchAISystemLifecycle(aiSystemId: string): Promise<LifecycleResponse> {
        return this.request<LifecycleResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/lifecycle`
        );
    }

    /**
     * Clear one conversation's spend against its session budget.
     *
     * Ledger rows are NOT deleted — the server writes a watermark and sums
     * from it, so the conversation's history stays queryable while its
     * budget starts again at zero.
     *
     * `sessionId` is the derived key shown on the log row, not the chat id
     * the client asserted.
     */
    async resetSessionBudget(
        sessionId: string,
        reason?: string
    ): Promise<{
        session_id: string;
        reset_at: string;
        reset_by: string | null;
        reason: string | null;
        ledger_rows_retained: boolean;
    }> {
        const qs = reason ? `?reason=${encodeURIComponent(reason)}` : '';
        return this.request(
            `/v1/admin/sessions/${encodeURIComponent(sessionId)}/budget/reset${qs}`,
            { method: 'POST' }
        );
    }

    async transitionAISystemLifecycle(
        aiSystemId: string,
        payload: TransitionRequest
    ): Promise<unknown> {
        return this.request<unknown>(
            `/v1/admin/ai-systems/${aiSystemId}/lifecycle`,
            { method: 'POST', body: JSON.stringify(payload) }
        );
    }

    // -----------------------------------------------------------------
    // Operating envelope — the "what it should achieve" half of
    // the contract, plus canonical exports of both artifact standards.
    // -----------------------------------------------------------------

    async fetchAISystemEnvelope(aiSystemId: string): Promise<EnvelopeResponse> {
        return this.request<EnvelopeResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/envelope`
        );
    }

    async putAISystemEnvelope(
        aiSystemId: string,
        envelope: Record<string, number> | null
    ): Promise<EnvelopeResponse> {
        return this.request<EnvelopeResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/envelope`,
            {
                method: 'PUT',
                body: JSON.stringify({ operating_envelope: envelope } satisfies ApiRequestBody<'/v1/admin/ai-systems/{ai_system_id}/envelope', 'put'>),
            }
        );
    }

    /** The declared outcome catalogue: live, and the one in the
     *  active contract the gateway judges reported outcomes against. */
    async fetchAISystemOutcomes(aiSystemId: string): Promise<OutcomesResponse> {
        return this.request<OutcomesResponse>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/outcomes`
        );
    }

    /** Replace the catalogue (full document). 422 carries `{loc, msg}` per
     *  field; `null` or `[]` clears it. */
    async putAISystemOutcomes(
        aiSystemId: string,
        outcomes: Record<string, unknown>[] | null
    ): Promise<OutcomesResponse> {
        return this.request<OutcomesResponse>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/outcomes`,
            { method: 'PUT', body: JSON.stringify({ outcomes } satisfies ApiRequestBody<'/v1/admin/ai-systems/{ai_system_id}/outcomes', 'put'>) }
        );
    }

    /** The canonical `ai.brutor/ai-system-contract` document for one version. */
    async exportContractDocument(
        aiSystemId: string,
        version: number
    ): Promise<Record<string, unknown>> {
        return this.request<Record<string, unknown>>(
            `/v1/admin/ai-systems/${aiSystemId}/contracts/${version}/export`
        );
    }

    /** A captured snapshot as a portable, verifiable Assurance Certificate. */
    async exportAssuranceCertificate(
        aiSystemId: string,
        reportId: string
    ): Promise<Record<string, unknown>> {
        return this.request<Record<string, unknown>>(
            `/v1/admin/ai-systems/${aiSystemId}/assurance/history/${reportId}/export`
        );
    }

    // -----------------------------------------------------------------
    // External evidence attachments — impact assessments, eval /
    // red-team / pen-test reports. The artifact stays in the org's
    // document system; these rows are verifiable pointers.
    // -----------------------------------------------------------------

    async fetchAISystemEvidence(aiSystemId: string): Promise<EvidenceListResponse> {
        return this.request<EvidenceListResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/evidence`
        );
    }

    async createAISystemEvidence(
        aiSystemId: string,
        payload: EvidenceCreateRequest
    ): Promise<AISystemEvidenceItem> {
        return this.request<AISystemEvidenceItem>(
            `/v1/admin/ai-systems/${aiSystemId}/evidence`,
            { method: 'POST', body: JSON.stringify(payload) }
        );
    }

    async deleteAISystemEvidence(
        aiSystemId: string,
        evidenceId: string
    ): Promise<unknown> {
        return this.request<unknown>(
            `/v1/admin/ai-systems/${aiSystemId}/evidence/${evidenceId}`,
            { method: 'DELETE' }
        );
    }

    // -----------------------------------------------------------------
    // Response policies + replay visibility
    // -----------------------------------------------------------------

    async fetchResponsePolicies(
        aiSystemId: string,
        includeInherited = true
    ): Promise<ResponsePolicyListResponse> {
        return this.request<ResponsePolicyListResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/response-policies?include_inherited=${includeInherited}`
        );
    }

    async createResponsePolicy(
        aiSystemId: string,
        body: Record<string, unknown>
    ): Promise<ResponsePolicy> {
        return this.request<ResponsePolicy>(
            `/v1/admin/ai-systems/${aiSystemId}/response-policies`,
            { method: 'POST', body: JSON.stringify(body) }
        );
    }

    /**
     * What the policy would do and how many systems it would touch —
     * fetched *before* arming, because autonomy resolves min-wins down the
     * tree and a policy on a parent restricts every inheriting child.
     */
    async previewResponsePolicy(
        aiSystemId: string,
        body: Record<string, unknown>
    ): Promise<PolicyPreview> {
        return this.request<PolicyPreview>(
            `/v1/admin/ai-systems/${aiSystemId}/response-policies/preview`,
            { method: 'POST', body: JSON.stringify(body) }
        );
    }

    async updateResponsePolicy<Sent>(
        policyId: string,
        body: ApiBodyExactly<'/v1/admin/response-policies/{policy_id}', 'patch', Sent>
    ): Promise<ResponsePolicy> {
        return this.request<ResponsePolicy>(
            `/v1/admin/response-policies/${policyId}`,
            { method: 'PATCH', body: JSON.stringify(body) }
        );
    }

    async deleteResponsePolicy(policyId: string): Promise<unknown> {
        return this.request<unknown>(`/v1/admin/response-policies/${policyId}`, {
            method: 'DELETE',
        });
    }

    /** What the closed loop did, and what it refused to do. */
    async fetchResponseHistory(aiSystemId: string): Promise<ResponseHistory> {
        return this.request<ResponseHistory>(
            `/v1/admin/ai-systems/${aiSystemId}/responses`
        );
    }

    async restoreAutonomy(
        aiSystemId: string,
        body: Record<string, unknown>
    ): Promise<unknown> {
        return this.request<unknown>(
            `/v1/admin/ai-systems/${aiSystemId}/restore`,
            { method: 'POST', body: JSON.stringify(body) }
        );
    }

    // -----------------------------------------------------------------
    // Run data scope and cross-run record reach
    // -----------------------------------------------------------------

    async fetchDataScope(aiSystemId: string): Promise<DataScopeConfig> {
        return this.request<DataScopeConfig>(
            `/v1/admin/ai-systems/${aiSystemId}/data-scope`
        );
    }

    /** 422 with a `detail` when the config is invalid; `warnings` on success. */
    async updateDataScope<Sent>(
        aiSystemId: string,
        body: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/data-scope', 'put', Sent>
    ): Promise<DataScopeConfig> {
        return this.request<DataScopeConfig>(
            `/v1/admin/ai-systems/${aiSystemId}/data-scope`,
            { method: 'PUT', body: JSON.stringify(body) }
        );
    }

    /** Verdicts per tool over recent days — what `enforce` would refuse. */
    async fetchDataScopeActivity(
        aiSystemId: string,
        days = 7
    ): Promise<DataScopeActivity> {
        return this.request<DataScopeActivity>(
            `/v1/admin/ai-systems/${aiSystemId}/data-scope/activity?days=${days}`
        );
    }

    async fetchReachRules(aiSystemId: string): Promise<ReachRuleList> {
        return this.request<ReachRuleList>(
            `/v1/admin/ai-systems/${aiSystemId}/reach-rules`
        );
    }

    async createReachRule(
        aiSystemId: string,
        body: ReachRuleBody
    ): Promise<ReachRule> {
        return this.request<ReachRule>(
            `/v1/admin/ai-systems/${aiSystemId}/reach-rules`,
            { method: 'POST', body: JSON.stringify(body) }
        );
    }

    async updateReachRule<Sent>(
        aiSystemId: string,
        ruleId: string,
        body: ApiBodyExactly<'/v1/admin/ai-systems/{ai_system_id}/reach-rules/{rule_id}', 'patch', Sent>
    ): Promise<ReachRule> {
        return this.request<ReachRule>(
            `/v1/admin/ai-systems/${aiSystemId}/reach-rules/${ruleId}`,
            { method: 'PATCH', body: JSON.stringify(body) }
        );
    }

    async deleteReachRule(aiSystemId: string, ruleId: string): Promise<unknown> {
        return this.request<unknown>(
            `/v1/admin/ai-systems/${aiSystemId}/reach-rules/${ruleId}`,
            { method: 'DELETE' }
        );
    }

    /** How a draft limit compares with the last 30 days. Read-only. */
    async previewReachRule(
        aiSystemId: string,
        body: ReachRuleBody
    ): Promise<ReachPreview> {
        return this.request<ReachPreview>(
            `/v1/admin/ai-systems/${aiSystemId}/reach-rules/preview`,
            { method: 'POST', body: JSON.stringify(body) }
        );
    }

    /** `suiteId` inspects a specific suite instead of the gate-bound one, so a
     *  suite can be reviewed before it is allowed to gate releases. `bound` in
     *  the response still reflects what the gate actually reads. */
    async fetchReplayStatus(
        aiSystemId: string,
        suiteId?: string
    ): Promise<ReplayStatus> {
        const qs = suiteId ? `?suite_id=${encodeURIComponent(suiteId)}` : '';
        return this.request<ReplayStatus>(
            `/v1/admin/ai-systems/${aiSystemId}/replay${qs}`
        );
    }

    async buildReplaySuite(
        aiSystemId: string,
        body: BuildSuiteRequest
    ): Promise<BuildSuiteResponse> {
        return this.request<BuildSuiteResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/replay/suite`,
            { method: 'POST', body: JSON.stringify(body) }
        );
    }

    /** Request a replay run. The control plane writes the row; gateway-core
     *  executes it. There is no synchronous result — poll `fetchReplayStatus`. */
    async requestReplayRun(
        aiSystemId: string,
        suiteId?: string
    ): Promise<RequestRunResponse> {
        const qs = suiteId ? `?suite_id=${encodeURIComponent(suiteId)}` : '';
        return this.request<RequestRunResponse>(
            `/v1/admin/ai-systems/${aiSystemId}/replay/run${qs}`,
            { method: 'POST' }
        );
    }

    /**
     * The words the lifecycle gate knows: its transitions, its requirements
     * and the floor no policy lowers.
     * GET /v1/admin/ai-systems/gate-policy/vocabulary
     */
    async getGatePolicyVocabulary(): Promise<ApiResponse<'/v1/admin/ai-systems/gate-policy/vocabulary', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/ai-systems/gate-policy/vocabulary', 'get'>>(
            '/v1/admin/ai-systems/gate-policy/vocabulary',
        );
    }
}
