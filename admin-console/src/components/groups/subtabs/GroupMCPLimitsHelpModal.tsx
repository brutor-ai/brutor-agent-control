/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { X, Clock, HardDrive, Zap } from 'lucide-react';
import { MCP_LIMIT_HELP } from '../../../lib/mcpExecutionLimits';

interface GroupMCPLimitsHelpModal {
    isOpen: boolean;
    onClose: () => void;
    initialTab?: 'execution' | 'data' | 'frequency';
}

export const MCPLimitsHelpModal: React.FC<GroupMCPLimitsHelpModal> = ({
                                                                          isOpen,
                                                                          onClose,
                                                                          initialTab = 'execution'
                                                                      }) => {
    const [activeTab, setActiveTab] = useState<'execution' | 'data' | 'frequency'>(initialTab);

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
                    <h2 className="text-xl font-semibold text-default">MCP Limits Help</h2>
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
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors whitespace-nowrap ${
                            activeTab === 'execution'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Clock className="w-4 h-4" />
                            Execution
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('data')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors whitespace-nowrap ${
                            activeTab === 'data'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <HardDrive className="w-4 h-4" />
                            Data
                        </div>
                    </button>
                    <button
                        onClick={() => setActiveTab('frequency')}
                        className={`px-4 py-3 font-medium text-sm border-b-2 transition-colors whitespace-nowrap ${
                            activeTab === 'frequency'
                                ? 'border-primary text-primary'
                                : 'border-transparent text-secondary hover:text-default'
                        }`}
                    >
                        <div className="flex items-center gap-2">
                            <Zap className="w-4 h-4" />
                            Frequency
                        </div>
                    </button>
                </div>

                {/* Modal Content */}
                <div className="scroll-region p-6">
                    {activeTab === 'execution' && <ExecutionLimitsHelp />}
                    {activeTab === 'data' && <DataLimitsHelp />}
                    {activeTab === 'frequency' && <FrequencyLimitsHelp />}
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
// EXECUTION LIMITS HELP CONTENT
// ============================================================================

const HelpCard: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
    <div className="bg-surface-alt border border-default rounded-lg p-4">
        <h4 className="font-semibold text-default mb-2">{title}</h4>
        <div className="text-sm text-secondary space-y-2">{children}</div>
    </div>
);

const ExecutionLimitsHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Execution Limits Overview</h3>
            <p className="text-secondary leading-relaxed">
                The gateway applies these limits to every MCP call of the group: on the group (merged with its
                ancestors) and on the group&apos;s binding of a server. The most restrictive statement wins: the lower
                timeout, and every concurrency allowance must admit the call. A stated value that is not a positive
                number (a whole number for concurrency) refuses every call of the group to that server.
            </p>
        </div>

        <div className="space-y-4">
            <HelpCard title="Timeout (seconds)">
                <p>{MCP_LIMIT_HELP.group['execution.timeout_seconds']}</p>
            </HelpCard>

            <HelpCard title="Max Concurrency (per user)">
                <p>{MCP_LIMIT_HELP.group['execution.max_concurrency_per_user']}</p>
                <p>
                    On a server binding the allowance counts the identity&apos;s calls to that server (scope
                    server-user).
                </p>
            </HelpCard>

            <HelpCard title="Max Concurrency (per group)">
                <p>{MCP_LIMIT_HELP.group['execution.max_concurrency_per_group']}</p>
                <p>On a server binding it counts the group&apos;s calls to that server (scope server).</p>
            </HelpCard>

            <HelpCard title="Per gateway instance">
                <p>
                    Each MCP server also has its own ceiling of requests in flight at once on one gateway instance, all
                    methods counted (10 when the server states none). Past it: 429 rate_limit_exceeded, scope
                    server-instance.
                </p>
            </HelpCard>
        </div>
    </div>
);

// ============================================================================
// DATA LIMITS HELP CONTENT
// ============================================================================

const DataLimitsHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Data Limits Overview</h3>
            <p className="text-secondary leading-relaxed">
                The payload limit bounds what passes through the gateway in one MCP call. The lower of the group&apos;s
                and the binding&apos;s value applies.
            </p>
        </div>

        <div className="space-y-4">
            <HelpCard title="Max Payload Size (MB)">
                <p>{MCP_LIMIT_HELP.group['data.max_payload_size_mb']}</p>
                <p>A megabyte here is 1,048,576 bytes; a value above 10 does not raise the request ceiling.</p>
            </HelpCard>
        </div>
    </div>
);

// ============================================================================
// FREQUENCY LIMITS HELP CONTENT
// ============================================================================

const FrequencyLimitsHelp: React.FC = () => (
    <div className="space-y-6">
        <div>
            <h3 className="text-lg font-semibold text-default mb-3">Frequency Limits Overview</h3>
            <p className="text-secondary leading-relaxed">
                Frequency limits control the rate at which MCP server calls can be made. These limits prevent abuse, manage system load, and implement rate limiting to ensure fair resource distribution across users.
            </p>
        </div>

        <div className="space-y-4">
            <div className="bg-surface-alt border border-default rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Max Calls (per minute)</h4>
                <p className="text-sm text-secondary mb-3">
                    Maximum number of MCP server calls that can be made within any 60-second window.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to 60 means up to 60 calls per minute, or 1 call per second on average.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Prevent rapid-fire requests from overwhelming systems. Protects against loops, retry storms, and aggressive polling.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Typical values:</strong>
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                    <li>Interactive users: 10-30 calls/minute</li>
                    <li>Automated workflows: 30-120 calls/minute</li>
                    <li>High-throughput systems: 120-600 calls/minute</li>
                </ul>
            </div>

            <div className="bg-surface-alt border border-default rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Max Calls (per hour)</h4>
                <p className="text-sm text-secondary mb-3">
                    Maximum number of MCP server calls that can be made within any 60-minute window.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to 1,000 means up to 1,000 calls per hour, regardless of per-minute bursts.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Relationship to minute limit:</strong> Hourly limit should be higher than (minute limit × 60) to allow for variable usage patterns.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Example calculation:</strong>
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                    <li>Per-minute limit: 30 calls/min</li>
                    <li>Theoretical max: 30 × 60 = 1,800 calls/hour</li>
                    <li>Realistic hourly limit: 1,000-1,200 calls/hour (allows variance)</li>
                </ul>
            </div>

            <div className="bg-surface-alt border border-default rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Max Calls (per day)</h4>
                <p className="text-sm text-secondary mb-3">
                    Maximum number of MCP server calls that can be made within a 24-hour period. Resets at midnight UTC.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to 10,000 means up to 10,000 calls per day across all time windows.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Overall daily cap to manage total usage and costs. Prevents excessive consumption over the full day.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Sizing by user type:</strong>
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                    <li>Light users: 100-500 calls/day</li>
                    <li>Regular users: 500-2,000 calls/day</li>
                    <li>Power users: 2,000-10,000 calls/day</li>
                    <li>Automation/batch: 10,000-100,000+ calls/day</li>
                </ul>
            </div>

            <div className="bg-surface-alt border border-default rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Burst Limit</h4>
                <p className="text-sm text-secondary mb-3">
                    Maximum number of calls that can be made in a very short time window (typically 1-5 seconds) before rate limiting kicks in.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to 10 means you can make 10 rapid calls immediately, but then must throttle to stay within per-minute limits.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>How it works:</strong> Think of this like a "token bucket" - you have 10 tokens available. Each call uses one token. Tokens regenerate at the rate defined by your per-minute limit.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Allows brief spikes in activity (page load with multiple API calls) while preventing sustained high-frequency abuse.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Recommended:</strong> Set to 2-5× your expected concurrent operations, typically 5-20 for most use cases.
                </p>
            </div>

            <div className="bg-surface-alt border border-default rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">Cooldown (seconds)</h4>
                <p className="text-sm text-secondary mb-3">
                    Minimum time that must elapse between consecutive MCP server calls from the same source.
                </p>
                <p className="text-sm text-secondary">
                    <strong>Example:</strong> Setting this to 2 seconds means after making a call, you must wait at least 2 seconds before making another call.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Use case:</strong> Prevent aggressive polling or rapid retry loops. Forces a breathing period between operations.
                </p>
                <p className="text-sm text-secondary mt-2">
                    <strong>Typical values:</strong>
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                    <li>No cooldown (0): High-frequency operations allowed</li>
                    <li>Short (1-2 sec): Reasonable for most interactive use</li>
                    <li>Medium (3-5 sec): Good for polling/monitoring</li>
                    <li>Long (10+ sec): Restrict to occasional operations</li>
                </ul>
                <p className="text-sm text-secondary mt-2">
                    <strong>Note:</strong> Cooldown works alongside other limits. A 2-second cooldown means max 30 calls/minute even if per-minute limit is higher.
                </p>
            </div>

            <div className="bg-surface-alt border border-default rounded-lg p-4">
                <h4 className="font-semibold text-default mb-2">⚡ How Frequency Limits Work Together</h4>
                <p className="text-sm text-secondary mb-3">
                    All frequency limits are enforced simultaneously. A call must pass ALL checks:
                </p>
                <div className="space-y-2 text-sm text-secondary">
                    <p>✓ Burst limit not exceeded (immediate check)</p>
                    <p>✓ Cooldown period has elapsed (immediate check)</p>
                    <p>✓ Per-minute limit not exceeded (rolling 60-second window)</p>
                    <p>✓ Per-hour limit not exceeded (rolling 60-minute window)</p>
                    <p>✓ Per-day limit not exceeded (resets at midnight UTC)</p>
                </div>
                <p className="text-sm text-secondary mt-3">
                    <strong>Example scenario:</strong> With burst=10, cooldown=1s, minute=30, hour=1000, day=10000:
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                    <li>Can make 10 rapid calls immediately (burst)</li>
                    <li>Must wait 1 second between each subsequent call (cooldown)</li>
                    <li>Cannot exceed 30 calls in any 60-second window (minute)</li>
                    <li>Cannot exceed 1,000 calls in any hour (hour)</li>
                    <li>Cannot exceed 10,000 calls today (day)</li>
                </ul>
            </div>
        </div>

        <div className="bg-surface-alt border border-default rounded-lg p-4">
            <h4 className="font-semibold text-default mb-2">💡 Best Practices</h4>
            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                <li>Set per-minute limits based on expected interactive usage patterns</li>
                <li>Set hourly limits to 50-60× per-minute limits (not 60×) for variability</li>
                <li>Set daily limits for overall capacity management, not strict enforcement</li>
                <li>Use burst limits to allow brief spikes without blocking normal use</li>
                <li>Apply cooldowns only when necessary to prevent polling abuse</li>
                <li>Monitor rate limit errors to identify if limits are too restrictive</li>
                <li>Implement exponential backoff in clients for better handling</li>
            </ul>
        </div>
    </div>
);