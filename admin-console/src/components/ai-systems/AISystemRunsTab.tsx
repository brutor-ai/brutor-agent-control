/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Runs for one AI System.
 *
 * Four signals and a run list. Everything here is descriptive: it
 * says what the system *did*, not whether that was normal, because this
 * tab compares against no baseline (Drift does). The UI is written to
 * make that limit visible rather than to paper over it:
 *
 *   - a 0% exhaustion rate with no client reporting renders as "not
 *     reported", never as a healthy zero;
 *   - a run is judged on three questions: how the execution
 *     ended (with its error cause), whether the outcome it reported is
 *     declared, and whether that outcome counts as work. No-work runs (an
 *     empty poll) are grey, never green, and never averaged into the cost
 *     or length of the work;
 *   - chain integrity below 100% is stated next to the numbers it
 *     qualifies, so an operator knows how much of the system we actually
 *     observed before trusting anything above.
 *
 * ## Layout: a one-row status strip, then five tabs
 *
 * The rule: the numbers' *qualifiers* must always be visible — a system that
 * stopped calling reads healthy on every traffic-derived signal, and a figure
 * covering 60% of the traffic must say so next to the figure. Pinning the
 * full liveness form and every drift card would satisfy that, but it grows
 * past a 1080p viewport and pushes the tabs below the fold: the qualifiers
 * pinned and everything they qualify out of sight.
 *
 * A qualifier is a **verdict**, not a form. `StatusStrip` pins the verdicts
 * (health + as-of, liveness, open drift count, coverage with the
 * signed/asserted/broken split) in ~50px; the coverage banner appears only
 * when coverage is actually below 100%. Forms and detail cards live one
 * click behind their chip:
 *
 *   Signals   — liveness config + the four descriptive signals + the
 *               declared incident windows, shaded on each chart
 *   Runs      — the run list; a row expands into its action timeline
 *   Drift     — findings (grouped ×N) + triage
 *   Contract  — contract, diff, lifecycle gate
 *   Assurance — replay, response policies, health detail, the report
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, BookOpen, Check, Copy, RefreshCw,
    Info,
    Workflow,
} from 'lucide-react';

import { ResizableTh } from '../ui/ResizableTh';
import { TABLE_TBODY, TABLE_THEAD } from '../ui/tableStyles';
import { Pagination } from '../ui/Pagination';
import { useColumnWidths } from '../../hooks/useColumnWidths';
import { LifecycleGuideModal } from './LifecycleGuideModal';

import { useGateway } from '../../hooks/useGateway';
import LivenessCard from './LivenessCard';
import SignalsOverview from './SignalsOverview';
import AssurancePanel from './AssurancePanel';
import AISystemComplianceTab from './AISystemComplianceTab';
import ContractsPanel, { type ContractSubTab } from './ContractsPanel';
import LifecyclePanel from './LifecyclePanel';
import CompositionTab from './CompositionTab';
import RunDetailsDialog from './RunDetailsDialog';
import ServedCallsTable from './ServedCallsTable';
import ReplayPanel from './ReplayPanel';
import TrajectoryExplorer from './TrajectoryExplorer';
import ResponsePolicyPanel from './ResponsePolicyPanel';
import DataScopePanel from './DataScopePanel';
import DriftPanel from './DriftPanel';
import StatusStrip from './StatusStrip';
import SignalTrendChart from './SignalTrendChart';
import IncidentWindowsPanel from './IncidentWindowsPanel';
import { SignalHeading, Th, Tile } from './SignalParts';
import { ContextSizeSection } from './ContextSizeSection';
import { ErrorCauseText, RunClassChip, RunOutcomeCell } from './RunOutcomeBadges';
import {
    ActionsByOutcomeTable,
    CostByOutcomeTable,
    ErrorCausesSection,
    NoWorkOverheadTile,
    OutcomesSignalSection,
} from './OutcomeSignals';
import { money, num, pct } from '../../lib/signalFormat';
import { RUN_CLASSES, RUN_CLASS_LABELS, type OutcomeDecl } from '../../lib/runOutcomes';
import type { RunClass } from '../../lib/aiSystemTypes';
import type { IncidentWindow } from '../../lib/driftResolutionTypes';
import { releaseLabel, releaseLongLabel, runRelease } from '../../lib/agentRelease';
import { TrustMarker } from '../agent/AgentReleaseBadges';
import { zeroFillTimeseries } from '../../lib/runTimeseries';
import { statusCodeText, stepErrorSummary } from '../../lib/stepErrors';
import { runActionDisplayOrder, runOverlayRoute } from '../../lib/compositionRunOverlay';
import { useTimespanHoursPreference, type Timespan } from '../../hooks/useTimespanPreference';
import {
    RunDetail,
    RunAction,
    RunListResponse,
    ServedCallListResponse,
    RunStatsResponse,
    RunSummary,
    CHAIN_INTEGRITY_LABELS,
    RunTimeseriesPoint,
    RunTimeseriesResponse,
    TERMINAL_STATE_CLASSES,
    TERMINAL_STATE_LABELS,
    STEP_KIND_LABELS,
    describeRunTiming,
    } from '../../lib/aiSystemTypes';
import { errorKind } from '../../lib/narrow';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface AISystemRunsTabProps {
    /** Open on a specific tab (deep links: Mission Control's findings
     *  badge lands directly on 'drift'). */
    initialSection?: Section;
    aiSystemId: string;
    onOpenRun?: (runId: string) => void;
    /** The breadcrumb + system name, rendered by the parent but placed here
     *  so it shares a row with the status verdicts. The strip depends on this
     *  component's `stats`/`stripReload` state, so bringing the identity line
     *  down is far less churn than lifting the strip up. */
    identity?: React.ReactNode;
    /** Lets the contract lifecycle gate link out to the group's
     *  Ownership & Purpose fields when it reports them missing. */
    setActiveTab?: (tab: string) => void;
    /** The run the route applies to the Composition tab's Simple graph. */
    routeRunId?: string | null;
}

/**
 * Windows offered on this surface. 90d is among them, as on every other
 * analytics surface: a quarter is the window drift and cost trends
 * actually show up in, and it is the one an auditor asks for.
 *
 * Values are the shared `Timespan` labels rather than raw hour counts so
 * the selection can be shared with Mission Control, Analytics and
 * Resource Groups; the request still goes out in hours via
 * TIMESPAN_HOURS.
 */
const PERIODS: { value: Exclude<Timespan, 'mtd' | 'lastm'>; label: string }[] = [
    { value: '24h', label: '24h' },
    { value: '7d', label: '7d' },
    { value: '30d', label: '30d' },
    { value: '90d', label: '90d' },
    { value: 'all', label: 'All time' },
];

const PERIOD_VALUES = PERIODS.map((p) => p.value);

type Section = 'composition' | 'lifecycle' | 'signals' | 'runs' | 'trajectory' | 'drift' | 'contract' | 'compliance' | 'assurance';

/** The Signals tab's sub-tabs. Overview first and by default: the overall
 *  verdict is worst-of, and the operator should see which signal it rests
 *  on before drilling into any one family. Then liveness: it is the one
 *  card an operator configures, and the one that fires on silence. */
type SignalSub = 'overview' | 'liveness' | 'cost' | 'trajectory' | 'errors' | 'outcomes' | 'incidents';
const SIGNAL_SUBS: { id: SignalSub; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'liveness', label: 'Liveness' },
    { id: 'cost', label: 'Cost' },
    // Id kept for links; the screen is about how long runs are, and
    // "trajectory" names the per-run path on the Actions tab.
    { id: 'trajectory', label: 'Run length' },
    { id: 'errors', label: 'Errors' },
    { id: 'outcomes', label: 'Outcomes' },
    // The periods every signal above does not judge.
    { id: 'incidents', label: 'Incident windows' },
];

/**
 * The run list's columns: default width, and what the column actually means.
 *
 * The help text is here rather than inline because these are the columns an
 * operator misreads. `abandoned` is not a failure, `blocked by policy` is
 * governance working, a `~` on a turn count is an inferred number and not a
 * reported one, and a chain that is not intact qualifies every figure on the
 * row. A column that needs a paragraph to read correctly should carry it.
 */
const RUN_COLUMNS = {
    run: { label: 'Run', width: 240 },
    started: { label: 'Started', width: 190 },
    finished: { label: 'Finished', width: 190 },
    actor: {
        label: 'Actor',
        width: 112,
        help: {
            title: 'Actor',
            text:
                'Who made this run. "agent" means an agent identity — a principal with ' +
                'its own credentials and grants — and the id is on the chip. Anything ' +
                'else (typically end_user) means the call authenticated as a person or ' +
                'an unbound key; on an autonomous system that usually means the agent ' +
                'is not using its own identity, so its actions cannot be attributed to it.',
        },
    },
    release: {
        label: 'Release',
        width: 176,
        help: {
            title: 'Release',
            text:
                'Which release of the agent\'s software made this run — name@version, ' +
                'from its first action that declared one — and how far to trust it: ' +
                '"verified" came from claims in a verified workload token, "declared" ' +
                'is what the agent reported about itself. It never decides access; the ' +
                'credential does. Below it, the client: the channel the run came ' +
                'through (X-Brutor-Client, else the User-Agent product name without ' +
                'its version).',
        },
    },
    state: {
        label: 'Execution',
        width: 150,
        help: {
            title: 'Execution',
            text:
                'How the run ended as an execution, as the gateway saw it: completed, ' +
                'completed (degraded), errored, exhausted, cancelled or abandoned. A ' +
                'run the gateway refused (a guardrail, a policy, an approval denied) is ' +
                'errored with the cause "blocked by policy". Abandoned means the idle ' +
                'window elapsed — usually a client that stopped calling rather than a fault.',
        },
    },
    runClass: {
        label: 'Run class',
        width: 120,
        help: {
            title: 'Run class',
            text:
                'The three answers combined: Success — completed, in a declared outcome ' +
                'that counts toward the baseline. No work — completed, in a declared ' +
                'outcome that does not count (nothing to do); grey, never a success. ' +
                'Deviation — completed with an undeclared or no outcome. Failed — errored ' +
                'or exhausted, whatever it was doing. Not decisive — abandoned or cancelled.',
        },
    },
    outcome: {
        label: 'Outcome',
        width: 230,
        help: {
            title: 'Outcome',
            text:
                'The outcome the agent reported when it closed the run ' +
                '(<outcome> or <outcome>.<variant>), checked by the gateway against the ' +
                'declared outcomes of the contract that was active: Declared, Undeclared ' +
                '(not in the contract, or malformed) or Not reported. "no work" marks a ' +
                'declared outcome that does not count toward the baseline.',
        },
    },
    errorCause: {
        label: 'Error cause',
        width: 170,
        help: {
            title: 'Error cause',
            text:
                'Why an errored or exhausted run failed, from its own calls: model ' +
                'unavailable, model rejected the request, rate limited, blocked by ' +
                'policy, tool or skill failed, delegate agent unreachable or answering ' +
                'outside its contract, iteration limit reached, or the client reporting ' +
                'an error. Empty for runs that did not fail.',
        },
    },
    turns: {
        label: 'Turns',
        width: 92,
        help: {
            title: 'Turns',
            text:
                'Passes of the agent loop. A run contains steps, a step contains turns, ' +
                'and a turn contains the individual governed calls counted under ' +
                'Actions. A "~" marks a count inferred from model-call boundaries ' +
                'because this client did not assert turn ids (X-Brutor-Turn-Id) — an ' +
                'estimate, never presented as a reported figure.',
        },
    },
    actions: {
        label: 'Actions',
        width: 108,
        help: {
            title: 'Actions',
            text:
                'The leaf unit: one governed call. Every model call, tool call, skill ' +
                'execution and agent-to-agent request the run made, including those made ' +
                'by peer agents it delegated to — so a run that fans out to other agents ' +
                'counts their work here too. This is the count that grows when a system ' +
                'starts working harder for the same result, which is usually the first ' +
                'visible sign of behavioural drift.',
        },
    },
    tools: {
        label: 'Tools',
        width: 108,
        help: {
            title: 'Tools',
            text:
                'Tool calls made during the run — MCP tools, skills and agent-to-agent ' +
                'requests. A red "(n err)" is how many of those calls failed. Rising ' +
                'tool errors are usually the first visible sign of a degrading ' +
                'dependency: the system keeps working, it just retries more.',
        },
    },
    cost: {
        label: 'Cost',
        width: 112,
        help: {
            title: 'Cost',
            text:
                'Total spend attributed to this run in USD, across every model call it ' +
                'made. This is the raw per-run figure and includes runs that produced ' +
                'nothing. The Cost signal divides only by processed outcomes instead, so ' +
                'spend on failed runs is reported rather than averaged away and empty ' +
                'polls are counted as overhead rather than as cheap work.',
        },
    },
    closed_by: {
        label: 'Closed by',
        width: 124,
        help: {
            title: 'Closed by',
            text:
                'What ended the run. "explicit" — the client reported it finished, the ' +
                'only close that carries the client\'s own terminal state. "terminal" — ' +
                'a terminal action ended it. "idle" — the idle window elapsed with no ' +
                'further calls. "sweeper" — a background pass closed a run the client ' +
                'never came back for. Anything but explicit means the ending was ' +
                'inferred by the gateway.',
        },
    },
    chain: {
        label: 'Chain',
        width: 116,
        help: {
            title: 'Chain integrity',
            text:
                'Whether the run\'s evidence chain holds: every action linked to the ' +
                'run it belongs to and attributable. "intact" — complete and gateway-signed. ' +
                '"client_asserted" — complete, linked on the client\'s word. "external" — ' +
                'complete as reported by an OpenTelemetry exporter: the gateway did not ' +
                'witness these steps and applied no control to them. "partial" — a ' +
                'client dropped the chain, so some actions could not be joined. ' +
                '"broken" — a signature failed to verify. Anything other than intact ' +
                'qualifies every other figure on this row: they cover what the gateway ' +
                'could join, which is less than what happened.',
        },
    },
} as const satisfies Record<string, { label: string; width: number; help?: { title: string; text: string } }>;

type RunColumn = keyof typeof RUN_COLUMNS;

const RUN_COLUMN_ORDER = Object.keys(RUN_COLUMNS) as RunColumn[];

const RUN_COLUMN_DEFAULTS = Object.fromEntries(
    RUN_COLUMN_ORDER.map((k) => [k, RUN_COLUMNS[k].width]),
) as Record<RunColumn, number>;

const RUN_COLUMN_WIDTH_KEY = 'brutor.aiSystem.runs.columnWidths';
/** The "?" on each Cost tile. Mirrors `run_signals` in the control plane:
 *  the population is the runs that started in the selected range, replays
 *  excluded; a "processed outcome" is a `success` run (completed, in a
 *  declared outcome that counts toward the baseline); percentiles are
 *  nearest-rank, so each is the cost of a real run. */
const COST_TILE_HELP = {
    perProcessed:
        'The average cost of a processed outcome: what the runs that completed in a declared outcome counting toward the baseline cost in this range, ' +
        'divided by how many there were. Runs that ended in a no-work outcome (an empty poll) are not averaged in — they are the No-work overhead card. ' +
        'Failed and deviating runs are not averaged in either; they are "Spent, nothing produced". Replays are left out. The table below splits it per outcome and variant.',
    median:
        'The cost of the middle processed outcome: half of the processed runs in this range cost less, half cost more. ' +
        'Unlike the average it is not pulled up by a few expensive runs, so an average well above the median means a few costly runs.',
    p95:
        '95% of processed outcomes in this range cost this much or less; about 1 in 20 cost more. It is the cost of an actual run ' +
        '(nearest rank, not interpolated). A p95 rising while the median stays flat means the expensive tail is growing.',
    wasted:
        'The total spent in this range on runs that produced nothing: every failed run (errored or exhausted — a run the gateway blocked by policy included) ' +
        'and every deviating run (completed, but with an undeclared or no outcome). No-work runs are not counted here: they ended as designed and are reported as overhead. ' +
        'This is a sum, not a per-run figure.',
};

/** The "?" on each Run length tile. Mirrors `run_signals`: an action is
 *  one governed call (`AISystemRun.action_count`); the length figures are
 *  over processed outcomes (`success` runs), exhaustion over every finished
 *  run; replays excluded. */
const ACTION_TILE_HELP = {
    median:
        'An action is one governed call through the gateway: a model call, a tool (MCP) call, a skill or a call to another agent. ' +
        'This is the action count of the middle processed run in this range — runs that completed in a declared outcome counting toward the baseline. ' +
        'Empty polls and failed runs are left out, so a one-action poll never reads as a short task; replays are left out. The table below splits it per outcome and variant.',
    p95:
        '95% of the processed runs in this range took this many actions or fewer; about 1 in 20 took more. It is the count of an actual run ' +
        '(nearest rank, not interpolated). A p95 pulling away from the median means some runs are looping or retrying far more than usual.',
    max:
        'The most actions any single processed run in this range took. One runaway run shows here first; open it from the Runs tab to see its steps.',
    exhausted:
        'The share of every finished run in this range that hit its iteration limit (the agent framework\'s max-steps cap) and stopped without an answer. ' +
        'Only the client knows its own limit, so this counts runs the client closed as exhausted when it reported the end of the run.',
    exhaustedUnknown:
        'The share of runs that hit their iteration limit. Only the client knows its own limit, and no run in this range was closed by the client ' +
        'reporting its end, so the rate is unknown rather than 0%. Send a run-end event from the client to measure it.',
};

/** Runs per page of the run list (the endpoint allows up to 500). */
const RUNS_PAGE_SIZE = 50;

/** Tab order is the order an operator asks the questions in: what is it made
 *  of and what does it depend on, where is it in
 *  its life, what did it do, show me the runs, has it changed, what binds it,
 *  what it owes under which framework (the declared profile and
 *  the obligations shown against sealed records), and what proves it. */
const SECTIONS: { id: Section; label: string }[] = [
    { id: 'composition', label: 'Composition' },
    { id: 'lifecycle', label: 'Lifecycle' },
    { id: 'signals', label: 'Signals' },
    { id: 'runs', label: 'Runs' },
    // Id kept for links. It finds actions across runs and inspects one
    // run's trajectory, so it is named for the thing it lists.
    { id: 'trajectory', label: 'Actions' },
    { id: 'drift', label: 'Drift' },
    { id: 'contract', label: 'Contract' },
    { id: 'compliance', label: 'Compliance' },
    { id: 'assurance', label: 'Assurance' },
];

export function AISystemRunsTab({ aiSystemId, onOpenRun, initialSection, identity, setActiveTab, routeRunId }: AISystemRunsTabProps) {
    const {
        fetchAISystemRuns,
        fetchAISystemServedCalls,
        fetchAISystemRunStats,
        fetchAISystemRunTimeseries,
        fetchAISystemIncidentWindows,
        fetchAISystemOutcomes,
        fetchRun,
        abortRun,
    } = useGateway();

    // Shared with Mission Control / Analytics / Resource Groups; '24h'
    // stays this surface's fallback so nothing changes until the admin
    // picks a window somewhere.
    const [period, periodHours, setPeriod] = useTimespanHoursPreference(PERIOD_VALUES, '24h');
    // Composition first: before "where is it in its life" comes
    // "what is it" — its agents, the gateway governing it, what it reaches and
    // what it depends on. Lifecycle is second.
    const [section, setSection] = useState<Section>(initialSection ?? 'composition');
    // The run overlay (Composition → Simple graph). Routed: the route holds
    // the run, so it is deep-linkable and survives section switches; without
    // a router (embedded, tests) the tab keeps it.
    const [localRun, setLocalRun] = useState<string | null>(routeRunId ?? null);
    const overlayRun = setActiveTab ? (routeRunId ?? null) : localRun;
    const showOnGraph = useCallback(
        (runId: string | null) => {
            setSection('composition');
            if (setActiveTab) setActiveTab(runOverlayRoute(aiSystemId, runId));
            else setLocalRun(runId);
        },
        [aiSystemId, setActiveTab],
    );
    // Which Contract sub-tab to land on when another tab hands off there.
    const [contractSub, setContractSub] = useState<ContractSubTab | undefined>(undefined);
    const [signalSub, setSignalSub] = useState<SignalSub>('overview');
    const [stateFilter, setStateFilter] = useState('');
    const [classFilter, setClassFilter] = useState<RunClass | ''>('');
    const [stats, setStats] = useState<RunStatsResponse | null>(null);
    const [series, setSeries] = useState<RunTimeseriesResponse | null>(null);
    const [runs, setRuns] = useState<RunListResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    // Bumped when a tab mutates something a strip chip reports on (drift
    // triage) so the pinned verdicts follow the action instead of going
    // stale until the next full page load.
    const [stripReload, setStripReload] = useState(0);

    // Stop one run. Refresh the strip afterwards so the
    // operator sees the intervention land rather than an unchanged screen,
    // which would leave them unsure whether the click took.
    const handleAbort = useCallback(async (rootTaskId: string, reason: string) => {
        await abortRun(rootTaskId, reason);
        setStripReload((k) => k + 1);
    }, [abortRun]);
    const [showGuide, setShowGuide] = useState(false);
    const [evidenceRev, setEvidenceRev] = useState(0);
    const [readFailure, reads] = useReadFailures<'trend' | 'served' | 'outcomes' | 'windows'>();
    const [outcomesAsked, setOutcomesAsked] = useState(0);
    const columns = useColumnWidths<RunColumn>(RUN_COLUMN_WIDTH_KEY, RUN_COLUMN_DEFAULTS);

    // The page of the run list, keyed by the query it pages through: a new
    // system, range or outcome filter starts again at the first page.
    const runsQuery = `${aiSystemId}|${periodHours}|${stateFilter}|${classFilter}`;
    const [runsPage, setRunsPage] = useState({ query: runsQuery, offset: 0 });
    const runOffset = runsPage.query === runsQuery ? runsPage.offset : 0;
    const setRunOffset = (offset: number) => setRunsPage({ query: runsQuery, offset });

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const [s, ts] = await Promise.all([
                fetchAISystemRunStats(aiSystemId, periodHours),
                // A chart that fails to load must not take down the tiles
                // it illustrates: the charts say they could not be loaded.
                fetchAISystemRunTimeseries(aiSystemId, periodHours).then(
                    (answer) => {
                        reads.loaded('trend');
                        return answer;
                    },
                    (err: unknown) => {
                        reads.lost('trend', 'Failed to load the trend charts', err);
                        return null;
                    },
                ),
            ]);
            setStats(s);
            setSeries(ts);
        } catch (err: unknown) {
            console.error('Failed to load runs:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load runs');
            setStats(null);
            setSeries(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchAISystemRunStats, fetchAISystemRunTimeseries, periodHours, reads]);

    useEffect(() => {
        void load();
    }, [load]);

    // The run list pages on its own, so turning a page does not refetch
    // the tiles and charts above it. A response for a page the operator
    // has already left is dropped, so a slow page cannot overwrite a newer one.
    const [runsRev, setRunsRev] = useState(0);
    useEffect(() => {
        let cancelled = false;
        fetchAISystemRuns(aiSystemId, {
            period_hours: periodHours,
            terminal_state: stateFilter || undefined,
            run_class: classFilter || undefined,
            limit: RUNS_PAGE_SIZE,
            offset: runOffset,
        }).then(
            (r) => { if (!cancelled) setRuns(r); },
            (err: unknown) => {
                console.error('Failed to load runs:', errorKind(err));
                if (cancelled) return;
                setError(err instanceof Error ? err.message : 'Failed to load runs');
                setRuns(null);
            },
        );
        return () => { cancelled = true; };
    }, [aiSystemId, fetchAISystemRuns, periodHours, stateFilter, classFilter, runOffset, runsRev]);

    // A shared service roots no runs; its work is the calls
    // it served inside other systems' runs. Fetched beside the run list over
    // the same window and paged on its own. Best-effort: a failure here must
    // not hide the runs above it.
    const servedQuery = `${aiSystemId}|${periodHours}`;
    const [servedPage, setServedPage] = useState({ query: servedQuery, offset: 0 });
    const servedOffset = servedPage.query === servedQuery ? servedPage.offset : 0;
    const [served, setServed] = useState<ServedCallListResponse | null>(null);
    useEffect(() => {
        let cancelled = false;
        Promise.resolve()
            .then(() => fetchAISystemServedCalls(aiSystemId, {
                period_hours: periodHours,
                limit: RUNS_PAGE_SIZE,
                offset: servedOffset,
            }))
            .then(
                (r) => {
                    if (cancelled) return;
                    setServed(r ?? null);
                    reads.loaded('served');
                },
                (err: unknown) => {
                    if (cancelled) return;
                    setServed(null);
                    reads.lost('served', 'Failed to load the calls this system served', err);
                },
            );
        return () => { cancelled = true; };
    }, [aiSystemId, fetchAISystemServedCalls, periodHours, servedOffset, runsRev, reads]);
    const servedTotal = served?.total ?? 0;

    // The declared outcomes, for labels on the Signals tables: the active
    // contract's catalogue (what runs are judged against), else the live
    // one. Best-effort — the ids alone still read.
    const [catalogue, setCatalogue] = useState<OutcomeDecl[] | null>(null);
    useEffect(() => {
        let cancelled = false;
        Promise.resolve()
            .then(() => fetchAISystemOutcomes(aiSystemId))
            .then(
                (res) => {
                    if (cancelled) return;
                    setCatalogue(res?.active_outcomes ?? res?.outcomes ?? null);
                    reads.loaded('outcomes');
                },
                (err: unknown) => {
                    if (cancelled) return;
                    setCatalogue(null);
                    reads.lost('outcomes', 'Failed to load the declared outcomes; outcomes are shown by id', err);
                },
            );
        return () => {
            cancelled = true;
        };
    }, [aiSystemId, fetchAISystemOutcomes, outcomesAsked, reads]);

    // Declared incident windows, shaded on every trend chart.
    // Best-effort: a chart without its shading still draws the data.
    const [incidentWindows, setIncidentWindows] = useState<IncidentWindow[]>([]);
    // Bumped when a window is declared, edited or withdrawn on this page.
    const [windowsRev, setWindowsRev] = useState(0);
    const loadWindows = () => setWindowsRev((n) => n + 1);

    useEffect(() => {
        let cancelled = false;
        fetchAISystemIncidentWindows(aiSystemId).then(
            (res) => {
                if (cancelled) return;
                setIncidentWindows(res.windows);
                reads.loaded('windows');
            },
            (err: unknown) => {
                if (cancelled) return;
                setIncidentWindows([]);
                reads.lost('windows', 'Failed to load the incident windows; the charts do not shade them', err);
            },
        );
        return () => {
            cancelled = true;
        };
    }, [aiSystemId, fetchAISystemIncidentWindows, windowsRev, reads]);

    const chartPoints: RunTimeseriesPoint[] = series ? zeroFillTimeseries(series) : [];
    const chartBucket = series?.bucket ?? 'day';
    const errs = stats ? stepErrorSummary(stats) : null;

    return (
        <div className="space-y-6" data-testid="ai-system-runs">
            {/* One header row: identity and verdicts on the left, the controls
                that scope and refresh them on the right. The name and the
                health of the thing named are one fact, so they read as one
                line — and the period/actions belong beside what they act on,
                not on a row of their own above the tabs. Wraps on narrow
                viewports rather than overflowing. */}
            <div className="flex flex-wrap items-center gap-3">
                {identity}
                <StatusStrip
                    aiSystemId={aiSystemId}
                    stats={stats}
                    onOpenTab={(t) => setSection(t)}
                    reloadKey={stripReload}
                />
                <div className="flex flex-wrap items-center gap-2 ml-auto">
                    <div className="flex items-center gap-1" data-testid="period-pills">
                        {PERIODS.map((p) => (
                            <button
                                key={p.value}
                                type="button"
                                onClick={() => setPeriod(p.value)}
                                className={`px-2.5 py-1 text-xs rounded-lg border ${
                                    period === p.value
                                        ? 'border-primary-600 bg-primary-50 text-primary-900'
                                        : 'border-default hover:border-strong'
                                }`}
                            >
                                {p.label}
                            </button>
                        ))}
                    </div>
                    {/* py-1.5 matches the status chips so the row has one
                        consistent height instead of the buttons setting it. */}
                    <button
                        type="button"
                        onClick={() => setShowGuide(true)}
                        data-testid="lifecycle-guide-button"
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                    >
                        <BookOpen className="w-4 h-4" />
                        Lifecycle Guide
                    </button>
                    <button
                        type="button"
                        onClick={() => { void load(); setRunsRev((n) => n + 1); }}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                </div>
            </div>

            {error && (
                <div className="flex items-start gap-3 p-4 border border-red-200 bg-red-50 rounded-lg">
                    <AlertTriangle className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}
            <ReadFailureNotice failure={readFailure('trend')} onRetry={() => void load()} />
            <ReadFailureNotice failure={readFailure('served')} onRetry={() => setRunsRev((n) => n + 1)} />
            <ReadFailureNotice failure={readFailure('windows')} onRetry={loadWindows} />
            <ReadFailureNotice failure={readFailure('outcomes')} onRetry={() => setOutcomesAsked((n) => n + 1)} />

            {showGuide && <LifecycleGuideModal onClose={() => setShowGuide(false)} />}

            {/* Coverage disclosure — only when coverage is genuinely below
                100%, and in coverage terms rather than provenance terms. A
                client-asserted run is a fully observed run; describing it as
                "not intact" would tell operators a third of their traffic is
                missing when all of it is on the ledger. */}
            {stats &&
                !error &&
                stats.observed_pct !== null &&
                stats.observed_pct !== undefined &&
                stats.observed_pct < 1 && (
                    <div
                        className="flex items-start gap-3 p-3 border border-amber-200 bg-amber-50 rounded-lg"
                        data-testid="chain-integrity-warning"
                    >
                        <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                        <span className="text-sm text-amber-900">
                            Only {pct(stats.observed_pct)} of runs in this window were observed
                            completely. The figures on every tab cover what the gateway saw,
                            which is less than everything that happened.
                        </span>
                    </div>
                )}

            {/* ── Tab strip ───────────────────────────────────────────────── */}
            <div className="border-b border-default" data-testid="runs-tabstrip">
                <nav className="flex gap-1 -mb-px" role="tablist" aria-label="AI System detail">
                    {SECTIONS.map((s) => (
                        <button
                            key={s.id}
                            type="button"
                            role="tab"
                            aria-selected={section === s.id}
                            data-testid={`runs-tab-${s.id}`}
                            onClick={() => setSection(s.id)}
                            className={`px-4 py-2 text-sm border-b-2 ${
                                section === s.id
                                    ? 'border-primary-600 text-primary-900 font-medium'
                                    : 'border-transparent text-secondary hover:text-default'
                            }`}
                        >
                            {s.label}
                        </button>
                    ))}
                </nav>
            </div>

            {section === 'signals' && (
                <div className="border-b border-default" data-testid="signals-subtabs">
                    <nav className="flex gap-1 -mb-px" role="tablist" aria-label="Signals">
                        {SIGNAL_SUBS.map((t) => (
                            <button
                                key={t.id}
                                type="button"
                                role="tab"
                                aria-selected={signalSub === t.id}
                                onClick={() => setSignalSub(t.id)}
                                data-testid={`signals-subtab-${t.id}`}
                                className={`px-3 py-1.5 text-sm border-b-2 ${
                                    signalSub === t.id
                                        ? 'border-primary-600 text-primary-900 font-medium'
                                        : 'border-transparent text-secondary hover:text-default'
                                }`}
                            >
                                {t.label}
                            </button>
                        ))}
                    </nav>
                </div>
            )}

            {/* Which window each sub-tab is over: without this the health
                verdict and the range pills read as one thing. */}
            {section === 'signals' && signalSub !== 'incidents' && (
                <p className="text-xs text-muted" data-testid="signals-window-note">
                    {signalSub === 'overview' || signalSub === 'liveness'
                        ? 'The health verdict has its own fixed window (see "How health is calculated") — the range buttons above do not change it.'
                        : `Range: ${PERIODS.find((p) => p.value === period)?.label ?? period}, from the range buttons above. The health verdict on Overview uses its own fixed window.`}
                </p>
            )}

            {section === 'signals' && signalSub === 'overview' && (
                <SignalsOverview
                    aiSystemId={aiSystemId}
                    onOpenSignal={(sub) => setSignalSub(sub as SignalSub)}
                    setActiveTab={setActiveTab}
                />
            )}

            {section === 'signals' && signalSub === 'incidents' && (
                <IncidentWindowsPanel aiSystemId={aiSystemId} onChanged={loadWindows} />
            )}

            {section === 'signals' && signalSub === 'liveness' && (
                /* Liveness config lives with the signals it qualifies; its
                   verdict stays pinned in the strip above. */
                <LivenessCard aiSystemId={aiSystemId} />
            )}

            {section === 'signals' && stats && !error && (
                <>
                    {/* Signal 1 — cost per processed outcome */}
                    {signalSub === 'cost' && (
                    <section data-testid="signal-cost">
                        <SignalHeading
                            title="Cost per processed outcome"
                            hint="Processed outcomes are the denominator: runs that completed in a declared outcome that counts toward the baseline, split per outcome and variant below. Spend on failed and deviating runs is reported separately rather than averaged away, and no-work runs (an empty poll) are overhead with their own card."
                        />
                        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                            <Tile
                                label="Per processed outcome"
                                value={money(stats.cost.cost_per_processed_outcome)}
                                hint={`${stats.cost.processed_runs} processed`}
                                help={COST_TILE_HELP.perProcessed}
                                testId="cost-per-processed"
                            />
                            <Tile
                                label="Median"
                                value={money(stats.cost.median_cost_per_processed_outcome)}
                                help={COST_TILE_HELP.median}
                            />
                            <Tile
                                label="p95"
                                value={money(stats.cost.p95_cost_per_processed_outcome)}
                                help={COST_TILE_HELP.p95}
                            />
                            <Tile
                                label="Spent, nothing produced"
                                value={money(stats.cost.wasted_cost_usd)}
                                tone={stats.cost.wasted_cost_usd > 0 ? 'warn' : undefined}
                                hint="Failed and deviating runs"
                                help={COST_TILE_HELP.wasted}
                                testId="cost-wasted"
                            />
                            <NoWorkOverheadTile cost={stats.cost} />
                        </div>
                        <CostByOutcomeTable cost={stats.cost} catalogue={catalogue} />
                        {series && (
                            <SignalTrendChart kind="cost" data={chartPoints} bucket={chartBucket} incidentWindows={incidentWindows} />
                        )}
                    </section>
                    )}

                    {/* Signal 2 — actions + exhaustion */}
                    {signalSub === 'trajectory' && (
                    <section data-testid="signal-actions">
                        <SignalHeading
                            title="Actions per processed outcome"
                            hint="A rising action count is the earliest visible sign that a system is working harder for the same result. The length figures are over processed outcomes only, per outcome and variant — a one-action empty poll is not a short task. Exhaustion is over every finished run."
                        />
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                            <Tile
                                label="Median actions"
                                value={num(stats.actions.median_actions)}
                                hint={`${stats.actions.runs} processed`}
                                help={ACTION_TILE_HELP.median}
                                testId="actions-median"
                            />
                            <Tile label="p95 actions" value={num(stats.actions.p95_actions)} help={ACTION_TILE_HELP.p95} />
                            <Tile label="Max actions" value={num(stats.actions.max_actions)} help={ACTION_TILE_HELP.max} />
                            {stats.actions.exhaustion_reporting_available ? (
                                <Tile
                                    label="Exhausted"
                                    value={pct(stats.actions.exhaustion_rate)}
                                    tone={
                                        (stats.actions.exhaustion_rate ?? 0) > 0 ? 'warn' : undefined
                                    }
                                    hint={`${stats.actions.exhausted_runs} of ${stats.actions.finished_runs} finished runs`}
                                    help={ACTION_TILE_HELP.exhausted}
                                />
                            ) : (
                                /* Not a zero. The proxy cannot see a framework's
                                   max-iteration cap, so with no client reporting
                                   this is genuinely unknown, and rendering 0%
                                   would read as "healthy". */
                                <Tile
                                    label="Exhausted"
                                    value="Not reported"
                                    tone="unknown"
                                    hint="Needs client run-end reporting"
                                    help={ACTION_TILE_HELP.exhaustedUnknown}
                                    testId="exhaustion-unknown"
                                />
                            )}
                        </div>
                        <ActionsByOutcomeTable actions={stats.actions} catalogue={catalogue} />
                        {series && (
                            <SignalTrendChart kind="actions" data={chartPoints} bucket={chartBucket} incidentWindows={incidentWindows} />
                        )}
                        <ActionHistogram stats={stats} />
                        {stats.context && <ContextSizeSection context={stats.context} />}
                        {stats.actions.abandoned_runs > 0 && (
                            <p className="text-xs text-muted mt-2">
                                {stats.actions.abandoned_runs} run(s) closed by idle timeout —
                                usually a client that stopped calling rather than a fault in
                                the system itself.
                            </p>
                        )}
                    </section>
                    )}

                    {/* Signal 3 — errors, by kind of call */}
                    {signalSub === 'errors' && errs && (
                    <section data-testid="signal-errors">
                        <SignalHeading
                            title={errs.allKinds ? 'Errors by call type' : 'Tool errors'}
                            hint="Every failed call inside the runs in this range — model, tool, skill and agent calls — and, beside them, the failed runs by error cause. A model provider outage shows here as model errors (and runs failing with 'model unavailable') even while every tool call succeeds. A call held for human approval is not an error."
                        />
                        <ErrorCausesSection signal={stats.error_causes} />
                        <div className="grid grid-cols-2 md:grid-cols-3 gap-4 mb-3">
                            <Tile label={errs.allKinds ? 'Calls' : 'Tool calls'} value={num(errs.totalCalls)} testId="errors-total-calls" />
                            <Tile label="Errors" value={num(errs.totalErrors)} tone={errs.totalErrors > 0 ? 'warn' : undefined} testId="errors-total-errors" />
                            <Tile
                                label="Error rate"
                                value={pct(errs.errorRate)}
                                tone={(errs.errorRate ?? 0) > 0.05 ? 'warn' : undefined}
                                testId="errors-rate"
                            />
                        </div>
                        {errs.rows.length > 0 && (
                            <div className="bg-surface border border-default rounded-lg overflow-hidden mb-3">
                                <table className="min-w-full divide-y divide-default text-sm" data-testid="errors-by-kind">
                                    <thead className={TABLE_THEAD}>
                                        <tr>
                                            <Th>Call type</Th>
                                            <Th>Calls</Th>
                                            <Th>Errors</Th>
                                            <Th>Error rate</Th>
                                            <Th>Status codes</Th>
                                        </tr>
                                    </thead>
                                    <tbody className={TABLE_TBODY}>
                                        {errs.rows.map((row) => (
                                            <tr key={row.kind} data-testid={`errors-kind-${row.kind}`}>
                                                <td className="px-4 py-2 text-default">{STEP_KIND_LABELS[row.kind] ?? row.kind}</td>
                                                <td className="px-4 py-2 text-secondary">{row.calls}</td>
                                                <td className={`px-4 py-2 ${row.errors > 0 ? 'text-red-700 dark:text-red-300 font-medium' : 'text-secondary'}`}>
                                                    {row.errors}
                                                </td>
                                                <td className="px-4 py-2 text-secondary">{pct(row.error_rate)}</td>
                                                <td className="px-4 py-2 text-secondary">{statusCodeText(row.status_codes)}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                        {series && (
                            <SignalTrendChart kind="errors" data={chartPoints} bucket={chartBucket} incidentWindows={incidentWindows} />
                        )}
                        {stats.tool_errors.by_tool.length > 0 && (
                            <div className="bg-surface border border-default rounded-lg overflow-hidden mt-3">
                                <table className="min-w-full divide-y divide-default text-sm">
                                    <thead className={TABLE_THEAD}>
                                        <tr>
                                            <Th>Tool error type</Th>
                                            <Th>Count</Th>
                                            <Th>Share of tool calls</Th>
                                        </tr>
                                    </thead>
                                    <tbody className={TABLE_TBODY}>
                                        {stats.tool_errors.by_tool.map((row) => (
                                            <tr key={row.tool}>
                                                <td className="px-4 py-2 text-default">{row.tool}</td>
                                                <td className="px-4 py-2 text-secondary">{row.errors}</td>
                                                <td className="px-4 py-2 text-secondary">
                                                    {pct(row.error_rate)}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>
                    )}

                    {/* Signal 4 — how runs ended: run classes, declared outcomes, execution */}
                    {signalSub === 'outcomes' && (
                        <OutcomesSignalSection
                            outcomes={stats.outcomes}
                            terminalStates={stats.terminal_states}
                            catalogue={catalogue}
                            chart={
                                series && (
                                    <SignalTrendChart kind="outcomes" data={chartPoints} bucket={chartBucket} incidentWindows={incidentWindows} />
                                )
                            }
                        />
                    )}
                </>
            )}

            {/* Configuration rather than observation: what this system has
                committed to, how it is allowed to respond, and the assurance
                evidence behind it. Grouped away from the descriptive signals
                because an operator reading "what did it do today" and one
                reading "what is it bound by" are on different errands. */}
            {section === 'trajectory' && <TrajectoryExplorer aiSystemId={aiSystemId} />}

            {/* Drift: findings on the left, the automatic response that acts
                on them on the right. A response policy fires on exactly the
                findings triaged here, so deciding "should this be automatic
                next time?" belongs beside the finding that prompts it. */}
            {section === 'drift' && (
                <div className="grid grid-cols-1 lg:grid-cols-5 gap-4 items-start">
                    <div className="lg:col-span-3 min-w-0">
                        <DriftPanel
                            aiSystemId={aiSystemId}
                            onChanged={() => setStripReload((n) => n + 1)}
                            setActiveTab={setActiveTab}
                        />
                    </div>
                    <div className="lg:col-span-2 min-w-0">
                        <ResponsePolicyPanel aiSystemId={aiSystemId} />
                        {/* The data scope and reach limits
                            whose breaches arrive as findings on the left. */}
                        <div className="mt-6">
                            <DataScopePanel aiSystemId={aiSystemId} />
                        </div>
                    </div>
                </div>
            )}

            {/* Contract: what binds the system on the left, the replay
                evidence that gates changing it on the right. Replay tests a
                candidate change against recorded runs before it is accepted,
                which is a step in the promotion decision — not a separate
                errand on another tab. */}
            {section === 'composition' && (
                <CompositionTab
                    aiSystemId={aiSystemId}
                    windowHours={periodHours}
                    setActiveTab={setActiveTab}
                    runId={overlayRun}
                    onSelectRun={showOnGraph}
                />
            )}

            {section === 'lifecycle' && (
                <div className="grid grid-cols-1 lg:grid-cols-5 gap-4 items-start">
                    <div className="lg:col-span-3 min-w-0">
                        {/* Re-mount when evidence changes: attaching an impact
                            assessment can flip a gate requirement from unmet to
                            satisfied, and a stale checklist would tell the
                            operator "no" wrongly. */}
                        <LifecyclePanel
                            key={evidenceRev}
                            aiSystemId={aiSystemId}
                            setActiveTab={setActiveTab}
                            onOpen={(target) => {
                                setContractSub(target.section === 'contract' ? target.sub : undefined);
                                setSection(target.section);
                            }}
                        />
                        {/* What this system is not judged on, next
                            to where it is in its life: a window explains a
                            gap in the evidence a gate reads. */}
                        <div className="mt-4">
                            <IncidentWindowsPanel aiSystemId={aiSystemId} onChanged={loadWindows} />
                        </div>
                    </div>
                    {/* Replay beside the gate it feeds: a passing replay is what
                        approved → active consumes. */}
                    <div className="lg:col-span-2 min-w-0 lg:sticky lg:top-4">
                        <ReplayPanel aiSystemId={aiSystemId} />
                    </div>
                </div>
            )}

            {section === 'contract' && (
                <ContractsPanel
                    key={evidenceRev}
                    aiSystemId={aiSystemId}
                    initialSubTab={contractSub}
                    onChanged={() => setEvidenceRev((n) => n + 1)}
                />
            )}

            {section === 'compliance' && <AISystemComplianceTab aiSystemId={aiSystemId} setActiveTab={setActiveTab} />}

            {section === 'assurance' && <AssurancePanel aiSystemId={aiSystemId} setActiveTab={setActiveTab} />}

            {/* Run list */}
            {section === 'runs' && (
            <section>
                <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-semibold text-default">Runs</h3>
                    <div className="flex items-center gap-2">
                        {/* Only offered once there is something to undo — a
                            permanent "reset" implies the layout is in a state
                            worth resetting when it is the default. */}
                        {columns.isCustomised && (
                            <button
                                type="button"
                                onClick={columns.resetAll}
                                data-testid="reset-column-widths"
                                className="px-2.5 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover text-secondary"
                            >
                                Reset columns
                            </button>
                        )}
                    <select
                        aria-label="Filter by run class"
                        value={classFilter}
                        onChange={(e) => setClassFilter(e.target.value as RunClass | '')}
                        className="px-3 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                        data-testid="runs-filter-class"
                    >
                        <option value="">All run classes</option>
                        {RUN_CLASSES.map((cls) => (
                            <option key={cls} value={cls}>
                                {RUN_CLASS_LABELS[cls]}
                            </option>
                        ))}
                    </select>
                    <select
                        aria-label="Filter by execution"
                        value={stateFilter}
                        onChange={(e) => setStateFilter(e.target.value)}
                        className="px-3 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                        data-testid="runs-filter-state"
                    >
                        <option value="">All executions</option>
                        {Object.entries(TERMINAL_STATE_LABELS).map(([state, label]) => (
                            <option key={state} value={state}>
                                {label}
                            </option>
                        ))}
                    </select>
                    </div>
                </div>

                {loading && !runs ? (
                    <div className="p-8 text-center text-muted text-sm">Loading runs…</div>
                ) : !runs || runs.runs.length === 0 ? (
                    <EmptyRuns
                        filtered={Boolean(stateFilter || classFilter)}
                        hasError={Boolean(error)}
                        servedCalls={servedTotal}
                    />
                ) : (
                    <div
                        // A new page starts at its top row, not where the last one was scrolled to.
                        key={runOffset}
                        className="overflow-x-auto overflow-y-auto max-h-[70vh] border border-default rounded-lg"
                        data-testid="runs-scroll"
                    >
                        {/* table-fixed is what makes the widths authoritative:
                            under auto layout the browser treats them as hints
                            and content wins, so a drag would do nothing on the
                            columns most worth widening. The trailing unsized
                            column soaks up any slack, so the table still fills
                            its container without the sized columns being
                            stretched proportionally. */}
                        <table className="min-w-full table-fixed divide-y divide-default text-sm">
                            <thead className="bg-surface-alt border-b border-default sticky top-0 z-10" data-testid="runs-thead">
                                <tr>
                                    {/* Column order is the order the questions come
                                        in: which run, when, who, how did it end, how
                                        much work, what did it cost, and how much of
                                        that can be trusted.

                                        The run's own identity leads. Every other
                                        surface that talks about a run — the Actions
                                        lookup, a drift finding, an assurance report
                                        line — names it by id, so the list it comes
                                        from has to show it. Truncated for the column,
                                        copied in full.

                                        Finished sits beside Started because it is what
                                        tells you a run is still open rather than
                                        closed. */}
                                    {RUN_COLUMN_ORDER.map((key) => {
                                        const col = RUN_COLUMNS[key];
                                        return (
                                            <ResizableTh
                                                key={key}
                                                width={columns.widths[key]}
                                                onResize={(px) => columns.setWidth(key, px)}
                                                onReset={() => columns.resetWidth(key)}
                                                help={'help' in col ? col.help : undefined}
                                                testId={`col-resize-${key}`}
                                            >
                                                {col.label}
                                            </ResizableTh>
                                        );
                                    })}
                                    <th className="px-3 py-2 text-right text-xs font-medium text-muted whitespace-nowrap">Details</th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {runs.runs.map((run) => (
                                    <RunRow
                                        key={run.id}
                                        run={run}
                                        onOpen={onOpenRun}
                                        fetchRun={fetchRun}
                                        onAbort={handleAbort}
                                        onShowOnGraph={showOnGraph}
                                    />
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
                {runs && runs.total > RUNS_PAGE_SIZE && (
                    <Pagination
                        offset={runOffset}
                        pageSize={RUNS_PAGE_SIZE}
                        total={runs.total}
                        onChange={setRunOffset}
                        testIdPrefix="runs-page"
                        noun="runs"
                        className="mt-2"
                    />
                )}
                {served && servedTotal > 0 && (
                    <ServedCallsTable
                        data={served}
                        offset={servedOffset}
                        pageSize={RUNS_PAGE_SIZE}
                        onPage={(offset) => setServedPage({ query: servedQuery, offset })}
                        fetchRun={fetchRun}
                    />
                )}
            </section>
            )}
        </div>
    );
}

function RunRow({
    run,
    onOpen,
    fetchRun,
    onAbort,
    onShowOnGraph,
}: {
    run: RunSummary;
    onOpen?: (id: string) => void;
    fetchRun?: (id: string) => Promise<RunDetail>;
    /** Stop this run. */
    onAbort?: (rootTaskId: string, reason: string) => Promise<void>;
    /** Open the Composition tab's Simple graph with this run applied. */
    onShowOnGraph?: (runId: string) => void;
}) {
    // A run is a story, and the row is its one-line summary. Expanding shows
    // the action timeline — what the system actually did, in order — which is
    // the rollup, actions and timeline of one run, and the thing an operator
    // reaches for the moment a row looks wrong.
    const [open, setOpen] = useState(false);
    const [detail, setDetail] = useState<RunDetail | null>(null);
    const [actionsError, setActionsError] = useState('');
    // Feedback lives on the button rather than in a toast: this row is inside
    // a table that renders without a ToastProvider in tests, and a copy is a
    // local act that deserves a local acknowledgement.
    const [copied, setCopied] = useState(false);
    const [showDetails, setShowDetails] = useState(false);
    const timing = describeRunTiming(run);

    const toggle = async () => {
        const next = !open;
        setOpen(next);
        if (next && !detail && fetchRun) {
            try {
                setDetail(await fetchRun(run.id));
            } catch (err: unknown) {
                setActionsError(err instanceof Error ? err.message : 'Failed to load actions');
            }
        }
        if (!next) return;
        onOpen?.(run.id);
    };

    return (
        <>
        {showDetails && (
            <RunDetailsDialog
                run={run}
                fetchRun={fetchRun}
                onAbort={onAbort}
                onShowOnGraph={onShowOnGraph ? () => onShowOnGraph(run.id) : undefined}
                onClose={() => setShowDetails(false)}
            />
        )}
        <tr
            className="hover:bg-surface-hover cursor-pointer"
            data-testid={`run-row-${run.id}`}
            onClick={() => void toggle()}
        >
            <td className="px-4 py-2 truncate">
                {/* Copying must not also expand the row — the click that reaches
                    for the id is not the click that asks for the timeline. */}
                <button
                    type="button"
                    onClick={(e) => {
                        e.stopPropagation();
                        void (async () => {
                            try {
                                await navigator.clipboard?.writeText(run.id);
                                setCopied(true);
                                setTimeout(() => setCopied(false), 1500);
                            } catch {
                                // Clipboard denied or unavailable. The full id is
                                // in the title attribute either way, so there is
                                // nothing to recover from and nothing to shout about.
                            }
                        })();
                    }}
                    title={`Click to copy run ID ${run.id}`}
                    data-testid={`run-id-${run.id}`}
                    className="inline-flex items-center gap-1 max-w-full min-w-0 font-mono text-xs text-secondary hover:text-default hover:bg-surface-alt px-1 py-0.5 rounded transition-colors"
                >
                    {copied ? (
                        <Check className="w-3 h-3 shrink-0 text-green-600" />
                    ) : (
                        <Copy className="w-3 h-3 shrink-0" />
                    )}
                    <span className="truncate">{run.id}</span>
                </button>
            </td>
            <td className="px-4 py-2 truncate">
                <span className="text-left text-default">
                    {run.started_at ? new Date(run.started_at).toLocaleString() : '—'}
                </span>
            </td>
            <td className="px-4 py-2 truncate" data-testid="run-finished">
                {run.ended_at ? (
                    <>
                        <span className="text-left text-default">
                            {new Date(run.ended_at).toLocaleString()}
                        </span>
                        {/* How long it took, with time parked awaiting a
                            person split out: the duration ceiling
                            measures only the active part, so an elapsed
                            figure alone would read a slow approval as a
                            slow system. */}
                        {timing && (
                            <span
                                className="block text-xs text-muted truncate"
                                title={timing}
                                data-testid="run-timing"
                            >
                                {timing}
                            </span>
                        )}
                    </>
                ) : (
                    // Not a missing value — the run has not closed yet. Saying
                    // "—" here would read as "no data" for something that is
                    // simply still in progress.
                    <span className="text-muted italic">still open</span>
                )}
            </td>
            <td className="px-4 py-2 truncate">
                <ActorCell run={run} />
            </td>
            <td className="px-4 py-2 truncate">
                <ReleaseCell run={run} />
            </td>
            <td className="px-4 py-2 truncate">
                <span
                    className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                        TERMINAL_STATE_CLASSES[run.terminal_state] ??
                        'bg-slate-100 text-slate-700'
                    }`}
                >
                    {TERMINAL_STATE_LABELS[run.terminal_state] ??
                        run.terminal_state}
                </span>
            </td>
            <td className="px-4 py-2 truncate">
                <RunClassChip cls={run.run_class} testId={`run-class-${run.id}`} />
            </td>
            <td className="px-4 py-2">
                <RunOutcomeCell run={run} />
            </td>
            <td className="px-4 py-2 truncate">
                <ErrorCauseText cause={run.error_cause} testId={`error-cause-${run.id}`} />
            </td>
            {/* Run > Step > Turn > Action. Turns group the actions;
                the order of magnitude an operator reasons in — "it took 4
                loops" lands before "it made 11 calls". An inferred count is
                marked, never silently upgraded to a reported one.

                The list endpoint does not serve either field today (the
                Control Plane's RunSummary omits them, so Pydantic strips
                them). Render that as unknown — "—" with no attribution
                marker. Printing `undefined` gave a blank cell, and testing
                `!== 'client_asserted'` against a missing value stamped every
                row "inferred", which is a claim about provenance we cannot
                make from a field we never received. Open the run for the
                real numbers; RunDetail carries them. */}
            <td className="px-4 py-2 truncate text-secondary">
                {run.turn_count == null ? (
                    <span className="text-muted" title="Not returned by the runs list — open the run for turn counts.">—</span>
                ) : (
                    <>
                        {run.turn_count}
                        {run.turn_attribution != null && run.turn_attribution !== 'client_asserted' && (
                            <span
                                className="text-muted ml-0.5"
                                title="Inferred from model-call boundaries — this client did not assert turn ids (X-Brutor-Turn-Id)."
                            >~</span>
                        )}
                    </>
                )}
            </td>
            <td className="px-4 py-2 truncate text-secondary">{run.action_count}</td>
            <td className="px-4 py-2 truncate text-secondary">
                {run.tool_call_count}
                {run.tool_error_count > 0 && (
                    <span className="text-red-700"> ({run.tool_error_count} err)</span>
                )}
            </td>
            <td className="px-4 py-2 truncate text-secondary">{money(run.total_cost_usd)}</td>
            <td className="px-4 py-2 truncate text-xs text-muted">{run.finalized_by}</td>
            <td className="px-4 py-2 truncate">
                {run.chain_integrity === 'intact' ? (
                    <span className="text-xs text-muted">intact</span>
                ) : run.chain_integrity === 'observed_external' ? (
                    // Reported, not witnessed. Its own tone: this is not a
                    // defect in the chain, it is a different kind of evidence.
                    <span
                        className="text-xs text-sky-700 font-medium"
                        title={CHAIN_INTEGRITY_LABELS.observed_external}
                        data-testid={`chain-${run.id}`}
                    >
                        external
                    </span>
                ) : (
                    <span
                        className="text-xs text-amber-700 font-medium"
                        data-testid={`chain-${run.id}`}
                    >
                        {run.chain_integrity}
                    </span>
                )}
            </td>
            <td className="px-3 py-2 text-right whitespace-nowrap">
                {onShowOnGraph && (
                    <button
                        type="button"
                        onClick={(e) => {
                            e.stopPropagation();
                            onShowOnGraph(run.id);
                        }}
                        title="Open the Composition graph with this run's actions numbered on it"
                        data-testid={`run-show-on-graph-${run.id}`}
                        className="inline-flex items-center gap-1 px-2 py-1 mr-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover"
                    >
                        <Workflow className="w-3.5 h-3.5" />
                        Show on graph
                    </button>
                )}
                <button
                    type="button"
                    onClick={(e) => {
                        e.stopPropagation();
                        setShowDetails(true);
                    }}
                    title="Every field the ledger holds for this run — contract version and hash included"
                    data-testid={`run-details-${run.id}`}
                    className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-lg"
                >
                    <Info className="w-3.5 h-3.5" />
                    Details
                </button>
            </td>
        </tr>
        {open && (
            <tr data-testid={`run-actions-${run.id}`}>
                {/* Must match the header count — a shorter span leaves the
                    timeline boxed under part of the row it belongs to. Derived
                    from the column list (plus the Details column), so adding a
                    column cannot shorten it. */}
                <td colSpan={RUN_COLUMN_ORDER.length + 1} className="px-6 py-3 bg-surface-subtle">
                    {actionsError ? (
                        <span className="text-sm text-red-700">{actionsError}</span>
                    ) : !detail ? (
                        <span className="text-sm text-muted">Loading actions…</span>
                    ) : (
                        <>
                            <ModelAttribution models={detail.models_used ?? []} />
                            <ActionTimeline steps={detail.actions ?? []} />
                        </>
                    )}
                </td>
            </tr>
        )}
        </>
    );
}

/**
 * Per-model outcome for one run.
 *
 * The run ledger records calls, errors, blocks, tokens, cost and median
 * latency per model. Without this panel a `model_change` drift finding can
 * only say the model mix moved — an operator then has to open runs by hand to
 * work out which model actually degraded.
 */
function ModelAttribution({
    models,
}: {
    models: RunDetail['models_used'];
}) {
    if (!models || models.length === 0) return null;

    return (
        <div className="mb-4" data-testid="model-attribution">
            <div className="text-xs font-medium text-secondary mb-1.5">
                Models used in this run
            </div>
            <div className="overflow-x-auto">
                <table className="text-xs w-full max-w-3xl">
                    <thead>
                        <tr className="text-muted">
                            <th className="text-left font-medium py-1 pr-4">Model</th>
                            <th className="text-right font-medium py-1 pr-4">Calls</th>
                            <th className="text-right font-medium py-1 pr-4">Errors</th>
                            <th className="text-right font-medium py-1 pr-4">Blocked</th>
                            <th className="text-right font-medium py-1 pr-4">Tokens</th>
                            <th className="text-right font-medium py-1 pr-4">Cost</th>
                            <th className="text-right font-medium py-1">Median</th>
                        </tr>
                    </thead>
                    <tbody>
                        {models.map((m) => (
                            <tr key={m.model} className="border-t border-default">
                                <td className="py-1 pr-4 font-mono text-default">{m.model}</td>
                                <td className="py-1 pr-4 text-right text-secondary">{m.calls}</td>
                                <td
                                    className={`py-1 pr-4 text-right ${
                                        m.errors > 0 ? 'text-red-700 font-medium' : 'text-secondary'
                                    }`}
                                >
                                    {m.errors}
                                </td>
                                <td
                                    className={`py-1 pr-4 text-right ${
                                        m.blocked > 0 ? 'text-amber-700 font-medium' : 'text-secondary'
                                    }`}
                                >
                                    {m.blocked}
                                </td>
                                <td className="py-1 pr-4 text-right text-secondary">{m.tokens.toLocaleString()}</td>
                                <td className="py-1 pr-4 text-right text-secondary">{money(m.cost)}</td>
                                <td className="py-1 text-right text-secondary">
                                    {m.p50_ms != null ? `${Math.round(m.p50_ms)}ms` : '—'}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

const STEP_TYPE_CLASSES: Record<string, string> = {
    llm: 'bg-sky-100 text-sky-800',
    mcp: 'bg-violet-100 text-violet-800',
    'virtual-mcp': 'bg-violet-100 text-violet-800',
    a2a: 'bg-orange-100 text-orange-800',
    skill: 'bg-teal-100 text-teal-800',
};

/** The run's actions, in order. What it called, what that answered, and how the
 *  step joined the chain — the last column matters because a mixed run
 *  (asserted steps + a verified delegated step) is exactly the case a single
 *  rollup value cannot narrate. */
function ActionTimeline({ steps }: { steps: RunAction[] }) {
    if (steps.length === 0) {
        return (
            <span className="text-sm text-muted">
                No actions on record — the action rows may have aged out of retention.
            </span>
        );
    }
    return (
        <ol className="space-y-1" data-testid="action-timeline">
            {/* The numbers are the control plane's (`run_actions`) — the same
                ones the Composition run overlay draws, so the two cannot
                disagree; a delegate's step (5a) is indented under its call. */}
            {runActionDisplayOrder(steps).map((h, i) => (
                <li
                    key={`${h.id ?? h.correlation_id ?? i}`}
                    className="flex items-center gap-3 text-sm"
                    style={{ paddingLeft: (h.nesting ?? 0) * 16 }}
                    data-testid={`action-timeline-${h.number ?? i + 1}`}
                >
                    <span className="w-8 text-right text-xs text-muted tabular-nums">{h.number ?? i + 1}</span>
                    <span
                        className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                            STEP_TYPE_CLASSES[h.proxy_type] ?? 'bg-slate-100 text-slate-700'
                        }`}
                    >
                        {h.proxy_type}
                    </span>
                    <span className="text-default truncate max-w-[22rem]">
                        {h.capability || h.model || h.proxy_subtype || '—'}
                    </span>
                    {h.guardrail_blocked && (
                        <span className="text-xs text-red-700 font-medium">blocked</span>
                    )}
                    {h.success === false && !h.guardrail_blocked && (
                        <span className="text-xs text-red-700">{h.status_code ?? 'failed'}</span>
                    )}
                    <span className="text-xs text-muted tabular-nums">
                        {h.duration_ms !== null && h.duration_ms !== undefined
                            ? `${Math.round(h.duration_ms)} ms`
                            : ''}
                    </span>
                    <span className="text-xs text-muted">{h.trace_continuity ?? ''}</span>
                    {(h.delegation_depth ?? 0) > 0 && (
                        <span className="text-xs text-muted">depth {h.delegation_depth}</span>
                    )}
                </li>
            ))}
        </ol>
    );
}

/** Who acted. An agentic system's runs are made by an *agent identity* — a
 *  principal with its own grants — and that is the answer to "which agent did
 *  this", the first question asked of any autonomous action. A run attributed
 *  to `end_user` on a system like this usually means the agent authenticated
 *  with an unbound key, so the distinction is worth showing rather than
 *  leaving in the payload. */
function ActorCell({ run }: { run: RunSummary }) {
    if (run.actor_type === 'agent_identity') {
        // The subject is who the agent acted FOR. "agent" alone answers
        // "which agent"; the person it acted for is the next question, and the
        // proxy already knows it from the signed delegation chain.
        const subject = run.subject_id
            ? `${run.subject_kind || 'subject'}:${run.subject_id}`
            : null;
        return (
            <span className="inline-flex items-center gap-1" data-testid={`actor-${run.id}`}>
                <span
                    className="inline-flex px-2 py-0.5 rounded text-xs font-medium bg-sky-100 text-sky-800"
                    title={run.actor_id || undefined}
                >
                    agent
                </span>
                {subject && (
                    <span
                        className="text-xs text-muted truncate max-w-[9rem]"
                        title={`Acting for ${subject}`}
                        data-testid={`subject-${run.id}`}
                    >
                        for {subject}
                    </span>
                )}
            </span>
        );
    }
    return (
        <span className="text-xs text-muted" data-testid={`actor-${run.id}`}>
            {run.actor_type || '—'}
        </span>
    );
}

/** The run's release (name@version + trust), "—" when the run
 *  declared none, and under it the normalised client label when there is one. */
function ReleaseCell({ run }: { run: RunSummary }) {
    const release = runRelease(run);
    return (
        <span className="block truncate" data-testid={`release-${run.id}`}>
            {release ? (
                <span className="inline-flex items-center gap-1">
                    <code className="truncate font-mono text-xs text-default" title={releaseLongLabel(release)}>
                        {releaseLabel(release)}
                    </code>
                    {release.trust && <TrustMarker trust={release.trust} />}
                </span>
            ) : (
                <span className="text-xs text-muted">—</span>
            )}
            {run.client_label && (
                <span className="block truncate text-xs text-muted" data-testid={`client-${run.id}`}>
                    via {run.client_label}
                </span>
            )}
        </span>
    );
}

function ActionHistogram({ stats }: { stats: RunStatsResponse }) {
    const max = Math.max(1, ...stats.actions.distribution.map((b) => b.count));
    return (
        <div className="space-y-1" data-testid="action-histogram">
            {stats.actions.distribution.map((b) => (
                <div key={`${b.lower}-${b.upper ?? 'plus'}`} className="flex items-center gap-3">
                    <span className="w-20 text-xs text-muted text-right shrink-0">
                        {b.upper === null || b.upper === undefined
                            ? `${b.lower}+`
                            : `${b.lower}–${b.upper}`}
                    </span>
                    <div className="flex-1 h-4 bg-surface-subtle rounded overflow-hidden min-h-0 min-w-0">
                        <div
                            className="h-full bg-primary-500"
                            style={{ width: `${(b.count / max) * 100}%` }}
                        />
                    </div>
                    <span className="w-10 text-xs text-secondary">{b.count}</span>
                </div>
            ))}
        </div>
    );
}

function EmptyRuns({
    filtered,
    hasError,
    servedCalls = 0,
}: {
    filtered: boolean;
    hasError: boolean;
    /** Calls served inside other systems' runs in the window. */
    servedCalls?: number;
}) {
    if (hasError) return null;
    return (
        <div className="p-8 text-center border border-dashed border-strong rounded-lg" data-testid="runs-empty">
            {filtered ? (
                <p className="text-sm text-muted">No runs match those filters in this window.</p>
            ) : servedCalls > 0 ? (
                <>
                    <p className="text-sm text-default font-medium">No runs of its own — it works as a shared service</p>
                    <p className="text-sm text-muted mt-1 max-w-lg mx-auto">
                        This system started no runs in this window, but it served{' '}
                        {servedCalls} call{servedCalls === 1 ? '' : 's'} inside other systems’ runs,
                        listed under Served calls below.
                    </p>
                </>
            ) : (
                <>
                    <p className="text-sm text-default font-medium">No runs recorded yet</p>
                    <p className="text-sm text-muted mt-1 max-w-lg mx-auto">
                        Runs appear once traffic through this AI System finishes. A run closes
                        when the client reports it done, or after its idle window elapses.
                    </p>
                </>
            )}
        </div>
    );
}

export default AISystemRunsTab;
