/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Composition — the first tab of every AI System.
 *
 * One picture of what the system is: where it sits, which agents it is made
 * of and who calls it (left), the Brutor Gateway that governs every call
 * (centre: policies and limits), and what the gateway lets it reach (right:
 * AI resources and delegation agents).
 *
 * The payload comes from `GET /v1/admin/ai-systems/{id}/composition`, which
 * the control plane builds from the contract closure — so what an approver
 * signs is what this tab draws. The diagram and the list view render ONE
 * model (`buildCompositionModel`), so they cannot disagree.
 *
 * Declared vs observed is drawn, not hidden: declared-and-observed nodes are
 * solid, declared-never-observed ones outlined, and observed-but-undeclared
 * ones highlighted with their finding and a Declare action.
 *
 * The header says whether the system ENFORCES its accepted
 * callers (the gateway refuses others) or treats them as advisory, and lists
 * every sealed separability declaration touching the system — the ones it
 * declares and the ones callers declare on it. External nodes carry their
 * supplier and the task-state mix seen at the gateway boundary.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Boxes,
  ChevronRight,
  Gauge,
  Info,
  LayoutList,
  Network,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  Users,
  Waypoints,
  Workflow,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { errorMessage } from '../../lib/narrow';
import {
  AISystemCompositionView,
  AUTONOMY_LABELS,
  AutonomyLevel,
  BoundaryMetrics,
  CompositionFinding,
  DependencyEdge,
  RunDetail,
  SeparabilityDeclaration,
} from '../../lib/aiSystemTypes';
import { LIFECYCLE_STAGE_LABELS, LifecycleStage } from '../../lib/groupTypes';
import {
  CATEGORY_META,
  COMPOSITION_CATEGORIES,
  CompositionCategory,
  CompositionModel,
  CompositionNode,
  CompositionSection,
  DeclareTarget,
  EDGE_STATE_LABELS,
  acceptedCallersText,
  aiSystemLink,
  buildCompositionModel,
  separabilityDeclarationText,
  effectiveTierText,
  findingLabel,
  groupLink,
  pinnedTargets,
  sortFindings,
} from '../../lib/composition';
import { buildSimpleGraph, graphHasGates, runOverlayArrangement } from '../../lib/compositionSimpleGraph';
import { useElementWidth } from '../../hooks/useElementWidth';
import { applyRunOverlay } from '../../lib/compositionRunOverlay';
import {
  CompositionViewId,
  readStoredView,
  storeView,
} from '../../lib/compositionView';
import { HelpTip } from '../ui/HelpTip';
import {
  TABLE_CONTAINER,
  TABLE_TBODY,
  TABLE_TH,
  TABLE_THEAD,
} from '../ui/tableStyles';
import { AgentReleaseChip, NoRelease } from '../agent/AgentReleaseBadges';
import { LinkText } from './CompositionLink';
import { useContributedLinks } from '../../app/links';
import { CompositionSimpleGraph } from './CompositionSimpleGraph';
import {
  RunOverlayLegend,
  RunPicker,
  RunSequenceList,
} from './CompositionRunOverlay';
import { ConnectionLegend, ConnectionStatusBadge } from './ConnectionStatus';
import { HumanApprovalLegend, HumanApprovalMark } from './HumanApprovalMark';
import { DeleteDependencyDialog, DependencyDialog } from './DependencyDialog';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface CompositionTabProps {
  aiSystemId: string;
  /** Observation window in hours; 0 / undefined = the server default. */
  windowHours?: number;
  /** Opens another page (App tab id). Without it, nodes render unlinked. */
  setActiveTab?: (tab: string) => void;
  /** The run applied to the Simple graph — the route's run segment
   *  (`ai-systems:{id}:composition:{runId}`). Controlled when
   *  `onSelectRun` is given; otherwise the tab keeps its own selection. */
  runId?: string | null;
  /** Select / clear the applied run (updates the route). */
  onSelectRun?: (runId: string | null) => void;
}

const CATEGORY_ICON: Record<
  CompositionCategory,
  React.ComponentType<{ className?: string }>
> = {
  ai_resource: Boxes,
  policy: ShieldCheck,
  limit: Gauge,
  delegation_agent: Waypoints,
};

const SEVERITY_CLASSES: Record<CompositionFinding['severity'], string> = {
  critical: 'bg-red-100 text-red-800',
  warning: 'bg-amber-100 text-amber-800',
  info: 'bg-slate-100 text-slate-700',
};

type DialogState =
  | { kind: 'declare'; prefill: DeclareTarget | null }
  | { kind: 'edit'; edge: DependencyEdge }
  | { kind: 'delete'; edge: DependencyEdge }
  | null;

export function CompositionTab({
  aiSystemId,
  windowHours,
  setActiveTab,
  runId,
  onSelectRun,
}: CompositionTabProps) {
  const {
    fetchAISystemComposition,
    fetchAISystemContracts,
    fetchSystemHealth,
    fetchRun,
  } = useGateway();
  // Links to pages this console may not have (a page another distribution
  // contributes): asked for, and absent when nobody answers.
  const contributedLink = useContributedLinks();
  const [view, setViewState] = useState<CompositionViewId>(() =>
    runId ? 'simple' : readStoredView()
  );
  const setView = useCallback((v: CompositionViewId) => {
    setViewState(v);
    storeView(v);
  }, []);

  // The run overlay's selection: the route's, when the parent routes it.
  const [localRun, setLocalRun] = useState<string | null>(runId ?? null);
  const selectedRun = onSelectRun ? (runId ?? null) : localRun;
  const selectRun = onSelectRun ?? setLocalRun;
  // A run arriving (a "Show on graph" deep link) opens the Simple graph —
  // the only view the overlay draws on. Adjusted during render, not in an
  // effect, so the first paint is already the right view.
  const [shownRun, setShownRun] = useState<string | null>(selectedRun);
  if (shownRun !== selectedRun) {
    setShownRun(selectedRun);
    if (selectedRun && view !== 'simple') setViewState('simple');
  }
  const [highlight, setHighlight] = useState<readonly string[] | null>(null);

  // The selected run's actions — THE projection Run details reads too.
  const [runResult, setRunResult] = useState<{
    key: string;
    data: RunDetail | null;
    error: string;
  } | null>(null);
  useEffect(() => {
    if (!selectedRun) return;
    let cancelled = false;
    fetchRun(selectedRun).then(
      (data) => {
        if (!cancelled) setRunResult({ key: selectedRun, data, error: '' });
      },
      (e: unknown) => {
        if (!cancelled)
          setRunResult({
            key: selectedRun,
            data: null,
            error: errorMessage(e, 'Failed to load the run'),
          });
      }
    );
    return () => {
      cancelled = true;
    };
  }, [selectedRun, fetchRun]);
  const runDetail =
    selectedRun && runResult?.key === selectedRun ? runResult.data : null;
  const runError =
    selectedRun && runResult?.key === selectedRun ? runResult.error : '';
  const runLoading = Boolean(selectedRun) && runResult?.key !== selectedRun;
  const [dialog, setDialog] = useState<DialogState>(null);

  // One request per (system, window, reload). The result is keyed by the
  // request it answers, so "loading" is derived rather than set inside the
  // effect, and a slow stale response can never overwrite a newer one.
  const [reloads, setReloads] = useState(0);
  const requestKey = `${aiSystemId}|${windowHours ?? ''}|${reloads}`;
  const [result, setResult] = useState<{
    key: string;
    data: AISystemCompositionView | null;
    error: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAISystemComposition(aiSystemId, windowHours || undefined).then(
      (data) => {
        if (!cancelled) setResult({ key: requestKey, data, error: '' });
      },
      (e: unknown) => {
        if (!cancelled) {
          setResult({
            key: requestKey,
            data: null,
            error: errorMessage(e, 'Failed to load the composition'),
          });
        }
      }
    );
    return () => {
      cancelled = true;
    };
  }, [aiSystemId, windowHours, requestKey, fetchAISystemComposition]);

  const loading = result?.key !== requestKey;
  // Keep showing the last composition while a refresh is in flight.
  const data = result?.data ?? null;
  const error = result?.error ?? '';
  const load = useCallback(() => setReloads((n) => n + 1), []);
  const [readFailure, reads] = useReadFailures<'targets' | 'boundary'>();

  // A pinned edge shows where its target is now ("pinned to v2 · target
  // active v3 — behind"): the pinned caller does not drift, so this is
  // where its owner sees the pin fall behind. Best-effort per target.
  const pinned = useMemo(
    () => (data ? pinnedTargets(data).join(',') : ''),
    [data]
  );
  const [targetActive, setTargetActive] = useState<
    Record<string, number | null>
  >({});
  useEffect(() => {
    if (!pinned) return;
    let cancelled = false;
    const targets = pinned.split(',');
    void Promise.allSettled(targets.map((target) => fetchAISystemContracts(target))).then((answers) => {
      if (cancelled) return;
      const active: Record<string, number | null> = {};
      answers.forEach((answer, i) => {
        if (answer.status === 'fulfilled') active[targets[i]] = answer.value.active_version ?? null;
      });
      setTargetActive((prev) => ({ ...prev, ...active }));
      // A target that was not read keeps its pinned-only wording; the tab says which read it lacks.
      const refused = answers.find((answer): answer is PromiseRejectedResult => answer.status === 'rejected');
      if (refused) reads.lost('targets', 'Failed to load the active version of a pinned target', refused.reason);
      else reads.loaded('targets');
    });
    return () => {
      cancelled = true;
    };
  }, [pinned, fetchAISystemContracts, reloads, reads]);

  // External agents are seen at the boundary only: their metrics (and
  // the A2A task-state mix) come from the system's dependency health.
  // Best-effort — a failed load leaves the nodes without them.
  const hasExternal = Boolean(
    data?.dependencies.some((d) => d.role === 'external' && d.declared)
  );
  const [boundaryByEdge, setBoundaryByEdge] = useState<
    Record<string, BoundaryMetrics | null>
  >({});
  useEffect(() => {
    if (!hasExternal) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchSystemHealth(aiSystemId))
      .then(
        (health) => {
          if (cancelled) return;
          const map: Record<string, BoundaryMetrics | null> = {};
          for (const e of health.dependency_health ?? []) {
            if (e.role === 'external') map[e.edge_id] = e.boundary;
          }
          setBoundaryByEdge(map);
          reads.loaded('boundary');
        },
        (err: unknown) => {
          // The nodes keep their observed facts only.
          if (!cancelled) reads.failed('boundary', 'Failed to load the metrics of the external agents', err);
        }
      );
    return () => {
      cancelled = true;
    };
  }, [aiSystemId, hasExternal, reloads, fetchSystemHealth, reads]);

  const model = useMemo(
    () =>
      data ? buildCompositionModel(data, targetActive, boundaryByEdge, contributedLink) : null,
    [data, targetActive, boundaryByEdge, contributedLink]
  );

  const simple = useMemo(
    () => (data && model ? buildSimpleGraph(data, model) : null),
    [data, model]
  );
  // The run applied to it: the graph to draw (plus anything the run reached
  // that the composition does not declare) and the overlay model.
  const applied = useMemo(
    () => (simple && runDetail ? applyRunOverlay(simple, runDetail) : null),
    [simple, runDetail]
  );
  // Two columns only with a run applied and room for them.
  const [measureRunLayout, runLayoutWidth] = useElementWidth();
  const arrangement = runOverlayArrangement(runLayoutWidth);
  const columns = Boolean(applied) && arrangement === 'columns';

  const actions: NodeActions = useMemo(
    () => ({
      open: setActiveTab,
      edit: (edge) => setDialog({ kind: 'edit', edge }),
      remove: (edge) => setDialog({ kind: 'delete', edge }),
      declare: (target) => setDialog({ kind: 'declare', prefill: target }),
    }),
    [setActiveTab]
  );

  const closeAndReload = () => {
    setDialog(null);
    load();
  };

  if (loading && !data) {
    return (
      <div className="p-8 text-center text-sm text-muted">
        Loading composition…
      </div>
    );
  }
  if (error) {
    return (
      <div
        className="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-4"
        data-testid="composition-error"
      >
        <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-red-600" />
        <span className="text-sm text-red-800">{error}</span>
      </div>
    );
  }
  if (!data || !model) return null;

  return (
    <div className="space-y-4" data-testid="composition-tab">
      <ReadFailureNotice failure={readFailure('targets')} onRetry={load} />
      <ReadFailureNotice failure={readFailure('boundary')} onRetry={load} />
      {/* The Simple graph's own sentence, so the header and the graph cannot differ. */}
      <SystemHeader data={data} open={setActiveTab} approvalSummary={simple?.humanApproval ?? null} />

      <div className="flex flex-wrap items-center gap-3">
        <Legend />
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-muted" data-testid="composition-window">
            Observed over {windowLabel(data.window_hours)}
          </span>
          <div
            className="flex rounded-lg border border-strong p-0.5"
            role="group"
            aria-label="Composition view"
          >
            <ViewButton
              active={view === 'simple'}
              onClick={() => setView('simple')}
              testId="composition-view-simple"
            >
              <Workflow className="h-3.5 w-3.5" /> Simple graph
            </ViewButton>
            <ViewButton
              active={view === 'diagram'}
              onClick={() => setView('diagram')}
              testId="composition-view-diagram"
            >
              <Network className="h-3.5 w-3.5" /> Diagram
            </ViewButton>
            <ViewButton
              active={view === 'list'}
              onClick={() => setView('list')}
              testId="composition-view-list"
            >
              <LayoutList className="h-3.5 w-3.5" /> List
            </ViewButton>
          </div>
          <button
            type="button"
            onClick={() => setDialog({ kind: 'declare', prefill: null })}
            className="flex items-center gap-1.5 rounded-lg border border-strong px-3 py-1.5 text-sm hover:bg-surface-hover"
            data-testid="composition-declare"
          >
            <Plus className="h-4 w-4" /> Declare dependency
          </button>
          <button
            type="button"
            onClick={load}
            className="rounded-lg border border-strong p-1.5 hover:bg-surface-hover"
            aria-label="Refresh composition"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {view === 'simple' && simple && (
        <div className="space-y-3" data-testid="composition-simple-view">
          <RunPicker
            aiSystemId={aiSystemId}
            selectedId={selectedRun}
            selected={runDetail}
            onSelect={(id) => {
              setHighlight(null);
              selectRun(id);
            }}
          />
          {runLoading && (
            <p className="text-xs text-muted" data-testid="run-overlay-loading">
              Loading the run…
            </p>
          )}
          {runError && (
            <p className="text-xs text-red-700" data-testid="run-overlay-error">
              {runError} — showing the plain graph.
            </p>
          )}
          {/* No run: one column, as always. A run applied: the graph and
              its connection legend on the left, the sequence list and its
              legend on the right — the right column takes the left one's
              height (min 32 rem) and scrolls on its own. Narrower than
              RUN_COLUMNS_MIN_WIDTH it stacks: graph, then sequence. */}
          <div
            ref={measureRunLayout}
            data-testid="composition-run-layout"
            data-arrangement={applied ? arrangement : 'single'}
            className={
              columns
                ? 'grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-4'
                : 'space-y-3'
            }
          >
            <div className="min-w-0" data-testid="composition-graph-column">
              <CompositionSimpleGraph
                graph={applied?.graph ?? simple}
                open={setActiveTab}
                overlay={applied?.overlay ?? null}
                highlight={{ active: highlight, setActive: setHighlight }}
                fit={Boolean(applied)}
              />
            </div>
            {applied && (
              <div
                className={columns ? 'relative min-h-[32rem] min-w-0' : 'min-w-0'}
                data-testid="composition-sequence-column"
              >
                {/* Columns: the absolute box keeps the list out of the row's
                    height (the graph column sets it); inside it the list and
                    its legend stick to the top of the page scroller, at most
                    a viewport tall, while the page scrolls past a tall graph. */}
                <div className={columns ? 'absolute inset-0' : undefined}>
                  <div
                    className={
                      columns
                        ? 'app-column sticky top-4 h-full max-h-[calc(100vh-2rem)] gap-3'
                        : 'space-y-3'
                    }
                    data-testid="composition-sequence-body"
                  >
                    <RunSequenceList
                      overlay={applied.overlay}
                      highlight={{ active: highlight, setActive: setHighlight }}
                      layout={columns ? 'column' : 'full'}
                    />
                    <div className={columns ? 'app-bar' : undefined}>
                      <RunOverlayLegend compact={columns} />
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
      {view === 'diagram' && <Diagram model={model} actions={actions} />}
      {view === 'list' && <ListView model={model} actions={actions} />}
      {/* The Simple graph carries its own legend; the Diagram and the List
          show the same connection statuses, so they explain them the same way. */}
      {view !== 'simple' && (
        <ConnectionLegend
          testId={`composition-${view}-connection-legend`}
          showExternal={false}
        />
      )}
      {view !== 'simple' && simple && graphHasGates(simple) && <HumanApprovalLegend />}

      <Findings findings={data.findings} />

      {dialog?.kind === 'declare' && (
        <DependencyDialog
          aiSystemId={aiSystemId}
          aiSystemName={data.system.name}
          prefill={dialog.prefill}
          onSaved={closeAndReload}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog?.kind === 'edit' && (
        <DependencyDialog
          aiSystemId={aiSystemId}
          aiSystemName={data.system.name}
          edge={dialog.edge}
          onSaved={closeAndReload}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog?.kind === 'delete' && (
        <DeleteDependencyDialog
          aiSystemId={aiSystemId}
          edge={dialog.edge}
          onDeleted={closeAndReload}
          onCancel={() => setDialog(null)}
        />
      )}
    </div>
  );
}

function windowLabel(hours: number): string {
  if (hours % 24 === 0 && hours >= 48) return `the last ${hours / 24} days`;
  return `the last ${hours} h`;
}

// ─── Header: location + facts ─────────────────────────────────────────────

function SystemHeader({
  data,
  open,
  approvalSummary,
}: {
  data: AISystemCompositionView;
  open?: (tab: string) => void;
  approvalSummary: string | null;
}) {
  const { system, location } = data;
  const tier = effectiveTierText(
    system.risk_tier_declared,
    system.risk_tier_effective,
    system.effective_tier_sources
  );
  const lifted = Boolean(
    system.risk_tier_effective &&
    system.risk_tier_effective !== system.risk_tier_declared
  );
  return (
    <div
      className="space-y-2 rounded-lg border border-default bg-surface p-3"
      data-testid="composition-header"
    >
      <nav aria-label="Location" data-testid="composition-location">
        <ol className="flex flex-wrap items-center gap-1 text-sm">
          {location.map((step, i) => (
            <li key={step.id} className="flex items-center gap-1">
              {i > 0 && (
                <ChevronRight
                  className="h-3.5 w-3.5 text-muted"
                  aria-hidden="true"
                />
              )}
              <LinkText
                to={groupLink(step.id)}
                open={open}
                className={
                  i === location.length - 1
                    ? 'font-semibold text-default'
                    : 'text-secondary'
                }
                testId={`composition-location-${step.id}`}
              >
                {step.name}
              </LinkText>
            </li>
          ))}
        </ol>
      </nav>
      <dl
        className="flex flex-wrap gap-x-6 gap-y-1 text-xs"
        data-testid="composition-facts"
      >
        <Fact label="Owner" value={system.owner} />
        <Fact
          label="Stage"
          value={
            system.lifecycle_stage
              ? (LIFECYCLE_STAGE_LABELS[
                  system.lifecycle_stage as LifecycleStage
                ] ?? system.lifecycle_stage)
              : null
          }
        />
        <Fact
          label="Risk"
          value={tier}
          testId="composition-fact-risk"
          emphasis={lifted}
          suffix={
            system.separability_declared ? ' · separability declared' : ''
          }
        />
        <Fact
          label="Autonomy"
          value={
            system.autonomy_level
              ? (AUTONOMY_LABELS[system.autonomy_level as AutonomyLevel] ??
                system.autonomy_level)
              : null
          }
        />
        {approvalSummary && (
          <Fact
            label="Human approval"
            value={approvalSummary}
            testId="composition-fact-human-approval"
          />
        )}
        <Fact
          label="Active contract"
          value={
            system.active_contract ? `v${system.active_contract.version}` : null
          }
          testId="composition-fact-contract"
        />
      </dl>
      <p
        className={`flex items-start gap-1.5 text-xs ${
          system.enforce_accepted_callers ? 'text-default' : 'text-muted'
        }`}
        data-testid="composition-accepted-callers"
        data-enforced={system.enforce_accepted_callers ? 'true' : 'false'}
      >
        <ShieldCheck
          className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${
            system.enforce_accepted_callers ? 'text-green-700' : 'text-muted'
          }`}
          aria-hidden="true"
        />
        <span>
          {acceptedCallersText(system.enforce_accepted_callers)}.{' '}
          <LinkText
            to={groupLink(system.id, 'general:ownership')}
            open={open}
            className="underline"
          >
            Change on Ownership &amp; Purpose
          </LinkText>
        </span>
      </p>
      <SeparabilityDeclarations
        declarations={system.separability_declarations}
        open={open}
      />
    </div>
  );
}

/**
 * Every separability claim touching this system: the ones
 * it declares on its own dependencies and the ones callers declare on it,
 * each with its sealed record — or plainly "not sealed", because the
 * registry does not accept an unsealed claim.
 */
function SeparabilityDeclarations({
  declarations,
  open,
}: {
  declarations: SeparabilityDeclaration[];
  open?: (tab: string) => void;
}) {
  if (declarations.length === 0) return null;
  return (
    <div
      className="space-y-1 border-t border-default pt-2"
      data-testid="composition-separability"
    >
      <div className="flex items-center gap-1.5">
        <h4 className="text-xs font-semibold text-default">
          Separability declarations
        </h4>
        <HelpTip
          title="Separability declarations"
          text="A dependency declared as not contributing to the decision, with a rationale. It is sealed as a declaration record and shown on both systems: without it, a contributing dependency is evaluated at its caller's risk tier."
        />
      </div>
      <ul className="space-y-1">
        {declarations.map((d) => {
          const other =
            d.side === 'declares' ? d.depends_on_system_id : d.ai_system_id;
          return (
            <li
              key={d.edge_id}
              className="text-xs"
              data-testid={`separability-${d.edge_id}`}
              data-side={d.side}
              data-sealed={d.sealed ? 'true' : 'false'}
            >
              <span className="font-medium text-default">
                {other ? (
                  <LinkText to={aiSystemLink(other)} open={open}>
                    {separabilityDeclarationText(d)}
                  </LinkText>
                ) : (
                  separabilityDeclarationText(d)
                )}
              </span>
              {d.separability_rationale && (
                <span className="text-secondary">
                  {' '}
                  — {d.separability_rationale}
                </span>
              )}
              <span className="block text-muted">
                {d.sealed && d.record_id ? (
                  <>
                    sealed as{' '}
                    <code className="font-mono">{d.record_id}</code>
                    {d.sealed_at
                      ? ` · ${new Date(d.sealed_at).toLocaleString()}`
                      : ''}
                  </>
                ) : (
                  <span className="text-amber-800">
                    not sealed — the registry does not accept it until it is
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Fact({
  label,
  value,
  testId,
  emphasis,
  suffix,
}: {
  label: string;
  value: string | null | undefined;
  testId?: string;
  emphasis?: boolean;
  suffix?: string;
}) {
  return (
    <div className="flex items-baseline gap-1.5" data-testid={testId}>
      <dt className="text-muted">{label}</dt>
      <dd className={emphasis ? 'font-semibold text-red-700' : 'text-default'}>
        {value ?? <span className="italic text-muted">not set</span>}
        {value && suffix}
      </dd>
    </div>
  );
}

// ─── Legend ───────────────────────────────────────────────────────────────

function Legend() {
  return (
    <ul
      className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-secondary"
      aria-label="Legend"
      data-testid="composition-legend"
    >
      {COMPOSITION_CATEGORIES.map((c) => {
        const Icon = CATEGORY_ICON[c.id];
        return (
          <li
            key={c.id}
            className={`flex items-center gap-1.5 ${c.className}`}
            data-testid={`legend-${c.id}`}
            title={c.description}
          >
            <span
              className="composition-swatch h-2.5 w-2.5 rounded-sm"
              aria-hidden="true"
            />
            <Icon className="composition-ink h-3.5 w-3.5" />
            {c.label}
          </li>
        );
      })}
      <li
        className="flex items-center gap-1.5 text-muted"
        data-testid="legend-neutral"
      >
        <Bot className="h-3.5 w-3.5" /> Agents &amp; callers
      </li>
      <li className="flex items-center gap-1.5 text-muted">
        <span
          className="h-2.5 w-4 rounded-sm border border-dashed border-strong"
          aria-hidden="true"
        />
        External · assumed, not tested
      </li>
    </ul>
  );
}

// ─── Diagram ──────────────────────────────────────────────────────────────

interface NodeActions {
  open?: (tab: string) => void;
  edit: (edge: DependencyEdge) => void;
  remove: (edge: DependencyEdge) => void;
  declare: (target: DeclareTarget) => void;
}

function Diagram({
  model,
  actions,
}: {
  model: CompositionModel;
  actions: NodeActions;
}) {
  const column = (c: CompositionSection['column']) =>
    model.sections.filter((s) => s.column === c);
  return (
    <div
      className="grid grid-cols-1 items-stretch gap-3 lg:grid-cols-[minmax(0,1fr)_1.5rem_minmax(0,1.25fr)_1.5rem_minmax(0,1.25fr)]"
      data-testid="composition-diagram"
    >
      <div className="min-w-0 space-y-3" data-testid="composition-column-left">
        {column('left').map((s) => (
          <SectionBlock key={s.id} section={s} actions={actions} />
        ))}
      </div>
      <Connector />
      <div
        className="composition-gateway min-w-0 rounded-xl p-3"
        data-testid="composition-gateway"
      >
        <div className="mb-3 flex items-center gap-2">
          <Network className="h-4 w-4 text-secondary" />
          <h3 className="text-sm font-semibold text-default">Brutor Gateway</h3>
          <span className="text-xs text-muted">applies to every call</span>
        </div>
        <div className="space-y-3">
          {column('centre').map((s) => (
            <SectionBlock key={s.id} section={s} actions={actions} />
          ))}
        </div>
      </div>
      <Connector />
      <div className="min-w-0 space-y-3" data-testid="composition-column-right">
        {column('right').map((s) => (
          <SectionBlock key={s.id} section={s} actions={actions} />
        ))}
      </div>
    </div>
  );
}

/** The arrow between columns: calls flow agents → gateway → resources. */
function Connector() {
  return (
    <div
      className="hidden items-center justify-center lg:flex"
      aria-hidden="true"
    >
      <ArrowRight className="h-5 w-5 text-muted" />
    </div>
  );
}

function SectionBlock({
  section,
  actions,
}: {
  section: CompositionSection;
  actions: NodeActions;
}) {
  const meta = section.category ? CATEGORY_META[section.category] : null;
  const Icon = section.category
    ? CATEGORY_ICON[section.category]
    : section.id === 'callers'
      ? Users
      : Bot;
  return (
    <section
      className={meta?.className}
      aria-label={section.title}
      data-testid={`composition-section-${section.id}`}
    >
      <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-secondary">
        <Icon
          className={`h-3.5 w-3.5 ${meta ? 'composition-ink' : 'text-muted'}`}
        />
        {section.title}
        <span className="font-normal text-muted">{section.nodes.length}</span>
      </h4>
      {section.nodes.length === 0 ? (
        <p className="text-xs italic text-muted">{section.empty}</p>
      ) : (
        <ul className="space-y-1.5">
          {section.nodes.map((n) => (
            <li key={n.key}>
              <NodeCard node={n} actions={actions} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function NodeCard({
  node,
  actions,
}: {
  node: CompositionNode;
  actions: NodeActions;
}) {
  const meta = node.category ? CATEGORY_META[node.category] : null;
  const Icon = node.category
    ? CATEGORY_ICON[node.category]
    : node.kind === 'Member agent'
      ? Bot
      : Users;
  const highlighted =
    node.state === 'observed_undeclared' || node.state === 'granted_undeclared';
  return (
    <div
      className={`composition-node ${meta?.className ?? ''} rounded-md px-2.5 py-1.5 ${
        highlighted ? 'ring-2 ring-amber-500' : ''
      }`}
      data-testid={`composition-node-${node.key}`}
      data-category={node.category ?? 'neutral'}
      data-state={node.state}
      data-dashed={node.dashed ? 'true' : 'false'}
    >
      <div className="flex items-center gap-1.5">
        <Icon
          className={`h-3.5 w-3.5 shrink-0 ${meta ? 'composition-ink' : 'text-muted'}`}
        />
        <LinkText
          to={node.link}
          open={actions.open}
          className="truncate text-sm font-medium text-default"
        >
          {node.label}
        </LinkText>
        {/* The category in words — colour is never the only signal. */}
        <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-muted">
          {meta ? `${meta.label} · ${node.kind}` : node.kind}
        </span>
      </div>
      {node.summary && (
        <p className="mt-0.5 text-xs text-secondary">{node.summary}</p>
      )}
      {/* The system's connection to this node — the Simple graph's line
          status, with its reason (resources and delegation nodes). */}
      {node.connection && (
        <div className="mt-0.5">
          <ConnectionStatusBadge
            name={node.label}
            connection={node.connection}
            testId={`connection-${node.key}`}
          />
        </div>
      )}
      {node.agentRelease && (
        <div className="mt-0.5" data-testid={`release-${node.key}`}>
          <MemberAgentRelease release={node.agentRelease} />
        </div>
      )}
      {node.humanApproval && (
        <div>
          <HumanApprovalMark
            approval={node.humanApproval}
            testId={`approval-${node.key}`}
          />
        </div>
      )}
      {node.inheritedFrom && (
        <p
          className="mt-0.5 text-[11px] text-muted"
          data-testid={`inherited-${node.key}`}
        >
          inherited from{' '}
          <LinkText
            to={groupLink(node.inheritedFrom.id)}
            open={actions.open}
            className="underline"
          >
            {node.inheritedFrom.name}
          </LinkText>
        </p>
      )}
      {/* Facts in the diagram for delegation agents only (role,
                criticality, contract, health, outcomes); the list view
                carries every node's facts. */}
      {node.facts.length > 0 && node.category === 'delegation_agent' && (
        <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 text-[11px]">
          {node.facts.map((f) => (
            <React.Fragment key={f.label}>
              <dt className="text-muted">{f.label}</dt>
              <dd className="text-secondary">{f.value}</dd>
            </React.Fragment>
          ))}
        </dl>
      )}
      {node.state !== 'static' && (
        <p
          className="mt-0.5 text-[11px] font-medium text-secondary"
          data-testid={`state-${node.key}`}
        >
          {EDGE_STATE_LABELS[node.state]}
        </p>
      )}
      {node.findings.map((f, i) => (
        <p
          key={`${f.code}-${i}`}
          className="mt-1 flex items-start gap-1 text-[11px] text-amber-800"
        >
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          {f.message}
        </p>
      ))}
      <NodeActionsRow node={node} actions={actions} />
    </div>
  );
}

function NodeActionsRow({
  node,
  actions,
}: {
  node: CompositionNode;
  actions: NodeActions;
}) {
  const { edge, declareTarget } = node;
  if (!edge && !declareTarget) return null;
  return (
    <div className="mt-1 flex gap-2">
      {declareTarget && (
        <button
          type="button"
          onClick={() => actions.declare(declareTarget)}
          className="flex items-center gap-1 rounded border border-amber-400 px-1.5 py-0.5 text-[11px] font-medium text-amber-800 hover:bg-amber-50"
          data-testid={`declare-${node.key}`}
        >
          <Plus className="h-3 w-3" /> Declare
        </button>
      )}
      {edge && (
        <>
          <button
            type="button"
            onClick={() => actions.edit(edge)}
            className="flex items-center gap-1 text-[11px] text-secondary hover:text-default"
            data-testid={`edit-${node.key}`}
            aria-label={`Edit dependency on ${node.label}`}
          >
            <Pencil className="h-3 w-3" /> Edit
          </button>
          <button
            type="button"
            onClick={() => actions.remove(edge)}
            className="flex items-center gap-1 text-[11px] text-secondary hover:text-red-700"
            data-testid={`delete-${node.key}`}
            aria-label={`Remove dependency on ${node.label}`}
          >
            <Trash2 className="h-3 w-3" /> Remove
          </button>
        </>
      )}
    </div>
  );
}

/** A member agent's current release: `name@version`, trust and
 *  approval; the same chip in the Diagram and the List. */
function MemberAgentRelease({
  release,
}: {
  release: NonNullable<CompositionNode['agentRelease']>;
}) {
  return release.ref ? (
    <AgentReleaseChip release={release.ref} approved={release.ref.approved} />
  ) : (
    <NoRelease />
  );
}

// ─── Accessible list view ─────────────────────────────────────────────────

const COLUMN_LABEL: Record<CompositionSection['column'], string> = {
  left: 'Agents and callers',
  centre: 'Brutor Gateway',
  right: 'What the gateway reaches',
};

function ListView({
  model,
  actions,
}: {
  model: CompositionModel;
  actions: NodeActions;
}) {
  return (
    <div className="space-y-4" data-testid="composition-list">
      {model.sections.map((s) => (
        <section key={s.id} aria-labelledby={`composition-list-${s.id}`}>
          <h4
            id={`composition-list-${s.id}`}
            className="mb-1 text-sm font-semibold text-default"
          >
            {COLUMN_LABEL[s.column]} — {s.title} ({s.nodes.length})
          </h4>
          {s.nodes.length === 0 ? (
            <p className="text-xs italic text-muted">{s.empty}</p>
          ) : (
            <div className={TABLE_CONTAINER}>
              <table className="min-w-full text-sm">
                <thead className={TABLE_THEAD}>
                  <tr>
                    <th className={TABLE_TH}>Name</th>
                    <th className={TABLE_TH}>Category</th>
                    <th className={TABLE_TH}>Kind</th>
                    <th className={TABLE_TH}>State</th>
                    <th className={TABLE_TH}>Status</th>
                    <th className={TABLE_TH}>Detail</th>
                  </tr>
                </thead>
                <tbody className={TABLE_TBODY}>
                  {s.nodes.map((n) => (
                    <tr
                      key={n.key}
                      data-testid={`composition-list-row-${n.key}`}
                      data-state={n.state}
                    >
                      <td className="px-4 py-2">
                        <LinkText
                          to={n.link}
                          open={actions.open}
                          className="text-default"
                        >
                          {n.label}
                        </LinkText>
                      </td>
                      <td className="px-4 py-2 text-secondary">
                        {n.category
                          ? CATEGORY_META[n.category].label
                          : 'Neutral'}
                      </td>
                      <td className="px-4 py-2 text-secondary">{n.kind}</td>
                      <td className="px-4 py-2 text-secondary">
                        {n.dashed
                          ? 'Assumed, not tested'
                          : EDGE_STATE_LABELS[n.state] || '—'}
                      </td>
                      <td className="px-4 py-2">
                        {n.connection ? (
                          <ConnectionStatusBadge
                            name={n.label}
                            connection={n.connection}
                            testId={`list-connection-${n.key}`}
                          />
                        ) : (
                          <span className="text-xs text-muted">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-xs text-muted">
                        {n.agentRelease && (
                          <div
                            className="mb-0.5"
                            data-testid={`list-release-${n.key}`}
                          >
                            <MemberAgentRelease release={n.agentRelease} />
                          </div>
                        )}
                        {[
                          n.summary,
                          n.inheritedFrom
                            ? `inherited from ${n.inheritedFrom.name}`
                            : null,
                          ...n.facts.map((f) => `${f.label}: ${f.value}`),
                          ...(n.humanApproval
                            ? [`Human approval: ${n.humanApproval.details.join('; ')}`]
                            : []),
                          ...n.findings.map((f) => f.message),
                        ]
                          .filter(Boolean)
                          .join(' · ')}
                        <NodeActionsRow node={n} actions={actions} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      ))}
    </div>
  );
}

// ─── Findings ─────────────────────────────────────────────────────────────

function Findings({ findings }: { findings: CompositionFinding[] }) {
  return (
    <section
      className="rounded-lg border border-default p-3"
      data-testid="composition-findings"
    >
      <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-default">
        Findings
        <span className="font-normal text-muted">{findings.length}</span>
        <HelpTip
          title="Composition findings"
          text="Where what is declared, what the gateway allows and what it observed disagree: an undeclared dependency, a caller the service has not accepted, an a2a_call grant with no declared dependency (or a dependency no grant permits), a dependency declared but never used, a role that does not match, a pinned contract the service has moved past, or a dependency cycle. Each clears when the declaration matches; the same findings reach the Assurance Inbox."
        />
      </h3>
      {findings.length === 0 ? (
        <p className="flex items-center gap-1.5 text-xs text-muted">
          <Info className="h-3.5 w-3.5" /> Declarations match what was observed.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {sortFindings(findings).map((f, i) => (
            <li
              key={`${f.code}-${f.subject_id ?? i}`}
              className="flex items-start gap-2 text-sm"
              data-testid={`composition-finding-${f.code}`}
            >
              <span
                className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium ${SEVERITY_CLASSES[f.severity]}`}
              >
                {f.severity}
              </span>
              <span className="shrink-0 font-medium text-default">
                {findingLabel(f.code)}
              </span>
              <span className="text-secondary">{f.message}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ─── Small pieces ─────────────────────────────────────────────────────────

function ViewButton({
  active,
  onClick,
  children,
  testId,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  testId: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      data-testid={testId}
      className={`flex items-center gap-1 rounded-md px-2.5 py-1 text-xs ${
        active
          ? 'bg-primary-50 text-primary-900'
          : 'text-secondary hover:text-default'
      }`}
    >
      {children}
    </button>
  );
}

export default CompositionTab;
