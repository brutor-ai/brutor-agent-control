/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * "Not served": the mark of a model the gateway calls no more although it
 * reads as enabled. From its retire date on, and while its provider is
 * disabled, a model takes no call. Nothing is rendered for a model the
 * gateway serves.
 */

import React from 'react';
import { Ban } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useLlmProviders } from '../../../hooks/useLlmProviders';
import type { LLModelConfig } from '../../../lib/llmTypes';
import { notServedReasons, notServedText } from '../../../lib/modelServed';

const LLModelNotServedBadge: React.FC<{ model: Pick<LLModelConfig, 'retire_date' | 'provider' | 'mode'> }> = ({ model }) => {
    const { gatewayClient } = useGateway();
    // The same cached list the provider logos read.
    const { providers } = useLlmProviders(gatewayClient ?? null);
    const reasons = notServedReasons(model, providers);
    if (reasons.length === 0) return null;
    return (
        <span
            data-testid="model-not-served"
            title={notServedText(reasons)}
            className="inline-flex items-center space-x-1 text-xs font-medium text-red-700 bg-red-100 px-2 py-0.5 rounded-full border border-red-300 whitespace-nowrap"
        >
            <Ban className="w-3 h-3" />
            <span>Not served</span>
        </span>
    );
};

export default LLModelNotServedBadge;
