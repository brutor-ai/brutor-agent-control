/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    X,
    Loader,
    AlertCircle,
    Shield,
    ChevronDown,
    ChevronRight,
    Thermometer,
    Monitor,
    Wrench,
    MessageSquare,
    Plus,
    Trash2,
    Info
} from 'lucide-react';
import {
    LLMGovernance,
    LLMModelGovernanceResponse,
    LLModelInfo,
    FragmentPosition
} from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { withoutRefused } from '../../../lib/refusedSettings';

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onSave?: () => void;
    groupId: string;
    groupName: string;
    model: LLModelInfo;
}

/** Badge showing an inherited value */
const InheritedBadge: React.FC<{ label: string }> = ({ label }) => (
    <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-surface-alt text-amber-700 rounded-full">
        Inherited: {label}
    </span>
);

const LLMGovernanceDialog: React.FC<Props> = ({
    isOpen,
    onClose,
    onSave,
    groupId,
    groupName,
    model,
}) => {
    const { getLLMModelGovernance, updateLLMModelGovernance, clearLLMModelGovernance } = useGateway();
    const toast = useToast();

    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [governance, setGovernance] = useState<LLMGovernance>({});
    const [inherited, setInherited] = useState<LLMGovernance | null>(null);

    // Section expand states
    const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({});

    const toggleSection = (section: string) => {
        setExpandedSections(prev => ({ ...prev, [section]: !prev[section] }));
    };

    const hasInheritedValue = useCallback((section: string): boolean => {
        if (!inherited) return false;
        const val = inherited[section as keyof LLMGovernance];
        return val !== undefined && val !== null && Object.keys(val).length > 0;
    }, [inherited]);

    const hasDirectValue = useCallback((section: string): boolean => {
        const val = governance[section as keyof LLMGovernance];
        return val !== undefined && val !== null && Object.keys(val).length > 0;
    }, [governance]);

    const loadGovernance = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const response: LLMModelGovernanceResponse = await getLLMModelGovernance(groupId, model.id);
            setGovernance(response.governance || {});
            setInherited(response.inherited_governance || null);

            // Auto-expand sections that have values
            const autoExpand: Record<string, boolean> = {};
            const sections = ['temperature', 'context_window', 'tool_use', 'system_prompt'];
            for (const s of sections) {
                const directVal = response.governance?.[s as keyof LLMGovernance];
                const inheritedVal = response.inherited_governance?.[s as keyof LLMGovernance];
                const hasDirect = directVal !== undefined && directVal !== null && Object.keys(directVal).length > 0;
                const hasInherited = inheritedVal !== undefined && inheritedVal !== null && Object.keys(inheritedVal).length > 0;
                autoExpand[s] = hasDirect || hasInherited;
            }
            setExpandedSections(autoExpand);
        } catch (err) {
            console.error('Failed to load LLM governance:', errorKind(err));
            setError(errorMessage(err, 'Failed to load governance settings'));
        } finally {
            setLoading(false);
        }
    }, [groupId, model.id, getLLMModelGovernance]);

    useEffect(() => {
        if (isOpen) {
            runBackground(loadGovernance(), 'governance');
        }
    }, [isOpen, loadGovernance]);

    const handleSave = async () => {
        try {
            setSaving(true);
            setError('');
            await updateLLMModelGovernance(groupId, model.id, withoutRefused('llm_governance', governance));
            toast.success('Governance saved', `Governance settings updated for ${model.name}`);
            onSave?.();
            onClose();
        } catch (err) {
            console.error('Failed to save governance:', errorKind(err));
            setError(errorMessage(err, 'Failed to save governance settings'));
        } finally {
            setSaving(false);
        }
    };

    const handleClearAll = async () => {
        try {
            setSaving(true);
            setError('');
            await clearLLMModelGovernance(groupId, model.id);
            toast.success('Governance cleared', `All governance settings cleared for ${model.name}`);
            onSave?.();
            onClose();
        } catch (err) {
            console.error('Failed to clear governance:', errorKind(err));
            setError(errorMessage(err, 'Failed to clear governance settings'));
        } finally {
            setSaving(false);
        }
    };

    // Update helpers
    const updateTemp = (field: string, value: any) => {
        setGovernance(prev => ({
            ...prev,
            temperature: { ...prev.temperature, [field]: value }
        }));
    };

    const updateContext = (field: string, value: any) => {
        setGovernance(prev => ({
            ...prev,
            context_window: { ...prev.context_window, [field]: value }
        }));
    };

    const updateToolUse = (field: string, value: any) => {
        setGovernance(prev => ({
            ...prev,
            tool_use: { ...prev.tool_use, [field]: value }
        }));
    };

    const updateSystemPrompt = (field: string, value: any) => {
        setGovernance(prev => ({
            ...prev,
            system_prompt: { ...prev.system_prompt, [field]: value }
        }));
    };

    // Fragment helpers
    const addFragment = () => {
        const fragments = governance.system_prompt?.mandatory_fragments || [];
        updateSystemPrompt('mandatory_fragments', [...fragments, { position: 'prepend' as FragmentPosition, text: '' }]);
    };

    const removeFragment = (index: number) => {
        const fragments = [...(governance.system_prompt?.mandatory_fragments || [])];
        fragments.splice(index, 1);
        updateSystemPrompt('mandatory_fragments', fragments);
    };

    const updateFragment = (index: number, field: string, value: any) => {
        const fragments = [...(governance.system_prompt?.mandatory_fragments || [])];
        fragments[index] = { ...fragments[index], [field]: value };
        updateSystemPrompt('mandatory_fragments', fragments);
    };

    if (!isOpen) return null;

    const sections = [
        { key: 'temperature', label: 'Temperature', icon: Thermometer, color: 'text-muted', bgColor: 'bg-surface-alt' },
        { key: 'context_window', label: 'Context Window', icon: Monitor, color: 'text-primary', bgColor: 'bg-primary-pale' },
        { key: 'tool_use', label: 'Tool Use', icon: Wrench, color: 'text-amber-600', bgColor: 'bg-surface-alt' },
        { key: 'system_prompt', label: 'System Prompt', icon: MessageSquare, color: 'text-muted', bgColor: 'bg-surface-alt' },
    ];

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full max-h-[85vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <div className="flex items-center space-x-3">
                        <div className="p-2 bg-primary-100 rounded-lg">
                            <Shield className="w-5 h-5 text-primary-600" />
                        </div>
                        <div>
                            <h2 className="text-lg font-semibold text-default">
                                LLM Governance
                            </h2>
                            <p className="text-sm text-muted">
                                {model.name} &middot; {groupName}
                            </p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region p-6 space-y-3">
                    {error && (
                        <div className="flex items-start space-x-3 p-3 bg-surface-alt border border-default rounded-lg">
                            <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" />
                            <p className="text-sm text-red-700">{error}</p>
                        </div>
                    )}

                    {loading ? (
                        <div className="flex items-center justify-center h-48">
                            <Loader className="w-8 h-8 text-primary-600 animate-spin" />
                        </div>
                    ) : (
                        <>
                            {inherited && Object.keys(inherited).length > 0 && (
                                <div className="flex items-start space-x-3 p-3 bg-primary-pale border border-primary rounded-lg">
                                    <Info className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                                    <p className="text-sm text-primary">
                                        Some governance settings are inherited from parent groups. Inherited values are shown as amber badges.
                                    </p>
                                </div>
                            )}

                            {/* Collapsible sections */}
                            {sections.map(({ key, label, icon: Icon, color, bgColor }) => {
                                const isExpanded = expandedSections[key] || false;
                                const hasInherited = hasInheritedValue(key);
                                const hasDirect = hasDirectValue(key);

                                return (
                                    <div key={key} className="border border-default rounded-lg overflow-hidden">
                                        <button
                                            onClick={() => toggleSection(key)}
                                            className="w-full flex items-center justify-between px-4 py-3 bg-surface-alt hover:bg-surface-raised transition-colors"
                                        >
                                            <div className="flex items-center space-x-2">
                                                <div className={`p-1 ${bgColor} rounded`}>
                                                    <Icon className={`w-4 h-4 ${color}`} />
                                                </div>
                                                <span className="text-sm font-medium text-default">{label}</span>
                                                {hasDirect && (
                                                    <span className="inline-flex items-center px-1.5 py-0.5 text-xs font-medium bg-primary-100 text-primary-700 rounded-full">
                                                        configured
                                                    </span>
                                                )}
                                                {hasInherited && (
                                                    <span className="inline-flex items-center px-1.5 py-0.5 text-xs font-medium bg-surface-alt text-amber-700 rounded-full">
                                                        inherited
                                                    </span>
                                                )}
                                            </div>
                                            {isExpanded ? (
                                                <ChevronDown className="w-4 h-4 text-muted" />
                                            ) : (
                                                <ChevronRight className="w-4 h-4 text-muted" />
                                            )}
                                        </button>

                                        {isExpanded && (
                                            <div className="px-4 py-4 space-y-4">
                                                {/* Temperature Section */}
                                                {key === 'temperature' && (
                                                    <>
                                                        {inherited?.temperature?.max !== undefined && (
                                                            <InheritedBadge label={`max ${inherited.temperature.max}`} />
                                                        )}
                                                        <div className="grid grid-cols-2 gap-4">
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Max Temperature</label>
                                                                <input
                                                                    type="number"
                                                                    min={0}
                                                                    max={2}
                                                                    step={0.1}
                                                                    value={governance.temperature?.max ?? ''}
                                                                    onChange={(e) => updateTemp('max', e.target.value ? parseFloat(e.target.value) : undefined)}
                                                                    placeholder="e.g. 0.7"
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                />
                                                            </div>
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Enforce Mode</label>
                                                                <select
                                                                    value={governance.temperature?.enforce ?? ''}
                                                                    onChange={(e) => updateTemp('enforce', e.target.value || undefined)}
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                >
                                                                    <option value="">Not set</option>
                                                                    <option value="clamp">Clamp</option>
                                                                    <option value="reject">Reject</option>
                                                                </select>
                                                            </div>
                                                        </div>
                                                    </>
                                                )}

                                                {/* Context Window Section */}
                                                {key === 'context_window' && (
                                                    <>
                                                        {inherited?.context_window && (
                                                            <div className="flex flex-wrap gap-2">
                                                                {inherited.context_window.max_input_tokens !== undefined && (
                                                                    <InheritedBadge label={`max input ${inherited.context_window.max_input_tokens}`} />
                                                                )}
                                                                {inherited.context_window.max_output_tokens !== undefined && (
                                                                    <InheritedBadge label={`max output ${inherited.context_window.max_output_tokens}`} />
                                                                )}
                                                            </div>
                                                        )}
                                                        <div className="grid grid-cols-2 gap-4">
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Max Input Tokens</label>
                                                                <input
                                                                    type="number"
                                                                    min={0}
                                                                    value={governance.context_window?.max_input_tokens ?? ''}
                                                                    onChange={(e) => updateContext('max_input_tokens', e.target.value ? parseInt(e.target.value, 10) : undefined)}
                                                                    placeholder="e.g. 4096"
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                />
                                                            </div>
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Max Output Tokens</label>
                                                                <input
                                                                    type="number"
                                                                    min={0}
                                                                    value={governance.context_window?.max_output_tokens ?? ''}
                                                                    onChange={(e) => updateContext('max_output_tokens', e.target.value ? parseInt(e.target.value, 10) : undefined)}
                                                                    placeholder="e.g. 2048"
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                />
                                                            </div>
                                                        </div>
                                                        <div>
                                                            <label className="block text-xs font-medium text-secondary mb-1">Enforce Mode</label>
                                                            <select
                                                                value={governance.context_window?.enforce ?? ''}
                                                                onChange={(e) => updateContext('enforce', e.target.value || undefined)}
                                                                className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                            >
                                                                <option value="">Not set</option>
                                                                {/* Clamp with an input ceiling is not offered: the gateway cannot cut a request's input. */}
                                                                {(governance.context_window?.max_input_tokens == null ||
                                                                    governance.context_window?.enforce === 'clamp') && (
                                                                    <option value="clamp">
                                                                        {governance.context_window?.max_input_tokens == null
                                                                            ? 'Clamp'
                                                                            : 'Clamp (with an input ceiling: not available in this release)'}
                                                                    </option>
                                                                )}
                                                                <option value="reject">Reject</option>
                                                            </select>
                                                        </div>
                                                    </>
                                                )}

                                                {/* Tool Use Section */}
                                                {key === 'tool_use' && (
                                                    <>
                                                        {inherited?.tool_use && (
                                                            <div className="flex flex-wrap gap-2">
                                                                {inherited.tool_use.access != null && (
                                                                    <InheritedBadge label={`access ${inherited.tool_use.access.replace('_', ' ')}`} />
                                                                )}
                                                            </div>
                                                        )}
                                                        <div className="grid grid-cols-2 gap-4">
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Access</label>
                                                                <select
                                                                    value={governance.tool_use?.access ?? ''}
                                                                    onChange={(e) => updateToolUse('access', e.target.value || undefined)}
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                >
                                                                    <option value="">Not set</option>
                                                                    <option value="enabled">Enabled</option>
                                                                    <option value="disabled">Disabled</option>
                                                                </select>
                                                            </div>
                                                            <p className="text-xs text-muted self-end" data-testid="tool-use-no-approval">
                                                                A model&apos;s tool calls are not held for approval. To have a person approve a call, put the approval on the
                                                                tool itself: an MCP capability, a skill, or an argument policy on the tool&apos;s own surface.
                                                            </p>
                                                        </div>
                                                        <div>
                                                            <label className="block text-xs font-medium text-secondary mb-1">Allowed Tools (comma-separated)</label>
                                                            <input
                                                                type="text"
                                                                value={(governance.tool_use?.allowed_tools || []).join(', ')}
                                                                onChange={(e) => {
                                                                    const val = e.target.value;
                                                                    updateToolUse('allowed_tools', val ? val.split(',').map(s => s.trim()).filter(Boolean) : undefined);
                                                                }}
                                                                placeholder="e.g. search, calculator"
                                                                className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                            />
                                                        </div>
                                                        <div>
                                                            <label className="block text-xs font-medium text-secondary mb-1">Blocked Tools (comma-separated)</label>
                                                            <input
                                                                type="text"
                                                                value={(governance.tool_use?.blocked_tools || []).join(', ')}
                                                                onChange={(e) => {
                                                                    const val = e.target.value;
                                                                    updateToolUse('blocked_tools', val ? val.split(',').map(s => s.trim()).filter(Boolean) : undefined);
                                                                }}
                                                                placeholder="e.g. file_write, shell_exec"
                                                                className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                            />
                                                        </div>
                                                        <div className="grid grid-cols-3 gap-4">
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Max Calls / Request</label>
                                                                <input
                                                                    type="number"
                                                                    min={1}
                                                                    value={governance.tool_use?.max_tool_calls_per_request ?? ''}
                                                                    onChange={(e) => updateToolUse('max_tool_calls_per_request', e.target.value ? parseInt(e.target.value, 10) : undefined)}
                                                                    placeholder="No limit"
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                />
                                                            </div>
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Max Calls / Hour</label>
                                                                <input
                                                                    type="number"
                                                                    min={1}
                                                                    value={governance.tool_use?.max_tool_calls_per_hour ?? ''}
                                                                    onChange={(e) => updateToolUse('max_tool_calls_per_hour', e.target.value ? parseInt(e.target.value, 10) : undefined)}
                                                                    placeholder="No limit"
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                />
                                                            </div>
                                                            <div>
                                                                <label className="block text-xs font-medium text-secondary mb-1">Max Calls / Day</label>
                                                                <input
                                                                    type="number"
                                                                    min={1}
                                                                    value={governance.tool_use?.max_tool_calls_per_day ?? ''}
                                                                    onChange={(e) => updateToolUse('max_tool_calls_per_day', e.target.value ? parseInt(e.target.value, 10) : undefined)}
                                                                    placeholder="No limit"
                                                                    className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                                />
                                                            </div>
                                                        </div>
                                                        <div>
                                                            <label className="block text-xs font-medium text-secondary mb-1">Banned Argument Patterns (comma-separated regex)</label>
                                                            <input
                                                                type="text"
                                                                value={(governance.tool_use?.banned_argument_patterns || []).join(', ')}
                                                                onChange={(e) => {
                                                                    const val = e.target.value;
                                                                    updateToolUse('banned_argument_patterns', val ? val.split(',').map(s => s.trim()).filter(Boolean) : undefined);
                                                                }}
                                                                placeholder="e.g. DROP\\s+TABLE, rm\\s+-rf"
                                                                className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                            />
                                                        </div>
                                                    </>
                                                )}

                                                {/* System Prompt Section */}
                                                {key === 'system_prompt' && (
                                                    <>
                                                        {inherited?.system_prompt?.mandatory_fragments && inherited.system_prompt.mandatory_fragments.length > 0 && (
                                                            <InheritedBadge label={`${inherited.system_prompt.mandatory_fragments.length} fragment(s)`} />
                                                        )}
                                                        {inherited?.system_prompt?.banned_patterns && inherited.system_prompt.banned_patterns.length > 0 && (
                                                            <InheritedBadge label={`${inherited.system_prompt.banned_patterns.length} banned pattern(s)`} />
                                                        )}

                                                        {/* Mandatory Fragments */}
                                                        <div>
                                                            <div className="flex items-center justify-between mb-2">
                                                                <label className="block text-xs font-medium text-secondary">Mandatory Fragments</label>
                                                                <button
                                                                    onClick={addFragment}
                                                                    className="flex items-center space-x-1 px-2 py-1 text-xs font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-md hover:bg-primary-100 transition-colors"
                                                                >
                                                                    <Plus className="w-3 h-3" />
                                                                    <span>Add Fragment</span>
                                                                </button>
                                                            </div>
                                                            {(governance.system_prompt?.mandatory_fragments || []).length === 0 ? (
                                                                <p className="text-xs text-muted italic">No mandatory fragments configured.</p>
                                                            ) : (
                                                                <div className="space-y-2">
                                                                    {(governance.system_prompt?.mandatory_fragments || []).map((fragment, idx) => (
                                                                        <div key={idx} className="flex items-start space-x-2 p-2 bg-surface-alt rounded-lg border border-default">
                                                                            <select
                                                                                value={fragment.position}
                                                                                onChange={(e) => updateFragment(idx, 'position', e.target.value)}
                                                                                className="px-2 py-1.5 text-xs border border-strong rounded focus:ring-1 focus:ring-primary-500"
                                                                            >
                                                                                <option value="prepend">Prepend</option>
                                                                                <option value="append">Append</option>
                                                                            </select>
                                                                            <textarea
                                                                                value={fragment.text}
                                                                                onChange={(e) => updateFragment(idx, 'text', e.target.value)}
                                                                                placeholder="Fragment text..."
                                                                                rows={2}
                                                                                className="flex-1 px-2 py-1.5 text-xs border border-strong rounded focus:ring-1 focus:ring-primary-500 resize-y min-h-0 min-w-0"
                                                                            />
                                                                            <button
                                                                                onClick={() => removeFragment(idx)}
                                                                                className="p-1 text-muted hover:text-red-500 hover:bg-surface-alt rounded transition-colors"
                                                                                title="Remove fragment"
                                                                            >
                                                                                <Trash2 className="w-3.5 h-3.5" />
                                                                            </button>
                                                                        </div>
                                                                    ))}
                                                                </div>
                                                            )}
                                                        </div>

                                                        {/* Banned Patterns */}
                                                        <div>
                                                            <label className="block text-xs font-medium text-secondary mb-1">Banned Patterns (comma-separated)</label>
                                                            <input
                                                                type="text"
                                                                value={(governance.system_prompt?.banned_patterns || []).join(', ')}
                                                                onChange={(e) => {
                                                                    const val = e.target.value;
                                                                    updateSystemPrompt('banned_patterns', val ? val.split(',').map(s => s.trim()).filter(Boolean) : undefined);
                                                                }}
                                                                placeholder="e.g. ignore previous instructions, act as"
                                                                className="w-full px-3 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                                            />
                                                        </div>
                                                    </>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </>
                    )}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between p-6 border-t border-default bg-surface-alt">
                    <div>
                        <button
                            onClick={asyncHandler(handleClearAll, 'click')}
                            disabled={saving || loading}
                            className="text-sm text-red-600 hover:text-red-700 transition-colors disabled:opacity-50"
                        >
                            Clear All
                        </button>
                    </div>
                    <div className="flex items-center space-x-3">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleSave, 'click')}
                            disabled={saving || loading}
                            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50"
                        >
                            {saving ? (
                                <span className="flex items-center space-x-2">
                                    <Loader className="w-4 h-4 animate-spin" />
                                    <span>Saving...</span>
                                </span>
                            ) : (
                                'Save'
                            )}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default LLMGovernanceDialog;
