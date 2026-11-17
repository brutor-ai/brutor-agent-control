/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { X, Thermometer, BookOpen, Wrench, MessageSquare, AlertCircle } from 'lucide-react';

interface LLMGovernanceHelpModalProps {
    isOpen: boolean;
    onClose: () => void;
    initialTab?: 'temperature' | 'context' | 'tool_use' | 'system_prompt';
}

export const GroupLLMGovernanceHelpModal: React.FC<LLMGovernanceHelpModalProps> = ({
                                                                                      isOpen,
                                                                                      onClose,
                                                                                      initialTab = 'temperature'
                                                                                  }) => {
    const [activeTab, setActiveTab] = useState<'temperature' | 'context' | 'tool_use' | 'system_prompt'>(initialTab);

    // Update active tab when initialTab changes
    React.useEffect(() => {
        if (isOpen) {
            setActiveTab(initialTab);
        }
    }, [isOpen, initialTab]);

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full max-h-[90vh] flex flex-col">
                {/* Modal Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <h2 className="text-xl font-semibold text-default">LLM Governance Help</h2>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary"
                    >
                        <X className="w-6 h-6" />
                    </button>
                </div>

                {/* Tabs */}
                <div className="flex border-b border-default px-6">
                    <button
                        onClick={() => setActiveTab('temperature')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'temperature'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Thermometer className="w-4 h-4" />
                            Temperature
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('context')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'context'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <BookOpen className="w-4 h-4" />
                            Context Window
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('tool_use')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'tool_use'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Wrench className="w-4 h-4" />
                            Tool Use
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('system_prompt')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'system_prompt'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <MessageSquare className="w-4 h-4" />
                            System Prompt
                        </div>
                    </button>
                </div>

                {/* Modal Content */}
                <div className="scroll-region p-6">
                    {activeTab === 'temperature' && <TemperatureGovernanceHelp />}
                    {activeTab === 'context' && <ContextWindowGovernanceHelp />}
                    {activeTab === 'tool_use' && <ToolUseGovernanceHelp />}
                    {activeTab === 'system_prompt' && <SystemPromptGovernanceHelp />}
                </div>

                {/* Modal Footer */}
                <div className="flex justify-end p-6 border-t border-default">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

// ============================================================================
// TEMPERATURE GOVERNANCE HELP CONTENT
// ============================================================================

const TemperatureGovernanceHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Temperature Governance Overview</h3>
            <p className="text-secondary leading-relaxed">
                Temperature governance controls restrict the creativity and randomness of LLM outputs. By setting maximum values for temperature and top_p, you ensure that responses stay within acceptable bounds of determinism and predictability for your organization's use cases.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">🔒 Governance vs Limits</h4>
            <p className="text-sm text-secondary">
                <strong>Governance</strong> controls define <em>what is allowed</em> — policy and security constraints that shape LLM behavior. <strong>Limits</strong> define <em>how much</em> — usage quotas like budgets and request counts. Governance is about safety and compliance; limits are about cost and capacity.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Max Temperature</h4>
                <p className="text-sm text-secondary mb-3">
                    Sets the maximum temperature value that requests from this group can use. Temperature controls how random or creative the LLM's output is, on a scale from 0.0 (fully deterministic) to 2.0 (highly creative/random).
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting max_temperature to 0.7 means users in this group cannot set temperature above 0.7 in their requests. Any request with a higher value will be capped or rejected.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Enforce deterministic outputs for compliance-sensitive applications like financial reporting or medical advice, where creative variance is undesirable.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Max Top P</h4>
                <p className="text-sm text-secondary mb-3">
                    Sets the maximum top_p (nucleus sampling) value. Top_p controls the diversity of token selection — lower values restrict the model to higher-probability tokens, reducing randomness. Range: 0.0 to 1.0.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting max_top_p to 0.9 prevents the model from considering very low-probability tokens, keeping outputs more focused and predictable.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Relationship to temperature:</strong> Temperature and top_p both control randomness but in different ways. Most providers recommend adjusting one or the other, not both. Governing both gives you layered control.
                </p>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Restrictive Inheritance
            </h4>
            <p className="text-sm text-secondary">
                Governance settings follow <strong>restrictive inheritance</strong>: child groups can only tighten governance, never relax it. If a parent group sets max_temperature to 1.0, a child group can lower it to 0.5, but cannot raise it to 1.5. This ensures security policies cascade downward.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Set max_temperature to 0.3-0.5 for factual/analytical workloads</li>
                <li>Allow higher temperatures (0.7-1.0) for creative writing or brainstorming groups</li>
                <li>Use per-model governance to set tighter temperature limits on specific models</li>
                <li>Start with moderate restrictions and tighten based on observed output quality</li>
            </ul>
        </div>
    </div>
);

// ============================================================================
// CONTEXT WINDOW GOVERNANCE HELP CONTENT
// ============================================================================

const ContextWindowGovernanceHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Context Window Governance Overview</h3>
            <p className="text-secondary leading-relaxed">
                Context window governance constrains the token budgets for individual requests. By limiting the context window size and output tokens, you control how much data each request can process and how long responses can be, managing both costs and computational resources at the per-request level.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">📐 Two Tiers of Governance</h4>
            <p className="text-sm text-secondary mb-2">
                Governance operates at two levels:
            </p>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li><strong>Global governance (group-level):</strong> Sets the overall ceiling for all models used by this group</li>
                <li><strong>Per-model governance:</strong> Can tighten limits further for specific models, but never exceed the global ceiling</li>
            </ul>
            <p className="text-sm text-secondary mt-2">
                This allows fine-grained control — e.g., allow 128K context globally but restrict a cheaper model to 32K.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Max Context Window</h4>
                <p className="text-sm text-secondary mb-3">
                    Sets the maximum number of input tokens (context window size) that a single request can use. This limits how much conversation history, document content, or context can be sent to the model.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting max_context_window to 16,384 tokens means requests cannot include more than ~12,000 words of context, even if the underlying model supports a larger window.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Cost impact:</strong> Larger context windows are significantly more expensive. A 128K-token request can cost 8x more than a 16K-token request. Governing this helps control per-request costs.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Max Output Tokens</h4>
                <p className="text-sm text-secondary mb-3">
                    Sets the maximum number of tokens the model can generate in a single response. This constrains response length and prevents excessively long completions.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting max_output_tokens to 4,096 limits responses to approximately 3,000 words. Useful for preventing models from generating excessively verbose replies.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Enforce concise responses for customer-facing applications, or prevent resource-intensive long-form generation in groups that don't need it.
                </p>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Restrictive Inheritance
            </h4>
            <p className="text-sm text-secondary">
                Like all governance settings, context window limits follow <strong>restrictive inheritance</strong>. A child group cannot set a larger context window or more output tokens than its parent allows. This ensures resource constraints are enforced throughout the group hierarchy.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Set max_context_window based on actual use-case needs, not model maximums</li>
                <li>Use per-model governance to set different context limits for expensive vs. affordable models</li>
                <li>Limit output tokens for chatbot/Q&A groups to encourage concise answers</li>
                <li>Allow larger context windows for document analysis or code review groups</li>
                <li>Monitor average context sizes to right-size governance over time</li>
            </ul>
        </div>
    </div>
);

// ============================================================================
// TOOL USE GOVERNANCE HELP CONTENT
// ============================================================================

const ToolUseGovernanceHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Tool Use Governance Overview</h3>
            <p className="text-secondary leading-relaxed">
                Tool use governance controls how LLMs interact with external tools, functions, and APIs. These settings determine which tools are available, how many tool calls are permitted, and what argument patterns are blocked. This is critical for security, as tool calls allow LLMs to take real-world actions.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">🔧 What is Tool Use?</h4>
            <p className="text-sm text-secondary mb-2">
                Tool use (also called function calling) allows LLMs to invoke external functions, APIs, and services. This includes MCP tools, custom functions, and built-in capabilities. Tool use governance applies across all tool types — MCP servers, skill tools, and native function calls.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Tool Use Policy</h4>
                <p className="text-sm text-secondary mb-3">
                    The top-level policy that controls whether tool use is permitted at all. Two modes:
                </p>
                <ul className="list-disc list-inside space-y-2 text-sm text-secondary">
                    <li><strong>enabled:</strong> Tool calls are allowed according to the other governance settings (allowed/blocked lists, rate limits)</li>
                    <li><strong>disabled:</strong> All tool calls are blocked. The LLM can only generate text responses</li>
                </ul>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Allowed Tools / Blocked Tools</h4>
                <p className="text-sm text-secondary mb-3">
                    Allowlist and blocklist for specific tools by name. These work together to create fine-grained access control:
                </p>
                <p className="text-sm text-secondary">
                    <strong>Allowed tools:</strong> When set, only these tools can be called. Acts as a whitelist — anything not listed is implicitly blocked.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Blocked tools:</strong> These tools are explicitly forbidden, even if they appear in the allowed list or are provided by an MCP server. Takes precedence over allowed tools.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Example:</strong> Allow all database query tools but block <code className="bg-surface-inset px-1 rounded">db_delete</code> and <code className="bg-surface-inset px-1 rounded">db_drop_table</code> to prevent destructive operations.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Tool Call Rate Limits</h4>
                <p className="text-sm text-secondary mb-3">
                    Controls how many tool calls are permitted within different time windows:
                </p>
                <ul className="list-disc list-inside space-y-2 text-sm text-secondary">
                    <li><strong>max_tool_calls_per_request:</strong> Maximum tool invocations in a single LLM request. Prevents runaway tool-calling loops within one conversation turn</li>
                    <li><strong>max_tool_calls_per_hour:</strong> Hourly cap across all requests from this group. Controls sustained tool usage rates</li>
                    <li><strong>max_tool_calls_per_day:</strong> Daily cap across all requests. Sets the overall daily budget for tool interactions</li>
                </ul>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Prevent agentic loops where the LLM calls tools hundreds of times in a single request, or limit external API calls to stay within third-party rate limits.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Banned Argument Patterns</h4>
                <p className="text-sm text-secondary mb-3">
                    A list of regular expressions that are matched against tool call arguments. If any argument matches a banned pattern, the tool call is blocked. This provides a security layer against prompt injection and data exfiltration.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Examples of banned patterns:</strong>
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                    <li><code className="bg-surface-inset px-1 rounded">rm\s+-rf</code> — Block destructive shell commands</li>
                    <li><code className="bg-surface-inset px-1 rounded">DROP\s+TABLE</code> — Block SQL table deletion</li>
                    <li><code className="bg-surface-inset px-1 rounded">https?://(?!.*\.example\.com)</code> — Block URLs outside your domain</li>
                    <li><code className="bg-surface-inset px-1 rounded">\b\d{3}-\d{2}-\d{4}\b</code> — Block SSN-like patterns from being sent to tools</li>
                </ul>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Security Considerations
            </h4>
            <p className="text-sm text-secondary">
                Tool use is the highest-risk LLM capability because it enables real-world actions. Governance here follows <strong>restrictive inheritance</strong>: child groups inherit parent restrictions and can only tighten them. A child group cannot re-enable a tool that a parent group has blocked, nor raise tool call limits above the parent's ceiling.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>A model&apos;s tool calls are not held for approval. To have a person approve a call, put the approval on the tool itself: an MCP capability, a skill, or an argument policy on the tool&apos;s own surface</li>
                <li>Use allowed_tools as a whitelist rather than relying solely on blocked_tools</li>
                <li>Set max_tool_calls_per_request to 10-25 to prevent agentic runaway loops</li>
                <li>Add banned_argument_patterns for sensitive data formats (SSNs, API keys, internal URLs)</li>
                <li>Block destructive tools (delete, drop, remove) at the parent group level</li>
                <li>Review tool call logs regularly to refine governance settings</li>
            </ul>
        </div>
    </div>
);

// ============================================================================
// SYSTEM PROMPT GOVERNANCE HELP CONTENT
// ============================================================================

const SystemPromptGovernanceHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">System Prompt Governance Overview</h3>
            <p className="text-secondary leading-relaxed">
                System prompt governance controls the system-level instructions sent to LLMs. These settings allow administrators to enforce organizational policies, inject safety instructions, and standardize LLM behavior across a group — regardless of what individual users or applications specify.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">📜 How System Prompts Work</h4>
            <p className="text-sm text-secondary mb-2">
                System prompts are special instructions sent to the LLM before user messages. They define the model's persona, rules, and behavioral boundaries. Governance controls determine how group-level system prompts interact with user-provided prompts:
            </p>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li><strong>Override</strong> replaces the user's system prompt entirely</li>
                <li><strong>Prefix</strong> prepends governance instructions before the user's prompt</li>
                <li><strong>Suffix</strong> appends governance instructions after the user's prompt</li>
            </ul>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">System Prompt Override</h4>
                <p className="text-sm text-secondary mb-3">
                    When set, this <strong>completely replaces</strong> any system prompt provided by the user or application. The user's original system prompt is discarded and this governance prompt is used instead.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Set an override to enforce a strict customer service persona: <em>"You are a helpful customer support agent for Acme Corp. You must not discuss competitors, provide financial advice, or share internal information."</em>
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Lock down the LLM's behavior completely for production-facing applications where the system prompt must not be modified by end users.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">System Prompt Prefix</h4>
                <p className="text-sm text-secondary mb-3">
                    Text that is <strong>prepended before</strong> the user's system prompt. The user's prompt is preserved, but governance instructions come first, giving them higher priority in the model's attention.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Prefix with safety rules: <em>"IMPORTANT: You must never reveal confidential information, generate harmful content, or bypass security controls. Always follow company policies."</em>
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Advantage:</strong> Allows users to customize their system prompts while ensuring critical governance rules always appear first.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">System Prompt Suffix</h4>
                <p className="text-sm text-secondary mb-3">
                    Text that is <strong>appended after</strong> the user's system prompt. Useful for adding reminders, compliance notices, or output format requirements that should come after the main instructions.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Suffix with compliance reminder: <em>"Remember: All responses must comply with GDPR data handling requirements. Do not include personally identifiable information in outputs."</em>
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Advantage:</strong> Provides a final instruction layer that reinforces governance rules, as models tend to pay attention to both the beginning and end of their instructions.
                </p>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">🔀 Combination Behavior</h4>
            <p className="text-sm text-secondary mb-2">
                These settings can be combined, but override takes precedence:
            </p>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li><strong>If override is set:</strong> Prefix and suffix are ignored — only the override is sent</li>
                <li><strong>If no override:</strong> Final prompt = prefix + user's prompt + suffix</li>
                <li><strong>If only prefix:</strong> Final prompt = prefix + user's prompt</li>
                <li><strong>If only suffix:</strong> Final prompt = user's prompt + suffix</li>
            </ul>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Restrictive Inheritance
            </h4>
            <p className="text-sm text-secondary">
                System prompt governance follows <strong>restrictive inheritance</strong>. Child groups inherit parent prompt governance and can add additional prefix/suffix text, but cannot remove or override governance set by a parent group. If a parent sets a system prompt override, child groups cannot bypass it.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Use prefix for mandatory safety rules that should apply to all use cases</li>
                <li>Use suffix for compliance reminders and output formatting requirements</li>
                <li>Reserve override for tightly controlled production applications</li>
                <li>Keep governance prompts concise — overly long system prompts consume context window tokens</li>
                <li>Test governance prompts with representative queries to ensure they work as intended</li>
                <li>Use per-model governance to tailor system prompts for different model capabilities</li>
            </ul>
        </div>
    </div>
);
