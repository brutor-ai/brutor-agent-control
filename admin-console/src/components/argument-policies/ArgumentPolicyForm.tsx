/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Argument Policy create / edit form. 5xl × 90vh modal with a left
 * sidebar nav (General / Access Control / Rules / Advanced),
 * mirroring GuardrailForm so the two governance forms share one
 * visual vocabulary.
 *
 * Group binding lives on the Access Control tab via the shared
 * HierarchicalGroupSelector — the Resource Group view is read-only.
 */

import React, { useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    AlertCircle,
    FileText,
    Info,
    Loader2,
    Plus,
    Save,
    Settings,
    ShieldCheck,
    Sliders,
    Trash2,
    Users,
    X,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import HierarchicalGroupSelector from '../common/HierarchicalGroupSelector';
import { GroupHierarchyTree } from '../../lib/groupTypes';
import type { MCPServerConfig } from '../../lib/types';
import type { AgentSkillConfig } from '../../lib/agentSkillTypes';
import type {
    Analyzer,
    ArgumentPolicy,
    ArgumentPolicyRule,
    Severity,
    PolicySeverity,
    AnalysisLimitKey,
    SeveritiesBySurface,
    Surface,
} from '../../lib/argumentPolicyTypes';
import {
    ANALYSIS_LIMITS,
    ANALYZER_LABEL,
    PREDICATE_LABEL,
    PREDICATES_BY_ANALYZER,
    PREDICATES_REQUIRING_LIST_VALUES,
    PREDICATES_REQUIRING_VALUES,
    PREDICATES_REQUIRING_FIELD,
    PREDICATES_REQUIRING_NUMERIC_VALUE,
    SEVERITY_LABEL,
    SURFACE_LABEL,
    severitiesOn,
} from '../../lib/argumentPolicyTypes';
import {
    PRESENCE_RULE,
    describeBand,
    isComparisonRule,
    shadowedRules,
    presenceGuardProblems,
    presenceGuardMessage,
    argumentPolicyRefusalText,
    PRESENCE_GUARD_HELP,
} from '../../lib/argumentPolicyBands';
import {
    MIXED_KIND_LABEL,
    OPERAND_KIND_LABEL,
    listLines,
    operandKindOf,
    operandListKindOf,
    toOperand,
    toOperandList,
    type ListOperandKind,
    type OperandKind,
} from '../../lib/argumentPolicyOperands';
import { errorMessage, isRecord } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';


// ============================================================================
// Tab Types — left sidebar nav
// ============================================================================

type TabId = 'general' | 'access' | 'rules' | 'advanced';

const [LIMIT_VALUES, LIMIT_LEVELS, LIMIT_ITEMS] = ANALYSIS_LIMITS;

const POLICY_SEVERITY_LABEL: Record<Severity, string> = {
    deny: 'deny (block with 403)',
    approval_required: 'approval required (hold for a person)',
    warn: 'warn (log-only)',
};

/** A stored severity as a policy's: one of the three, and deny for anything else. */
function asPolicySeverity(stored: unknown): PolicySeverity {
    return stored === 'warn' || stored === 'approval_required' ? stored : 'deny';
}

/** The limits a stored analyzer config states, as their fields show them: '' for one it does not state. */
function storedLimits(config: Record<string, unknown> | null | undefined): Record<AnalysisLimitKey, string> {
    const shown = { max_leaves: '', max_depth: '', max_array_elements: '' };
    for (const { key } of ANALYSIS_LIMITS) {
        const stated = config?.[key];
        if (typeof stated === 'number') shown[key] = String(stated);
    }
    return shown;
}

/** A stored analyzer config without the limits that have a field; null when nothing else is in it. */
function withoutLimits(config: Record<string, unknown> | null | undefined): Record<string, unknown> | null {
    if (!config) return null;
    const rest = Object.fromEntries(
        Object.entries(config).filter(([key, stated]) => !(ANALYSIS_LIMITS.some((limit) => limit.key === key) && typeof stated === 'number')),
    );
    return Object.keys(rest).length > 0 ? rest : null;
}


// ============================================================================
// Props
// ============================================================================

interface Props {
    policy: ArgumentPolicy | null;
    onClose: (saved: boolean) => void;
    /** The severities each surface takes, from the list's answer. */
    severitiesBySurface: SeveritiesBySurface;
}


// ============================================================================
// Defaults
// ============================================================================

const blankRule: ArgumentPolicyRule = {
    deny_if: 'writes',
    values: undefined,
    message: '',
};

const MANUAL_OPTION = '__manual__';

/** The predicates whose operand is one string, number or boolean. */
const EQUALITY_PREDICATES: ReadonlySet<string> = new Set(['field_eq', 'field_ne']);

/**
 * A rule as the form holds it: the rule, and for an equality or membership
 * rule which kind its operand is (text, a number, true or false). The kind
 * is the form's; it is not sent.
 *
 * A stored membership list that holds more than one type is `mixed`: its
 * lines are its values as they are stored (`listLines`), `storedValues` is
 * the list as it was read, and each value keeps its type on a save until
 * the administrator chooses one kind for all of them.
 */
type FormRule = ArgumentPolicyRule & { operandKind?: ListOperandKind; storedValues?: unknown[] };

/** A stored rule, with the kind its operand has. */
function asFormRule(rule: ArgumentPolicyRule): FormRule {
    const predicate = rule.deny_if || rule.allow_only_if || '';
    if (EQUALITY_PREDICATES.has(predicate)) return { ...rule, operandKind: operandKindOf(rule.value) };
    if (PREDICATES_REQUIRING_LIST_VALUES.has(predicate)) {
        const kind = operandListKindOf(rule.values);
        return kind === 'mixed'
            ? { ...rule, operandKind: kind, values: listLines(kind, rule.values), storedValues: rule.values }
            : { ...rule, operandKind: kind };
    }
    return rule;
}

/**
 * The lines of a membership list after "Compare as" is changed to `kind`.
 * Leaving a mixed list, each value is written plainly; back to it, the
 * list is the stored one again. Between two single kinds the lines stay
 * as typed.
 */
function linesAs(rule: FormRule, kind: ListOperandKind): unknown[] | undefined {
    if (kind === 'mixed') return listLines('mixed', rule.storedValues);
    if (rule.operandKind !== 'mixed') return rule.values;
    const typed = toOperandList('mixed', rule.values ?? []);
    return typed.ok ? listLines(kind, typed.value) : rule.values;
}

/** Does this predicate read its operands from the rule's `values` list? */
const takesValueList = (predicate: string): boolean =>
    PREDICATES_REQUIRING_VALUES.has(predicate) || PREDICATES_REQUIRING_LIST_VALUES.has(predicate);

/**
 * A text field that upgrades to a `<select>` when discovered values are
 * available — used for the Tool/Skill name and Argument key fields.
 *
 * When `options` is empty it is a plain text input (the pre-discovery
 * fallback). When options exist it renders a dropdown plus an
 * "Other — enter manually…" escape; a value not in the option set (a
 * hand-typed name loaded from an existing policy) also drops it into
 * manual mode. Callers must re-`key` it when the option set changes so
 * the manual/select decision is recomputed from fresh data.
 */
const ComboField: React.FC<{
    value: string;
    onChange: (v: string) => void;
    options: string[];
    placeholder?: string;
    allowWildcard?: boolean;
    monospace?: boolean;
    required?: boolean;
}> = ({ value, onChange, options, placeholder, allowWildcard, monospace, required }) => {
    const known = new Set<string>(options);
    if (allowWildcard) known.add('*');
    const [manual, setManual] = useState(options.length === 0 || !known.has(value));

    const cls = `w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500${monospace ? ' font-mono' : ''}`;

    if (manual) {
        return (
            <>
                <input
                    type="text"
                    value={value}
                    required={required}
                    placeholder={placeholder}
                    onChange={(e) => onChange(e.target.value)}
                    className={cls}
                />
                {options.length > 0 && (
                    <button
                        type="button"
                        onClick={() => setManual(false)}
                        className="mt-1 text-xs text-primary-600 hover:text-primary-700"
                    >
                        Pick from discovered list
                    </button>
                )}
            </>
        );
    }

    return (
        <select
            value={known.has(value) ? value : ''}
            onChange={(e) => {
                if (e.target.value === MANUAL_OPTION) setManual(true);
                else onChange(e.target.value);
            }}
            className={`${cls} bg-surface`}
        >
            {allowWildcard && <option value="*">* (match any)</option>}
            {options.map((o) => (
                <option key={o} value={o}>{o}</option>
            ))}
            <option value={MANUAL_OPTION}>Other — enter manually…</option>
        </select>
    );
};


// ============================================================================
// Form
// ============================================================================

const ArgumentPolicyForm: React.FC<Props> = ({ policy, onClose, severitiesBySurface }) => {
    const gateway = useGateway() as any;
    const {
        createArgumentPolicy,
        updateArgumentPolicy,
        setArgumentPolicyGroups,
    } = gateway;
    const toast = useToast();

    const isCreate = policy === null;

    const [activeTab, setActiveTab] = useState<TabId>('general');
    const [saving, setSaving] = useState(false);
    const [serverError, setServerError] = useState<string | null>(null);

    // ── General state ────────────────────────────────────────────────
    const [name, setName] = useState(policy?.name ?? '');
    const [description, setDescription] = useState(policy?.description ?? '');
    const [surface, setSurface] = useState<Surface>(policy?.surface ?? 'llm_output_tool_calls');
    const [target, setTarget] = useState(policy?.target ?? '*');
    // No default — the argument key is target-specific (sql for query_sales_db,
    // url for fetch_url, …). A literal default like 'query' here would leak
    // into saves when the operator picks a tool whose inputSchema doesn't have
    // that key and doesn't manually overwrite the pre-filled text input.
    const [argumentKey, setArgumentKey] = useState(policy?.argument_key ?? '');
    const [analyzer, setAnalyzer] = useState<Analyzer>(policy?.analyzer ?? 'sql');
    const [enabled, setEnabled] = useState(policy?.enabled ?? true);
    // A policy's own severity is one of the three; anything else reads as
    // deny, as the gateway reads it.
    const [severity, setSeverity] = useState<PolicySeverity>(asPolicySeverity(policy?.severity));

    // What the policy and its rules may do on the chosen surface. Moving to
    // a surface that holds nothing for a person turns each approval into a
    // refusal: the stricter of the two outcomes that surface has.
    const severities = severitiesOn(surface, severitiesBySurface);
    const chooseSurface = (next: Surface) => {
        setSurface(next);
        if (severitiesOn(next, severitiesBySurface).includes('approval_required')) return;
        setSeverity((current) => (current === 'approval_required' ? 'deny' : current));
        setRules((current) => current.map((rule) => (rule.severity === 'approval_required' ? { ...rule, severity: 'deny' } : rule)));
    };

    // ── Discovery pickers (UI-only — narrow the tool/skill list) ─────
    // The policy data model keys on tool/skill *name* only; the server
    // chosen here is never persisted, it just scopes the dropdown.
    const [mcpServers, setMcpServers] = useState<MCPServerConfig[]>([]);
    const [agentSkills, setAgentSkills] = useState<AgentSkillConfig[]>([]);
    const [selectedServerId, setSelectedServerId] = useState('');

    // ── Rules state ──────────────────────────────────────────────────
    const [rules, setRules] = useState<FormRule[]>(
        policy?.rules?.length ? policy.rules.map(asFormRule) : [blankRule],
    );
    // Ordering IS the band, and nothing errors when it is
    // wrong: a looser rule above a tighter one simply makes the tighter one
    // unreachable. Surfaced per rule and summarised under the list so an
    // operator can read the band back rather than simulating the evaluator.
    const shadowWarnings = shadowedRules(rules);
    // A policy stored with an approval on a surface that has none (the
    // Control Plane refuses it on save) is shown as it is stored: what must
    // be changed is said, and Save stays off until it is.
    const approvalAllowed = severities.includes('approval_required');
    const storedApprovalInPolicy = !approvalAllowed && severity === 'approval_required';
    const storedApprovalInRules = approvalAllowed
        ? []
        : rules.flatMap((rule, index) => (rule.severity === 'approval_required' ? [index + 1] : []));
    const storedApprovalMustChange = storedApprovalInPolicy || storedApprovalInRules.length > 0;
    const whatMustChange = [
        ...(storedApprovalInPolicy ? ['the severity of the policy'] : []),
        ...(storedApprovalInRules.length > 0
            ? [`the outcome of rule${storedApprovalInRules.length === 1 ? '' : 's'} ${storedApprovalInRules.join(', ')}`]
            : []),
    ].join(' and ');

    const bandSummary = describeBand(rules, severity);

    // ── Access Control state (mirrors GuardrailForm pattern) ────────
    const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>(
        policy?.group_ids ?? [],
    );
    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);
    const [groupsLoading, setGroupsLoading] = useState(false);
    const [readFailure, reads] = useReadFailures<'groups' | 'servers' | 'skills'>();
    const [groupsAsked, setGroupsAsked] = useState(0);
    const [discoveryAsked, setDiscoveryAsked] = useState(0);

    // ── Advanced state ───────────────────────────────────────────────
    // The limits of the analysis are keys of the analyzer config with a
    // field each; the JSON holds the rest of the config.
    const [analysisLimits, setAnalysisLimits] = useState<Record<AnalysisLimitKey, string>>(() => storedLimits(policy?.analyzer_config));
    const [analyzerConfigText, setAnalyzerConfigText] = useState(() => {
        const rest = withoutLimits(policy?.analyzer_config);
        return rest ? JSON.stringify(rest, null, 2) : '';
    });

    // ====================================================================
    // Load Resource Group tree (same pattern as LLModelForm / GuardrailForm)
    // ====================================================================

    useEffect(() => {
        // Run-once on mount (matches GuardrailForm). Using `gateway`
        // as a dep loops infinitely because useGateway() returns a
        // new object reference on every render, which re-fires the
        // effect, which sets state, which re-renders.
        const fetchGroups = async () => {
            if (!gateway?.gatewayClient) return;
            setGroupsLoading(true);
            try {
                const response = await gateway.gatewayClient.fetchGroupHierarchyTree();
                setGroupTree(response.tree || []);
                reads.loaded('groups');
            } catch (err) {
                reads.failed('groups', 'Failed to load the resource groups', err);
            } finally {
                setGroupsLoading(false);
            }
        };
        runBackground(fetchGroups(), 'groups');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [groupsAsked]);

    // ====================================================================
    // Load discovered MCP servers + Agent Skills (powers the dropdowns).
    // Best-effort: any failure leaves the fields as free-text inputs.
    // ====================================================================

    useEffect(() => {
        const fetchDiscovery = async () => {
            if (!gateway?.gatewayClient) return;
            try {
                const servers = await gateway.gatewayClient.fetchServers();
                setMcpServers(Array.isArray(servers) ? servers : []);
                reads.loaded('servers');
            } catch (err) {
                reads.failed('servers', 'Failed to load the MCP servers; a tool name can still be typed', err);
            }
            try {
                const resp = await gateway.gatewayClient.listAgentSkills();
                setAgentSkills(Object.values(resp?.skills ?? {}));
                reads.loaded('skills');
            } catch (err) {
                reads.failed('skills', 'Failed to load the agent skills; a skill name can still be typed', err);
            }
        };
        runBackground(fetchDiscovery(), 'discovery');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [discoveryAsked]);

    // ====================================================================
    // On edit-load, best-effort match the stored tool name back to the
    // server that exposes it, so the tool dropdown is pre-scoped.
    // ====================================================================

    useEffect(() => {
        if (isCreate || surface !== 'mcp_input') return;
        if (selectedServerId || !target || target === '*' || mcpServers.length === 0) return;
        const match = mcpServers.find((s) =>
            (s.capabilities?.tools ?? []).some((t: any) => t?.name === target),
        );
        if (match) setSelectedServerId(match.id);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [mcpServers]);

    // ====================================================================
    // Reset rules when analyzer changes (keeps predicates valid)
    // ====================================================================

    useEffect(() => {
        const allowed = PREDICATES_BY_ANALYZER[analyzer];
        setRules((rs) => rs.map((r) => {
            const current = r.deny_if ?? r.allow_only_if ?? '';
            if (allowed.includes(current)) return r;
            return {
                ...r,
                deny_if: allowed[0],
                allow_only_if: undefined,
                values: undefined,
            };
        }));
    }, [analyzer]);

    // ====================================================================
    // Submit
    // ====================================================================

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (storedApprovalMustChange) return;
        setServerError(null);

        if (!name.trim()) {
            setActiveTab('general');
            toast.error('Validation', 'Name is required');
            return;
        }
        // Checked here because the footer's submit button sits outside the
        // <form>, so the browser's own `required` check never runs.
        if (!argumentKey.trim()) {
            setActiveTab('general');
            toast.error('Validation', 'Argument key is required');
            return;
        }
        if (rules.length === 0) {
            setActiveTab('rules');
            toast.error('Validation', 'At least one rule is required');
            return;
        }
        for (const [i, r] of rules.entries()) {
            const pred = r.deny_if || r.allow_only_if || '';
            if (!pred) {
                setActiveTab('rules');
                toast.error('Validation', `Rule #${i + 1} has no predicate`);
                return;
            }
            const cleanValues = (r.values ?? []).map((v) => String(v).trim()).filter(Boolean);
            if (takesValueList(pred) && cleanValues.length === 0) {
                setActiveTab('rules');
                toast.error('Validation', `Rule #${i + 1} (${pred}) requires values`);
                return;
            }
            if (PREDICATES_REQUIRING_FIELD.has(pred) && !r.field?.trim()) {
                setActiveTab('rules');
                toast.error('Validation', `Rule #${i + 1} (${pred}) needs a field path`);
                return;
            }
            if (PREDICATES_REQUIRING_NUMERIC_VALUE.has(pred)) {
                const n = Number(r.value);
                if (r.value === undefined || r.value === '' || Number.isNaN(n)) {
                    setActiveTab('rules');
                    toast.error('Validation', `Rule #${i + 1} (${pred}) needs a numeric value`);
                    return;
                }
            } else if (
                PREDICATES_REQUIRING_FIELD.has(pred) &&
                !takesValueList(pred) &&
                (r.value === undefined || r.value === '')
            ) {
                setActiveTab('rules');
                toast.error('Validation', `Rule #${i + 1} (${pred}) needs a value`);
                return;
            }
        }

        // The omit-the-field bypass. A threshold on a field the caller can
        // simply leave out never fires, so the API refuses to store one. Catch
        // it here so an operator never meets that refusal by accident.
        let parsedConfigForGuard: Record<string, unknown> | null = null;
        if (analyzerConfigText.trim()) {
            try {
                parsedConfigForGuard = JSON.parse(analyzerConfigText);
            } catch {
                /* reported below by the analyzer-config parse */
            }
        }
        const unguarded = presenceGuardProblems(rules, parsedConfigForGuard, severity);
        if (unguarded.length > 0) {
            setActiveTab('rules');
            toast.error('Validation', presenceGuardMessage(unguarded));
            return;
        }
        if (rules.some(isComparisonRule) && argumentKey.trim() !== '*') {
            setActiveTab('general');
            toast.error(
                'Validation',
                "Comparison rules address fields inside the whole argument object, " +
                    "so the argument key must be '*'.",
            );
            return;
        }

        let analyzerConfig: Record<string, unknown> | null = null;
        if (analyzerConfigText.trim()) {
            let parsed: unknown;
            try {
                parsed = JSON.parse(analyzerConfigText);
            } catch {
                setActiveTab('advanced');
                toast.error('Validation', 'Analyzer config is not valid JSON');
                return;
            }
            if (!isRecord(parsed)) {
                setActiveTab('advanced');
                toast.error('Validation', 'Analyzer config must be a JSON object');
                return;
            }
            analyzerConfig = parsed;
        }
        if (analyzer === 'json') {
            for (const { key, label, most } of ANALYSIS_LIMITS) {
                const typed = analysisLimits[key].trim();
                if (!typed) continue;
                const refuse = (why: string) => {
                    setActiveTab('advanced');
                    toast.error('Validation', why);
                };
                if (analyzerConfig && key in analyzerConfig) {
                    return refuse(`${key} is set twice: in its field and in the analyzer config. Set it in one place.`);
                }
                const stated = Number(typed);
                if (!/^\d+$/.test(typed) || stated < 1 || stated > most) {
                    return refuse(`${label} must be a whole number from 1 to ${most}`);
                }
                analyzerConfig = { ...(analyzerConfig ?? {}), [key]: stated };
            }
        }

        // The rules as they are sent. The operand of an equality or
        // membership rule goes as the kind chosen for it (text, a number,
        // true or false): the gateway compares a field with an operand of
        // the field's own type.
        const wireRules: ArgumentPolicyRule[] = [];
        for (const [index, r] of rules.entries()) {
            const pred = r.deny_if || r.allow_only_if || '';
            const kind = r.operandKind ?? 'text';
            const refuse = (why: string) => {
                setActiveTab('rules');
                toast.error('Validation', `Rule ${index + 1}: ${why}`);
            };
            let values: unknown[];
            if (PREDICATES_REQUIRING_LIST_VALUES.has(pred)) {
                const options = toOperandList(kind, r.values ?? []);
                if (!options.ok) return refuse(options.error);
                values = options.value;
            } else {
                values = (r.values ?? []).map((v) => String(v).trim()).filter(Boolean);
            }
            let value: unknown;
            if (r.value === undefined || r.value === '') {
                value = undefined;
            } else if (PREDICATES_REQUIRING_NUMERIC_VALUE.has(pred)) {
                value = Number(r.value);
            } else if (EQUALITY_PREDICATES.has(pred)) {
                const operand = toOperand(kind === 'mixed' ? 'text' : kind, r.value);
                if (!operand.ok) return refuse(operand.error);
                value = operand.value;
            } else {
                value = r.value;
            }
            wireRules.push({
                deny_if: r.deny_if || undefined,
                allow_only_if: r.allow_only_if || undefined,
                values: values.length > 0 ? values : undefined,
                field: r.field?.trim() || undefined,
                value,
                severity: r.severity || undefined,
                message: r.message?.trim() || undefined,
            });
        }

        const body = {
            name: name.trim(),
            description: description.trim() || null,
            surface,
            target: target.trim() || '*',
            argument_key: argumentKey.trim(),
            analyzer,
            analyzer_config: analyzerConfig,
            rules: wireRules,
            enabled,
            severity,
        };

        setSaving(true);
        try {
            if (isCreate) {
                await createArgumentPolicy({ ...body, group_ids: selectedGroupIds });
                toast.success('Policy Created', `"${name.trim()}" has been created.`);
            } else {
                await updateArgumentPolicy(policy.id, body);
                // Group assignment is a separate endpoint so we can surface
                // tenant-mismatch / missing-group errors clearly.
                await setArgumentPolicyGroups(policy.id, selectedGroupIds);
                toast.success('Policy Updated', `"${name.trim()}" has been saved.`);
            }
            onClose(true);
        } catch (err) {
            const msg = argumentPolicyRefusalText(err) ?? errorMessage(err) ?? 'Failed to save argument policy';
            setServerError(msg);
            toast.error('Save Failed', msg);
        } finally {
            setSaving(false);
        }
    };

    // ====================================================================
    // Left Sidebar NavItem (matches GuardrailForm / LLModelForm pattern)
    // ====================================================================

    const NavItem: React.FC<{ id: TabId; label: string; icon: React.ReactNode }> = ({
        id, label, icon,
    }) => (
        <button
            type="button"
            onClick={() => setActiveTab(id)}
            className={`flex items-center space-x-2.5 w-full px-3 py-2.5 text-sm font-medium rounded-lg transition-colors text-left ${
                activeTab === id
                    ? 'bg-primary-50 text-primary-700 border-l-3 border-primary-500'
                    : 'text-secondary hover:text-default hover:bg-surface-alt'
            }`}
        >
            {icon}
            <span>{label}</span>
        </button>
    );

    const allowedPredicates = PREDICATES_BY_ANALYZER[analyzer];

    // ── Discovery-driven dropdown options ────────────────────────────
    const selectedServer = mcpServers.find((s) => s.id === selectedServerId);

    // Tool / Skill name choices: MCP server's discovered tools, the
    // tenant's agent skills, or none (→ free-text) for LLM tool_calls.
    const toolOptions: string[] =
        surface === 'mcp_input'
            ? (selectedServer?.capabilities?.tools ?? [])
                  .map((t: any) => t?.name)
                  .filter((n: any): n is string => typeof n === 'string' && n.length > 0)
            : surface === 'skill_input'
              ? agentSkills.map((s) => s.name).filter(Boolean)
              : [];

    // Argument key choices: the selected MCP tool's inputSchema keys.
    const argKeyOptions: string[] = (() => {
        if (surface !== 'mcp_input' || !selectedServer) return [];
        const tool = (selectedServer.capabilities?.tools ?? []).find(
            (t: any) => t?.name === target,
        );
        const props = tool?.inputSchema?.properties;
        return props && typeof props === 'object' ? Object.keys(props) : [];
    })();

    // Keep argumentKey honest when the operator switches tools.
    //   - If the discovered list has exactly one key, auto-pick it (the
    //     common case — every tool in the demo MCP has a single argument).
    //   - If the current key isn't on the discovered list AND the list is
    //     non-empty, clear it so the operator must pick from the dropdown
    //     rather than save a stale value from the previously selected tool.
    useEffect(() => {
        if (argKeyOptions.length === 0) return;
        if (argKeyOptions.length === 1 && argumentKey !== argKeyOptions[0]) {
            setArgumentKey(argKeyOptions[0]);
            return;
        }
        if (!argKeyOptions.includes(argumentKey)) {
            setArgumentKey('');
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [argKeyOptions.join('|')]);

    // ====================================================================
    // Render
    // ====================================================================

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-5xl h-[90vh] flex flex-col">

                {/* ─── Header ─── */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default flex-shrink-0">
                    <div className="flex items-center space-x-3">
                        <ShieldCheck className="w-6 h-6 text-primary-600" />
                        <h2 className="text-xl font-semibold text-default">
                            {isCreate ? 'New Argument Policy' : `Edit Argument Policy — ${policy.name}`}
                        </h2>
                    </div>
                    <button
                        onClick={() => onClose(false)}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* ─── Sidebar + Content ─── */}
                <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="flex-1 flex overflow-hidden min-h-0 min-w-0">

                    {/* Left Sidebar */}
                    <nav className="w-48 flex-shrink-0 border-r border-default bg-surface-alt py-3 px-2 space-y-0.5 overflow-y-auto">
                        <NavItem id="general"  label="General"        icon={<FileText className="w-4 h-4" />} />
                        <NavItem id="access"   label="Access Control" icon={<Users className="w-4 h-4" />} />
                        <NavItem id="rules"    label="Rules"          icon={<Sliders className="w-4 h-4" />} />
                        <NavItem id="advanced" label="Advanced"       icon={<Settings className="w-4 h-4" />} />
                    </nav>

                    {/* Right Content */}
                    <div className="scroll-region">
                        <div className="p-6">

                            {/* Server Error */}
                            {storedApprovalMustChange && (
                                <div
                                    role="alert"
                                    data-testid="stored-approval-must-change"
                                    className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800"
                                >
                                    {`This policy is stored with Approval required on the surface of model tool calls, which the Control Plane refuses: the gateway holds nothing for a person there. Change ${whatMustChange} to save it.`}
                                </div>
                            )}
                            {serverError && (
                                <div className="mb-6 bg-red-50 border border-red-300 rounded-lg p-4 flex items-start space-x-3">
                                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                                    <div className="flex-1 min-h-0 min-w-0">
                                        <p className="text-sm font-medium text-red-800 mb-1">
                                            {isCreate ? 'Failed to create policy' : 'Failed to update policy'}
                                        </p>
                                        <pre className="text-sm text-red-700 whitespace-pre-wrap font-mono">{serverError}</pre>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => setServerError(null)}
                                        className="text-red-400 hover:text-red-600"
                                    >
                                        <X className="w-4 h-4" />
                                    </button>
                                </div>
                            )}

                            {/* ─── GENERAL TAB ─── */}
                            {activeTab === 'general' && (
                                <div className="space-y-4">
                                    {/* Name */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Name <span className="text-red-500">*</span>
                                        </label>
                                        <input
                                            type="text"
                                            value={name}
                                            onChange={(e) => setName(e.target.value)}
                                            placeholder="e.g. pg-readonly-analytics"
                                            maxLength={255}
                                            required
                                            className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500"
                                        />
                                    </div>

                                    {/* Description */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Description
                                        </label>
                                        <input
                                            type="text"
                                            value={description ?? ''}
                                            onChange={(e) => setDescription(e.target.value)}
                                            placeholder="Block writes / sensitive tables on postgres tool"
                                            maxLength={1024}
                                            className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500"
                                        />
                                    </div>

                                    {/* Surface */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Surface
                                        </label>
                                        <select
                                            value={surface}
                                            onChange={(e) => chooseSurface(e.target.value as Surface)}
                                            className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                        >
                                            {(Object.keys(SURFACE_LABEL) as Surface[]).map((s) => (
                                                <option key={s} value={s}>{SURFACE_LABEL[s]}</option>
                                            ))}
                                        </select>
                                        <p className="mt-1 text-xs text-muted">
                                            Where the check runs. LLM tool_calls fires on model output; MCP inbound
                                            fires on incoming <span className="font-mono">tools/call</span>; Skill input
                                            fires before skill execution.
                                        </p>
                                    </div>

                                    {/* MCP Server picker — narrows the tool list below.
                                        UI-only: the policy matches by tool name, the
                                        chosen server is never persisted. */}
                                    {surface === 'mcp_input' && (
                                        <ReadFailureNotice compact failure={readFailure('servers')} onRetry={() => setDiscoveryAsked((n) => n + 1)} />
                                    )}
                                    {surface === 'skill_input' && (
                                        <ReadFailureNotice compact failure={readFailure('skills')} onRetry={() => setDiscoveryAsked((n) => n + 1)} />
                                    )}
                                    {surface === 'mcp_input' && (
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                MCP Server
                                            </label>
                                            <select
                                                value={selectedServerId}
                                                onChange={(e) => setSelectedServerId(e.target.value)}
                                                className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                            >
                                                <option value="">— Select a server to list its tools —</option>
                                                {mcpServers.map((s) => (
                                                    <option key={s.id} value={s.id}>{s.name}</option>
                                                ))}
                                            </select>
                                            <p className="mt-1 text-xs text-muted">
                                                {selectedServer && toolOptions.length === 0
                                                    ? 'No tools discovered for this server yet — enter the tool name manually below.'
                                                    : 'Scopes the tool list below. Not saved on the policy.'}
                                            </p>
                                        </div>
                                    )}

                                    {/* Target + Argument */}
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                Tool / Skill name
                                            </label>
                                            <ComboField
                                                key={`target-${surface}-${selectedServerId}-${toolOptions.length}`}
                                                value={target}
                                                onChange={setTarget}
                                                options={toolOptions}
                                                placeholder="postgres_query (or *)"
                                                allowWildcard
                                                monospace
                                            />
                                            <p className="mt-1 text-xs text-muted">
                                                Use <span className="font-mono">*</span> to match any.
                                                {surface === 'mcp_input' && !selectedServerId &&
                                                    ' Select a server above to pick from its tools.'}
                                            </p>
                                        </div>
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                Argument key <span className="text-red-500">*</span>
                                            </label>
                                            <ComboField
                                                key={`argkey-${argKeyOptions.join('|')}`}
                                                value={argumentKey}
                                                onChange={setArgumentKey}
                                                options={argKeyOptions}
                                                placeholder="e.g. sql, url, command"
                                                monospace
                                                required
                                            />
                                            <p className="mt-1 text-xs text-muted">JSON key inside the tool's arguments object.</p>
                                        </div>
                                    </div>

                                    {/* Analyzer */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Analyzer
                                        </label>
                                        <select
                                            value={analyzer}
                                            onChange={(e) => setAnalyzer(e.target.value as Analyzer)}
                                            className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                        >
                                            {(Object.keys(ANALYZER_LABEL) as Analyzer[]).map((a) => (
                                                <option key={a} value={a}>{ANALYZER_LABEL[a]}</option>
                                            ))}
                                        </select>
                                        <p className="mt-1 text-xs text-muted">
                                            Picks the parser. Switching the analyzer filters the predicates available on the Rules tab.
                                        </p>
                                    </div>

                                    {/* Enabled + Severity */}
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2">
                                        <label className="flex items-center space-x-2 text-sm">
                                            <input
                                                type="checkbox"
                                                checked={enabled}
                                                onChange={(e) => setEnabled(e.target.checked)}
                                                className="w-4 h-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                                            />
                                            <span className="font-medium text-secondary">Enabled</span>
                                        </label>
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                Severity
                                            </label>
                                            <select
                                                value={severity}
                                                onChange={(e) => setSeverity(asPolicySeverity(e.target.value))}
                                                className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                            >
                                                {storedApprovalInPolicy && (
                                                    <option value="approval_required">
                                                        approval required (stored: not available for model tool calls)
                                                    </option>
                                                )}
                                                {severities.map((sv) => (
                                                    <option key={sv} value={sv}>{POLICY_SEVERITY_LABEL[sv]}</option>
                                                ))}
                                            </select>
                                            {!severities.includes('approval_required') && (
                                                <p className="text-xs text-muted mt-1" data-testid="no-approval-on-surface">
                                                    Approvals are not available for model tool calls: the gateway holds nothing for a
                                                    person on that surface. To hold a call for a person, put the policy on the
                                                    tool&apos;s own surface (MCP inbound or Skill input).
                                                </p>
                                            )}
                                            <p className="text-xs text-muted mt-1" data-testid="policy-severity-help">
                                                What a rule does when it has no outcome of its own: it inherits this one. A
                                                rule that guards a compared field must still deny: when this is not Deny,
                                                give that rule the outcome Deny itself.
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* ─── ACCESS CONTROL TAB ─── */}
                            {activeTab === 'access' && (
                                <div className="space-y-4">
                                    {/* Info Box */}
                                    <div className="bg-primary-50 border border-primary-200 rounded-lg p-4 flex items-start space-x-3">
                                        <Info className="w-5 h-5 text-primary-600 flex-shrink-0 mt-0.5" />
                                        <div className="flex-1 min-h-0 min-w-0">
                                            <p className="text-sm text-primary-900">
                                                <strong>Control which Resource Groups this argument policy applies to.</strong>{' '}
                                                Bindings are additive: a group inherits policies bound to any of its ancestors,
                                                so attaching to the root group covers the whole tree.
                                            </p>
                                        </div>
                                    </div>

                                    {/* Group Selector */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-2">
                                            Resource Groups
                                        </label>

                                        {groupsLoading ? (
                                            <div className="text-sm text-muted py-4 text-center">
                                                Loading groups…
                                            </div>
                                        ) : readFailure('groups') && groupTree.length === 0 ? (
                                            <ReadFailureNotice compact failure={readFailure('groups')} onRetry={() => setGroupsAsked((n) => n + 1)} />
                                        ) : groupTree.length === 0 ? (
                                            <div className="text-sm text-muted py-4 px-3 bg-surface-alt rounded-lg border border-default">
                                                <Info className="w-4 h-4 inline mr-1" />
                                                No Resource Groups defined yet. Create groups in the Groups section
                                                to scope argument policies per assistant, app, or agent.
                                            </div>
                                        ) : (
                                            <>
                                                <HierarchicalGroupSelector
                                                    groupTree={groupTree}
                                                    selectedGroupIds={selectedGroupIds}
                                                    onSelectionChange={setSelectedGroupIds}
                                                />

                                                <div className="mt-2 text-sm text-secondary flex items-center">
                                                    <Users className="w-4 h-4 mr-1" />
                                                    <span>
                                                        {selectedGroupIds.length === 0
                                                            ? 'No groups selected — policy is defined but unbound'
                                                            : `${selectedGroupIds.length} group${selectedGroupIds.length !== 1 ? 's' : ''} selected`}
                                                    </span>
                                                </div>

                                                <div className="mt-2 text-xs text-muted">
                                                    <Info className="w-3 h-3 inline mr-1" />
                                                    A policy with no group bindings is inert. To make it apply, bind it
                                                    here and the proxy will pick it up on every request the group serves.
                                                </div>
                                            </>
                                        )}
                                    </div>
                                </div>
                            )}

                            {/* ─── RULES TAB ─── */}
                            {activeTab === 'rules' && (
                                <div className="space-y-4">
                                    <div className="bg-primary-50 border border-primary-200 rounded-lg p-4 flex items-start space-x-3">
                                        <Info className="w-5 h-5 text-primary-600 flex-shrink-0 mt-0.5" />
                                        <div className="flex-1 text-sm text-primary-900 min-h-0 min-w-0">
                                            <strong>One predicate per rule.</strong>{' '}
                                            <em>Deny if</em> blocks when the predicate is true; <em>allow only if</em>{' '}
                                            blocks when the predicate is false. The evaluator stops at the first
                                            violation, so order rules from most-specific to most-general.
                                            Predicates are filtered to those valid for the analyzer chosen on General.
                                        </div>
                                    </div>

                                    {analyzer === 'json' && (
                                        <div
                                            data-testid="argument-policy-rule-help"
                                            className="bg-surface-alt border border-default rounded-lg p-4 text-xs text-secondary space-y-2"
                                        >
                                            <p>
                                                <strong>A compared field needs a guard.</strong> {PRESENCE_GUARD_HELP}
                                            </p>
                                            <p>
                                                <strong>What cannot be compared is refused.</strong> A field that is present with a value
                                                the rule cannot compare (null, a list, an object, text where a number is expected) is
                                                refused, not let through.
                                            </p>
                                            <p>
                                                <strong>Limits of the analysis.</strong> Unless this policy sets its own limits (Advanced),
                                                the gateway reads at most {LIMIT_VALUES.byDefault} values, {LIMIT_LEVELS.byDefault} levels of
                                                nesting and {LIMIT_ITEMS.byDefault} items of a list in the arguments of one call. A call with
                                                more is refused when this policy applies to it, whatever the rules say.
                                            </p>
                                        </div>
                                    )}

                                    <div className="flex items-center justify-between">
                                        <span className="text-sm font-medium text-secondary">
                                            {rules.length} rule{rules.length !== 1 ? 's' : ''}
                                        </span>
                                        <button
                                            type="button"
                                            onClick={() => setRules((rs) => [
                                                ...rs,
                                                {
                                                    deny_if: allowedPredicates[0],
                                                    allow_only_if: undefined,
                                                    values: undefined,
                                                    message: '',
                                                },
                                            ])}
                                            className="flex items-center space-x-1 px-3 py-1.5 text-xs font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                                        >
                                            <Plus className="w-3 h-3" />
                                            <span>Add rule</span>
                                        </button>
                                    </div>

                                    {bandSummary.length > 0 && (
                                        <div
                                            className="mb-3 p-3 rounded-lg border border-default bg-surface-alt text-xs"
                                            data-testid="band-summary"
                                        >
                                            <p className="font-medium text-default mb-1">
                                                This policy, in evaluation order
                                            </p>
                                            <ol className="list-decimal pl-5 space-y-0.5 text-secondary">
                                                {bandSummary.map((line, idx) => (
                                                    <li key={idx}>{line}</li>
                                                ))}
                                            </ol>
                                            <p className="text-muted mt-1">
                                                The first matching rule decides. Put the tightest threshold first.
                                            </p>
                                        </div>
                                    )}

                                    <div className="space-y-3">
                                        {rules.map((r, i) => {
                                            const mode = r.deny_if ? 'deny_if' : 'allow_only_if';
                                            const predicate = r.deny_if ?? r.allow_only_if ?? '';
                                            const needsValues = takesValueList(predicate);
                                            const isComparison = PREDICATES_REQUIRING_FIELD.has(predicate);
                                            const needsNumericValue = PREDICATES_REQUIRING_NUMERIC_VALUE.has(predicate);
                                            const operandKind: ListOperandKind = r.operandKind ?? 'text';
                                            const shadow = shadowWarnings.find((w) => w.index === i);
                                            return (
                                                <div
                                                    key={i}
                                                    className="p-4 border border-default rounded-lg bg-surface-alt space-y-3"
                                                >
                                                    <div className="flex items-center justify-between">
                                                        <span className={TABLE_TH_TYPE}>
                                                            Rule #{i + 1}
                                                        </span>
                                                        <button
                                                            type="button"
                                                            onClick={() => setRules((rs) => rs.filter((_, idx) => idx !== i))}
                                                            disabled={rules.length === 1}
                                                            className="p-1 text-muted hover:text-red-600 hover:bg-red-50 rounded disabled:opacity-30 disabled:cursor-not-allowed"
                                                            title={rules.length === 1 ? 'A policy needs at least one rule' : 'Remove rule'}
                                                        >
                                                            <Trash2 className="w-4 h-4" />
                                                        </button>
                                                    </div>

                                                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                                                        <div>
                                                            <label className="block text-xs font-medium text-secondary mb-1">
                                                                Mode
                                                            </label>
                                                            <select
                                                                value={mode}
                                                                onChange={(e) => {
                                                                    const m = e.target.value as 'deny_if' | 'allow_only_if';
                                                                    setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                        ...rr,
                                                                        deny_if: m === 'deny_if' ? predicate : undefined,
                                                                        allow_only_if: m === 'allow_only_if' ? predicate : undefined,
                                                                    } : rr));
                                                                }}
                                                                className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                            >
                                                                <option value="deny_if">deny if</option>
                                                                <option value="allow_only_if">allow only if</option>
                                                            </select>
                                                        </div>
                                                        <div className="sm:col-span-2">
                                                            <label className="block text-xs font-medium text-secondary mb-1">
                                                                Predicate
                                                            </label>
                                                            <select
                                                                value={predicate}
                                                                onChange={(e) => {
                                                                    const p = e.target.value;
                                                                    setRules((rs) => {
                                                                        const next = rs.map((rr, idx) => idx === i ? {
                                                                            ...rr,
                                                                            deny_if: mode === 'deny_if' ? p : undefined,
                                                                            allow_only_if: mode === 'allow_only_if' ? p : undefined,
                                                                            // A list of more than one type belongs to a membership
                                                                            // rule: any other predicate takes its lines plainly.
                                                                            ...(rr.operandKind === 'mixed' && !PREDICATES_REQUIRING_LIST_VALUES.has(p)
                                                                                ? { operandKind: 'text' as const, storedValues: undefined, values: takesValueList(p) ? linesAs(rr, 'text') : undefined }
                                                                                : { values: takesValueList(p) ? rr.values : undefined }),
                                                                        } : rr);
                                                                        // Seed the presence
                                                                        // rule the first time a comparison is added.
                                                                        // A threshold on a field the caller can omit
                                                                        // never fires, and the API refuses to store
                                                                        // one — so put the guard in before the
                                                                        // operator can hit that refusal.
                                                                        const needsSeed =
                                                                            PREDICATES_REQUIRING_FIELD.has(p) &&
                                                                            !next.some((rr) => rr.deny_if === 'schema_invalid');
                                                                        return needsSeed ? [PRESENCE_RULE, ...next] : next;
                                                                    });
                                                                }}
                                                                className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                            >
                                                                {allowedPredicates.map((p) => (
                                                                    <option key={p} value={p}>
                                                                        {PREDICATE_LABEL[p] ?? p}
                                                                    </option>
                                                                ))}
                                                            </select>
                                                        </div>
                                                    </div>

                                                    {isComparison && (
                                                        <div
                                                            className="grid grid-cols-1 sm:grid-cols-2 gap-3"
                                                            data-testid={`rule-comparison-${i}`}
                                                        >
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">
                                                                    Field (dotted path)
                                                                </label>
                                                                <input
                                                                    type="text"
                                                                    value={r.field ?? ''}
                                                                    onChange={(e) => setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                        ...rr,
                                                                        field: e.target.value,
                                                                    } : rr))}
                                                                    placeholder="amount"
                                                                    data-testid={`rule-field-${i}`}
                                                                    className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono bg-surface"
                                                                />
                                                                <p className="text-[11px] text-muted mt-0.5">
                                                                    Inside the argument object, e.g. <code>amount</code> or <code>transfer.amount</code>.
                                                                    The gateway reads the first {LIMIT_ITEMS.byDefault} items of a list and {LIMIT_LEVELS.byDefault} levels
                                                                    of nesting, or this policy&apos;s own limits (Advanced). A path beyond them is
                                                                    never reached, and the call is refused.
                                                                </p>
                                                            </div>
                                                            {needsNumericValue && (
                                                                <div>
                                                                    <label className="block text-xs font-medium text-secondary mb-1">
                                                                        Threshold
                                                                    </label>
                                                                    <input
                                                                        type="number"
                                                                        value={r.value === undefined ? '' : String(r.value)}
                                                                        onChange={(e) => setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                            ...rr,
                                                                            value: e.target.value,
                                                                        } : rr))}
                                                                        data-testid={`rule-value-${i}`}
                                                                        className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                                    />
                                                                </div>
                                                            )}
                                                            {!needsNumericValue && !needsValues && (
                                                                <div>
                                                                    <label className="block text-xs font-medium text-secondary mb-1">
                                                                        Value
                                                                    </label>
                                                                    {operandKind === 'boolean' ? (
                                                                        <select
                                                                            value={String(r.value) === 'false' ? 'false' : 'true'}
                                                                            onChange={(e) => setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                                ...rr,
                                                                                value: e.target.value,
                                                                            } : rr))}
                                                                            data-testid={`rule-value-${i}`}
                                                                            className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                                        >
                                                                            <option value="true">true</option>
                                                                            <option value="false">false</option>
                                                                        </select>
                                                                    ) : (
                                                                        <input
                                                                            type={operandKind === 'number' ? 'number' : 'text'}
                                                                            value={r.value === undefined ? '' : String(r.value)}
                                                                            onChange={(e) => setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                                ...rr,
                                                                                value: e.target.value,
                                                                            } : rr))}
                                                                            data-testid={`rule-value-${i}`}
                                                                            className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                                        />
                                                                    )}
                                                                </div>
                                                            )}
                                                            {!needsNumericValue && (
                                                                <div className="sm:col-span-2">
                                                                    <label className="block text-xs font-medium text-secondary mb-1">
                                                                        Compare as
                                                                    </label>
                                                                    <select
                                                                        value={operandKind}
                                                                        onChange={(e) => {
                                                                            const kind = e.target.value as ListOperandKind;
                                                                            setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                                ...rr,
                                                                                operandKind: kind,
                                                                                values: needsValues ? linesAs(rr, kind) : rr.values,
                                                                                // True or false is chosen, not typed: start from true.
                                                                                value: kind === 'boolean' && !needsValues && String(rr.value) !== 'false' ? 'true' : rr.value,
                                                                            } : rr));
                                                                        }}
                                                                        data-testid={`rule-operand-kind-${i}`}
                                                                        className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                                    >
                                                                        {(Object.keys(OPERAND_KIND_LABEL) as OperandKind[]).map((kind) => (
                                                                            <option key={kind} value={kind}>
                                                                                {OPERAND_KIND_LABEL[kind]}
                                                                            </option>
                                                                        ))}
                                                                        {r.storedValues && <option value="mixed">{MIXED_KIND_LABEL}</option>}
                                                                    </select>
                                                                    {operandKind === 'mixed' && (
                                                                        <p className="text-[11px] text-amber-700 mt-0.5" data-testid={`rule-mixed-note-${i}`}>
                                                                            This list holds values of more than one type. Each line is one value as
                                                                            it is stored: text in double quotes, a number, or true or false. To make
                                                                            every value one type, choose it under Compare as.
                                                                        </p>
                                                                    )}
                                                                    <p className="text-[11px] text-muted mt-0.5">
                                                                        The gateway compares a field with a value of the field&apos;s own type. A field
                                                                        that holds another type cannot be decided, and the call is refused.
                                                                    </p>
                                                                </div>
                                                            )}
                                                        </div>
                                                    )}

                                                    <div>
                                                        <label className="block text-xs font-medium text-secondary mb-1">
                                                            Outcome for this rule
                                                        </label>
                                                        <select
                                                            value={r.severity ?? ''}
                                                            onChange={(e) => setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                ...rr,
                                                                severity: (e.target.value || undefined) as Severity | undefined,
                                                            } : rr))}
                                                            data-testid={`rule-severity-${i}`}
                                                            className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                        >
                                                            <option value="">Use the policy default</option>
                                                            {!approvalAllowed && r.severity === 'approval_required' && (
                                                                <option value="approval_required">
                                                                    Approval required (stored: not available for model tool calls)
                                                                </option>
                                                            )}
                                                            {severities.map((sv) => (
                                                                <option key={sv} value={sv}>
                                                                    {SEVERITY_LABEL[sv]}
                                                                </option>
                                                            ))}
                                                        </select>
                                                    </div>

                                                    {needsValues && (
                                                        <div>
                                                            <label className="block text-xs font-medium text-secondary mb-1">
                                                                Values (one per line)
                                                            </label>
                                                            <textarea
                                                                data-testid={`rule-values-${i}`}
                                                                value={(r.values ?? []).join('\n')}
                                                                onChange={(e) => {
                                                                    // Store the raw lines verbatim so the
                                                                    // textarea stays editable (blank lines,
                                                                    // mid-text newlines). Trimming / dropping
                                                                    // empties happens at submit, not here.
                                                                    const vals = e.target.value.split('\n');
                                                                    setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                        ...rr,
                                                                        values: vals,
                                                                    } : rr));
                                                                }}
                                                                rows={3}
                                                                className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono bg-surface"
                                                                placeholder={predicate.includes('command') ? 'ls\ncat\ngit' : 'value-1\nvalue-2'}
                                                            />
                                                        </div>
                                                    )}

                                                    {shadow && (
                                                        <p
                                                            className="text-xs text-amber-700 dark:text-amber-400"
                                                            data-testid={`rule-shadowed-${i}`}
                                                        >
                                                            {shadow.message}
                                                        </p>
                                                    )}

                                                    <div>
                                                        <label className="block text-xs font-medium text-secondary mb-1">
                                                            Message (shown to caller on deny)
                                                        </label>
                                                        <input
                                                            type="text"
                                                            value={r.message ?? ''}
                                                            onChange={(e) => setRules((rs) => rs.map((rr, idx) => idx === i ? {
                                                                ...rr,
                                                                message: e.target.value,
                                                            } : rr))}
                                                            maxLength={512}
                                                            placeholder="Read-only analytics — no INSERT/UPDATE/DELETE/DDL."
                                                            className="w-full px-2 py-1.5 text-xs border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                        />
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>
                            )}

                            {/* ─── ADVANCED TAB ─── */}
                            {activeTab === 'advanced' && (
                                <div className="space-y-4">
                                    <div className="bg-primary-50 border border-primary-200 rounded-lg p-4 flex items-start space-x-3">
                                        <Info className="w-5 h-5 text-primary-600 flex-shrink-0 mt-0.5" />
                                        <div className="flex-1 text-sm text-primary-900 min-h-0 min-w-0">
                                            <strong>Analyzer config</strong> — optional JSON object passed to the parser.
                                            Use it to set SQL dialect (<span className="font-mono">{`{"dialect":"postgresql"}`}</span>),
                                            JSON schema (<span className="font-mono">{`{"schema":{...}}`}</span>), or
                                            allowlist roots (<span className="font-mono">{`{"roots":["/data"]}`}</span>) — see
                                            the Argument Policies docs for the per-analyzer schema.
                                        </div>
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Analyzer config (JSON)
                                        </label>
                                        <textarea
                                            value={analyzerConfigText}
                                            onChange={(e) => setAnalyzerConfigText(e.target.value)}
                                            rows={8}
                                            className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono bg-surface"
                                            placeholder={analyzer === 'sql' ? '{ "dialect": "postgresql" }' : '{}'}
                                        />
                                        <p className="mt-1 text-xs text-muted">
                                            Leave blank to use the analyzer's defaults.
                                        </p>
                                    </div>
                                    {analyzer === 'json' && (
                                        <div data-testid="analysis-limits" className="space-y-3">
                                            <div>
                                                <h4 className="text-sm font-medium text-secondary">Limits of the analysis</h4>
                                                <p className="text-xs text-muted mt-1">
                                                    How much of the arguments of one call the gateway reads for this policy. A call
                                                    beyond a limit is refused when this policy applies to it, whatever the rules say.
                                                    Leave a limit empty for its default. A call that several policies apply to must fit
                                                    the limits of each.
                                                </p>
                                            </div>
                                            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                                                {ANALYSIS_LIMITS.map(({ key, help, byDefault, most }) => (
                                                    <div key={key}>
                                                        <label
                                                            htmlFor={`analysis-limit-${key}`}
                                                            className="block text-xs font-medium text-secondary mb-1"
                                                        >
                                                            {help} (1 to {most})
                                                        </label>
                                                        <input
                                                            id={`analysis-limit-${key}`}
                                                            data-testid={`analysis-limit-${key}`}
                                                            type="number"
                                                            min={1}
                                                            max={most}
                                                            step={1}
                                                            value={analysisLimits[key]}
                                                            onChange={(e) => setAnalysisLimits((limits) => ({ ...limits, [key]: e.target.value }))}
                                                            placeholder={String(byDefault)}
                                                            className="w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500 bg-surface"
                                                        />
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}

                        </div>
                    </div>
                </form>

                {/* ─── Footer ─── */}
                <div className="px-6 py-4 border-t border-default flex items-center justify-end space-x-3 flex-shrink-0">
                    <button
                        type="button"
                        onClick={() => onClose(false)}
                        disabled={saving}
                        className="px-4 py-2 text-sm font-medium text-secondary bg-surface-raised hover:bg-surface-alt rounded-lg transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        type="submit"
                        onClick={asyncHandler(handleSubmit, 'click')}
                        disabled={saving || storedApprovalMustChange}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors disabled:opacity-50"
                    >
                        {saving ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Save className="w-4 h-4" />
                        )}
                        <span>{saving ? 'Saving…' : isCreate ? 'Create Policy' : 'Save Changes'}</span>
                    </button>
                </div>
            </div>
        </div>
    );
};

export default ArgumentPolicyForm;
