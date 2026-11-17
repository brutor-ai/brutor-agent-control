/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The `run.*` facts a deterministic continuous check can read — the
 * authoring reference behind the expression field's info button.
 *
 * Mirrors the gateway core's pinned contract: `RUN_FACT_KEYS` and the two
 * builders in `brutor-gateway-core/src/services/run_ledger.rs` (every key
 * always present; absent values are "" / 0 / false / []), the function
 * whitelist `ALLOWED_FUNCTIONS` in `services/assurance_checks.rs`, and the
 * value vocabularies (`terminal`, `finalized_by`, `chain`, `source`, the
 * control plane's `SYSTEM_KINDS`). `runFacts.test.ts` pins the key list
 * against the core source when both repos are checked out side by side.
 */

export type FactType = 'string' | 'int' | 'double' | 'bool' | 'list<string>';

export interface RunFact {
  key: string;
  type: FactType;
  description: string;
  /** Known values, for string facts with a closed vocabulary. */
  values?: string[];
}

export interface RunFactGroup {
  title: string;
  facts: RunFact[];
}

export const RUN_FACT_GROUPS: RunFactGroup[] = [
  {
    title: 'Identity & attribution',
    facts: [
      { key: 'run_id', type: 'string', description: 'The run’s id.' },
      { key: 'ai_system_id', type: 'string', description: 'The AI System (resource group id) the run is attributed to; "" when unattributed.' },
      {
        key: 'system_kind',
        type: 'string',
        description: 'The AI System’s kind; "" when unattributed.',
        values: ['agent', 'assistant', 'application', 'integration', 'workflow', 'service'],
      },
      { key: 'via_system_ids', type: 'list<string>', description: 'AI Systems the run passed through (composite systems).' },
      {
        key: 'actor_type',
        type: 'string',
        description: 'Who the run is attributed to; "" when unknown.',
        values: ['agent_identity', 'end_user', 'api_key', 'scheduler'],
      },
      { key: 'agent_id', type: 'string', description: 'Id of the acting agent identity, end user or API key (per actor_type).' },
      { key: 'subject_id', type: 'string', description: 'On whose behalf an agent acted; "" when not delegated.' },
      { key: 'subject_kind', type: 'string', description: 'Kind of that subject, e.g. "user" or "service"; "" when not delegated.' },
      { key: 'client_label', type: 'string', description: 'The client channel label the caller sent; "" when none.' },
    ],
  },
  {
    title: 'Outcome & evidence',
    facts: [
      {
        key: 'terminal_state',
        type: 'string',
        description: 'How the run ended as an execution. A run the gateway refused is "errored" with error_cause "blocked_by_policy".',
        values: ['completed', 'completed_degraded', 'exhausted', 'errored', 'abandoned', 'cancelled'],
      },
      {
        key: 'error_cause',
        type: 'string',
        description: 'Why an errored or exhausted run failed, from its own calls; "" when it did not fail.',
        values: [
          'blocked_by_policy', 'rate_limited', 'model_unavailable', 'model_rejected', 'tool_failed',
          'skill_failed', 'agent_unreachable', 'delegate_deviated', 'iteration_limit', 'client_reported',
        ],
      },
      {
        key: 'outcome_id',
        type: 'string',
        description: 'The outcome the agent reported when it closed the run (<outcome> or <outcome>.<variant>), verbatim; "" when none.',
      },
      {
        key: 'outcome_conformance',
        type: 'string',
        description: 'Whether that outcome is one the active contract declares.',
        values: ['declared', 'undeclared', 'unreported'],
      },
      {
        key: 'run_class',
        type: 'string',
        description: 'The run judged on all three: completed in a declared outcome that counts (success), one that does not count (no_work), an undeclared or missing outcome (deviation), errored or exhausted (failed), abandoned or cancelled (not_decisive).',
        values: ['success', 'no_work', 'deviation', 'failed', 'not_decisive'],
      },
      {
        key: 'finalized_by',
        type: 'string',
        description: 'What closed the run.',
        values: ['explicit', 'terminal', 'idle', 'sweeper', 'operator'],
      },
      {
        key: 'chain_integrity',
        type: 'string',
        description: 'How well the run’s steps are linked: gateway-signed, client-asserted, external telemetry, or incomplete.',
        values: ['intact', 'client_asserted', 'observed_external', 'partial', 'broken'],
      },
      {
        key: 'source',
        type: 'string',
        description: 'Where the run’s steps came from.',
        values: ['gateway', 'otel', 'mixed'],
      },
      { key: 'contract_present', type: 'bool', description: 'An operating contract governed the run (the one active when it started).' },
      { key: 'reopened', type: 'bool', description: 'Late steps reopened the run after it was finalized.' },
      { key: 'is_replay', type: 'bool', description: 'The run is a replay (backtests skip replays).' },
      { key: 'feedback_score', type: 'int', description: 'Net user feedback on the run (thumbs up minus down); 0 when none.' },
    ],
  },
  {
    title: 'Activity',
    facts: [
      { key: 'action_count', type: 'int', description: 'All gateway actions in the run.' },
      { key: 'step_count', type: 'int', description: 'Steps (a run holds steps, a step holds turns, a turn holds actions).' },
      { key: 'turn_count', type: 'int', description: 'Turns.' },
      { key: 'llm_call_count', type: 'int', description: 'LLM calls.' },
      { key: 'tool_call_count', type: 'int', description: 'MCP tool calls (skills are counted separately).' },
      { key: 'skill_call_count', type: 'int', description: 'Skill runs.' },
      { key: 'a2a_call_count', type: 'int', description: 'Agent-to-agent calls.' },
      { key: 'distinct_tools', type: 'int', description: 'Number of different tools called.' },
      { key: 'max_delegation_depth', type: 'int', description: 'Deepest A2A delegation hop (0 = no delegation).' },
      { key: 'models', type: 'list<string>', description: 'Model names the run called.' },
      { key: 'tools', type: 'list<string>', description: 'Tools the run called or tried to call, sorted; [] on runs recorded before tool lists were kept.' },
    ],
  },
  {
    title: 'Data scope',
    facts: [
      { key: 'scope_declared', type: 'bool', description: 'The run declared which records it works on (its data scope).' },
      { key: 'records_touched', type: 'int', description: 'Distinct records the run’s tool calls named.' },
      { key: 'scope_violation_count', type: 'int', description: 'Tool calls that named a record outside the run’s declared scope.' },
      { key: 'scope_unbound_count', type: 'int', description: 'Tool calls that named a record while the run declared no scope for that kind of record.' },
    ],
  },
  {
    title: 'Governance',
    facts: [
      { key: 'guardrail_trip_count', type: 'int', description: 'Actions where a guardrail blocked or PII was detected (a trip without a block is a tuning signal).' },
      { key: 'guardrail_block_count', type: 'int', description: 'Actions a guardrail blocked.' },
      { key: 'policy_denial_count', type: 'int', description: 'Tool or skill calls whose arguments an argument policy denied.' },
      { key: 'approval_request_count', type: 'int', description: 'Actions held for human approval.' },
      { key: 'error_count', type: 'int', description: 'Failed actions of any kind, refusals included; approval holds are not errors.' },
      { key: 'tool_error_count', type: 'int', description: 'Failed tool calls.' },
      { key: 'fallback_count', type: 'int', description: 'LLM calls served by a fallback model.' },
    ],
  },
  {
    title: 'Time, tokens & cost',
    facts: [
      { key: 'duration_ms', type: 'int', description: 'Wall-clock length of the run.' },
      { key: 'active_duration_ms', type: 'int', description: 'Time spent working, excluding waits for a human.' },
      { key: 'awaiting_human_ms', type: 'int', description: 'Time spent waiting on approvals.' },
      { key: 'total_tokens', type: 'int', description: 'Prompt + completion tokens.' },
      { key: 'prompt_tokens', type: 'int', description: 'Prompt tokens.' },
      { key: 'completion_tokens', type: 'int', description: 'Completion tokens.' },
      { key: 'total_cost_usd', type: 'double', description: 'Estimated cost in USD.' },
      { key: 'cache_hit_count', type: 'int', description: 'Calls served from the semantic cache.' },
      { key: 'tokens_saved', type: 'int', description: 'Tokens the cache saved.' },
    ],
  },
  {
    title: 'Knowledge & implementation',
    facts: [
      { key: 'rag_used', type: 'bool', description: 'The run retrieved from a knowledge base.' },
      { key: 'rag_chunks', type: 'int', description: 'Chunks retrieved.' },
      { key: 'rag_collections', type: 'list<string>', description: 'The first knowledge-base collection set the run retrieved from.' },
      { key: 'prompt_fingerprint', type: 'string', description: 'The run’s dominant instruction (system prompt) fingerprint, never the prompt itself; "" when none.' },
      { key: 'agent_impl_name', type: 'string', description: 'Agent software name the caller declared; "" when none.' },
      { key: 'agent_impl_version', type: 'string', description: 'Declared agent version.' },
      { key: 'agent_impl_build', type: 'string', description: 'Declared build id.' },
      {
        key: 'agent_impl_trust',
        type: 'string',
        description: 'How much the declaration is worth: from a verified token claim, or the caller’s word; "" when none.',
        values: ['verified', 'declared'],
      },
      { key: 'implementation_fingerprint', type: 'string', description: 'Fingerprint over the first User-Agent, MCP client name/version and instruction fingerprint; "" when unknown.' },
    ],
  },
];

export const RUN_FACT_KEYS: string[] = RUN_FACT_GROUPS.flatMap((g) => g.facts.map((f) => f.key));

/** The bounded-CEL function whitelist (anything else fails compile-check). */
export const CHECK_FUNCTIONS: { name: string; example: string }[] = [
  { name: 'size', example: 'size(run.models) > 1' },
  { name: 'in', example: '"gpt-5.2" in run.models' },
  { name: 'exists', example: 'run.models.exists(m, m.startsWith("claude"))' },
  { name: 'all', example: 'run.rag_collections.all(c, c.startsWith("public-"))' },
  { name: 'exists_one', example: 'run.models.exists_one(m, m == "gpt-5.2")' },
  { name: 'filter', example: 'size(run.models.filter(m, m.contains("mini"))) > 0' },
  { name: 'map', example: 'run.models.map(m, m.size())' },
  { name: 'contains', example: 'run.agent_impl_name.contains("copilot")' },
  { name: 'startsWith / endsWith', example: 'run.client_label.startsWith("portal")' },
  { name: 'matches', example: 'run.agent_impl_version.matches("^1\\\\.")' },
  { name: 'has', example: 'has(run.models)' },
  { name: 'string / int / double / uint', example: 'double(run.error_count) / double(run.action_count)' },
];

export interface CheckExample {
  title: string;
  output: 'boolean' | 'count' | 'score' | 'classification';
  expression: string;
}

export const CHECK_EXAMPLES: CheckExample[] = [
  {
    title: 'An agent run was stopped by a guardrail',
    output: 'boolean',
    expression: 'run.guardrail_block_count > 0 && run.system_kind == "agent"',
  },
  {
    title: 'Delegation went deeper than 3 hops',
    output: 'boolean',
    expression: 'run.max_delegation_depth > 3',
  },
  {
    title: 'Tools were called but nothing was held for approval',
    output: 'boolean',
    expression: 'run.tool_call_count > 0 && run.approval_request_count == 0',
  },
  {
    title: 'A run cost more than $1',
    output: 'boolean',
    expression: 'run.total_cost_usd > 1.0',
  },
  {
    title: 'The run failed or was refused',
    output: 'boolean',
    expression: 'run.run_class == "failed"',
  },
  {
    title: 'A policy stopped the run',
    output: 'boolean',
    expression: 'run.error_cause == "blocked_by_policy"',
  },
  {
    title: 'The run ended somewhere the contract never approved',
    output: 'boolean',
    expression: 'run.outcome_conformance == "undeclared"',
  },
  {
    title: 'A poll that found nothing still called a model',
    output: 'boolean',
    expression: 'run.run_class == "no_work" && run.llm_call_count > 0',
  },
  {
    title: 'An agent ran on an unverified implementation',
    output: 'boolean',
    expression: 'run.system_kind == "agent" && run.agent_impl_trust != "verified"',
  },
  {
    title: 'Evidence is incomplete',
    output: 'boolean',
    expression: 'run.chain_integrity in ["partial", "broken"]',
  },
  {
    title: 'A Claude model was used',
    output: 'boolean',
    expression: 'run.models.exists(m, m.startsWith("claude"))',
  },
  {
    title: 'Failures per run',
    output: 'count',
    expression: 'run.error_count + run.tool_error_count',
  },
  {
    title: 'Share of actions that failed (0–1)',
    output: 'score',
    expression:
      'run.action_count > 0 ? double(run.error_count) / double(run.action_count) : 0.0',
  },
  {
    title: 'Cost band',
    output: 'classification',
    expression:
      'run.total_cost_usd > 1.0 ? "high" : run.total_cost_usd > 0.1 ? "medium" : "low"',
  },
];
