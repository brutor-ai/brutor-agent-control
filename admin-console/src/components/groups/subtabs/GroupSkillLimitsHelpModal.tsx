/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { X } from 'lucide-react';

interface GroupSkillLimitsHelpModalProps {
    isOpen: boolean;
    onClose: () => void;
}

export const GroupSkillLimitsHelpModal: React.FC<GroupSkillLimitsHelpModalProps> = ({
    isOpen,
    onClose
}) => {
    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-3xl w-full max-h-[90vh] flex flex-col">
                {/* Modal Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <h2 className="text-xl font-semibold text-default">Skill Execution Limits Help</h2>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary"
                    >
                        <X className="w-6 h-6" />
                    </button>
                </div>

                {/* Modal Content */}
                <div className="scroll-region p-6">
                    <div className="space-y-6">
                        <div>
                            <h3 className="text-lg font-semibold text-default mb-3">Execution Limits Overview</h3>
                            <p className="text-secondary leading-relaxed">
                                Execution limits control how many agent skill executions are allowed per day and per month.
                                These limits help manage resource consumption and prevent excessive skill usage within
                                a group. Warning thresholds notify administrators when usage approaches configured limits.
                            </p>
                        </div>

                        <div className="space-y-4">
                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <h4 className="font-semibold text-default mb-2">Daily Limit</h4>
                                <p className="text-sm text-secondary mb-3">
                                    Maximum number of skill executions allowed per day across all users in this group.
                                    Resets at midnight UTC.
                                </p>
                                <p className="text-sm text-secondary">
                                    <strong>Example:</strong> Setting this to 500 means all users in the group can collectively
                                    execute up to 500 skill calls per day.
                                </p>
                                <p className="text-sm text-secondary mt-2">
                                    <strong>Sizing guide:</strong>
                                </p>
                                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                                    <li>Small teams (1-5 users): 100-500 executions/day</li>
                                    <li>Medium teams (5-20 users): 500-2,000 executions/day</li>
                                    <li>Large teams (20-100 users): 2,000-10,000 executions/day</li>
                                    <li>Automation/batch workloads: 10,000+ executions/day</li>
                                </ul>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <h4 className="font-semibold text-default mb-2">Monthly Limit</h4>
                                <p className="text-sm text-secondary mb-3">
                                    Maximum number of skill executions allowed per calendar month across all users in this group.
                                    Resets on the first day of each month at midnight UTC.
                                </p>
                                <p className="text-sm text-secondary">
                                    <strong>Example:</strong> Setting this to 10,000 means all users in the group can collectively
                                    execute up to 10,000 skill calls per month.
                                </p>
                                <p className="text-sm text-secondary mt-2">
                                    <strong>Relationship to daily limit:</strong> The monthly limit should generally be higher than
                                    (daily limit x 30) to allow for variable usage patterns, or lower if you want to enforce
                                    stricter monthly budgets.
                                </p>
                                <p className="text-sm text-secondary mt-2">
                                    <strong>Sizing guide:</strong>
                                </p>
                                <ul className="list-disc list-inside space-y-1 text-sm text-secondary ml-4 mt-2">
                                    <li>Small teams: 2,000-10,000 executions/month</li>
                                    <li>Medium teams: 10,000-50,000 executions/month</li>
                                    <li>Large teams: 50,000-200,000 executions/month</li>
                                    <li>Enterprise/automation: 200,000+ executions/month</li>
                                </ul>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <h4 className="font-semibold text-default mb-2">Daily Warning (%)</h4>
                                <p className="text-sm text-secondary mb-3">
                                    Percentage of the daily limit at which a warning notification is triggered.
                                    Alerts administrators that the group is approaching its daily execution limit.
                                </p>
                                <p className="text-sm text-secondary">
                                    <strong>Example:</strong> Setting this to 80 means a warning is triggered when 80% of the
                                    daily limit has been consumed.
                                </p>
                                <p className="text-sm text-secondary mt-2">
                                    <strong>Recommended:</strong> 70-90%. Lower values give more time to react, higher values
                                    reduce notification noise.
                                </p>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <h4 className="font-semibold text-default mb-2">Monthly Warning (%)</h4>
                                <p className="text-sm text-secondary mb-3">
                                    Percentage of the monthly limit at which a warning notification is triggered.
                                    Alerts administrators that the group is approaching its monthly execution limit.
                                </p>
                                <p className="text-sm text-secondary">
                                    <strong>Example:</strong> Setting this to 75 means a warning is triggered when 75% of the
                                    monthly limit has been consumed.
                                </p>
                                <p className="text-sm text-secondary mt-2">
                                    <strong>Recommended:</strong> 60-85%. Monthly warnings should generally be set lower than
                                    daily warnings to allow time for budget adjustments within the billing cycle.
                                </p>
                            </div>
                        </div>

                        <div className="bg-surface-alt border border-default rounded-lg p-4">
                            <h4 className="font-semibold text-default mb-2">Best Practices</h4>
                            <ul className="list-disc list-inside space-y-1 text-sm text-secondary">
                                <li>Start with conservative limits and increase based on actual usage patterns</li>
                                <li>Set monthly limits to 20-25x daily limits to allow for variable usage across days</li>
                                <li>Configure warning thresholds at 75-80% to give time for review before limits are hit</li>
                                <li>Monitor warning notifications to right-size limits over time</li>
                                <li>Use governance inheritance to enforce organization-wide caps from parent groups</li>
                                <li>Review usage reports regularly to identify groups that may need limit adjustments</li>
                            </ul>
                        </div>
                    </div>
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
