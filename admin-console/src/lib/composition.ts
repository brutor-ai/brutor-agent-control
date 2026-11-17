/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Composite AI Systems — the pure model behind the
 * Composition tab.
 *
 * The diagram and its accessible list view are both rendered from ONE
 * `CompositionModel` built here, so the two cannot disagree: a node that
 * appears in one appears in the other with the same label, category, state
 * and link. The tests pin that by comparing the rendered node sets.
 *
 * Layout (agents → gateway → resources):
 *   LEFT    member agents (components), then callers — "used by"
 *   CENTRE  the Brutor Gateway: policies (guardrails included) and limits
 *   RIGHT   AI resources, then delegation agents (declared dependencies and
 *           observed-but-undeclared ones)
 *
 * Colour coding is exactly four categories (`COMPOSITION_CATEGORIES`); member
 * agents and callers are neutral (`category: null`). Colour is never the only
 * signal — every node also carries its category label.
 */

import { nodeApproval, type NodeApproval } from './humanApproval';
import {
  AISystemCompositionView,
  AISystemContract,
  ClosureDependency,
  DiffEntry,
  COMPOSITION_FINDING_LABELS,
  COMPOSITION_LIMIT_KIND_LABELS,
  COMPOSITION_POLICY_KIND_LABELS,
  CompositionConnection,
  CompositionDependency,
  CompositionFinding,
  CompositionFindingCode,
  CompositionInheritedFrom,
  CompositionMcpResource,
  CompositionResource,
  DEPENDENCY_CRITICALITY_LABELS,
  DEPENDENCY_HEALTH_LABELS,
  DEPENDENCY_EVIDENCE_PROVIDED,
  DEPENDENCY_EVIDENCE_PROVIDED_LABELS,
  DEPENDENCY_ROLE_LABELS,
  BoundaryMetrics,
  DependencyAssumptions,
  DependencyDraft,
  DependencyEdge,
  DependencyEvidenceProvided,
  SeparabilityDeclaration,
} from './aiSystemTypes';
import type { AgentReleaseRef } from './agentIdentityTypes';
import { CONTRIBUTED_LINKS, NO_CONTRIBUTED_LINKS, type LinkResolver } from './contributedLinks';
import { agentImplementationLink } from './agentRelease';
import { getArray, getBoolean, getNumber, getString } from './narrow';
import { OUTCOME_ID_RE } from './runOutcomes';
import { ago } from './relativeTime';

// ─── Categories ───────────────────────────────────────────────────────────

export type CompositionCategory =
  'ai_resource' | 'policy' | 'limit' | 'delegation_agent';

export interface CompositionCategoryMeta {
  id: CompositionCategory;
  label: string;
  /** Class that binds `--composition-cat` to the category's token
   *  (defined once in globals.css, light and dark). */
  className: string;
  description: string;
}

/** Exactly four. Their colours live in `src/styles/globals.css` as
 *  `--category-*` custom properties — the only place they are defined. */
export const COMPOSITION_CATEGORIES: readonly CompositionCategoryMeta[] = [
  {
    id: 'ai_resource',
    label: 'AI resource',
    className: 'composition-cat-ai-resource',
    description:
      'Models, MCP servers, skills and knowledge bases the gateway lets it reach.',
  },
  {
    id: 'policy',
    label: 'Policy',
    className: 'composition-cat-policy',
    description:
      'Guardrails, argument and response policies, governance and cache settings applied to every call.',
  },
  {
    id: 'limit',
    label: 'Limit',
    className: 'composition-cat-limit',
    description:
      'Budgets, rate and token limits, run caps, concurrency and the operating envelope.',
  },
  {
    id: 'delegation_agent',
    label: 'Delegation agent',
    className: 'composition-cat-delegation-agent',
    description:
      'Other agents it calls: shared services (solid) and external agents (dashed, assumed, not tested).',
  },
] as const;

export const CATEGORY_META: Record<
  CompositionCategory,
  CompositionCategoryMeta
> = Object.fromEntries(COMPOSITION_CATEGORIES.map((c) => [c.id, c])) as Record<
  CompositionCategory,
  CompositionCategoryMeta
>;

// ─── Declared vs observed ─────────────────────────────────────────────────

/**
 * declared_observed    — declared and seen in the window: solid.
 * declared_unobserved  — declared, no traffic in the window: outlined.
 * observed_undeclared  — traffic with no declaration: highlighted, with the
 *                        finding and a Declare action.
 * granted_undeclared   — an a2a_call grant reaches the card, no declaration
 *                        and no traffic yet: highlighted, Declare action.
 * static               — configuration that has no traffic dimension
 *                        (agents, resources, policies, limits).
 */
export type EdgeState =
  | 'declared_observed'
  | 'declared_unobserved'
  | 'observed_undeclared'
  | 'granted_undeclared'
  | 'static';

export const EDGE_STATE_LABELS: Record<EdgeState, string> = {
  declared_observed: 'Declared · observed',
  declared_unobserved: 'Declared · not observed',
  observed_undeclared: 'Observed · not declared',
  granted_undeclared: 'Granted · not declared',
  static: '',
};

export function edgeState(declared: boolean, observedCalls: number): EdgeState {
  if (!declared) return 'observed_undeclared';
  return observedCalls > 0 ? 'declared_observed' : 'declared_unobserved';
}

// ─── Links ────────────────────────────────────────────────────────────────
//
// Targets are App tab ids (the `setActiveTab` vocabulary). Pages with an id
// deep link (`ai-systems:<id>`, `groups:<id>`, `llm-models:<id>`) use it;
// the rest open their list page.

export const aiSystemLink = (id: string, section = 'composition') =>
  `ai-systems:${id}:${section}`;
export const groupLink = (id: string, tab?: string) =>
  tab ? `groups:${id}:${tab}` : `groups:${id}`;

// ─── The model ────────────────────────────────────────────────────────────

export interface DeclareTarget {
  system_id: string | null;
  agent_card_id: string | null;
  name: string;
}

export type CompositionColumn = 'left' | 'centre' | 'right';

export interface NodeFact {
  label: string;
  value: string;
}

export interface CompositionNode {
  /** Stable across renders, unique within the model. */
  key: string;
  label: string;
  /** What kind of thing it is ("Model", "Guardrail", "Shared service"). */
  kind: string;
  /** null = neutral (member agents, callers). */
  category: CompositionCategory | null;
  state: EdgeState;
  /** External delegates are drawn dashed: assessed at the boundary only. */
  dashed: boolean;
  inheritedFrom: CompositionInheritedFrom | null;
  /** One-line description under the label. */
  summary: string | null;
  /** Structured facts (delegation agents: role, criticality, …). */
  facts: NodeFact[];
  /** Findings that name this node. */
  findings: CompositionFinding[];
  /** App tab to open, or null when the page it would open is not part of
   *  this console (a page another distribution contributes). */
  link: string | null;
  /** Present on declared edges — the dialog edits/deletes it. */
  edge: DependencyEdge | null;
  /** Present on undeclared delegation nodes — the Declare action pre-fills
   *  the edge (a shared service when a system is known, else external). */
  declareTarget: DeclareTarget | null;
  /** Member agents only: the agent's current release (`ref`
   *  null = never declared one). Every view renders it as `name@version`
   *  with its trust; the Diagram and List add the approval. */
  agentRelease: { agentId: string; ref: AgentReleaseRef | null } | null;
  /** Declared human approval on this node (resources and delegation agents);
   *  null = none declared. */
  humanApproval: NodeApproval | null;
  /** The system's connection to this node in the window (resources and
   *  delegation nodes; null elsewhere) — the Simple graph's line status. */
  connection: CompositionConnection | null;
  /** The ids a run action's target can name this node by (resources: its
   *  id, plus the mcp_servers id behind an MCP binding; delegation nodes:
   *  the agent card and the AI System) — how the run overlay places a
   *  run's actions. Empty elsewhere. */
  targetIds: string[];
}

export interface CompositionSection {
  id: string;
  column: CompositionColumn;
  title: string;
  /** Section-level category (for the heading swatch); null = neutral. */
  category: CompositionCategory | null;
  nodes: CompositionNode[];
  /** Shown when `nodes` is empty. */
  empty: string;
}

export interface CompositionModel {
  sections: CompositionSection[];
}

/** "4 calls in window · last seen 3h ago" */
export function callsSummary(calls: number, lastSeen: string | null): string {
  const base = `${calls} call${calls === 1 ? '' : 's'} in window`;
  return lastSeen ? `${base} · last seen ${ago(lastSeen)}` : base;
}

function findingsFor(
  findings: CompositionFinding[],
  ...ids: (string | null | undefined)[]
) {
  const wanted = new Set(ids.filter((v): v is string => Boolean(v)));
  if (wanted.size === 0) return [];
  return findings.filter(
    (f) => f.subject_id !== null && wanted.has(f.subject_id)
  );
}

function node(
  partial: Partial<CompositionNode> &
    Pick<CompositionNode, 'key' | 'label' | 'kind' | 'link'>
): CompositionNode {
  return {
    category: null,
    state: 'static',
    dashed: false,
    inheritedFrom: null,
    summary: null,
    facts: [],
    findings: [],
    edge: null,
    declareTarget: null,
    agentRelease: null,
    humanApproval: null,
    connection: null,
    targetIds: [],
    ...partial,
  };
}

const present = (...ids: (string | null | undefined)[]): string[] =>
  ids.filter((v): v is string => Boolean(v));

function resourceNodes(
  items: CompositionResource[],
  kind: string,
  keyPrefix: string,
  link: (r: CompositionResource) => string | null,
  findings: CompositionFinding[]
): CompositionNode[] {
  return items.map((r) =>
    node({
      key: `${keyPrefix}:${r.id}`,
      label: r.name ?? r.id,
      kind,
      category: 'ai_resource',
      inheritedFrom: r.inherited_from,
      link: link(r),
      findings: findingsFor(findings, r.id),
      connection: r.connection,
      targetIds: present(
        r.id,
        'mcp_server_id' in r ? (r as CompositionMcpResource).mcp_server_id : null
      ),
    })
  );
}

/** "12 answered · 1 failed" — only the non-zero outcomes, answered first. */
export function outcomeMix(o: CompositionDependency['observed']): string {
  const parts: string[] = [];
  if (o.answered) parts.push(`${o.answered} answered`);
  if (o.failed) parts.push(`${o.failed} failed`);
  if (o.rejected) parts.push(`${o.rejected} rejected`);
  if (o.timed_out) parts.push(`${o.timed_out} timed out`);
  if (o.blocked) parts.push(`${o.blocked} blocked`);
  return parts.length ? parts.join(' · ') : 'no calls';
}

/**
 * The contract a shared-service edge runs against. A pinned edge also says
 * where the target is now (`targetActive`, the target's active version when
 * known): "pinned to v2 · target active v3 — behind". A pin that is behind
 * is the `composition.dependency_pin_stale` finding; the caller does not
 * drift, so this line is where an owner sees it.
 */
export function contractFact(
  dep: CompositionDependency,
  targetActive?: number | null
): string {
  if (dep.role === 'external') return 'not applicable';
  if (!dep.target_contract) return 'target has no active contract';
  if (!dep.target_contract.pinned) {
    return `following active v${dep.target_contract.version}`;
  }
  const pinned = `pinned to v${dep.target_contract.version}`;
  if (targetActive === undefined) return pinned;
  if (targetActive === null) return `${pinned} · target has no active contract`;
  return targetActive === dep.target_contract.version
    ? `${pinned} · target active v${targetActive}`
    : `${pinned} · target active v${targetActive} — behind`;
}

/** Target systems whose active version a pinned edge needs to show. */
export function pinnedTargets(view: AISystemCompositionView): string[] {
  return [
    ...new Set(
      view.dependencies
        .filter(
          (d) =>
            d.role === 'shared_service' &&
            d.target_contract?.pinned &&
            d.depends_on_system_id
        )
        .map((d) => d.depends_on_system_id as string)
    ),
  ].sort();
}

/** The declared edge behind a delegation node, or null when the node is a
 *  card reached by a grant with no declaration. */
export function edgeOf(dep: CompositionDependency): DependencyEdge | null {
  if (
    !dep.declared ||
    !dep.id ||
    !dep.role ||
    !dep.criticality ||
    dep.contributes_to_decision === null
  ) {
    return null;
  }
  return {
    id: dep.id,
    ai_system_id: dep.ai_system_id,
    role: dep.role,
    depends_on_system_id: dep.depends_on_system_id,
    depends_on_system_name: dep.depends_on_system_name,
    agent_card_id: dep.agent_card_id,
    agent_card_name: dep.agent_card_name,
    criticality: dep.criticality,
    fallback: dep.fallback,
    contributes_to_decision: dep.contributes_to_decision,
    separability_rationale: dep.separability_rationale,
    separability_record_id: dep.separability_record_id,
    separability_sealed_at: dep.separability_sealed_at,
    contract_pin: dep.contract_pin,
    supplier_legal_entity: dep.supplier_legal_entity,
    assumptions: dep.assumptions,
    declared_by: dep.declared_by,
    declared_at: dep.declared_at,
    updated_by: dep.updated_by,
    updated_at: dep.updated_at,
  };
}

/** "12 completed · 2 failed · 1 input required" — the terminal A2A task
 *  states the root received from a dependency, most frequent first. */
export function taskStateMix(
  states: Record<string, number> | null | undefined,
  separator = ' · '
): string | null {
  const entries = Object.entries(states ?? {}).filter(([, n]) => n > 0);
  if (entries.length === 0) return null;
  return entries
    .sort(([a, x], [b, y]) => y - x || a.localeCompare(b))
    .map(([state, n]) => `${n} ${state.replace(/_/g, ' ')}`)
    .join(separator);
}

/** The separability claim in words: sealed with its record, or saved but
 *  not sealed yet (the registry does not accept an unsealed claim). */
export function separabilityText(
  recordId: string | null | undefined,
  sealedAt: string | null | undefined
): string {
  if (!recordId) return 'separable (rationale declared) · not sealed yet';
  const at = sealedAt ? ` ${sealedAt.slice(0, 10)}` : '';
  return `separable · sealed${at} as ${recordId.slice(0, 16)}`;
}

function dependencyNode(
  dep: CompositionDependency,
  findings: CompositionFinding[],
  targetActive: Record<string, number | null>,
  boundary: BoundaryMetrics | null
): CompositionNode {
  const edge = edgeOf(dep);
  const external = dep.role === 'external';
  const target =
    dep.depends_on_system_name ??
    dep.depends_on_system_id ??
    dep.agent_card_name ??
    dep.agent_card_id ??
    'Agent';
  const cardKey =
    dep.id ?? `card:${dep.agent_card_id ?? dep.depends_on_system_id ?? target}`;
  const link =
    dep.depends_on_system_id && !external
      ? aiSystemLink(dep.depends_on_system_id)
      : 'a2a-agent-cards';
  const observedFacts: NodeFact[] = [
    { label: 'Health', value: DEPENDENCY_HEALTH_LABELS[dep.health] },
    { label: 'Calls', value: String(dep.observed.calls) },
    { label: 'Outcomes', value: outcomeMix(dep.observed) },
  ];
  if (dep.observed.last_seen_at)
    observedFacts.push({
      label: 'Last seen',
      value: ago(dep.observed.last_seen_at),
    });
  const nodeFindings = findingsFor(
    findings,
    dep.id,
    dep.depends_on_system_id,
    dep.agent_card_id
  );

  if (!edge) {
    // A card the grants reach with no declared edge: traffic makes it an
    // observed-undeclared dependency; no traffic, a grant nobody declared.
    return node({
      key: `dep:${cardKey}`,
      label: target,
      kind: 'Undeclared dependency',
      category: 'delegation_agent',
      state:
        dep.observed.calls > 0 ? 'observed_undeclared' : 'granted_undeclared',
      summary: dep.granted
        ? 'Reached by an a2a_call grant; no declared edge'
        : 'No declared edge',
      facts: observedFacts,
      findings: nodeFindings,
      link,
      connection: dep.connection,
      targetIds: present(dep.agent_card_id, dep.depends_on_system_id),
      declareTarget: {
        system_id: dep.depends_on_system_id,
        agent_card_id: dep.agent_card_id,
        name: target,
      },
    });
  }

  const facts: NodeFact[] = [
    { label: 'Role', value: DEPENDENCY_ROLE_LABELS[edge.role] },
    {
      label: 'Criticality',
      value: DEPENDENCY_CRITICALITY_LABELS[edge.criticality],
    },
    {
      label: 'Contribution',
      value: edge.contributes_to_decision
        ? 'contributes to decision'
        : separabilityText(
            edge.separability_record_id,
            edge.separability_sealed_at
          ),
    },
    ...(edge.supplier_legal_entity
      ? [{ label: 'Supplier', value: edge.supplier_legal_entity }]
      : []),
    {
      label: 'Contract',
      value: contractFact(
        dep,
        dep.depends_on_system_id
          ? targetActive[dep.depends_on_system_id]
          : undefined
      ),
    },
    ...observedFacts,
  ];
  // External agents are seen at the boundary only: the task states the
  // root received are what Brutor knows of how the delegate answered.
  const mix = external ? taskStateMix(boundary?.task_states) : null;
  if (mix) facts.push({ label: 'Task states', value: mix });
  return node({
    key: `dep:${cardKey}`,
    label: target,
    kind: DEPENDENCY_ROLE_LABELS[edge.role],
    category: 'delegation_agent',
    state: edgeState(true, dep.observed.calls),
    dashed: external,
    summary: external ? 'Assumed, not tested' : null,
    facts,
    findings: nodeFindings,
    link,
    edge,
    connection: dep.connection,
    targetIds: present(dep.agent_card_id, dep.depends_on_system_id),
  });
}

export function buildCompositionModel(
  view: AISystemCompositionView,
  /** Active contract version per pinned target (see `pinnedTargets`);
   *  missing = not loaded, null = the target has none. */
  targetActive: Record<string, number | null> = {},
  /** Boundary metrics per external edge id, from the system's dependency
   *  health (best-effort; missing = not loaded). */
  boundaryByEdge: Record<string, BoundaryMetrics | null> = {},
  /** Answers the links to pages this console may not have (`app/links.ts`);
   *  by default none is there, and those nodes carry no link. */
  contributedLink: LinkResolver = NO_CONTRIBUTED_LINKS
): CompositionModel {
  const { findings } = view;
  const selfId = view.system.id;
  const sourceOrSelf = (from: CompositionInheritedFrom | null) =>
    from?.id ?? selfId;

  const agents = view.agents.map((a) =>
    node({
      key: `agent:${a.id}`,
      label: a.name ?? a.id,
      kind: 'Member agent',
      // Straight to the identity's Implementation section,
      // where its releases are approved.
      link: agentImplementationLink(a.id),
      findings: findingsFor(findings, a.id),
      agentRelease: { agentId: a.id, ref: a.release ?? null },
    })
  );

  const callers: CompositionNode[] = [
    ...view.dependents.map((d) =>
      node({
        key: `caller:${d.system_id}`,
        label: d.system_name ?? d.system_id,
        kind: 'Calling system',
        // `declared` is THIS system's acceptance (intended_client_systems);
        // a caller that only declares its own edge and has not called
        // yet is declared (by one side), not observed.
        state:
          d.declared || d.observed_calls === 0
            ? edgeState(true, d.observed_calls)
            : 'observed_undeclared',
        summary: callsSummary(d.observed_calls, d.last_seen_at),
        facts: [
          {
            label: 'Accepted here',
            value: d.declared
              ? 'yes'
              : view.system.enforce_accepted_callers
                ? 'no — refused at the gateway'
                : 'no (advisory)',
          },
          {
            label: 'Caller declares edge',
            value: d.edge_declared ? 'yes' : 'no',
          },
        ],
        findings: findingsFor(findings, d.system_id),
        link: aiSystemLink(d.system_id),
      })
    ),
    ...view.client_labels.map((c) =>
      node({
        key: `client:${c.label}`,
        label: c.label,
        kind: 'Client label',
        state: edgeState(c.declared, c.observed_calls),
        summary: callsSummary(c.observed_calls, null),
        findings: findingsFor(findings, c.label),
        // Client labels are declared on the system's own
        // Ownership & Purpose tab.
        link: groupLink(selfId, 'general:ownership'),
      })
    ),
  ];

  const policies = view.gateway.policies.map((p, i) =>
    node({
      key: `policy:${p.kind}:${p.id ?? `${p.name}#${i}`}`,
      label: p.name,
      kind: COMPOSITION_POLICY_KIND_LABELS[p.kind] ?? p.kind,
      category: 'policy',
      inheritedFrom: p.inherited_from,
      summary: p.summary || null,
      findings: findingsFor(findings, p.id),
      link:
        p.kind === 'guardrail'
          ? 'guardrails'
          : p.kind === 'argument_policy'
            ? 'argument-policies'
            : p.kind === 'response_policy'
              ? aiSystemLink(selfId, 'drift')
              : p.kind === 'cache'
                ? contributedLink(CONTRIBUTED_LINKS.cachePolicies)
                : groupLink(sourceOrSelf(p.inherited_from), 'governance'),
    })
  );

  const limits = view.gateway.limits.map((l) =>
    node({
      key: `limit:${l.kind}:${l.key}`,
      label: COMPOSITION_LIMIT_KIND_LABELS[l.kind] ?? l.kind,
      kind: l.key,
      category: 'limit',
      inheritedFrom: l.inherited_from,
      summary: l.summary || null,
      link:
        l.kind === 'envelope'
          ? aiSystemLink(selfId, 'contract')
          : groupLink(sourceOrSelf(l.inherited_from), 'governance'),
    })
  );

  const resources: CompositionNode[] = [
    ...resourceNodes(
      view.resources.models,
      'Model',
      'model',
      (r) => `llm-models:${r.id}`,
      findings
    ),
    ...resourceNodes(
      view.resources.mcp_servers,
      'MCP server',
      'mcp',
      () => 'mcp-config',
      findings
    ),
    ...resourceNodes(
      view.resources.skills,
      'Skill',
      'skill',
      () => 'agent-skills',
      findings
    ),
    ...resourceNodes(
      view.resources.knowledge_bases,
      'Knowledge base',
      'kb',
      () => contributedLink(CONTRIBUTED_LINKS.knowledgeBases),
      findings
    ),
  ];

  const delegation: CompositionNode[] = [
    ...view.dependencies.map((d) =>
      dependencyNode(
        d,
        findings,
        targetActive,
        d.id ? (boundaryByEdge[d.id] ?? null) : null
      )
    ),
    ...view.observed_undeclared.map((o) =>
      node({
        key: `undeclared:${o.system_id}`,
        label: o.system_name ?? o.system_id,
        kind: 'Undeclared dependency',
        category: 'delegation_agent',
        state: 'observed_undeclared',
        summary: `${callsSummary(o.calls, o.last_seen_at)} · no declared edge`,
        facts: [{ label: 'Calls', value: String(o.calls) }],
        findings: findingsFor(findings, o.system_id),
        link: aiSystemLink(o.system_id),
        connection: o.connection,
        targetIds: [o.system_id],
        declareTarget: {
          system_id: o.system_id,
          agent_card_id: null,
          name: o.system_name ?? o.system_id,
        },
      })
    ),
  ];

  const withApproval = (nodes: CompositionNode[]) =>
    nodes.map((n) => ({ ...n, humanApproval: nodeApproval(view, n.targetIds) }));

  return {
    sections: [
      {
        id: 'agents',
        column: 'left',
        title: 'Member agents',
        category: null,
        nodes: agents,
        empty: 'No member agents',
      },
      {
        id: 'callers',
        column: 'left',
        title: 'Used by',
        category: null,
        nodes: callers,
        empty: 'No callers',
      },
      {
        id: 'policies',
        column: 'centre',
        title: 'Policies',
        category: 'policy',
        nodes: policies,
        empty: 'No policies apply',
      },
      {
        id: 'limits',
        column: 'centre',
        title: 'Limits',
        category: 'limit',
        nodes: limits,
        empty: 'No limits apply',
      },
      {
        id: 'resources',
        column: 'right',
        title: 'AI resources',
        category: 'ai_resource',
        nodes: withApproval(resources),
        empty: 'No AI resources bound',
      },
      {
        id: 'delegation',
        column: 'right',
        title: 'Delegation agents',
        category: 'delegation_agent',
        nodes: withApproval(delegation),
        empty: 'No delegation agents',
      },
    ],
  };
}

/** Every node key in the model, in section order — the list view and the
 *  diagram must each render exactly this set. */
export function modelNodeKeys(model: CompositionModel): string[] {
  return model.sections.flatMap((s) => s.nodes.map((n) => n.key));
}

export function findingLabel(code: string): string {
  return COMPOSITION_FINDING_LABELS[code as CompositionFindingCode] ?? code;
}

/** Findings, most severe first. */
export function sortFindings(
  findings: CompositionFinding[]
): CompositionFinding[] {
  const rank = { critical: 0, warning: 1, info: 2 } as const;
  return [...findings].sort((a, b) => rank[a.severity] - rank[b.severity]);
}

// ─── Dependency validation (mirrors the server's 422 rules) ───────────────

export type DependencyField =
  | 'depends_on_system_id'
  | 'agent_card_id'
  | 'fallback'
  | 'separability_rationale'
  | 'contract_pin'
  | 'supplier_legal_entity'
  | 'expected_outcomes';

/** The server's column width for supplier_legal_entity. */
export const SUPPLIER_LEGAL_ENTITY_MAX = 255;

export type DependencyErrors = Partial<Record<DependencyField, string>>;

/**
 * Client-side mirror of the control plane's 422 rules for a dependency
 * edge. The server stays the authority (it also checks that the target is an
 * AI System in this tenant and that a pin is one of the target's contracts);
 * this exists so the dialog says what is missing before a round trip.
 */
export function validateDependencyDraft(
  draft: DependencyDraft,
  selfId: string
): DependencyErrors {
  const errors: DependencyErrors = {};
  if (draft.role === 'shared_service') {
    if (!draft.depends_on_system_id) {
      errors.depends_on_system_id =
        'A shared service needs the AI System it depends on.';
    } else if (draft.depends_on_system_id === selfId) {
      errors.depends_on_system_id = 'A system cannot depend on itself.';
    }
  }
  if (draft.role === 'external') {
    if (!draft.agent_card_id) {
      errors.agent_card_id =
        'An external dependency needs the agent card it calls.';
    }
    if (draft.contract_pin) {
      errors.contract_pin = 'Only a shared service has contracts to pin.';
    }
  }
  if (draft.criticality === 'degradable' && !draft.fallback?.trim()) {
    errors.fallback = 'A degradable dependency needs a declared fallback.';
  }
  if (!draft.contributes_to_decision && !draft.separability_rationale?.trim()) {
    errors.separability_rationale =
      'Declaring that it does not contribute to the decision needs a separability rationale.';
  }
  const supplier = draft.supplier_legal_entity?.trim() ?? '';
  if (draft.role === 'external' && !supplier) {
    errors.supplier_legal_entity =
      'An external dependency needs the legal entity that provides it (EU AI Act Art 25(4)).';
  } else if (supplier.length > SUPPLIER_LEGAL_ENTITY_MAX) {
    errors.supplier_legal_entity = `At most ${SUPPLIER_LEGAL_ENTITY_MAX} characters.`;
  }
  const badOutcomes = (draft.expected_outcomes ?? []).filter(
    (id) => !OUTCOME_ID_RE.test(id)
  );
  if (badOutcomes.length) {
    errors.expected_outcomes = `Each expected outcome is <outcome> or <outcome>.<variant> (lowercase letters, digits, _ and -): ${badOutcomes.join(', ')}`;
  }
  return errors;
}

/** The expected-outcome list as typed: "a.b, c" → ["a.b", "c"] — trimmed,
 *  de-duplicated and sorted (the server stores it sorted). */
export function parseExpectedOutcomes(raw: string): string[] {
  return [
    ...new Set(
      raw
        .split(/[,\s]+/)
        .map((v) => v.trim())
        .filter(Boolean)
    ),
  ].sort();
}

// ─── Assumptions (the root's side of an assume-guarantee contract) ────────

/** Server bounds (`DependencyAssumptions`, strict). */
export const ASSUMPTION_LIMITS = {
  data_classes_shared: 50,
  retention_at_delegate: 500,
  evidence_provided: 10,
} as const;

/** The form's raw inputs: numbers stay text until parsed, so "abc" is an
 *  error message rather than a silently dropped value. */
export interface AssumptionsForm {
  data_classes_shared: string;
  retention_at_delegate: string;
  human_oversight_at_delegate: 'unstated' | 'yes' | 'no';
  expected_availability_pct: string;
  expected_p95_latency_ms: string;
  evidence_provided: DependencyEvidenceProvided[];
}

export type AssumptionErrors = Partial<
  Record<keyof DependencyAssumptions, string>
>;

const isEvidenceProvided = (v: unknown): v is DependencyEvidenceProvided =>
  typeof v === 'string' &&
  (DEPENDENCY_EVIDENCE_PROVIDED as readonly string[]).includes(v);

/**
 * The assumptions in a stored object: the keys of the Control Plane's
 * schema, each read by its type. The schema is strict (an unknown key is
 * refused on write), so an edge holds no other key.
 */
export function readAssumptions(raw: Record<string, unknown> | null | undefined): DependencyAssumptions {
  const src = raw ?? {};
  const value: DependencyAssumptions = {};
  const classes = getArray(src, 'data_classes_shared');
  if (classes) value.data_classes_shared = classes.filter((c): c is string => typeof c === 'string');
  const retention = getString(src, 'retention_at_delegate');
  if (retention !== undefined) value.retention_at_delegate = retention;
  const oversight = getBoolean(src, 'human_oversight_at_delegate');
  if (oversight !== undefined) value.human_oversight_at_delegate = oversight;
  const availability = getNumber(src, 'expected_availability_pct');
  if (availability !== undefined) value.expected_availability_pct = availability;
  const latency = getNumber(src, 'expected_p95_latency_ms');
  if (latency !== undefined) value.expected_p95_latency_ms = latency;
  const evidence = getArray(src, 'evidence_provided');
  if (evidence) value.evidence_provided = evidence.filter(isEvidenceProvided);
  return value;
}

export function assumptionsForm(a: DependencyAssumptions): AssumptionsForm {
  return {
    data_classes_shared: (a.data_classes_shared ?? []).join(', '),
    retention_at_delegate: a.retention_at_delegate ?? '',
    human_oversight_at_delegate:
      a.human_oversight_at_delegate === true
        ? 'yes'
        : a.human_oversight_at_delegate === false
          ? 'no'
          : 'unstated',
    expected_availability_pct:
      a.expected_availability_pct != null ? String(a.expected_availability_pct) : '',
    expected_p95_latency_ms:
      a.expected_p95_latency_ms != null ? String(a.expected_p95_latency_ms) : '',
    evidence_provided: [...(a.evidence_provided ?? [])],
  };
}

/**
 * Form → the object sent, with the server's rules mirrored (ranges, integer
 * latency, list and text bounds). Unset fields are omitted — the server
 * stores `exclude_none`, and an empty object states nothing.
 */
export function parseAssumptionsForm(form: AssumptionsForm): {
  value: DependencyAssumptions;
  errors: AssumptionErrors;
} {
  const value: DependencyAssumptions = {};
  const errors: AssumptionErrors = {};

  const classes = form.data_classes_shared
    .split(',')
    .map((c) => c.trim())
    .filter(Boolean);
  const unique = [...new Set(classes)];
  if (unique.length > ASSUMPTION_LIMITS.data_classes_shared) {
    errors.data_classes_shared = `At most ${ASSUMPTION_LIMITS.data_classes_shared} data classes.`;
  } else if (unique.length) {
    value.data_classes_shared = unique;
  }

  const retention = form.retention_at_delegate.trim();
  if (retention.length > ASSUMPTION_LIMITS.retention_at_delegate) {
    errors.retention_at_delegate = `At most ${ASSUMPTION_LIMITS.retention_at_delegate} characters.`;
  } else if (retention) {
    value.retention_at_delegate = retention;
  }

  if (form.human_oversight_at_delegate !== 'unstated') {
    value.human_oversight_at_delegate = form.human_oversight_at_delegate === 'yes';
  }

  const availability = form.expected_availability_pct.trim();
  if (availability) {
    const n = Number(availability);
    if (!Number.isFinite(n) || n < 0 || n > 100) {
      errors.expected_availability_pct = 'A percentage between 0 and 100.';
    } else {
      value.expected_availability_pct = n;
    }
  }

  const latency = form.expected_p95_latency_ms.trim();
  if (latency) {
    const n = Number(latency);
    if (!Number.isInteger(n) || n < 0) {
      errors.expected_p95_latency_ms = 'A whole number of milliseconds, 0 or more.';
    } else {
      value.expected_p95_latency_ms = n;
    }
  }

  const evidence = [...new Set(form.evidence_provided)];
  if (evidence.length > ASSUMPTION_LIMITS.evidence_provided) {
    errors.evidence_provided = `At most ${ASSUMPTION_LIMITS.evidence_provided} entries.`;
  } else if (evidence.length) {
    // Canonical order, so an unchanged selection re-saves identically.
    value.evidence_provided = DEPENDENCY_EVIDENCE_PROVIDED.filter((e) =>
      evidence.includes(e)
    );
  }
  return { value, errors };
}

/** Assumptions as readable rows, in schema order with typed wording. */
export function assumptionFacts(
  raw: Record<string, unknown> | null | undefined
): [string, string][] {
  if (!raw) return [];
  const value = readAssumptions(raw);
  const rows: [string, string][] = [];
  if (value.data_classes_shared?.length)
    rows.push(['Data classes shared', value.data_classes_shared.join(', ')]);
  if (value.retention_at_delegate)
    rows.push(['Retention at the delegate', value.retention_at_delegate]);
  if (value.human_oversight_at_delegate != null)
    rows.push([
      'Human oversight at the delegate',
      value.human_oversight_at_delegate ? 'yes' : 'no',
    ]);
  if (value.expected_availability_pct != null)
    rows.push(['Expected availability', `${value.expected_availability_pct}%`]);
  if (value.expected_p95_latency_ms != null)
    rows.push(['Expected p95 latency', `${value.expected_p95_latency_ms} ms`]);
  if (value.evidence_provided?.length)
    rows.push([
      'Evidence it provides',
      value.evidence_provided
        .map((e) => DEPENDENCY_EVIDENCE_PROVIDED_LABELS[e])
        .join(', '),
    ]);
  return rows;
}

// ─── Supplier information (Art 25(4)) ─────────────────────────────────────

/** Does this edge need supplier information on file? Every external
 *  dependency, and a shared service another legal entity provides — the
 *  control plane's `supplier_required`, mirrored. */
export function needsSupplierInformation(
  edge: Pick<DependencyEdge, 'role' | 'supplier_legal_entity'>
): boolean {
  return edge.role === 'external' || Boolean(edge.supplier_legal_entity?.trim());
}

/** "Credit Bureau Agent · External · Nordic Credit Bureau AB" */
export function dependencyEdgeLabel(edge: DependencyEdge): string {
  const target =
    edge.depends_on_system_name ??
    edge.agent_card_name ??
    edge.depends_on_system_id ??
    edge.agent_card_id ??
    edge.id;
  return [target, DEPENDENCY_ROLE_LABELS[edge.role], edge.supplier_legal_entity]
    .filter(Boolean)
    .join(' · ');
}

/** Edges for the supplier-information picker: those that need it first
 *  (external, or supplied by another entity), then the rest; by label. */
export function supplierEdgesFirst(edges: DependencyEdge[]): DependencyEdge[] {
  return [...edges].sort(
    (a, b) =>
      Number(needsSupplierInformation(b)) - Number(needsSupplierInformation(a)) ||
      dependencyEdgeLabel(a).localeCompare(dependencyEdgeLabel(b))
  );
}

// ─── Separability declarations and accepted callers ───────────────────────

/** "Scoring Service declares Audit Logger separable" / "declared separable
 *  by Brutor Demo System" — from the viewing system's side. */
export function separabilityDeclarationText(d: SeparabilityDeclaration): string {
  const target =
    d.depends_on_system_name ??
    d.agent_card_name ??
    d.depends_on_system_id ??
    d.agent_card_id ??
    'a dependency';
  if (d.side === 'declares') return `Declares ${target} separable`;
  return `Declared separable by ${d.ai_system_name ?? d.ai_system_id}`;
}

/** "accepts only listed callers — enforced" vs the advisory wording. */
export function acceptedCallersText(enforced: boolean): string {
  return enforced
    ? 'Accepts only listed callers — enforced: the gateway refuses any other calling system'
    : 'Accepted callers are advisory: an unlisted caller is a finding, not refused';
}

/**
 * The target contracts an edge may pin to: signed ones only (active,
 * superseded, or approved and awaiting promotion). A rejected contract was
 * never signed and an unapproved draft may never be — pinning either would
 * make the caller rely on terms nobody approved. Newest first.
 */
export function pinnableContracts(
  contracts: AISystemContract[]
): AISystemContract[] {
  return contracts
    .filter(
      (c) =>
        c.status === 'active' ||
        c.status === 'superseded' ||
        (c.status === 'draft' && c.is_approved)
    )
    .sort((a, b) => b.version - a.version);
}

/** "Pinned v2, target active v3 — this pin is behind." */
export function pinStatusText(
  pinnedVersion: number,
  activeVersion: number | null
): string {
  if (activeVersion === null) {
    return `Pinned v${pinnedVersion}; the target has no active contract.`;
  }
  if (activeVersion === pinnedVersion) {
    return `Pinned v${pinnedVersion}, target active v${activeVersion} — up to date.`;
  }
  return (
    `Pinned v${pinnedVersion}, target active v${activeVersion} — this pin is behind. ` +
    `Review the service's diff, then move the pin or follow the active contract.`
  );
}

// ─── Dependencies as contract terms ────────────────────────────────

/** A diff entry that carries a dependency's contract change. */
export function isDependencyContractChange(entry: DiffEntry): boolean {
  return entry.kind === 'dependency_contract_changed';
}

/** The contract a sealed dependency edge ran against: "follows v3 · 1a2b3c4d"
 *  or "pinned v2 · …". External agents have none. */
export function closureDependencyContract(d: ClosureDependency): string {
  if (d.role === 'external') return 'no contract (external)';
  if (!d.contract) return 'target had no active contract';
  const hash = d.contract.hash ? ` · ${d.contract.hash.slice(0, 8)}` : '';
  return `${d.contract_pin ? 'pinned' : 'follows'} v${d.contract.version}${hash}`;
}

export function emptyDependencyDraft(): DependencyDraft {
  return {
    role: 'shared_service',
    depends_on_system_id: null,
    agent_card_id: null,
    criticality: 'critical',
    fallback: null,
    contributes_to_decision: true,
    separability_rationale: null,
    contract_pin: null,
    supplier_legal_entity: null,
    assumptions: {},
    expected_outcomes: null,
  };
}

export function draftFromEdge(edge: DependencyEdge): DependencyDraft {
  return {
    role: edge.role,
    depends_on_system_id: edge.depends_on_system_id,
    agent_card_id: edge.agent_card_id,
    criticality: edge.criticality,
    fallback: edge.fallback,
    contributes_to_decision: edge.contributes_to_decision,
    separability_rationale: edge.separability_rationale,
    contract_pin: edge.contract_pin,
    supplier_legal_entity: edge.supplier_legal_entity,
    assumptions: readAssumptions(edge.assumptions),
    expected_outcomes: edge.expected_outcomes ?? null,
  };
}

/**
 * The body actually sent: fields that do not apply to the chosen role or
 * setting are cleared, so a degradable→critical flip does not keep a stale
 * fallback, and an external edge never carries a pin.
 */
export function normalizeDependencyDraft(
  draft: DependencyDraft
): DependencyDraft {
  const trimmed = (v: string | null) => (v && v.trim() ? v.trim() : null);
  return {
    ...draft,
    depends_on_system_id: draft.depends_on_system_id || null,
    agent_card_id: draft.agent_card_id || null,
    fallback:
      draft.criticality === 'degradable' ? trimmed(draft.fallback) : null,
    separability_rationale: draft.contributes_to_decision
      ? null
      : trimmed(draft.separability_rationale),
    contract_pin:
      draft.role === 'shared_service' ? draft.contract_pin || null : null,
    supplier_legal_entity: trimmed(draft.supplier_legal_entity),
    // null = not declared (no check); a declared list is sent sorted and
    // de-duplicated, as the server stores it.
    expected_outcomes:
      draft.expected_outcomes === null
        ? null
        : [...new Set(draft.expected_outcomes.map((v) => v.trim()).filter(Boolean))].sort(),
  };
}

// ─── Fleet list helpers ───────────────────────────────────────────────────

/** "minimal → high via Brutor Demo System" — or the declared tier alone
 *  when nothing lifts it. `null` when neither is set. */
export function effectiveTierText(
  declared: string | null | undefined,
  effective: string | null | undefined,
  sources: { system_id: string; system_name: string | null }[] | undefined
): string | null {
  if (!declared && !effective) return null;
  if (!effective || effective === declared)
    return declared ?? effective ?? null;
  const via =
    sources && sources.length
      ? ` via ${sources.map((s) => s.system_name ?? s.system_id).join(', ')}`
      : '';
  return `${declared ?? 'undeclared'} → ${effective}${via}`;
}
