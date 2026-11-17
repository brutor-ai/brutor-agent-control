/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';

import { explainGatewayRefusal } from '../../lib/gatewayRefusals';

/**
 * Beside a recorded error: what it means and what to do, for a refusal of
 * the gateway the console has words for (`gatewayRefusals.ts`). Nothing for
 * any other error. The recorded text stays where it is shown.
 */
export const GatewayRefusalNote: React.FC<{ text: string | null | undefined }> = ({ text }) => {
    const explained = explainGatewayRefusal(text);
    if (!explained) return null;
    return (
        <p data-testid="gateway-refusal-explained" className="mb-1 text-xs text-red-800 font-medium">
            {explained}
        </p>
    );
};
