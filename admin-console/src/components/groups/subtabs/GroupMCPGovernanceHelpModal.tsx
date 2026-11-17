/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { X, Shield, Clock, Filter, AlertCircle } from 'lucide-react';

interface MCPGovernanceHelpModalProps {
    isOpen: boolean;
    onClose: () => void;
    initialTab?: 'access' | 'approval' | 'filters';
}

export const GroupMCPGovernanceHelpModal: React.FC<MCPGovernanceHelpModalProps> = ({
                                                                                      isOpen,
                                                                                      onClose,
                                                                                      initialTab = 'access'
                                                                                  }) => {
    const [activeTab, setActiveTab] = useState<'access' | 'approval' | 'filters'>(initialTab);

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
                    <h2 className="text-xl font-semibold text-default">MCP Governance Help</h2>
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
                        onClick={() => setActiveTab('access')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'access'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Shield className="w-4 h-4" />
                            Default Access
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('approval')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'approval'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Clock className="w-4 h-4" />
                            Approval
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('filters')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'filters'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Filter className="w-4 h-4" />
                            Per-Server Filters
                        </div>
                    </button>
                </div>

                {/* Modal Content */}
                <div className="scroll-region p-6">
                    {activeTab === 'access' && <DefaultAccessHelp />}
                    {activeTab === 'approval' && <ApprovalHelp />}
                    {activeTab === 'filters' && <PerServerFiltersHelp />}
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
// DEFAULT ACCESS HELP CONTENT
// ============================================================================

const DefaultAccessHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Default Access Overview</h3>
            <p className="text-secondary leading-relaxed">
                Default access settings control the baseline access level for MCP server capabilities within this group. These global defaults apply to all MCP servers unless overridden by per-server capability filters. Each capability type can be set to one of three access levels.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">🔑 Access Levels</h4>
            <ul className="list-disc list-inside space-y-2 text-sm text-secondary">
                <li><strong>enabled</strong> — Capability is allowed without restriction. Users can invoke it freely.</li>
                <li><strong>approval_required</strong> — Capability requires user or admin approval before execution. See the Approval tab for timeout configuration.</li>
                <li><strong>disabled</strong> — Capability is completely blocked. Any attempt to use it will be denied.</li>
            </ul>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Default Tool Access</h4>
                <p className="text-sm text-secondary mb-3">
                    Controls the default access level for MCP server tools. Tools are executable functions that MCP servers expose, such as running queries, creating resources, or performing actions.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to <code className="bg-surface-inset px-1 rounded">approval_required</code> means any tool invocation across all MCP servers will require approval unless a per-server filter overrides it.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Recommendation:</strong> Tools can have side effects (writing data, triggering actions), so <code className="bg-surface-inset px-1 rounded">approval_required</code> is a safe default for production environments.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Default Resource Access</h4>
                <p className="text-sm text-secondary mb-3">
                    Controls the default access level for MCP server resources. Resources are read-only data endpoints that provide context, such as files, database records, or configuration data.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to <code className="bg-surface-inset px-1 rounded">enabled</code> allows users to read resource data from any MCP server without restriction.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Recommendation:</strong> Resources are read-only and generally safe to enable by default. Consider <code className="bg-surface-inset px-1 rounded">approval_required</code> if resources expose sensitive data.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Default Prompt Access</h4>
                <p className="text-sm text-secondary mb-3">
                    Controls the default access level for MCP server prompts. Prompts are pre-defined templates that guide LLM interactions, such as analysis templates or structured workflows.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to <code className="bg-surface-inset px-1 rounded">enabled</code> allows users to use any prompt template from any MCP server.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Recommendation:</strong> Prompts are typically safe to enable, as they only structure interactions without performing actions.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Default Resource Template Access</h4>
                <p className="text-sm text-secondary mb-3">
                    Controls the default access level for MCP server resource templates. Resource templates are parameterized URI patterns that dynamically resolve to specific resources based on user input.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> A template like <code className="bg-surface-inset px-1 rounded">db://tables/{'{table_name}'}/rows</code> resolves to different data depending on the table name provided.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Recommendation:</strong> Similar to resources, templates are read-only. Enable by default unless they provide access to sensitive or restricted data.
                </p>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Restrictive Inheritance
            </h4>
            <p className="text-sm text-secondary">
                Child groups can only <strong>tighten</strong> (make more restrictive) parent governance settings, never loosen them. For example, if a parent group sets tool access to <code className="bg-surface-inset px-1 rounded">approval_required</code>, a child group can change it to <code className="bg-surface-inset px-1 rounded">disabled</code> but not to <code className="bg-surface-inset px-1 rounded">enabled</code>.
            </p>
            <p className="text-sm text-secondary mt-2">
                Restriction order: <code className="bg-surface-inset px-1 rounded">enabled</code> → <code className="bg-surface-inset px-1 rounded">approval_required</code> → <code className="bg-surface-inset px-1 rounded">disabled</code> (most to least permissive).
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Set tool access to <code className="bg-surface-inset px-1 rounded">approval_required</code> by default for production groups</li>
                <li>Enable resources and prompts by default unless they expose sensitive data</li>
                <li>Use per-server filters to grant broader access to trusted MCP servers</li>
                <li>Start restrictive and loosen access as trust is established</li>
                <li>Review governance settings when adding new MCP servers to the group</li>
            </ul>
        </div>
    </div>
);

// ============================================================================
// APPROVAL HELP CONTENT
// ============================================================================

const ApprovalHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Approval Settings Overview</h3>
            <p className="text-secondary leading-relaxed">
                When a capability's access level is set to <code className="bg-surface-inset px-1 rounded">approval_required</code>, the system pauses execution and waits for approval before proceeding. The approval timeout controls how long the system waits before the request is automatically denied.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Approval Timeout (seconds)</h4>
                <p className="text-sm text-secondary mb-3">
                    Specifies the maximum number of seconds the system will wait for an approval decision when a capability requires approval. If no decision is made within this window, the request is automatically denied.
                </p>
                <p className="text-sm text-secondary">
                    <strong>How it works:</strong>
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                    <li>User or agent invokes a capability marked as <code className="bg-surface-inset px-1 rounded">approval_required</code></li>
                    <li>The system sends an approval request and starts the timeout countdown</li>
                    <li>An approver (user or admin) can approve or deny the request</li>
                    <li>If the timeout expires without a decision, the request is automatically denied</li>
                </ul>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Choosing a Timeout Value</h4>
                <p className="text-sm text-secondary mb-3">
                    The right timeout depends on your workflow and how quickly approvers can respond.
                </p>
                <div className="space-y-3">
                    <div>
                        <p className="text-sm font-medium text-default">Interactive / Real-time (30-120 seconds)</p>
                        <p className="text-sm text-secondary">
                            For scenarios where the user is actively waiting and an approver is available. Short timeouts keep the experience responsive.
                        </p>
                    </div>
                    <div>
                        <p className="text-sm font-medium text-default">Standard Workflow (120-600 seconds)</p>
                        <p className="text-sm text-secondary">
                            For typical business workflows where approvers may need a few minutes to review. Balances responsiveness with flexibility.
                        </p>
                    </div>
                    <div>
                        <p className="text-sm font-medium text-default">Asynchronous / Batch (600-3600 seconds)</p>
                        <p className="text-sm text-secondary">
                            For automated pipelines or batch jobs where approval can be asynchronous and longer delays are acceptable.
                        </p>
                    </div>
                </div>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Timeout Behavior
            </h4>
            <p className="text-sm text-secondary mb-3">
                When the approval timeout expires, the request is <strong>automatically denied</strong>. This is a safety measure to prevent requests from hanging indefinitely and to ensure that unattended capabilities are not silently approved.
            </p>
            <p className="text-sm text-red-600 font-medium">
                ⚠️ Setting the timeout too short may cause legitimate requests to be denied before approvers can respond. Setting it too long may leave users waiting unnecessarily.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Start with 120 seconds (2 minutes) as a reasonable default</li>
                <li>Increase timeout for groups where approvers may not be immediately available</li>
                <li>Use shorter timeouts for interactive user-facing workflows</li>
                <li>Ensure approvers are notified promptly so they can respond within the window</li>
                <li>Monitor timeout-based denials to identify if the value needs adjustment</li>
            </ul>
        </div>
    </div>
);

// ============================================================================
// PER-SERVER FILTERS HELP CONTENT
// ============================================================================

const PerServerFiltersHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Per-Server Capability Filters Overview</h3>
            <p className="text-secondary leading-relaxed">
                Per-server capability filters allow you to override global default access levels for specific capabilities on individual MCP servers. This provides fine-grained control, enabling you to tighten access for specific tools, resources, prompts, or resource templates on a server-by-server basis.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">🔍 How Filters Work</h4>
            <p className="text-sm text-secondary mb-2">
                Per-server filters operate within the boundaries set by global default access:
            </p>
            <ul className="list-disc list-inside space-y-2 text-sm text-secondary">
                <li>Global defaults set the <strong>maximum permissiveness</strong> for all capabilities</li>
                <li>Per-server filters can only <strong>tighten</strong> access for specific capabilities, never loosen beyond the global default</li>
                <li>Capabilities without a per-server filter inherit the global default access level</li>
            </ul>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Per-Capability Access</h4>
                <p className="text-sm text-secondary mb-3">
                    Each filter targets a specific capability (tool, resource, prompt, or resource template) on a specific MCP server and assigns it an access level.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> If global default tool access is <code className="bg-surface-inset px-1 rounded">enabled</code>, you can set a per-server filter to make the <code className="bg-surface-inset px-1 rounded">delete_records</code> tool on a database MCP server require <code className="bg-surface-inset px-1 rounded">approval_required</code>, while all other tools remain enabled.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Filter Resolution Order</h4>
                <p className="text-sm text-secondary mb-3">
                    When a capability is invoked, the system resolves access in the following order:
                </p>
                <ol className="list-decimal list-inside space-y-2 text-sm text-secondary">
                    <li><strong>Check per-server filter</strong> — If a filter exists for this specific capability on this server, use its access level</li>
                    <li><strong>Fall back to global default</strong> — If no per-server filter exists, use the corresponding global default access level (e.g., default_tool_access for tools)</li>
                    <li><strong>Apply inheritance</strong> — The effective access is the most restrictive between the resolved level and any parent group governance</li>
                </ol>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Common Filter Patterns</h4>
                <div className="space-y-3">
                    <div>
                        <p className="text-sm font-medium text-default">Restrict dangerous tools</p>
                        <p className="text-sm text-secondary">
                            Global tools <code className="bg-surface-inset px-1 rounded">enabled</code>, but set specific write/delete tools to <code className="bg-surface-inset px-1 rounded">approval_required</code> or <code className="bg-surface-inset px-1 rounded">disabled</code>.
                        </p>
                    </div>
                    <div>
                        <p className="text-sm font-medium text-default">Block sensitive resources</p>
                        <p className="text-sm text-secondary">
                            Global resources <code className="bg-surface-inset px-1 rounded">enabled</code>, but set specific PII or credential resources to <code className="bg-surface-inset px-1 rounded">disabled</code>.
                        </p>
                    </div>
                    <div>
                        <p className="text-sm font-medium text-default">Require approval for high-impact actions</p>
                        <p className="text-sm text-secondary">
                            Global tools <code className="bg-surface-inset px-1 rounded">enabled</code>, but set deployment, billing, or admin tools to <code className="bg-surface-inset px-1 rounded">approval_required</code>.
                        </p>
                    </div>
                </div>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Restrictive Only
            </h4>
            <p className="text-sm text-secondary">
                Per-server filters can only <strong>tighten</strong> access relative to the global default. If the global default for tools is <code className="bg-surface-inset px-1 rounded">approval_required</code>, a per-server filter can set a tool to <code className="bg-surface-inset px-1 rounded">disabled</code> but cannot set it to <code className="bg-surface-inset px-1 rounded">enabled</code>. To grant broader access, you must change the global default.
            </p>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">🛡️ Multi-Group Users</h4>
            <p className="text-sm text-secondary mb-2">
                When a user belongs to multiple groups, the system evaluates capability access across all groups using an <strong>allow-wins</strong> strategy for capability filters:
            </p>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>If any group grants <code className="bg-surface-inset px-1 rounded">enabled</code> access to a capability, the user gets access</li>
                <li>This ensures users are not unnecessarily blocked by restrictive groups they also belong to</li>
                <li>Governance within each group still follows restrictive inheritance from parent groups</li>
            </ul>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Use global defaults for the common case and filters only for exceptions</li>
                <li>Document why each per-server filter exists to aid future audits</li>
                <li>Review filters when MCP servers add new capabilities</li>
                <li>Test filter behavior with multi-group users to ensure expected access</li>
                <li>Prefer <code className="bg-surface-inset px-1 rounded">approval_required</code> over <code className="bg-surface-inset px-1 rounded">disabled</code> for tools that may sometimes be needed</li>
            </ul>
        </div>
    </div>
);
