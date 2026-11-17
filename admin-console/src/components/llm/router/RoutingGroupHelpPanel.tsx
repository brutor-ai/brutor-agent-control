/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
    X,
    ArrowRight,
    Shield,
    Zap,
    DollarSign,
    Server,
    GitBranch,
    BarChart3,
    AlertTriangle,
    Share2,
} from 'lucide-react';
import { TABLE_TH_TYPE, TABLE_THEAD } from '../../ui/tableStyles';

// ─── Scenario card ───────────────────────────────────────────────────────────

const ScenarioCard: React.FC<{
    icon: React.ElementType;
    title: string;
    challenge: string;
    solution: string;
    strategy: string;
    strategyId: string;
    color: string;
}> = ({ icon: Icon, title, challenge, solution, strategy, strategyId, color }) => (
    <div className="border border-default rounded-lg overflow-hidden">
        <div className={`flex items-center space-x-2 px-4 py-2.5 ${color}`}>
            <Icon className="w-4 h-4 flex-shrink-0" />
            <span className="text-sm font-semibold">{title}</span>
        </div>
        <div className="p-4 space-y-2 text-sm">
            <div>
                <span className="font-medium text-secondary">Challenge: </span>
                <span className="text-secondary">{challenge}</span>
            </div>
            <div>
                <span className="font-medium text-secondary">Solution: </span>
                <span className="text-secondary">{solution}</span>
            </div>
            <div className="flex items-center space-x-2 pt-1">
                <ArrowRight className="w-3 h-3 text-muted" />
                <span className="text-xs text-muted">
                    Recommended strategy: <strong className="text-secondary">{strategy}</strong>
                    <code className="ml-1.5 text-[10px] text-muted bg-surface-raised px-1 py-0.5 rounded">{strategyId}</code>
                </span>
            </div>
        </div>
    </div>
);

// ─── Component ───────────────────────────────────────────────────────────────

const RoutingGroupHelpPanel: React.FC<{ onClose?: () => void }> = ({ onClose }) => {
    return (
        <div className="bg-surface border border-default rounded-lg overflow-hidden">
            {/* ── Header ──────────────────────────────────────────────── */}
            <div className="flex items-center justify-between px-6 py-4 bg-gradient-to-r from-primary-50 to-blue-50 border-b border-default">
                <div className="flex items-center space-x-2.5">
                    <Share2 className="w-5 h-5 text-primary-600" />
                    <h3 className="text-base font-semibold text-default">Routing &amp; Load Balancing Guide</h3>
                </div>
                {onClose && (
                    <button onClick={onClose} className="p-1 text-muted hover:text-secondary rounded transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                )}
            </div>

            <div className="px-6 py-5 space-y-8 max-h-[70vh] overflow-y-auto">

                {/* ── What is routing? ─────────────────────────────────── */}
                <section>
                    <h4 className="text-sm font-bold text-default mb-2 uppercase tracking-wide">What is Routing?</h4>
                    <p className="text-sm text-secondary leading-relaxed">
                        Routing enables you to place multiple LLM deployments behind a single virtual endpoint
                        called a <strong>Routing Group</strong>. When a client sends a request to the group,
                        the router automatically selects the best available model based on the group's strategy —
                        handling failover, load distribution, and cost control transparently.
                    </p>
                    <p className="text-sm text-secondary mt-2 leading-relaxed">
                        This means your applications never need to know about individual models, API keys, or
                        provider-specific quirks. They just call the routing group ID as if it were a single model.
                    </p>
                </section>

                {/* ── How it works ─────────────────────────────────────── */}
                <section>
                    <h4 className="text-sm font-bold text-default mb-3 uppercase tracking-wide">How It Works</h4>
                    <div className="flex items-center justify-center space-x-3 py-4 bg-surface-alt rounded-lg border border-default">
                        <div className="flex flex-col items-center">
                            <div className="w-10 h-10 rounded-full bg-blue-100 flex items-center justify-center">
                                <Server className="w-5 h-5 text-blue-600" />
                            </div>
                            <span className="text-xs text-secondary mt-1.5 font-medium">Client App</span>
                        </div>
                        <ArrowRight className="w-4 h-4 text-muted" />
                        <div className="flex flex-col items-center">
                            <div className="w-10 h-10 rounded-full bg-primary-100 flex items-center justify-center">
                                <GitBranch className="w-5 h-5 text-primary-600" />
                            </div>
                            <span className="text-xs text-secondary mt-1.5 font-medium">Routing Group</span>
                        </div>
                        <ArrowRight className="w-4 h-4 text-muted" />
                        <div className="flex flex-col items-center space-y-1">
                            <div className="flex space-x-1.5">
                                <div className="w-8 h-8 rounded bg-green-100 flex items-center justify-center">
                                    <span className="text-[10px] font-bold text-green-700">A</span>
                                </div>
                                <div className="w-8 h-8 rounded bg-green-100 flex items-center justify-center">
                                    <span className="text-[10px] font-bold text-green-700">B</span>
                                </div>
                                <div className="w-8 h-8 rounded bg-surface-raised flex items-center justify-center">
                                    <span className="text-[10px] font-bold text-muted">C</span>
                                </div>
                            </div>
                            <span className="text-xs text-secondary font-medium">LLM Deployments</span>
                        </div>
                    </div>
                    <div className="mt-3 grid grid-cols-3 gap-3 text-center">
                        <div className="p-2.5 bg-surface-alt rounded-lg">
                            <span className="text-xs font-semibold text-default block">1. Request arrives</span>
                            <span className="text-[11px] text-muted">Client calls the routing group ID</span>
                        </div>
                        <div className="p-2.5 bg-surface-alt rounded-lg">
                            <span className="text-xs font-semibold text-default block">2. Strategy selects</span>
                            <span className="text-[11px] text-muted">Router picks the best healthy model</span>
                        </div>
                        <div className="p-2.5 bg-surface-alt rounded-lg">
                            <span className="text-xs font-semibold text-default block">3. Failover if needed</span>
                            <span className="text-[11px] text-muted">Automatic retry with fallback models</span>
                        </div>
                    </div>
                </section>

                {/* ── Available strategies ─────────────────────────────── */}
                <section>
                    <h4 className="text-sm font-bold text-default mb-3 uppercase tracking-wide">Routing Strategies at a Glance</h4>
                    <div className="overflow-hidden rounded-lg border border-default">
                        <table className="w-full text-sm">
                            <thead className={TABLE_THEAD}>
                            <tr className="text-left">
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Strategy</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Optimizes For</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Best When</th>
                            </tr>
                            </thead>
                            <tbody className="divide-y divide-default">
                            <tr>
                                <td className="px-3 py-2 font-medium text-default">Weighted Shuffle<br/><code className="text-[10px] text-muted">simple-shuffle</code></td>
                                <td className="px-3 py-2 text-secondary">Even distribution</td>
                                <td className="px-3 py-2 text-secondary">Models have similar performance &amp; capacity</td>
                            </tr>
                            <tr>
                                <td className="px-3 py-2 font-medium text-default">Queue-Aware<br/><code className="text-[10px] text-muted">least-busy</code></td>
                                <td className="px-3 py-2 text-secondary">Throughput</td>
                                <td className="px-3 py-2 text-secondary">Requests vary in length; some models are slower</td>
                            </tr>
                            <tr className="bg-green-50/50">
                                <td className="px-3 py-2 font-medium text-default">Rate-Limit Optimized<br/><code className="text-[10px] text-muted">usage-based-routing-v2</code></td>
                                <td className="px-3 py-2 text-secondary">Rate-limit headroom</td>
                                <td className="px-3 py-2 text-secondary">Multiple API keys or strict provider limits</td>
                            </tr>
                            <tr>
                                <td className="px-3 py-2 font-medium text-default">Fastest Response<br/><code className="text-[10px] text-muted">latency-based-routing</code></td>
                                <td className="px-3 py-2 text-secondary">Latency</td>
                                <td className="px-3 py-2 text-secondary">Real-time apps where every 100ms matters</td>
                            </tr>
                            <tr>
                                <td className="px-3 py-2 font-medium text-default">Cheapest Healthy<br/><code className="text-[10px] text-muted">cost-based-routing</code></td>
                                <td className="px-3 py-2 text-secondary">Blended input+output price</td>
                                <td className="px-3 py-2 text-secondary">Equivalent deployments (same model, 2 providers/regions) + failover. Deterministic — a static pin across capability tiers, not a per-request optimization.</td>
                            </tr>
                            </tbody>
                        </table>
                    </div>
                </section>

                {/* ── Common scenarios ─────────────────────────────────── */}
                <section>
                    <h4 className="text-sm font-bold text-default mb-3 uppercase tracking-wide">Common Scenarios</h4>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <ScenarioCard
                            icon={Shield}
                            title="High-Availability Production"
                            challenge="Your app can't go down if one provider has an outage."
                            solution="Create a routing group with 2–3 providers (e.g., OpenAI + Anthropic + Azure). The router automatically fails over if one goes down."
                            strategy="Weighted Shuffle or Rate-Limit Optimized"
                            strategyId="simple-shuffle / usage-based-routing-v2"
                            color="bg-sky-50 text-sky-800"
                        />
                        <ScenarioCard
                            icon={DollarSign}
                            title="Cost Optimization"
                            challenge="You want to minimize spend without sacrificing availability."
                            solution="Mix cheap and premium models in one group. Cost-Based routing prefers the cheapest healthy model and only escalates when it's unavailable."
                            strategy="Cheapest Healthy"
                            strategyId="cost-based-routing"
                            color="bg-sky-50 text-sky-800"
                        />
                        <ScenarioCard
                            icon={Zap}
                            title="Latency-Sensitive Chatbot"
                            challenge="Users expect near-instant responses; provider latency fluctuates."
                            solution="Pool several fast models. Latency-Based routing continuously measures response times and steers traffic to whoever is fastest right now."
                            strategy="Fastest Response"
                            strategyId="latency-based-routing"
                            color="bg-sky-50 text-sky-800"
                        />
                        <ScenarioCard
                            icon={AlertTriangle}
                            title="Rate-Limit Management"
                            challenge="You have multiple API keys with different TPM/RPM limits across providers."
                            solution="Group all keys under one endpoint. Rate-Limit Optimized routing tracks real-time usage via Redis and distributes requests to stay under each key's limits."
                            strategy="Rate-Limit Optimized"
                            strategyId="usage-based-routing-v2"
                            color="bg-sky-50 text-sky-800"
                        />
                        <ScenarioCard
                            icon={BarChart3}
                            title="A/B Testing Models"
                            challenge="You want to compare GPT-4o vs Claude Sonnet in production."
                            solution="Put both in a Weighted Shuffle group with 50/50 weights. Monitor per-model metrics in the Status tab to compare performance, cost, and error rates."
                            strategy="Weighted Shuffle"
                            strategyId="simple-shuffle"
                            color="bg-sky-50 text-sky-800"
                        />
                        <ScenarioCard
                            icon={Server}
                            title="Self-Hosted + Cloud Hybrid"
                            challenge="You run a local vLLM/Ollama instance but need cloud fallback for spikes."
                            solution="Create a group with your self-hosted model as primary and a cloud model as fallback. Queue-Aware routing avoids overloading your local GPU."
                            strategy="Queue-Aware"
                            strategyId="least-busy"
                            color="bg-sky-50 text-sky-800"
                        />
                    </div>
                </section>

                {/* ── Getting started ──────────────────────────────────── */}
                <section>
                    <h4 className="text-sm font-bold text-default mb-3 uppercase tracking-wide">Getting Started</h4>
                    <div className="bg-surface-alt rounded-lg border border-default p-4">
                        <ol className="space-y-3 text-sm text-secondary">
                            <li className="flex items-start space-x-3">
                                <span className="flex-shrink-0 w-6 h-6 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center text-xs font-bold">1</span>
                                <div>
                                    <strong className="text-default">Create a Routing Group</strong>
                                    <p className="text-xs text-muted mt-0.5">Click "Create Routing Group" above, give it an ID (this becomes the model name in API calls), and pick a strategy.</p>
                                </div>
                            </li>
                            <li className="flex items-start space-x-3">
                                <span className="flex-shrink-0 w-6 h-6 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center text-xs font-bold">2</span>
                                <div>
                                    <strong className="text-default">Assign Models</strong>
                                    <p className="text-xs text-muted mt-0.5">Go to the <strong>LLMs</strong> tab and set each model's <em>routing_group_id</em> to your new group. Adjust weights if needed.</p>
                                </div>
                            </li>
                            <li className="flex items-start space-x-3">
                                <span className="flex-shrink-0 w-6 h-6 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center text-xs font-bold">3</span>
                                <div>
                                    <strong className="text-default">Test</strong>
                                    <p className="text-xs text-muted mt-0.5">Use the <strong>Test Router</strong> tab to verify routing decisions without sending real requests.</p>
                                </div>
                            </li>
                            <li className="flex items-start space-x-3">
                                <span className="flex-shrink-0 w-6 h-6 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center text-xs font-bold">4</span>
                                <div>
                                    <strong className="text-default">Point Your App</strong>
                                    <p className="text-xs text-muted mt-0.5">Update your client to use the routing group ID as the <code className="bg-surface-inset px-1 rounded text-xs">model</code> parameter. That's it — the gateway handles the rest.</p>
                                </div>
                            </li>
                        </ol>
                    </div>
                </section>
            </div>
        </div>
    );
};

export default RoutingGroupHelpPanel;