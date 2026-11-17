/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Continuous Assurance Checks — Mission Control's
 * spec-authoring surface.
 *
 * The authoring flow enforces the trust boundary visually:
 * NL question → (optional) compile assist → the operator reviews the
 * ACTUAL spec (expression or rubric) → compile verdict from the Rust
 * evaluator → create (disabled) → backtest ("what would this have
 * flagged") → enable, which records the approver. Spec edits bump the
 * version and drop enablement until re-approved.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CheckCircle,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  Info,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { getBoolean, getRecord, getString, failureSentence } from '../../lib/narrow';
import BlindReviewPanel from './BlindReviewPanel';
import RunFactsHelp from './RunFactsHelp';
import {
  AssuranceCheck,
  AssuranceCheckResult,
  CheckBacktestResponse,
  CheckCompileResponse,
  CheckOutput,
  CheckTier,
  CHECK_OUTPUTS,
} from '../../lib/assuranceCheckTypes';
import toast from 'react-hot-toast';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

const fmtTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '—');

/** A judged result's stated reason, and whether the judge saw a cut-down
 *  transcript — both read from the untyped evidence blob. */
function ResultEvidenceNote({ evidence }: { evidence: unknown }) {
  const reason = getString(evidence, 'reason');
  const truncated = getBoolean(getRecord(evidence, 'transcript'), 'truncated') === true;
  if (!reason && !truncated) return null;
  return (
    <span className="block pl-4 font-sans">
      {reason && (
        <span className="text-default" data-testid="check-result-reason">
          Reason: {reason}
        </span>
      )}
      {truncated && (
        <span
          className="ml-2 inline-flex px-1.5 py-0.5 rounded bg-amber-100 text-amber-800"
          data-testid="check-result-truncated"
          title="The transcript the judge read was cut to fit its limit"
        >
          transcript truncated
        </span>
      )}
    </span>
  );
}

const tierBadge = (tier: CheckTier) =>
  tier === 'deterministic'
    ? 'bg-blue-100 text-blue-800'
    : 'bg-purple-100 text-purple-800';

interface FormState {
  name: string;
  description_nl: string;
  tier: CheckTier;
  expression: string;
  rubric: string;
  sample_rate: string;
  labels: string;
  output: CheckOutput;
  thresholdOn: boolean;
  severity: 'info' | 'warning' | 'critical';
}

const EMPTY_FORM: FormState = {
  name: '',
  description_nl: '',
  tier: 'deterministic',
  expression: '',
  rubric: '',
  sample_rate: '1.0',
  labels: '',
  output: 'boolean',
  thresholdOn: true,
  severity: 'warning',
};

interface AssuranceChecksProps {
  /** Open this judged check's blind review queue
   *  (`inbox:reviews:<id>`, linked from judge_* inbox findings). */
  focusReviewCheckId?: string;
}

export default function AssuranceChecks({ focusReviewCheckId }: AssuranceChecksProps = {}) {
  const { gatewayClient } = useGateway();
  const [checks, setChecks] = useState<AssuranceCheck[]>([]);
  const [loading, setLoading] = useState(true);
  const [readFailure, reads] = useReadFailures<'checks'>();
  const [collapsed, setCollapsed] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [compile, setCompile] = useState<CheckCompileResponse | null>(null);
  const [assistQuestion, setAssistQuestion] = useState('');
  const [assistBusy, setAssistBusy] = useState(false);
  const [assistNote, setAssistNote] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [results, setResults] = useState<AssuranceCheckResult[]>([]);
  const [backtest, setBacktest] = useState<CheckBacktestResponse | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showFacts, setShowFacts] = useState(false);
  const expressionRef = useRef<HTMLTextAreaElement>(null);

  /** Insert `text` at the expression's cursor (or append). */
  const insertIntoExpression = (text: string) => {
    const el = expressionRef.current;
    const at = el ? el.selectionStart : form.expression.length;
    const end = el ? el.selectionEnd : at;
    const next = form.expression.slice(0, at) + text + form.expression.slice(end);
    setForm({ ...form, expression: next });
    setCompile(null);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(at + text.length, at + text.length);
    });
  };

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setChecks(await gatewayClient.listAssuranceChecks());
      reads.loaded('checks');
    } catch (err) {
      // Mission Control stays usable; this panel says the checks were not read.
      reads.failed('checks', 'Failed to load the assurance checks', err);
    } finally {
      setLoading(false);
    }
  }, [gatewayClient, reads]);

  useEffect(() => {
    void load();
  }, [load]);

  // Deep link: expand the section and the check once the list is in.
  useEffect(() => {
    if (!focusReviewCheckId || !checks.some((c) => c.id === focusReviewCheckId)) return;
    setCollapsed(false);
    setExpanded(focusReviewCheckId);
  }, [focusReviewCheckId, checks]);

  const openDetails = async (check: AssuranceCheck) => {
    if (expanded === check.id) {
      setExpanded(null);
      return;
    }
    setExpanded(check.id);
    setBacktest(null);
    try {
      const listed = await gatewayClient.listAssuranceCheckResults(check.id, {
        limit: 20,
      });
      setResults(listed.results);
    } catch (err) {
      setResults([]);
      toast.error(failureSentence('The results of the check could not be read', err));
    }
  };

  const runCompile = async () => {
    setCompile(null);
    try {
      setCompile(
        await gatewayClient.compileAssuranceCheck(form.expression, form.output),
      );
    } catch (e) {
      setCompile({
        ok: false,
        errors: [e instanceof Error ? e.message : 'evaluator unreachable'],
      });
    }
  };

  const runAssist = async () => {
    if (!assistQuestion.trim()) return;
    setAssistBusy(true);
    setAssistNote(null);
    try {
      const res = await gatewayClient.compileAssistAssuranceCheck(assistQuestion);
      if (!res.proposal) {
        setAssistNote(res.error || 'The assist could not propose a spec.');
        return;
      }
      const proposal = res.proposal;
      setForm((f) => ({
        ...f,
        tier: proposal.tier || 'deterministic',
        expression: proposal.evaluator?.expression || '',
        rubric: proposal.evaluator?.rubric || '',
        output: proposal.output || 'boolean',
        description_nl: assistQuestion,
        severity: proposal.threshold?.severity || 'warning',
      }));
      setCompile(res.compile || null);
      setAssistNote(
        res.downgraded
          ? 'Not answerable from run facts alone — a semantic (LLM-judge) spec was proposed. Review the rubric.'
          : `Proposed a deterministic spec (${proposal.reason || 'review below'}).`,
      );
    } catch (e) {
      setAssistNote(e instanceof Error ? e.message : 'assist failed');
    } finally {
      setAssistBusy(false);
    }
  };

  const submit = async () => {
    setFormError(null);
    try {
      const evaluator =
        form.tier === 'deterministic'
          ? { expression: form.expression }
          : {
              rubric: form.rubric,
              sample_rate: parseFloat(form.sample_rate) || 1.0,
              max_tokens: 300,
              ...(form.output === 'classification' && form.labels
                ? { labels: form.labels.split(',').map((l) => l.trim()) }
                : {}),
            };
      await gatewayClient.createAssuranceCheck({
        name: form.name,
        description_nl: form.description_nl || null,
        tier: form.tier,
        evaluator,
        output: form.output,
        threshold: form.thresholdOn
          ? { kind: 'value', op: 'eq', value: true, severity: form.severity }
          : null,
      });
      setShowForm(false);
      setForm(EMPTY_FORM);
      setCompile(null);
      await load();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : 'create failed');
    }
  };

  const toggleEnabled = async (check: AssuranceCheck) => {
    setBusy(check.id);
    try {
      await gatewayClient.updateAssuranceCheck(check.id, {
        enabled: !check.enabled,
      });
      await load();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : 'update failed');
    } finally {
      setBusy(null);
    }
  };

  const remove = async (check: AssuranceCheck) => {
    if (!window.confirm(`Delete check "${check.name}" and all its results?`)) {
      return;
    }
    await gatewayClient.deleteAssuranceCheck(check.id);
    setExpanded(null);
    await load();
  };

  const runBacktest = async (check: AssuranceCheck) => {
    setBusy(check.id);
    setBacktest(null);
    try {
      setBacktest(await gatewayClient.backtestAssuranceCheck(check.id, 7));
    } catch (e) {
      setBacktest({
        ok: false,
        error: e instanceof Error ? e.message : 'backtest failed',
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      className="rounded-xl border border-default bg-surface"
      data-testid="assurance-checks"
    >
      <button
        type="button"
        className="w-full flex items-center justify-between px-4 py-3"
        onClick={() => setCollapsed((c) => !c)}
        data-testid="checks-toggle"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-default">
          <ShieldCheck className="w-4 h-4 text-primary-600" />
          Continuous checks
          <span className="text-xs font-normal text-secondary">
            {checks.filter((c) => c.enabled).length} enabled /{' '}
            {checks.length} defined
          </span>
        </span>
        {collapsed ? (
          <ChevronRight className="w-4 h-4 text-secondary" />
        ) : (
          <ChevronDown className="w-4 h-4 text-secondary" />
        )}
      </button>

      {!collapsed && (
        <div className="px-4 pb-4 space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-xs text-secondary max-w-xl">
              Versioned, operator-approved specs evaluated over the run
              ledger. Deterministic checks run at finalization in the
              gateway core; semantic checks are judged in governed batches.
            </p>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="p-1.5 rounded-lg hover:bg-surface-raised text-secondary"
                title="Refresh"
                onClick={() => void load()}
              >
                <RefreshCw className="w-4 h-4" />
              </button>
              <button
                type="button"
                className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700"
                onClick={() => setShowForm(true)}
                data-testid="new-check"
              >
                <Plus className="w-3.5 h-3.5" /> New check
              </button>
            </div>
          </div>

          {loading ? (
            <p className="text-sm text-secondary py-2">Loading…</p>
          ) : readFailure('checks') && checks.length === 0 ? (
            <ReadFailureNotice failure={readFailure('checks')} onRetry={() => void load()} />
          ) : checks.length === 0 ? (
            <p className="text-sm text-secondary py-2" data-testid="no-checks">
              No checks defined yet. Ask a question of your run ledger —
              &ldquo;flag any run that called a payment tool without an
              approval&rdquo; — and pin it as a continuous check.
            </p>
          ) : (
            <div className="divide-y divide-default border border-default rounded-lg">
              {checks.map((check) => (
                <div key={check.id}>
                  <div
                    className="flex items-center gap-3 px-3 py-2 hover:bg-surface-raised cursor-pointer"
                    onClick={() => void openDetails(check)}
                    data-testid="check-row"
                  >
                    <span
                      className={`px-1.5 py-0.5 text-[10px] font-medium rounded ${tierBadge(check.tier)}`}
                    >
                      {check.tier === 'deterministic' ? 'TIER A' : 'TIER B'}
                    </span>
                    <span className="text-sm text-default font-medium truncate">
                      {check.name}
                    </span>
                    <span className="text-xs text-secondary">
                      v{check.version} · {check.output}
                    </span>
                    <span className="flex-1 min-h-0 min-w-0" />
                    {check.approved_by && check.enabled && (
                      <span
                        className="text-[10px] text-secondary"
                        title={`Approved by ${check.approved_by}`}
                      >
                        approved: {check.approved_by}
                      </span>
                    )}
                    <button
                      type="button"
                      disabled={busy === check.id}
                      className={`px-2 py-0.5 text-[11px] font-medium rounded-full ${
                        check.enabled
                          ? 'bg-green-100 text-green-800'
                          : 'bg-surface-raised text-secondary'
                      }`}
                      onClick={(e) => {
                        e.stopPropagation();
                        void toggleEnabled(check);
                      }}
                      data-testid="check-enabled-toggle"
                    >
                      {check.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                  </div>

                  {expanded === check.id && (
                    <div
                      className="px-4 py-3 bg-surface-raised space-y-3 text-sm"
                      data-testid="check-details"
                    >
                      <div className="font-mono text-xs bg-surface border border-default rounded p-2 overflow-x-auto">
                        {check.tier === 'deterministic'
                          ? check.evaluator.expression
                          : check.evaluator.rubric}
                      </div>
                      {check.tier === 'semantic' && (
                        <p className="text-xs text-secondary">
                          Sampling:{' '}
                          {Math.round((check.evaluator.sample_rate ?? 1) * 100)}%
                          of runs (declared &amp; disclosed) · judge:{' '}
                          {check.evaluator.model_ref || 'tenant default'}
                        </p>
                      )}
                      <div className="flex items-center gap-2">
                        {check.tier === 'deterministic' && (
                          <button
                            type="button"
                            disabled={busy === check.id}
                            className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-lg border border-default hover:bg-surface"
                            onClick={() => void runBacktest(check)}
                            data-testid="run-backtest"
                          >
                            <FlaskConical className="w-3.5 h-3.5" />
                            Backtest 7 days
                          </button>
                        )}
                        <button
                          type="button"
                          className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-lg border border-default text-red-600 hover:bg-surface"
                          onClick={() => void remove(check)}
                        >
                          <Trash2 className="w-3.5 h-3.5" /> Delete
                        </button>
                      </div>
                      {backtest && (
                        <div
                          className="text-xs rounded-lg border border-default bg-surface p-2"
                          data-testid="backtest-result"
                        >
                          {backtest.ok ? (
                            <>
                              Evaluated {backtest.evaluated} runs over{' '}
                              {backtest.window_days} days —{' '}
                              <strong>{backtest.breaches} would have breached</strong>
                              {(backtest.errors ?? 0) > 0 &&
                                ` (${backtest.errors} evaluation errors)`}
                              .
                            </>
                          ) : (
                            <span className="text-red-600">{backtest.error}</span>
                          )}
                        </div>
                      )}
                      {check.tier === 'semantic' && (
                        <BlindReviewPanel checkId={check.id} />
                      )}
                      <div>
                        <p className="text-xs font-medium text-secondary mb-1">
                          Recent results
                        </p>
                        {results.length === 0 ? (
                          <p className="text-xs text-secondary">
                            No results yet.
                          </p>
                        ) : (
                          <ul className="space-y-0.5">
                            {results.map((result) => (
                              <li
                                key={result.id}
                                className="text-xs text-secondary font-mono"
                                data-testid="check-result-row"
                              >
                                {fmtTime(result.evaluated_at)} ·{' '}
                                {result.run_id ??
                                  `window ${result.value.hits}/${result.value.total}`}{' '}
                                → {JSON.stringify(result.value.value)}
                                {result.backtest ? ' (backtest)' : ''}
                                {result.tokens_used
                                  ? ` · ${result.tokens_used} tokens`
                                  : ''}
                                <ResultEvidenceNote evidence={result.evidence} />
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {showForm && (
        <div className="modal-overlay" onClick={() => setShowForm(false)}>
          <div
            className="modal-content max-w-2xl p-6"
            onClick={(e) => e.stopPropagation()}
            data-testid="check-form"
          >
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold text-default">
                New continuous check
              </h3>
              <button
                type="button"
                className="p-1 rounded hover:bg-surface-raised"
                onClick={() => setShowForm(false)}
              >
                <X className="w-4 h-4 text-secondary" />
              </button>
            </div>

            <div className="space-y-4">
              <div className="rounded-lg border border-dashed border-default p-3 space-y-2">
                <label className="text-xs font-medium text-secondary flex items-center gap-1">
                  <Sparkles className="w-3.5 h-3.5" /> Ask in plain language
                  (optional — proposes a spec, never runs)
                </label>
                <div className="flex gap-2">
                  <input
                    className="flex-1 px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default min-h-0 min-w-0"
                    placeholder="Flag any run that exceeded 3 delegation hops"
                    value={assistQuestion}
                    onChange={(e) => setAssistQuestion(e.target.value)}
                    data-testid="assist-question"
                  />
                  <button
                    type="button"
                    disabled={assistBusy}
                    className="px-2.5 py-1.5 text-xs rounded-lg border border-default hover:bg-surface-raised"
                    onClick={() => void runAssist()}
                    data-testid="assist-run"
                  >
                    {assistBusy ? 'Proposing…' : 'Propose spec'}
                  </button>
                </div>
                {assistNote && (
                  <p className="text-xs text-secondary" data-testid="assist-note">
                    {assistNote}
                  </p>
                )}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block mb-1 text-xs font-medium text-secondary">
                    Name
                  </label>
                  <input
                    className="w-full px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default"
                    placeholder="payment-tools-need-approval"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                  />
                </div>
                <div>
                  <label
                    className="block mb-1 text-xs font-medium text-secondary"
                    htmlFor="check-tier"
                  >
                    Tier
                  </label>
                  <select
                    id="check-tier"
                    className="w-full px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default"
                    value={form.tier}
                    onChange={(e) =>
                      setForm({ ...form, tier: e.target.value as CheckTier })
                    }
                  >
                    <option value="deterministic">
                      Deterministic (CEL over run facts)
                    </option>
                    <option value="semantic">Semantic (LLM judge)</option>
                  </select>
                </div>
              </div>

              {form.tier === 'deterministic' ? (
                <div>
                  <div className="flex items-center gap-1.5 mb-1">
                    <label
                      className="text-xs font-medium text-secondary"
                      htmlFor="check-expression"
                    >
                      Expression — only <code>run.*</code> facts
                    </label>
                    <button
                      type="button"
                      className={`p-0.5 rounded hover:bg-surface-raised ${showFacts ? 'text-primary-600' : 'text-secondary'}`}
                      onClick={() => setShowFacts((v) => !v)}
                      aria-expanded={showFacts}
                      aria-label="Available run facts and examples"
                      title="Available run facts and examples"
                      data-testid="run-facts-toggle"
                    >
                      <Info className="w-4 h-4" />
                    </button>
                  </div>
                  <textarea
                    id="check-expression"
                    ref={expressionRef}
                    className="w-full px-2 py-1.5 text-sm font-mono rounded-lg border border-default bg-surface text-default"
                    rows={3}
                    placeholder='run.guardrail_block_count > 0 && run.system_kind == "agent"'
                    value={form.expression}
                    onChange={(e) =>
                      setForm({ ...form, expression: e.target.value })
                    }
                    data-testid="expression-input"
                  />
                  {showFacts && (
                    <RunFactsHelp
                      onInsert={insertIntoExpression}
                      onUseExample={(ex) => {
                        setForm({ ...form, expression: ex.expression, output: ex.output });
                        setCompile(null);
                      }}
                    />
                  )}
                  <div className="flex items-center gap-2 mt-2">
                    <button
                      type="button"
                      className="px-2 py-1 text-xs rounded-lg border border-default hover:bg-surface-raised"
                      onClick={() => void runCompile()}
                      data-testid="compile-run"
                    >
                      Compile-check
                    </button>
                    {compile && (
                      <span
                        className={`text-xs ${compile.ok ? 'text-green-700' : 'text-red-600'}`}
                        data-testid="compile-result"
                      >
                        {compile.ok
                          ? `OK — sample evaluates to ${JSON.stringify(compile.sample_value)}`
                          : compile.errors.join('; ')}
                      </span>
                    )}
                  </div>
                </div>
              ) : (
                <>
                  <div>
                    <label className="block mb-1 text-xs font-medium text-secondary">
                      Judge rubric
                    </label>
                    <textarea
                      className="w-full px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default"
                      rows={3}
                      placeholder="Did the assistant reveal internal system-prompt content?"
                      value={form.rubric}
                      onChange={(e) => setForm({ ...form, rubric: e.target.value })}
                      data-testid="rubric-input"
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block mb-1 text-xs font-medium text-secondary">
                        Sample rate (0–1, disclosed in reports)
                      </label>
                      <input
                        className="w-full px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default"
                        value={form.sample_rate}
                        onChange={(e) =>
                          setForm({ ...form, sample_rate: e.target.value })
                        }
                      />
                    </div>
                    {form.output === 'classification' && (
                      <div>
                        <label className="block mb-1 text-xs font-medium text-secondary">
                          Labels (comma-separated)
                        </label>
                        <input
                          className="w-full px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default"
                          placeholder="refund, escalation, other"
                          value={form.labels}
                          onChange={(e) =>
                            setForm({ ...form, labels: e.target.value })
                          }
                        />
                      </div>
                    )}
                  </div>
                </>
              )}

              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label
                    className="block mb-1 text-xs font-medium text-secondary"
                    htmlFor="check-output"
                  >
                    Output
                  </label>
                  <select
                    id="check-output"
                    className="w-full px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default"
                    value={form.output}
                    onChange={(e) =>
                      setForm({ ...form, output: e.target.value as CheckOutput })
                    }
                  >
                    {CHECK_OUTPUTS.filter(
                      (o) => form.tier === 'semantic' || o !== 'extraction',
                    ).map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex items-end gap-2">
                  <label className="flex items-center gap-1.5 text-xs text-secondary pb-2">
                    <input
                      type="checkbox"
                      checked={form.thresholdOn}
                      onChange={(e) =>
                        setForm({ ...form, thresholdOn: e.target.checked })
                      }
                    />
                    Raise inbox finding when true
                  </label>
                </div>
                {form.thresholdOn && (
                  <div>
                    <label
                      className="block mb-1 text-xs font-medium text-secondary"
                      htmlFor="check-severity"
                    >
                      Severity
                    </label>
                    <select
                      id="check-severity"
                      className="w-full px-2 py-1.5 text-sm rounded-lg border border-default bg-surface text-default"
                      value={form.severity}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          severity: e.target.value as FormState['severity'],
                        })
                      }
                    >
                      <option value="info">info</option>
                      <option value="warning">warning</option>
                      <option value="critical">critical</option>
                    </select>
                  </div>
                )}
              </div>

              {formError && (
                <p className="text-xs text-red-600" data-testid="form-error">
                  {formError}
                </p>
              )}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  className="px-3 py-1.5 text-sm rounded-lg border border-default text-secondary hover:bg-surface-raised"
                  onClick={() => setShowForm(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700"
                  onClick={() => void submit()}
                  data-testid="create-check"
                >
                  <CheckCircle className="w-4 h-4" /> Create check
                </button>
              </div>
              <p className="text-[11px] text-secondary">
                Checks are created disabled. Backtest first, then enable —
                enabling records you as the approver of this spec version.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
