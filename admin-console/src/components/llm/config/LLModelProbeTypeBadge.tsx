/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Secondary health badge — sits next to `LLModelStatusBadge`
 * (Healthy / Unhealthy / …) and tells the operator *which kind*
 * of health probe produced that verdict. Three values:
 *
 *   - "Catalog Probe"   (blue)  — provider's `/models` endpoint was
 *                                 hit; key + reachability + model
 *                                 name presence verified at zero
 *                                 cost. Strongest possible signal
 *                                 short of actual inference.
 *
 *   - "Endpoint Probe"  (green) — a real (cheap) inference call
 *                                 to the model's mode endpoint
 *                                 succeeded. Verifies the actual
 *                                 generation path end-to-end.
 *
 *   - "Synthetic Probe" (amber) — Brutor-side validation 400 only.
 *                                 The request never reached the
 *                                 provider; the green Healthy state
 *                                 next door is tentative. Operator
 *                                 should treat as "configured" not
 *                                 "verified-reachable".
 *
 * A `null` / missing probe_type renders nothing: no probe ran for the
 * row (a routing model, an unexpected error, or a check that backed
 * off).
 */

import React from 'react';
import { ServerCog, Zap, AlertTriangle } from 'lucide-react';
import { ProbeType } from '../../../lib/llmTypes';

interface Props {
    probeType?: ProbeType | null;
    /** Show the textual label. Default true; set false to render
     *  the icon-only chip in tight spaces. */
    showLabel?: boolean;
}

const probeTypeConfig: Record<
    ProbeType,
    { label: string; bg: string; text: string; icon: typeof ServerCog; tooltip: string }
> = {
    catalog: {
        label: 'Catalog Probe',
        bg: 'bg-blue-100',
        text: 'text-blue-700',
        icon: ServerCog,
        tooltip:
            "Provider's /models endpoint was reached and the configured " +
            'model name was verified in the listing. Free to run, ' +
            'verifies API key + reachability + that the model exists ' +
            'on the provider side.',
    },
    endpoint: {
        label: 'Endpoint Probe',
        bg: 'bg-green-100',
        text: 'text-green-700',
        icon: Zap,
        tooltip:
            'A real (cheap) inference call to the model endpoint ' +
            'succeeded. Verifies the actual generation path end-to-end.',
    },
    synthetic: {
        label: 'Synthetic Probe',
        bg: 'bg-amber-100',
        text: 'text-amber-800',
        icon: AlertTriangle,
        tooltip:
            'Brutor-side validation only — the request never reached ' +
            "the provider. The Healthy state is tentative; treat as " +
            "'configured' not 'verified-reachable'. To get a stronger " +
            "signal, set the provider's Models API endpoint URL or " +
            'pick a model mode that supports a real probe (chat, ' +
            'embedding, TTS, STT all do).',
    },
};

const LLModelProbeTypeBadge: React.FC<Props> = ({
    probeType,
    showLabel = true,
}) => {
    if (!probeType) {
        return null;
    }
    const config = probeTypeConfig[probeType];
    if (!config) {
        // Defensive: backend introduced a new probe_type the UI
        // doesn't recognise. Render nothing rather than crashing.
        return null;
    }
    const Icon = config.icon;
    return (
        <div
            className={`inline-flex items-center space-x-1 rounded px-2 py-0.5 text-xs ${config.bg} ${config.text}`}
            title={config.tooltip}
        >
            <Icon className="w-3 h-3" />
            {showLabel && <span className="font-medium">{config.label}</span>}
        </div>
    );
};

export default LLModelProbeTypeBadge;
