/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * RouterTestSubTab — pick a configured routing group, send a
 * test message through the proxy, see which member model the
 * router actually selected and the response it produced.
 *
 * How the test works:
 *   - The form offers no "Model" dropdown: a routing decision
 *     mocked DB-side gives operators no real signal about
 *     whether their routing config works. For direct-model
 *     testing, the LLM Models page does that.
 *   - The button drives the actual proxy: control plane →
 *     `/v1/proxy/llm/chat/completions` with `model = <group_id>`
 *     → routing dispatch → upstream → response.
 *   - Each test result is appended to the per-tenant history,
 *     viewable in the Status tab.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
    Play,
    RefreshCw,
    CheckCircle2,
    XCircle,
    Server,
    Zap,
    Clock,
} from 'lucide-react';
import { useAuth } from '../../../hooks/useAuth';
import { useGateway } from '../../../hooks/useGateway';
import type {
    RoutingGroupListItem,
    RouterTestResponse,
} from '../../../lib/llmTypes';
import toast from 'react-hot-toast';
import { errorMessage } from '../../../lib/narrow';
import { explainDispatchRefusal } from '../../../lib/dispatchRefusal';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

const DEFAULT_TEST_MESSAGE = 'Hello! Reply with a single short sentence.';

const RouterTestSubTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    // A test is a real, billed request: the Control Plane asks for
    // llm:router:test, which reading routing groups does not give.
    const { hasPermission } = useAuth();
    const canTest = hasPermission('llm:router:test');
    const [groups, setGroups] = useState<RoutingGroupListItem[]>([]);
    const [groupsLoading, setGroupsLoading] = useState(true);
    const [routingGroupId, setRoutingGroupId] = useState<string>('');
    const [testMessage, setTestMessage] = useState(DEFAULT_TEST_MESSAGE);
    const [testing, setTesting] = useState(false);
    const [result, setResult] = useState<RouterTestResponse | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        runBackground((async () => {
            try {
                setGroupsLoading(true);
                const resp = await gatewayClient.listRoutingGroups();
                if (cancelled) return;
                // Only show enabled groups — dispatching to a
                // disabled group would predictably 404.
                const enabled = (resp.routing_groups || []).filter(
                    (g: RoutingGroupListItem) => g.enabled,
                );
                setGroups(enabled);
                // Auto-select the first group so the operator
                // can hit Run without scrolling — common case.
                if (enabled.length > 0 && !routingGroupId) {
                    setRoutingGroupId(enabled[0].id);
                }
            } catch (e) {
                if (!cancelled) {
                    toast.error(`Failed to load routing groups: ${errorMessage(e, 'unknown error')}`);
                }
            } finally {
                if (!cancelled) setGroupsLoading(false);
            }
        })(), 'async work');
        return () => {
            cancelled = true;
        };
        // Mount-once. Operators can refresh the page if they
        // just created a group; avoid auto-polling here so the
        // selection doesn't shift under them mid-edit.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const selectedGroup = useMemo(
        () => groups.find((g) => g.id === routingGroupId),
        [groups, routingGroupId],
    );

    const runTest = async () => {
        if (!routingGroupId) {
            toast.error('Pick a routing group');
            return;
        }
        setTesting(true);
        setError(null);
        setResult(null);
        try {
            const response = await gatewayClient.testRouting({
                routing_group_id: routingGroupId,
                message: testMessage.trim() || DEFAULT_TEST_MESSAGE,
            });
            setResult(response);
            if (response.error) {
                toast.error(`Router returned an error — see details below`);
            } else {
                toast.success(
                    `Router selected: ${response.selected_model || '<unknown>'}`,
                );
            }
        } catch (err) {
            const msg = errorMessage(err, 'Test failed');
            setError(msg);
            toast.error(`Test failed: ${msg}`);
        } finally {
            setTesting(false);
        }
    };

    return (
        <div className="space-y-6 max-w-4xl">
            {/* Info banner */}
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                <div className="flex items-start space-x-3">
                    <Zap className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
                    <div className="flex-1 text-sm text-blue-900 leading-relaxed min-h-0 min-w-0">
                        <p className="font-semibold mb-1">Test a routing group</p>
                        <p>
                            Sends a real chat-completion through the proxy against
                            the selected routing group. The proxy applies the
                            group's strategy + cooldown / rate-limit filters and
                            picks a member model — this page reports which one.
                            Every test is recorded in the history visible in the{' '}
                            <strong>Status</strong> tab.
                        </p>
                    </div>
                </div>
            </div>

            {/* Test form */}
            <div className="bg-surface border border-default rounded-lg p-6">
                <h3 className="text-lg font-semibold text-default mb-4">Test Configuration</h3>

                <div className="space-y-4">
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Routing Group
                        </label>
                        {groupsLoading ? (
                            <div className="flex items-center space-x-2 text-sm text-muted">
                                <RefreshCw className="w-4 h-4 animate-spin" />
                                <span>Loading routing groups…</span>
                            </div>
                        ) : groups.length === 0 ? (
                            <div className="px-3 py-2 bg-yellow-50 border border-yellow-200 rounded-lg text-sm text-yellow-800">
                                No enabled routing groups for this tenant. Create
                                one in <em>Routing Groups</em> first.
                            </div>
                        ) : (
                            <select
                                value={routingGroupId}
                                onChange={(e) => setRoutingGroupId(e.target.value)}
                                className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                            >
                                {groups.map((g) => (
                                    <option key={g.id} value={g.id}>
                                        {g.name} — {g.mode || 'chat'} · {g.routing_strategy} ({g.num_models} model
                                        {g.num_models === 1 ? '' : 's'})
                                    </option>
                                ))}
                            </select>
                        )}
                        {selectedGroup && (
                            <p className="text-xs text-muted mt-1">
                                {selectedGroup.num_models} model
                                {selectedGroup.num_models === 1 ? '' : 's'} in this
                                group · mode{' '}
                                <code className="text-[11px]">
                                    {selectedGroup.mode || 'chat'}
                                </code>{' '}
                                · strategy{' '}
                                <code className="text-[11px]">
                                    {selectedGroup.routing_strategy}
                                </code>
                            </p>
                        )}
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Test Message
                        </label>
                        <textarea
                            value={testMessage}
                            onChange={(e) => setTestMessage(e.target.value)}
                            rows={3}
                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                            placeholder={DEFAULT_TEST_MESSAGE}
                        />
                        <p className="text-xs text-muted mt-1">
                            Sent as the user turn. The default prompt is small and
                            costs roughly $0.0001 across most catalogue models.
                        </p>
                    </div>

                    <div className="flex items-center justify-end gap-4 pt-4 border-t border-default">
                        {!canTest && (
                            <p className="text-xs text-muted" data-testid="router-test-not-permitted">
                                A test sends a real, billed request through the routing group. It needs the
                                llm:router:test permission, which you do not hold.
                            </p>
                        )}
                        <button
                            onClick={asyncHandler(runTest, 'click')}
                            disabled={!canTest || testing || !routingGroupId || groups.length === 0}
                            className="flex items-center space-x-2 px-6 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {testing ? (
                                <>
                                    <RefreshCw className="w-4 h-4 animate-spin" />
                                    <span>Testing…</span>
                                </>
                            ) : (
                                <>
                                    <Play className="w-4 h-4" />
                                    <span>Run Test</span>
                                </>
                            )}
                        </button>
                    </div>
                </div>
            </div>

            {/* Result */}
            {result && (
                <div className="bg-surface border border-default rounded-lg p-6">
                    <div className="flex items-center space-x-2 mb-4">
                        {result.error ? (
                            <>
                                <XCircle className="w-5 h-5 text-red-600" />
                                <h3 className="text-lg font-semibold text-default">
                                    Test failed
                                </h3>
                            </>
                        ) : (
                            <>
                                <CheckCircle2 className="w-5 h-5 text-green-600" />
                                <h3 className="text-lg font-semibold text-default">
                                    Routing decision
                                </h3>
                            </>
                        )}
                    </div>

                    {!result.error ? (
                        <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-4">
                            <div className="flex items-start space-x-3">
                                <Server className="w-5 h-5 text-green-700 mt-0.5" />
                                <div className="flex-1 min-h-0 min-w-0">
                                    <div className="font-semibold text-green-900 mb-1">
                                        Selected model: {result.selected_model || '<unknown>'}
                                    </div>
                                    <div className="text-sm text-green-800">
                                        {result.selected_provider
                                            ? `Provider: ${result.selected_provider} · `
                                            : ''}
                                        Strategy: {result.routing_strategy}
                                    </div>
                                </div>
                            </div>
                        </div>
                    ) : (
                        <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4 text-sm text-red-800 whitespace-pre-wrap break-words">
                            {/* A model the gateway cannot dispatch: what to fix
                                first, then the gateway's answer, which names
                                the model of the group it is about. */}
                            {explainDispatchRefusal(result.error) && (
                                <p className="font-medium mb-2">{explainDispatchRefusal(result.error)}</p>
                            )}
                            <p>{result.error}</p>
                        </div>
                    )}

                    <div className="grid grid-cols-3 gap-3 mb-4">
                        <div className="p-3 bg-surface-alt rounded-lg">
                            <div className="text-xs text-secondary mb-1">Status</div>
                            <div className="text-sm font-semibold text-default">
                                {result.status_code ?? '—'}
                            </div>
                        </div>
                        <div className="p-3 bg-surface-alt rounded-lg flex flex-col">
                            <div className="text-xs text-secondary mb-1">Latency</div>
                            <div className="text-sm font-semibold text-default flex items-center space-x-1">
                                <Clock className="w-3.5 h-3.5 text-muted" />
                                <span>
                                    {typeof result.latency_ms === 'number'
                                        ? `${Math.round(result.latency_ms)} ms`
                                        : '—'}
                                </span>
                            </div>
                        </div>
                        <div className="p-3 bg-surface-alt rounded-lg">
                            <div className="text-xs text-secondary mb-1">Group</div>
                            <div className="text-sm font-semibold text-default truncate">
                                {result.routing_group_name}
                            </div>
                        </div>
                    </div>

                    {result.response_text && (
                        <div className="p-4 bg-surface-alt border border-default rounded-lg">
                            <div className="text-xs text-secondary uppercase tracking-wide mb-2">
                                Response
                            </div>
                            <div className="text-sm text-default whitespace-pre-wrap break-words">
                                {result.response_text}
                            </div>
                        </div>
                    )}
                </div>
            )}

            {/* Pre-test placeholder — kept around so the page
                doesn't feel empty before the first run. */}
            {error && (
                <div className="bg-red-50 border border-red-200 rounded-lg p-4">
                    <div className="flex items-start space-x-3">
                        <XCircle className="w-5 h-5 text-red-600 mt-0.5 flex-shrink-0" />
                        <div className="flex-1 text-sm text-red-800 min-h-0 min-w-0">{error}</div>
                    </div>
                </div>
            )}

            {!result && !error && !testing && groups.length > 0 && (
                <div className="text-center py-12 bg-surface-alt rounded-lg border border-default">
                    <Play className="w-16 h-16 text-muted mx-auto mb-4" />
                    <h3 className="text-lg font-semibold text-default mb-2">
                        Ready to Test
                    </h3>
                    <p className="text-secondary">
                        Pick a routing group above and click <strong>Run Test</strong>{' '}
                        to send a message through the proxy.
                    </p>
                </div>
            )}
        </div>
    );
};

export default RouterTestSubTab;
