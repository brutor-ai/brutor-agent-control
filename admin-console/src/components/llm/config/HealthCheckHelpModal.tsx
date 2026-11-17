/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * HealthCheckHelpModal — operator-facing reference for the
 * different probe taxonomies behind Brutor's health-check system.
 *
 * Shown from the info button on the AI Models config screen's
 * health-check banner. Explains:
 *
 *   - The three probe types (catalog, endpoint, synthetic) and
 *     what each verifies vs leaves untested.
 *   - Per-mode probe choices (chat, embedding, TTS, STT, image,
 *     video, etc.) and why each was picked.
 *   - The cost profile of every probe so operators can reason
 *     about how much $$$ a health-check sweep can incur.
 *   - The "synthetic ⚠" warning — what it actually means and
 *     when to trust the green Healthy badge.
 *
 * No state of its own; closes via `onClose`. Static reference
 * content — easier to edit-as-prose than to scrape from running
 * config.
 */

import React from 'react';
import {
    X,
    ServerCog,
    Zap,
    AlertTriangle,
    DollarSign,
    Info as InfoIcon,
} from 'lucide-react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';

interface Props {
    isOpen: boolean;
    onClose: () => void;
}

const HealthCheckHelpModal: React.FC<Props> = ({ isOpen, onClose }) => {
    if (!isOpen) return null;

    return (
        <div
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
            onClick={onClose}
        >
            <div
                className="bg-surface rounded-lg shadow-xl max-w-3xl w-full max-h-[90vh] overflow-hidden flex flex-col"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="flex items-center justify-between p-5 border-b border-default">
                    <div className="flex items-center space-x-2">
                        <InfoIcon className="w-5 h-5 text-primary-600" />
                        <h2 className="text-lg font-semibold text-default">
                            Model health checks — how they work
                        </h2>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1 hover:bg-surface-alt rounded transition-colors"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5 text-secondary" />
                    </button>
                </div>

                {/* Body — scrolls within the dialog */}
                <div className="scroll-region p-6 space-y-6">
                    {/* Intro */}
                    <p className="text-sm text-secondary leading-6">
                        Brutor probes every enabled model on a schedule and on demand.
                        The probe type chosen depends on the model&apos;s mode and the
                        provider&apos;s capabilities. The two badges next to each model
                        — the green/red <strong>Health</strong> badge and the colored{' '}
                        <strong>Probe-type</strong> badge — together tell you both{' '}
                        <em>what</em> the latest probe found and <em>how thoroughly</em>{' '}
                        it actually checked.
                    </p>

                    {/* Probe-type taxonomy */}
                    <section>
                        <h3 className="text-sm font-semibold text-default uppercase tracking-wide mb-3">
                            Probe types
                        </h3>
                        <div className="space-y-3">
                            {/* Catalog probe */}
                            <div className="border border-blue-200 bg-blue-50 rounded-lg p-4">
                                <div className="flex items-center space-x-2 mb-2">
                                    <ServerCog className="w-4 h-4 text-blue-700" />
                                    <span className="font-semibold text-blue-700">
                                        Catalog Probe
                                    </span>
                                </div>
                                <p className="text-sm text-secondary leading-6">
                                    Issues <code className="font-mono text-xs bg-surface px-1 rounded">GET /v1/models</code>{' '}
                                    against the provider with the model&apos;s API key,
                                    then verifies the configured <code className="font-mono text-xs bg-surface px-1 rounded">model_name</code>{' '}
                                    is in the returned listing.
                                </p>
                                <ul className="text-sm text-secondary mt-2 ml-5 list-disc space-y-0.5">
                                    <li>
                                        <strong>Verifies:</strong> API key valid + network
                                        reachable + model exists at the provider.
                                    </li>
                                    <li>
                                        <strong>Cost:</strong> $0 — every supported provider
                                        offers a free model-listing endpoint.
                                    </li>
                                    <li>
                                        <strong>Used for:</strong> image, video, and any
                                        mode whose provider has a known catalog endpoint
                                        (OpenAI, Anthropic, Mistral, etc.).
                                    </li>
                                </ul>
                            </div>

                            {/* Endpoint probe */}
                            <div className="border border-green-200 bg-green-50 rounded-lg p-4">
                                <div className="flex items-center space-x-2 mb-2">
                                    <Zap className="w-4 h-4 text-green-700" />
                                    <span className="font-semibold text-green-700">
                                        Endpoint Probe
                                    </span>
                                </div>
                                <p className="text-sm text-secondary leading-6">
                                    Issues a real, minimum-viable inference call to the
                                    model&apos;s actual mode endpoint. Returns a real
                                    response from the upstream provider.
                                </p>
                                <ul className="text-sm text-secondary mt-2 ml-5 list-disc space-y-0.5">
                                    <li>
                                        <strong>Verifies:</strong> the full inference path
                                        end-to-end — auth, network, model, generation.
                                    </li>
                                    <li>
                                        <strong>Cost:</strong> negligible (~$0.000003 –
                                        $0.0001 per probe depending on mode).
                                    </li>
                                    <li>
                                        <strong>Used for:</strong> chat, embeddings, TTS
                                        (1-character input), STT (silent 1-second WAV).
                                    </li>
                                </ul>
                            </div>

                            {/* Synthetic probe */}
                            <div className="border border-amber-200 bg-amber-50 rounded-lg p-4">
                                <div className="flex items-center space-x-2 mb-2">
                                    <AlertTriangle className="w-4 h-4 text-amber-800" />
                                    <span className="font-semibold text-amber-800">
                                        Synthetic Probe
                                    </span>
                                </div>
                                <p className="text-sm text-secondary leading-6">
                                    Sends an intentionally-invalid request that
                                    Brutor&apos;s own validation layer rejects with{' '}
                                    <code className="font-mono text-xs bg-surface px-1 rounded">400</code>{' '}
                                    before any upstream call. Used as a fallback when the
                                    provider has no <code className="font-mono text-xs bg-surface px-1 rounded">/models</code>{' '}
                                    endpoint and a real probe would burn money (e.g. video
                                    generation).
                                </p>
                                <ul className="text-sm text-secondary mt-2 ml-5 list-disc space-y-0.5">
                                    <li>
                                        <strong>Verifies:</strong> the proxy is reachable
                                        and Brutor&apos;s validation logic loads.{' '}
                                        <strong>Does NOT</strong> verify the upstream API
                                        key, network, or model availability.
                                    </li>
                                    <li>
                                        <strong>Cost:</strong> $0.
                                    </li>
                                    <li>
                                        <strong>Trust level:</strong> tentative. A green{' '}
                                        <em>Healthy</em> badge alongside a synthetic probe
                                        means &quot;configured&quot;, not
                                        &quot;verified&quot;. The first real request is
                                        what actually validates.
                                    </li>
                                    <li>
                                        <strong>Used for:</strong> image &amp; video models
                                        on providers without a catalog endpoint
                                        (e.g. <code className="font-mono text-xs bg-surface px-1 rounded">fal_ai</code>).
                                    </li>
                                </ul>
                            </div>
                        </div>
                    </section>

                    {/* Per-mode quick reference */}
                    <section>
                        <h3 className="text-sm font-semibold text-default uppercase tracking-wide mb-3">
                            What each model mode actually probes
                        </h3>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm border border-default rounded-lg">
                                <thead className="bg-surface-alt">
                                    <tr>
                                        <th className={`text-left px-3 py-2 border-b border-default ${TABLE_TH_TYPE}`}>
                                            Mode
                                        </th>
                                        <th className={`text-left px-3 py-2 border-b border-default ${TABLE_TH_TYPE}`}>
                                            Probe type
                                        </th>
                                        <th className={`text-left px-3 py-2 border-b border-default ${TABLE_TH_TYPE}`}>
                                            What runs
                                        </th>
                                        <th className={`text-left px-3 py-2 border-b border-default ${TABLE_TH_TYPE}`}>
                                            Cost
                                        </th>
                                    </tr>
                                </thead>
                                <tbody className="text-secondary">
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">chat</td>
                                        <td className="px-3 py-2">Endpoint</td>
                                        <td className="px-3 py-2">
                                            <code className="text-xs">POST /chat/completions</code>{' '}
                                            with 1 user turn, max_tokens=5
                                        </td>
                                        <td className="px-3 py-2">~$0.0001</td>
                                    </tr>
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">responses</td>
                                        <td className="px-3 py-2">Endpoint</td>
                                        <td className="px-3 py-2">
                                            <code className="text-xs">POST /responses</code>{' '}
                                            with reasoning.effort=&quot;low&quot;
                                        </td>
                                        <td className="px-3 py-2">~$0.0001</td>
                                    </tr>
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">completion</td>
                                        <td className="px-3 py-2">Endpoint</td>
                                        <td className="px-3 py-2">
                                            <code className="text-xs">POST /completions</code>{' '}
                                            with prompt=&quot;Say hello&quot;
                                        </td>
                                        <td className="px-3 py-2">~$0.0001</td>
                                    </tr>
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">embedding</td>
                                        <td className="px-3 py-2">Endpoint</td>
                                        <td className="px-3 py-2">
                                            <code className="text-xs">POST /embeddings</code>{' '}
                                            with input=[&quot;health check&quot;]
                                        </td>
                                        <td className="px-3 py-2">~$0.000003 (free on Ollama)</td>
                                    </tr>
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">audio_speech</td>
                                        <td className="px-3 py-2">Endpoint</td>
                                        <td className="px-3 py-2">
                                            <code className="text-xs">POST /audio/speech</code>{' '}
                                            with 1-character input
                                        </td>
                                        <td className="px-3 py-2">~$0.000060</td>
                                    </tr>
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">audio_transcription</td>
                                        <td className="px-3 py-2">Endpoint</td>
                                        <td className="px-3 py-2">
                                            <code className="text-xs">POST /audio/transcriptions</code>{' '}
                                            with 1-second silent WAV
                                        </td>
                                        <td className="px-3 py-2">~$0.0001</td>
                                    </tr>
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">image_generation</td>
                                        <td className="px-3 py-2">
                                            Catalog (preferred)<br />
                                            <span className="text-amber-700">Synthetic</span> fallback
                                        </td>
                                        <td className="px-3 py-2">
                                            Provider listing if available; else Brutor
                                            validates <code className="text-xs">n=0</code>{' '}
                                            and 400s
                                        </td>
                                        <td className="px-3 py-2">$0</td>
                                    </tr>
                                    <tr className="border-b border-default">
                                        <td className="px-3 py-2 font-mono text-xs">video_generation</td>
                                        <td className="px-3 py-2">
                                            Catalog (preferred)<br />
                                            <span className="text-amber-700">Synthetic</span> fallback
                                        </td>
                                        <td className="px-3 py-2">
                                            Provider listing if available; else Brutor
                                            validates <code className="text-xs">duration_seconds=-1</code>{' '}
                                            and 400s
                                        </td>
                                        <td className="px-3 py-2">$0</td>
                                    </tr>
                                    <tr>
                                        <td className="px-3 py-2 font-mono text-xs">rerank, moderation, routing</td>
                                        <td className="px-3 py-2 text-muted">— skipped —</td>
                                        <td className="px-3 py-2">
                                            No synchronous probe endpoint — health is
                                            implicitly verified by live request traffic
                                        </td>
                                        <td className="px-3 py-2">$0</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* When to worry */}
                    <section className="border border-amber-200 bg-amber-50 rounded-lg p-4">
                        <div className="flex items-center space-x-2 mb-2">
                            <AlertTriangle className="w-4 h-4 text-amber-800" />
                            <span className="font-semibold text-amber-800">
                                When the green badge is NOT enough
                            </span>
                        </div>
                        <p className="text-sm text-secondary leading-6">
                            A model showing <strong>Healthy</strong> + <strong>Synthetic
                            Probe</strong> is configured well enough to pass Brutor&apos;s
                            own validation, but the upstream API key has not been verified
                            against the provider. The first real generation request from a
                            user is what genuinely confirms it works. To get a stronger
                            signal upgrade the provider&apos;s{' '}
                            <code className="font-mono text-xs bg-surface px-1 rounded">
                                Models API endpoint
                            </code>{' '}
                            URL on the Providers page if one exists, or accept the
                            tentative state and rely on operational monitoring of real
                            traffic.
                        </p>
                    </section>

                    {/* Cost concern */}
                    <section className="border border-default rounded-lg p-4 bg-surface-alt">
                        <div className="flex items-center space-x-2 mb-2">
                            <DollarSign className="w-4 h-4 text-secondary" />
                            <span className="font-semibold text-default">
                                Probing cost — what to expect
                            </span>
                        </div>
                        <p className="text-sm text-secondary leading-6">
                            A full health-check sweep against the default seed (~8 enabled
                            models) costs well under <strong>$0.001 per cycle</strong>.
                            Catalog and synthetic probes are free; endpoint probes burn
                            negligible token volume. Even at the default 5-minute cadence
                            running 24/7, expect under <strong>$0.30 / month</strong> in
                            probe traffic. If your tenant has many enabled models or a
                            higher cadence, the largest contributor is chat-completion
                            probes against frontier models — the rest is rounding error.
                        </p>
                    </section>
                </div>

                {/* Footer */}
                <div className="border-t border-default p-4 flex justify-end">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-sm font-medium bg-primary-600 text-white rounded hover:bg-primary-700 transition-colors"
                    >
                        Got it
                    </button>
                </div>
            </div>
        </div>
    );
};

export default HealthCheckHelpModal;
