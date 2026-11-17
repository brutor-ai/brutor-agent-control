/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Declare, edit and remove a dependency edge.
 *
 * The owner records which role the callee plays — a shared service (another
 * AI System in this tenant) or an external agent (a card with no Brutor AI
 * System behind it) — and how the caller relies on it. Client-side checks
 * mirror the control plane's 422 rules (`validateDependencyDraft`), so the
 * form says what is missing before a round trip; the server stays the
 * authority and its message is shown verbatim when it disagrees.
 *
 * The owner also states three things here:
 *  - **who supplies it** (`supplier_legal_entity`) — required for an external
 *    agent, optional for a shared service another legal entity owns; either
 *    way the edge then needs supplier information on file (Art 25(4));
 *  - **what the root assumes** about an external agent — a typed form over
 *    the server's STRICT schema (unknown keys are a 422), with its ranges;
 *  - **the sealed separability claim** — a non-contributing edge's rationale
 *    is sealed as a declaration after the save. A seal that fails leaves the
 *    edge saved but unsealed; the dialog stays open to say so, and the next
 *    save retries it.
 */

import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Loader, ShieldCheck, Trash2, Waypoints, X } from 'lucide-react';
import toast from 'react-hot-toast';

import { useGateway } from '../../hooks/useGateway';
import { asyncHandler } from '../../lib/asyncSafe';
import { errorMessage } from '../../lib/narrow';
import {
  AISystemContract,
  DEPENDENCY_CRITICALITY_LABELS,
  DEPENDENCY_EVIDENCE_PROVIDED,
  DEPENDENCY_EVIDENCE_PROVIDED_LABELS,
  DEPENDENCY_ROLE_LABELS,
  DependencyCriticality,
  DependencyDraft,
  DependencyEdge,
  DependencyRole,
  DependencySealStatus,
} from '../../lib/aiSystemTypes';
import {
  ASSUMPTION_LIMITS,
  AssumptionsForm,
  DeclareTarget,
  DependencyErrors,
  SUPPLIER_LEGAL_ENTITY_MAX,
  assumptionsForm,
  draftFromEdge,
  emptyDependencyDraft,
  normalizeDependencyDraft,
  parseAssumptionsForm,
  parseExpectedOutcomes,
  pinStatusText,
  pinnableContracts,
  validateDependencyDraft,
} from '../../lib/composition';
import { reportableIds } from '../../lib/runOutcomes';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface Option {
  id: string;
  name: string;
}

export interface DependencyDialogProps {
  aiSystemId: string;
  aiSystemName: string;
  /** Edit this edge; omitted = declare a new one. */
  edge?: DependencyEdge | null;
  /** Declare action on an undeclared node: pre-fill the target — a
   *  shared service when a system is known, otherwise an external card. */
  prefill?: DeclareTarget | null;
  onSaved: () => void;
  onCancel: () => void;
}

const INPUT =
  'w-full rounded-md border border-strong bg-surface px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500';
const LABEL = 'mb-1 block text-sm font-medium text-secondary';

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} className="mt-1 text-xs text-red-600" data-testid={id}>
      {message}
    </p>
  );
}

export function DependencyDialog({
  aiSystemId,
  aiSystemName,
  edge,
  prefill,
  onSaved,
  onCancel,
}: DependencyDialogProps) {
  const {
    fetchAISystems,
    listAgentCards,
    fetchAISystemContracts,
    fetchAISystemOutcomes,
    createAISystemDependency,
    updateAISystemDependency,
  } = useGateway();

  const editing = Boolean(edge);
  const [draft, setDraft] = useState<DependencyDraft>(() => {
    if (edge) return draftFromEdge(edge);
    const d = emptyDependencyDraft();
    if (prefill) {
      d.role = prefill.system_id ? 'shared_service' : 'external';
      d.depends_on_system_id = prefill.system_id;
      d.agent_card_id = prefill.agent_card_id;
    }
    return d;
  });
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  // The assumptions form keeps raw text (numbers parse on validate).
  const [form, setForm] = useState<AssumptionsForm>(() =>
    assumptionsForm(draft.assumptions)
  );
  // Saved, but the separability claim did not seal: keep the dialog open
  // with the reason instead of closing as if all is well.
  const [sealFailure, setSealFailure] = useState<DependencySealStatus | null>(
    null
  );

  const [systems, setSystems] = useState<Option[]>([]);
  const [cards, setCards] = useState<Option[]>([]);
  // Keyed by the target they belong to, so a stale list never shows
  // against a newly chosen target.
  const [targetContracts, setTargetContracts] = useState<{
    target: string;
    contracts: AISystemContract[];
    activeVersion: number | null;
  } | null>(null);

  // A list that could not be loaded leaves its select empty: the dialog
  // says which, beside the validation message that explains what is needed.
  const [readFailure, reads] = useReadFailures<'systems' | 'cards' | 'contracts' | 'outcomes'>();
  const [asked, setAsked] = useState(0);
  const askAgain = () => setAsked((n) => n + 1);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetchAISystems({ limit: 500 });
        if (!cancelled) {
          setSystems(
            res.systems
              .filter((s) => s.id !== aiSystemId)
              .map((s) => ({ id: s.id, name: s.display_name || s.name }))
          );
          reads.loaded('systems');
        }
      } catch (err) {
        if (cancelled) return;
        setSystems([]);
        reads.lost('systems', 'Failed to load the AI systems', err);
      }
    })();
    void (async () => {
      try {
        const res = await listAgentCards();
        if (cancelled) return;
        setCards(res.cards.map((c) => ({ id: c.id, name: c.name })));
        reads.loaded('cards');
      } catch (err) {
        if (cancelled) return;
        setCards([]);
        reads.lost('cards', 'Failed to load the agent cards', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [aiSystemId, fetchAISystems, listAgentCards, asked, reads]);

  // The pin options are the TARGET's contracts.
  const targetId =
    draft.role === 'shared_service' ? draft.depends_on_system_id : null;
  const loaded =
    targetId && targetContracts?.target === targetId ? targetContracts : null;
  const contracts = loaded ? pinnableContracts(loaded.contracts) : [];
  const activeVersion = loaded?.activeVersion ?? null;
  const pinnedVersion =
    loaded?.contracts.find((c) => c.id === draft.contract_pin)?.version ??
    null;
  // "Pin" is a mode, not only a value: choosing it before a version is
  // picked must not snap back to "follow".
  const [pinMode, setPinMode] = useState<'follow' | 'pin'>(
    draft.contract_pin ? 'pin' : 'follow'
  );
  useEffect(() => {
    if (!targetId) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetchAISystemContracts(targetId);
        if (cancelled) return;
        setTargetContracts({
          target: targetId,
          contracts: res.contracts,
          activeVersion:
            res.active_version ??
            res.contracts.find((c) => c.is_active || c.status === 'active')
              ?.version ??
            null,
        });
        reads.loaded('contracts');
      } catch (err) {
        if (cancelled) return;
        setTargetContracts({
          target: targetId,
          contracts: [],
          activeVersion: null,
        });
        reads.lost('contracts', 'Failed to load the contracts of the target; no version can be pinned', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [targetId, fetchAISystemContracts]);

  // The outcomes the TARGET declares (its active contract's catalogue, else
  // its live one) — what the caller can choose to expect back. Keyed by
  // target, like the contracts; best-effort.
  const [targetOutcomes, setTargetOutcomes] = useState<{
    target: string;
    ids: string[];
  } | null>(null);
  useEffect(() => {
    if (!targetId) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchAISystemOutcomes(targetId))
      .then(
        (res) => {
          if (cancelled) return;
          setTargetOutcomes({
            target: targetId,
            ids: reportableIds(res?.active_outcomes ?? res?.outcomes),
          });
          reads.loaded('outcomes');
        },
        (err: unknown) => {
          if (cancelled) return;
          setTargetOutcomes({ target: targetId, ids: [] });
          reads.lost('outcomes', 'Failed to load the outcomes the target declares', err);
        }
      );
    return () => {
      cancelled = true;
    };
  }, [targetId, fetchAISystemOutcomes, asked, reads]);
  const declaredByTarget =
    targetId && targetOutcomes?.target === targetId ? targetOutcomes.ids : [];

  const parsedAssumptions = useMemo(() => parseAssumptionsForm(form), [form]);
  const assumptionErrors =
    draft.role === 'external' ? parsedAssumptions.errors : {};
  const errors: DependencyErrors = useMemo(() => {
    const e = validateDependencyDraft(draft, aiSystemId);
    if (
      draft.role === 'shared_service' &&
      pinMode === 'pin' &&
      !draft.contract_pin
    ) {
      e.contract_pin = 'Choose the contract version to pin to.';
    }
    return e;
  }, [draft, aiSystemId, pinMode]);
  const hasErrors =
    Object.keys(errors).length > 0 || Object.keys(assumptionErrors).length > 0;
  const shown = touched ? errors : {};
  const shownAssumptions = touched ? assumptionErrors : {};

  const set = <K extends keyof DependencyDraft>(
    key: K,
    value: DependencyDraft[K]
  ) => setDraft((d) => ({ ...d, [key]: value }));

  const submit = async () => {
    setTouched(true);
    if (hasErrors) return;
    setSaving(true);
    setServerError(null);
    const body = normalizeDependencyDraft({
      ...draft,
      // External: what the form states. A shared service is assured from
      // its own evidence; its (not shown) known assumptions pass through.
      assumptions:
        draft.role === 'external' ? parsedAssumptions.value : draft.assumptions,
    });
    try {
      let saved: DependencyEdge;
      if (edge) {
        // Role and target identify the edge; PATCH carries the rest.
        const patch: Partial<DependencyDraft> = {
          criticality: body.criticality,
          fallback: body.fallback,
          contributes_to_decision: body.contributes_to_decision,
          separability_rationale: body.separability_rationale,
          contract_pin: body.contract_pin,
          supplier_legal_entity: body.supplier_legal_entity,
          assumptions: body.assumptions,
          expected_outcomes: body.expected_outcomes,
        };
        saved = await updateAISystemDependency(aiSystemId, edge.id, patch);
      } else {
        saved = await createAISystemDependency(aiSystemId, body);
      }
      const seal = saved.separability_seal ?? null;
      if (seal && !seal.sealed) {
        setSealFailure(seal);
        setSaving(false);
        return;
      }
      if (seal?.sealed) {
        toast.success('Dependency saved; separability claim sealed');
      }
      onSaved();
    } catch (e) {
      setServerError(errorMessage(e, 'Failed to save the dependency'));
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="dependency-dialog-title"
      data-testid="dependency-dialog"
    >
      <div className="flex max-h-[90vh] w-full max-w-xl flex-col rounded-lg border border-default bg-surface shadow-xl">
        <div className="flex items-start justify-between border-b border-default p-4">
          <div className="flex items-start gap-3">
            <Waypoints className="mt-0.5 h-5 w-5 flex-shrink-0 composition-cat-delegation-agent composition-ink" />
            <div>
              <h2
                id="dependency-dialog-title"
                className="text-base font-medium text-default"
              >
                {editing ? 'Edit dependency' : 'Declare dependency'}
              </h2>
              <p className="mt-0.5 text-xs text-muted">
                What <strong>{aiSystemName}</strong> relies on, and how.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onCancel}
            className="rounded p-1 text-muted hover:bg-surface-alt"
            aria-label="Cancel"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="scroll-region space-y-4 p-4">
          <fieldset>
            <legend className={LABEL}>Role</legend>
            <div className="flex gap-4">
              {(Object.keys(DEPENDENCY_ROLE_LABELS) as DependencyRole[]).map(
                (r) => (
                  <label
                    key={r}
                    className="flex items-center gap-2 text-sm text-default"
                  >
                    <input
                      type="radio"
                      name="dependency-role"
                      value={r}
                      checked={draft.role === r}
                      disabled={editing}
                      onChange={() => set('role', r)}
                      data-testid={`dependency-role-${r}`}
                    />
                    {DEPENDENCY_ROLE_LABELS[r]}
                  </label>
                )
              )}
            </div>
            <p className="mt-1 text-xs text-muted">
              {draft.role === 'shared_service'
                ? 'Another AI System in this tenant, separately owned and approved. Assured from its own runs.'
                : 'A third-party agent not governed here. Assessed at the boundary only — assumed, not tested.'}
            </p>
          </fieldset>

          {draft.role === 'shared_service' ? (
            <div>
              <label htmlFor="dependency-target" className={LABEL}>
                AI System it depends on <span className="text-red-600">*</span>
              </label>
              <select
                id="dependency-target"
                className={INPUT}
                value={draft.depends_on_system_id ?? ''}
                disabled={editing}
                onChange={(e) => {
                  set('depends_on_system_id', e.target.value || null);
                  set('contract_pin', null);
                  setPinMode('follow');
                }}
                aria-invalid={Boolean(shown.depends_on_system_id)}
                data-testid="dependency-target"
              >
                <option value="">Select an AI System…</option>
                {/* Keep the current target selectable even if the
                                    list failed or filtered it out. */}
                {draft.depends_on_system_id &&
                  !systems.some((s) => s.id === draft.depends_on_system_id) && (
                    <option value={draft.depends_on_system_id}>
                      {edge?.depends_on_system_name ??
                        prefill?.name ??
                        draft.depends_on_system_id}
                    </option>
                  )}
                {systems.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <FieldError
                id="dependency-error-depends_on_system_id"
                message={shown.depends_on_system_id}
              />
            </div>
          ) : null}

          <div>
            <label htmlFor="dependency-card" className={LABEL}>
              Agent card
              {draft.role === 'external' ? (
                <span className="text-red-600"> *</span>
              ) : (
                ' (optional)'
              )}
            </label>
            <select
              id="dependency-card"
              className={INPUT}
              value={draft.agent_card_id ?? ''}
              disabled={editing}
              onChange={(e) => set('agent_card_id', e.target.value || null)}
              aria-invalid={Boolean(shown.agent_card_id)}
              data-testid="dependency-card"
            >
              <option value="">
                {draft.role === 'external'
                  ? 'Select an agent card…'
                  : 'Resolved from the system'}
              </option>
              {draft.agent_card_id &&
                !cards.some((c) => c.id === draft.agent_card_id) && (
                  <option value={draft.agent_card_id}>
                    {edge?.agent_card_name ?? draft.agent_card_id}
                  </option>
                )}
              {cards.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <FieldError
              id="dependency-error-agent_card_id"
              message={shown.agent_card_id}
            />
          </div>

          <div>
            <label htmlFor="dependency-supplier" className={LABEL}>
              Supplier legal entity
              {draft.role === 'external' ? (
                <span className="text-red-600"> *</span>
              ) : (
                ' (optional)'
              )}
            </label>
            <input
              id="dependency-supplier"
              type="text"
              className={INPUT}
              maxLength={SUPPLIER_LEGAL_ENTITY_MAX}
              value={draft.supplier_legal_entity ?? ''}
              onChange={(e) => set('supplier_legal_entity', e.target.value)}
              placeholder={
                draft.role === 'external'
                  ? 'The company that provides this agent'
                  : 'Only if another legal entity provides this service'
              }
              aria-invalid={Boolean(shown.supplier_legal_entity)}
              data-testid="dependency-supplier"
            />
            <p className="mt-1 text-xs text-muted">
              {draft.role === 'external'
                ? 'A third-party agent is supplied by someone other than you. '
                : 'Set this when another legal entity provides this service. '}
              The edge then needs supplier information (the written agreement)
              on file under Evidence — EU AI Act Art 25(4).
            </p>
            <FieldError
              id="dependency-error-supplier_legal_entity"
              message={shown.supplier_legal_entity}
            />
          </div>

          <fieldset>
            <legend className={LABEL}>Criticality</legend>
            <div className="flex gap-4">
              {(
                Object.keys(
                  DEPENDENCY_CRITICALITY_LABELS
                ) as DependencyCriticality[]
              ).map((c) => (
                <label
                  key={c}
                  className="flex items-center gap-2 text-sm text-default"
                >
                  <input
                    type="radio"
                    name="dependency-criticality"
                    value={c}
                    checked={draft.criticality === c}
                    onChange={() => set('criticality', c)}
                    data-testid={`dependency-criticality-${c}`}
                  />
                  {DEPENDENCY_CRITICALITY_LABELS[c]}
                </label>
              ))}
            </div>
          </fieldset>

          {draft.criticality === 'degradable' && (
            <div>
              <label htmlFor="dependency-fallback" className={LABEL}>
                Fallback <span className="text-red-600">*</span>
              </label>
              <textarea
                id="dependency-fallback"
                rows={2}
                className={INPUT}
                value={draft.fallback ?? ''}
                onChange={(e) => set('fallback', e.target.value)}
                placeholder="What the system does when this dependency fails"
                aria-invalid={Boolean(shown.fallback)}
                data-testid="dependency-fallback"
              />
              <FieldError
                id="dependency-error-fallback"
                message={shown.fallback}
              />
            </div>
          )}

          <ReadFailureNotice compact failure={readFailure('systems')} onRetry={askAgain} />
          <ReadFailureNotice compact failure={readFailure('cards')} onRetry={askAgain} />
          {targetId && <ReadFailureNotice compact failure={readFailure('contracts')} onRetry={askAgain} />}
          {targetId && <ReadFailureNotice compact failure={readFailure('outcomes')} onRetry={askAgain} />}
          <ExpectedOutcomesFieldset
            value={draft.expected_outcomes}
            declared={declaredByTarget}
            onChange={(v) => set('expected_outcomes', v)}
            error={shown.expected_outcomes}
          />

          <div>
            <label className="flex items-center gap-2 text-sm text-default">
              <input
                type="checkbox"
                checked={draft.contributes_to_decision}
                onChange={(e) =>
                  set('contributes_to_decision', e.target.checked)
                }
                data-testid="dependency-contributes"
              />
              Its output contributes to this system&apos;s decision
            </label>
            <p className="mt-1 text-xs text-muted">
              A contributing dependency is evaluated at the caller&apos;s risk
              tier.
            </p>
          </div>

          {!draft.contributes_to_decision && (
            <div>
              <label htmlFor="dependency-rationale" className={LABEL}>
                Separability rationale <span className="text-red-600">*</span>
              </label>
              <textarea
                id="dependency-rationale"
                rows={3}
                className={INPUT}
                value={draft.separability_rationale ?? ''}
                onChange={(e) => set('separability_rationale', e.target.value)}
                placeholder="Why its output does not influence the outcome"
                aria-invalid={Boolean(shown.separability_rationale)}
                data-testid="dependency-rationale"
              />
              <FieldError
                id="dependency-error-separability_rationale"
                message={shown.separability_rationale}
              />
              <SeparabilitySealLine
                edge={edge ?? null}
                rationale={draft.separability_rationale}
              />
            </div>
          )}

          {draft.role === 'external' && (
            <AssumptionsFieldset
              form={form}
              onChange={setForm}
              errors={shownAssumptions}
            />
          )}

          {draft.role === 'shared_service' && (
            <fieldset data-testid="dependency-contract">
              <legend className={LABEL}>Contract it runs against</legend>
              <div className="space-y-2">
                <label className="flex items-start gap-2 text-sm text-default">
                  <input
                    type="radio"
                    name="dependency-pin-mode"
                    checked={pinMode === 'follow'}
                    onChange={() => {
                      setPinMode('follow');
                      set('contract_pin', null);
                    }}
                    data-testid="dependency-pin-follow"
                  />
                  <span>
                    Follow the active contract
                    {targetId && (
                      <span
                        className="text-muted"
                        data-testid="dependency-active-version"
                      >
                        {' '}
                        {activeVersion !== null
                          ? `(currently v${activeVersion})`
                          : loaded
                            ? '(the target has no active contract yet)'
                            : ''}
                      </span>
                    )}
                    <span className="block text-xs text-muted">
                      When the service promotes a new contract, this system
                      drifts and its approver reviews the service&apos;s diff.
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2 text-sm text-default">
                  <input
                    type="radio"
                    name="dependency-pin-mode"
                    checked={pinMode === 'pin'}
                    onChange={() => setPinMode('pin')}
                    disabled={!targetId}
                    data-testid="dependency-pin-pinned"
                  />
                  <span>
                    Pin to a specific contract
                    <span className="block text-xs text-muted">
                      This system does not drift when the service moves on; a
                      &ldquo;pinned contract is behind&rdquo; finding says so
                      instead, and you move the pin on your own schedule.
                    </span>
                  </span>
                </label>
              </div>
              {pinMode === 'pin' && (
                <div className="mt-2">
                  <label htmlFor="dependency-pin" className="sr-only">
                    Contract version
                  </label>
                  <select
                    id="dependency-pin"
                    className={INPUT}
                    value={draft.contract_pin ?? ''}
                    onChange={(e) =>
                      set('contract_pin', e.target.value || null)
                    }
                    aria-invalid={Boolean(shown.contract_pin)}
                    data-testid="dependency-pin"
                  >
                    <option value="">Select a contract version…</option>
                    {draft.contract_pin &&
                      !contracts.some((c) => c.id === draft.contract_pin) && (
                        <option value={draft.contract_pin}>
                          {pinnedVersion !== null
                            ? `v${pinnedVersion}`
                            : draft.contract_pin}
                        </option>
                      )}
                    {contracts.map((c) => (
                      <option key={c.id} value={c.id}>
                        v{c.version} ({c.is_active ? 'active' : c.status})
                      </option>
                    ))}
                  </select>
                  {pinnedVersion !== null && (
                    <p
                      className={`mt-1 text-xs ${
                        activeVersion !== null && activeVersion !== pinnedVersion
                          ? 'text-amber-800'
                          : 'text-muted'
                      }`}
                      data-testid="dependency-pin-status"
                    >
                      {pinStatusText(pinnedVersion, activeVersion)}
                    </p>
                  )}
                </div>
              )}
              <FieldError
                id="dependency-error-contract_pin"
                message={shown.contract_pin}
              />
            </fieldset>
          )}

          {serverError && (
            <p
              className="rounded-md border border-red-200 bg-red-50 p-2 text-sm text-red-700"
              data-testid="dependency-server-error"
            >
              {serverError}
            </p>
          )}

          {sealFailure && (
            <div
              className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900"
              role="alert"
              data-testid="dependency-seal-failure"
            >
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <p>
                The dependency is saved, but its separability claim is{' '}
                <strong>not sealed</strong>
                {sealFailure.seal_error ? ` (${sealFailure.seal_error})` : ''}.
                {sealFailure.seal_detail ? ` ${sealFailure.seal_detail}` : ''}{' '}
                The compliance registry does not accept an unsealed claim; the
                seal is retried the next time this dependency is saved.
              </p>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-default p-4">
          {sealFailure ? (
            <button
              type="button"
              onClick={onSaved}
              data-testid="dependency-close-saved"
              className="rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700"
            >
              Close
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={onCancel}
                disabled={saving}
                className="rounded-lg border border-strong px-4 py-2 text-sm font-medium text-secondary hover:bg-surface-alt disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={asyncHandler(submit, 'click')}
                disabled={saving || (touched && hasErrors)}
                data-testid="dependency-save"
                className="flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {saving && <Loader className="h-4 w-4 animate-spin" />}
                {editing ? 'Save' : 'Declare'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Where the separability claim stands: sealed (record and date), or not
 * sealed yet. Editing the rationale is a NEW claim — the old record no
 * longer describes it, so the save seals a fresh one.
 */
/**
 * The delegate outcomes this system is built to handle. Not
 * declared = no check; a declared list makes any other answer from the
 * delegate a finding on this system, even when the delegate's own contract
 * allows it — the composition drifted from what was signed off. An
 * `<outcome>` entry covers all of its variants.
 */
function ExpectedOutcomesFieldset({
  value,
  declared,
  onChange,
  error,
}: {
  value: string[] | null;
  /** Ids the target's catalogue declares (empty when unknown/external). */
  declared: string[];
  onChange: (v: string[] | null) => void;
  error?: string;
}) {
  const [text, setText] = useState(() => (value ?? []).join(', '));
  const declaring = value !== null;
  const list = value ?? [];
  const toggle = (id: string, on: boolean) => {
    const next = on ? [...list, id].sort() : list.filter((v) => v !== id);
    onChange(next);
    setText(next.join(', '));
  };
  const unknown = declared.length
    ? list.filter((id) => !declared.includes(id) && !declared.some((d) => d.startsWith(`${id}.`)))
    : [];
  return (
    <fieldset data-testid="dependency-expected-outcomes">
      <legend className={LABEL}>Outcomes it expects back</legend>
      <div className="flex gap-4">
        <label className="flex items-center gap-2 text-sm text-default">
          <input
            type="radio"
            name="dependency-expected-outcomes-mode"
            checked={!declaring}
            onChange={() => onChange(null)}
            data-testid="dependency-expected-outcomes-off"
          />
          Not declared
        </label>
        <label className="flex items-center gap-2 text-sm text-default">
          <input
            type="radio"
            name="dependency-expected-outcomes-mode"
            checked={declaring}
            onChange={() => onChange(parseExpectedOutcomes(text))}
            data-testid="dependency-expected-outcomes-on"
          />
          Declare the outcomes this system handles
        </label>
      </div>
      <p className="mt-1 text-xs text-muted">
        The gateway checks every answer this dependency gives against its own
        contract. Declaring a list here also makes any answer outside it a
        finding on this system — the composition has drifted from what was
        signed off. An outcome without a variant covers all its variants.
      </p>
      {declaring && (
        <div className="mt-2 space-y-2">
          {declared.length > 0 && (
            <div
              className="flex flex-wrap gap-x-4 gap-y-1"
              data-testid="dependency-expected-outcomes-declared"
            >
              {declared.map((id) => (
                <label
                  key={id}
                  className="flex items-center gap-1.5 text-xs text-default"
                >
                  <input
                    type="checkbox"
                    checked={list.includes(id)}
                    onChange={(e) => toggle(id, e.target.checked)}
                    data-testid={`dependency-expected-outcome-${id}`}
                  />
                  <code className="font-mono">{id}</code>
                </label>
              ))}
            </div>
          )}
          <input
            type="text"
            className={INPUT}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              onChange(parseExpectedOutcomes(e.target.value));
            }}
            placeholder="screening_completed.clear, screening_completed.hit"
            aria-label="Expected outcome ids"
            aria-invalid={Boolean(error)}
            data-testid="dependency-expected-outcomes-input"
          />
          {unknown.length > 0 && (
            <p
              className="text-xs text-amber-700"
              data-testid="dependency-expected-outcomes-unknown"
            >
              Not declared by the dependency: {unknown.join(', ')}. It cannot
              answer with these unless its own contract changes.
            </p>
          )}
          {list.length === 0 && (
            <p className="text-xs text-amber-700">
              An empty list expects nothing: every answer would be a finding.
            </p>
          )}
        </div>
      )}
      <FieldError id="dependency-error-expected_outcomes" message={error} />
    </fieldset>
  );
}

function SeparabilitySealLine({
  edge,
  rationale,
}: {
  edge: DependencyEdge | null;
  rationale: string | null;
}) {
  const recordId = edge?.separability_record_id ?? null;
  const unchanged =
    edge !== null &&
    !edge.contributes_to_decision &&
    (edge.separability_rationale ?? '').trim() === (rationale ?? '').trim();
  if (recordId && unchanged) {
    return (
      <p
        className="mt-1 flex items-start gap-1 text-xs text-secondary"
        data-testid="dependency-separability-sealed"
      >
        <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-green-700" />
        <span>
          Sealed as record <code className="font-mono">{recordId}</code>
          {edge?.separability_sealed_at
            ? ` on ${new Date(edge.separability_sealed_at).toLocaleString()}`
            : ''}
          . Changing the rationale seals a new record.
        </span>
      </p>
    );
  }
  return (
    <p
      className="mt-1 text-xs text-muted"
      data-testid="dependency-separability-unsealed"
    >
      {edge && !edge.contributes_to_decision && !recordId && unchanged
        ? 'Not sealed yet — an earlier seal did not succeed. Saving retries it.'
        : 'The claim is sealed as a declaration record when you save, and shown on both systems.'}
    </p>
  );
}

const SMALL_INPUT =
  'w-full rounded-md border border-strong bg-surface px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500';

/**
 * The root's assumptions about an external agent it cannot test. Mirrors
 * the server's strict schema: the fields below are the only keys sent.
 */
function AssumptionsFieldset({
  form,
  onChange,
  errors,
}: {
  form: AssumptionsForm;
  onChange: (f: AssumptionsForm) => void;
  errors: Partial<Record<keyof AssumptionsForm, string>>;
}) {
  const set = <K extends keyof AssumptionsForm>(k: K, v: AssumptionsForm[K]) =>
    onChange({ ...form, [k]: v });
  const toggleEvidence = (e: (typeof DEPENDENCY_EVIDENCE_PROVIDED)[number]) =>
    set(
      'evidence_provided',
      form.evidence_provided.includes(e)
        ? form.evidence_provided.filter((x) => x !== e)
        : [...form.evidence_provided, e]
    );
  return (
    <fieldset
      className="space-y-3 rounded-md border border-default p-3"
      data-testid="dependency-assumptions"
    >
      <legend className="px-1 text-sm font-medium text-secondary">
        Assumptions
      </legend>
      <p className="text-xs text-muted">
        What this system assumes about an agent it cannot test. Each is stated
        in the contract and shown next to the agent&apos;s boundary metrics;
        leave a field empty to state nothing about it.
      </p>
      <div>
        <label htmlFor="assumption-data-classes" className={LABEL}>
          Data classes shared
        </label>
        <input
          id="assumption-data-classes"
          type="text"
          className={SMALL_INPUT}
          value={form.data_classes_shared}
          onChange={(e) => set('data_classes_shared', e.target.value)}
          placeholder="e.g. personal_data, financial"
          data-testid="assumption-data-classes"
        />
        <p className="mt-1 text-xs text-muted">
          Comma-separated, at most {ASSUMPTION_LIMITS.data_classes_shared}.
        </p>
        <FieldError
          id="assumption-error-data_classes_shared"
          message={errors.data_classes_shared}
        />
      </div>
      <div>
        <label htmlFor="assumption-retention" className={LABEL}>
          Retention at the delegate
        </label>
        <input
          id="assumption-retention"
          type="text"
          className={SMALL_INPUT}
          value={form.retention_at_delegate}
          onChange={(e) => set('retention_at_delegate', e.target.value)}
          placeholder="What it keeps, and for how long"
          data-testid="assumption-retention"
        />
        <FieldError
          id="assumption-error-retention_at_delegate"
          message={errors.retention_at_delegate}
        />
      </div>
      <div>
        <label htmlFor="assumption-oversight" className={LABEL}>
          Human oversight at the delegate
        </label>
        <select
          id="assumption-oversight"
          className={SMALL_INPUT}
          value={form.human_oversight_at_delegate}
          onChange={(e) =>
            set(
              'human_oversight_at_delegate',
              e.target.value as AssumptionsForm['human_oversight_at_delegate']
            )
          }
          data-testid="assumption-oversight"
        >
          <option value="unstated">Not stated</option>
          <option value="yes">Yes — a human oversees its decisions</option>
          <option value="no">No human oversight on its side</option>
        </select>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="assumption-availability" className={LABEL}>
            Expected availability (%)
          </label>
          <input
            id="assumption-availability"
            type="number"
            min={0}
            max={100}
            step="any"
            className={SMALL_INPUT}
            value={form.expected_availability_pct}
            onChange={(e) => set('expected_availability_pct', e.target.value)}
            aria-invalid={Boolean(errors.expected_availability_pct)}
            data-testid="assumption-availability"
          />
          <FieldError
            id="assumption-error-expected_availability_pct"
            message={errors.expected_availability_pct}
          />
        </div>
        <div>
          <label htmlFor="assumption-latency" className={LABEL}>
            Expected p95 latency (ms)
          </label>
          <input
            id="assumption-latency"
            type="number"
            min={0}
            step={1}
            className={SMALL_INPUT}
            value={form.expected_p95_latency_ms}
            onChange={(e) => set('expected_p95_latency_ms', e.target.value)}
            aria-invalid={Boolean(errors.expected_p95_latency_ms)}
            data-testid="assumption-latency"
          />
          <FieldError
            id="assumption-error-expected_p95_latency_ms"
            message={errors.expected_p95_latency_ms}
          />
        </div>
      </div>
      <fieldset>
        <legend className={LABEL}>Evidence it provides</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {DEPENDENCY_EVIDENCE_PROVIDED.map((e) => (
            <label
              key={e}
              className="flex items-center gap-2 text-sm text-default"
            >
              <input
                type="checkbox"
                checked={form.evidence_provided.includes(e)}
                onChange={() => toggleEvidence(e)}
                data-testid={`assumption-evidence-${e}`}
              />
              {DEPENDENCY_EVIDENCE_PROVIDED_LABELS[e]}
            </label>
          ))}
        </div>
        <FieldError
          id="assumption-error-evidence_provided"
          message={errors.evidence_provided}
        />
      </fieldset>
    </fieldset>
  );
}

export interface DeleteDependencyDialogProps {
  aiSystemId: string;
  edge: DependencyEdge;
  onDeleted: () => void;
  onCancel: () => void;
}

export function DeleteDependencyDialog({
  aiSystemId,
  edge,
  onDeleted,
  onCancel,
}: DeleteDependencyDialogProps) {
  const { deleteAISystemDependency } = useGateway();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const target =
    edge.depends_on_system_name ??
    edge.agent_card_name ??
    edge.depends_on_system_id ??
    edge.agent_card_id ??
    edge.id;

  const confirm = async () => {
    setDeleting(true);
    setError(null);
    try {
      await deleteAISystemDependency(aiSystemId, edge.id);
      onDeleted();
    } catch (e) {
      setError(errorMessage(e, 'Failed to remove the dependency'));
      setDeleting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="dependency-delete-title"
      data-testid="dependency-delete-dialog"
    >
      <div className="w-full max-w-md rounded-lg border border-default bg-surface shadow-xl">
        <div className="flex items-start gap-3 border-b border-default p-4">
          <Trash2 className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-600" />
          <h2
            id="dependency-delete-title"
            className="text-base font-medium text-default"
          >
            Remove the dependency on {target}?
          </h2>
        </div>
        <div className="space-y-3 p-4 text-sm text-secondary">
          <p>
            The declaration is removed; the agent grant that permits the call is
            not. If the system keeps calling {target}, it shows as an undeclared
            dependency.
          </p>
          {error && (
            <p
              className="text-sm text-red-600"
              data-testid="dependency-delete-error"
            >
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-default p-4">
          <button
            type="button"
            onClick={onCancel}
            disabled={deleting}
            className="rounded-lg border border-strong px-4 py-2 text-sm font-medium text-secondary hover:bg-surface-alt disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={asyncHandler(confirm, 'click')}
            disabled={deleting}
            data-testid="dependency-delete-confirm"
            className="flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
          >
            {deleting && <Loader className="h-4 w-4 animate-spin" />}
            Remove
          </button>
        </div>
      </div>
    </div>
  );
}

export default DependencyDialog;
