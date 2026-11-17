/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { X, Play, Search, Clock, AlertCircle } from 'lucide-react';

interface SkillGovernanceHelpModalProps {
    isOpen: boolean;
    onClose: () => void;
    initialTab?: 'execution' | 'discovery' | 'approval';
}

export const GroupSkillGovernanceHelpModal: React.FC<SkillGovernanceHelpModalProps> = ({
                                                                                          isOpen,
                                                                                          onClose,
                                                                                          initialTab = 'execution'
                                                                                      }) => {
    const [activeTab, setActiveTab] = useState<'execution' | 'discovery' | 'approval'>(initialTab);

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
                    <h2 className="text-xl font-semibold text-default">Skill Governance Help</h2>
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
                        onClick={() => setActiveTab('execution')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'execution'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Play className="w-4 h-4" />
                            Execution Mode
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('discovery')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
                            activeTab === 'discovery'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Search className="w-4 h-4" />
                            Discovery
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
                </div>

                {/* Modal Content */}
                <div className="scroll-region p-6">
                    {activeTab === 'execution' && <ExecutionModeHelp />}
                    {activeTab === 'discovery' && <DiscoveryHelp />}
                    {activeTab === 'approval' && <ApprovalHelp />}
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
// EXECUTION MODE HELP CONTENT
// ============================================================================

const ExecutionModeHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Execution Mode Overview</h3>
            <p className="text-secondary leading-relaxed">
                Execution mode controls how agent skills are allowed to run within this group. It determines whether skills execute automatically, require human approval, or are completely blocked. This is the primary control for skill governance policy.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Unrestricted</h4>
                <p className="text-sm text-secondary mb-3">
                    Skills execute automatically without any approval step. When an agent decides to invoke a skill, it runs immediately and returns results to the agent.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> An agent calling a weather lookup skill will get results instantly without user intervention.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Trusted environments, low-risk skills, or development/testing groups where speed is prioritized.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Approval Required</h4>
                <p className="text-sm text-secondary mb-3">
                    Skills must be explicitly approved by a user before they execute. When an agent attempts to invoke a skill, the execution is paused and a notification is sent to the user for approval or rejection.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> An agent requesting to send an email will wait for the user to review and approve the action before it proceeds.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Skills that perform write operations, access sensitive data, or interact with external systems where human oversight is important.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Disabled</h4>
                <p className="text-sm text-secondary mb-3">
                    Skills are completely blocked from execution. Any attempt by an agent to invoke a skill will be rejected immediately.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> If a group has skill execution disabled, agents in that group cannot use any skills regardless of their configuration.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Groups that should only use direct LLM capabilities, or when skills need to be temporarily suspended.
                </p>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Restrictive Inheritance
            </h4>
            <p className="text-sm text-secondary mb-3">
                Child groups can only <strong>tighten</strong> the execution mode, never loosen it. The restrictiveness order is:
            </p>
            <p className="text-sm text-secondary font-mono mb-3">
                unrestricted &lt; approval_required &lt; disabled
            </p>
            <p className="text-sm text-secondary">
                <strong>Example:</strong> If a parent group is set to <em>approval_required</em>, child groups can set <em>approval_required</em> or <em>disabled</em>, but cannot set <em>unrestricted</em>.
            </p>
            <p className="text-sm text-red-600 font-medium mt-3">
                This ensures that security policies set at higher levels cannot be overridden by lower-level groups.
            </p>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">Global vs Per-Skill Governance</h4>
            <p className="text-sm text-secondary mb-2">
                The execution mode set here acts as the <strong>global default</strong> for the group. You can override this for specific skills using per-skill governance:
            </p>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li><strong>Global governance:</strong> Sets the group-level default execution mode for all skills</li>
                <li><strong>Per-skill governance:</strong> Overrides the global default for individual skills</li>
            </ul>
            <p className="text-sm text-secondary mt-2">
                Per-skill overrides follow the same restrictive inheritance rules - they can only tighten, never loosen, the global setting.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Start with <em>approval_required</em> for new groups until trust is established</li>
                <li>Use <em>unrestricted</em> only for well-tested, low-risk skills in trusted environments</li>
                <li>Use per-skill overrides to set <em>disabled</em> for high-risk skills while keeping others unrestricted</li>
                <li>Review execution logs regularly to identify skills that can be safely moved to unrestricted</li>
                <li>Use <em>disabled</em> at the group level to temporarily suspend all skill execution during incidents</li>
            </ul>
        </div>
    </div>
);

// ============================================================================
// DISCOVERY HELP CONTENT
// ============================================================================

const DiscoveryHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Discovery Overview</h3>
            <p className="text-secondary leading-relaxed">
                Discovery controls whether users in this group can see and browse available skills. When discovery is enabled, users can explore what skills are available, view their descriptions, and understand their capabilities. When disabled, skills are hidden from the user interface.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Discovery Enabled</h4>
                <p className="text-sm text-secondary mb-3">
                    Users can browse and view all available skills in the portal. Skill listings, descriptions, parameters, and documentation are visible.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> A user opening the skills catalog will see all skills they have access to, along with descriptions of what each skill does.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Most groups should have discovery enabled to allow users to understand and leverage available capabilities.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Discovery Disabled</h4>
                <p className="text-sm text-secondary mb-3">
                    Skills are hidden from the user interface. Users cannot browse or view available skills, though agents may still be able to invoke them if execution is not disabled.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> The skills catalog will appear empty or be hidden entirely. Skills can still be invoked programmatically by agents if execution mode allows it.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Groups where skills should only be used by agents automatically, without user-initiated invocations or browsing.
                </p>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                AND Merge Inheritance
            </h4>
            <p className="text-sm text-secondary mb-3">
                Discovery uses <strong>AND merge</strong> logic across the group hierarchy. This means if <strong>any ancestor group</strong> disables discovery, it is effectively disabled for all child groups as well.
            </p>
            <p className="text-sm text-secondary">
                <strong>Example:</strong> If a parent group disables discovery but a child group enables it, discovery remains disabled for the child group because the parent's setting takes precedence.
            </p>
            <p className="text-sm text-red-600 font-medium mt-3">
                All groups in the hierarchy must have discovery enabled for it to be active. A single disabled ancestor will override all descendants.
            </p>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">Discovery vs Execution</h4>
            <p className="text-sm text-secondary mb-2">
                Discovery and execution mode are independent settings:
            </p>
            <div className="grid grid-cols-2 gap-4 mt-3">
                <div>
                    <p className="text-sm font-medium text-default">Discovery</p>
                    <ul className="list-disc list-inside space-y-1 text-sm text-secondary mt-1">
                        <li>Controls visibility</li>
                        <li>Affects user browsing</li>
                        <li>AND merge inheritance</li>
                    </ul>
                </div>
                <div>
                    <p className="text-sm font-medium text-default">Execution Mode</p>
                    <ul className="list-disc list-inside space-y-1 text-sm text-secondary mt-1">
                        <li>Controls invocation</li>
                        <li>Affects agent behavior</li>
                        <li>Restrictive inheritance</li>
                    </ul>
                </div>
            </div>
            <p className="text-sm text-secondary mt-3">
                A skill can be discoverable but not executable (view-only), or executable but not discoverable (agent-only).
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Keep discovery enabled for most groups to promote awareness of available capabilities</li>
                <li>Disable discovery only when you need to hide skill availability from users entirely</li>
                <li>Remember that disabling discovery at a parent level affects all child groups</li>
                <li>Use execution mode (not discovery) to control whether skills can actually run</li>
                <li>Consider enabling discovery with <em>approval_required</em> execution for maximum visibility with controlled access</li>
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
            <h3 className="text-lg font-semibold text-default mb-3">Approval Timeout Overview</h3>
            <p className="text-secondary leading-relaxed">
                The approval timeout controls how long the system waits for a user to approve or reject a skill execution request. This setting only applies when the execution mode is set to <em>approval_required</em> (either globally or per-skill). If no action is taken within the timeout period, the skill execution request is automatically rejected.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Approval Timeout (Seconds)</h4>
                <p className="text-sm text-secondary mb-3">
                    The number of seconds the system will wait for a user to approve or reject a pending skill execution before automatically timing out and rejecting the request.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> With a 300-second (5-minute) timeout, a user has 5 minutes to review and approve a skill execution request. After that, the request is automatically rejected and the agent is notified.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Default:</strong> 300 seconds (5 minutes) is the typical default, providing a reasonable window for human review.
                </p>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">How It Works</h4>
                <ol className="list-decimal list-inside space-y-2 text-sm text-secondary">
                    <li>An agent attempts to invoke a skill that requires approval</li>
                    <li>The execution is paused and a notification is sent to the user</li>
                    <li>The timeout countdown begins</li>
                    <li>If the user <strong>approves</strong> within the timeout, the skill executes</li>
                    <li>If the user <strong>rejects</strong> within the timeout, the skill is blocked</li>
                    <li>If <strong>no action</strong> is taken before timeout, the skill is automatically rejected</li>
                </ol>
            </div>

            <div className="bg-surface-alt rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Choosing the Right Timeout</h4>
                <p className="text-sm text-secondary mb-3">
                    The ideal timeout depends on how your users interact with the system:
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                    <li><strong>30-60 seconds:</strong> Real-time interactive sessions where users are actively watching</li>
                    <li><strong>120-300 seconds:</strong> Standard usage where users may briefly step away</li>
                    <li><strong>600-900 seconds:</strong> Asynchronous workflows where users may be multitasking</li>
                    <li><strong>1800+ seconds:</strong> Batch or long-running workflows with infrequent check-ins</li>
                </ul>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2 flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-yellow-600" />
                Timeout Behavior
            </h4>
            <p className="text-sm text-secondary mb-3">
                When a timeout occurs, the default behavior is to <strong>reject the execution</strong>. This is a safety measure to prevent skills from being left in a pending state indefinitely.
            </p>
            <p className="text-sm text-secondary">
                <strong>Impact on agents:</strong> The agent will receive a timeout rejection response and can decide how to proceed - it may retry, skip the skill, or inform the user.
            </p>
            <p className="text-sm text-red-600 font-medium mt-3">
                Setting the timeout too short may cause frequent automatic rejections if users cannot respond quickly enough. Setting it too long may leave agent workflows blocked for extended periods.
            </p>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">When Does This Apply?</h4>
            <p className="text-sm text-secondary mb-2">
                The approval timeout is only relevant when execution mode is <em>approval_required</em>:
            </p>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li><strong>Unrestricted mode:</strong> Timeout is ignored - skills execute immediately</li>
                <li><strong>Approval required:</strong> Timeout actively enforced on all pending approvals</li>
                <li><strong>Disabled mode:</strong> Timeout is ignored - skills are blocked immediately</li>
            </ul>
            <p className="text-sm text-secondary mt-2">
                The timeout applies to both global approval requirements and per-skill approval overrides.
            </p>
        </div>

        <div className="bg-primary-pale border border-primary rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Set timeout based on how actively users engage with the system</li>
                <li>Use shorter timeouts (60-120s) for real-time interactive use cases</li>
                <li>Use longer timeouts (300-600s) for asynchronous or batch workflows</li>
                <li>Monitor timeout rejection rates to identify if the window is too short</li>
                <li>Communicate the timeout policy to users so they know how quickly to respond</li>
                <li>Consider combining short timeouts with notification systems (email, Slack) for better UX</li>
            </ul>
        </div>
    </div>
);
