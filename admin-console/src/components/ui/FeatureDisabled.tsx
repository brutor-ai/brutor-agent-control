/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { Lock } from 'lucide-react';

import { useSlot } from '../../app/slots';

interface FeatureDisabledProps {
    /** The tenant feature flag that is off. */
    flag: string;
    featureName: string;
}

/**
 * Shown in place of a screen whose feature is off for the current tenant.
 *
 * The console on its own knows only that the flag is off. A composition
 * that knows why (the slot `feature-disabled.reason`: the license does not
 * include the feature, or it is switched off for this tenant) says so in
 * place of the words below, which it is handed for the time it does not
 * know.
 */
export function FeatureDisabled({ flag, featureName }: FeatureDisabledProps) {
    const [reason] = useSlot('feature-disabled.reason');
    const ownWords = (
        <>
            <h3 className="text-lg font-semibold text-default mb-2">{featureName} is not enabled</h3>
            <p className="text-muted max-w-md text-sm">
                This feature is not enabled for your tenant. Contact your administrator or{' '}
                <a href="mailto:support@brutor.ai" className="text-primary-500 hover:underline">
                    support@brutor.ai
                </a>{' '}
                to enable it.
            </p>
        </>
    );
    return (
        <div className="flex flex-col items-center justify-center py-20 text-center">
            <div className="w-16 h-16 rounded-full bg-surface-alt flex items-center justify-center mb-4">
                <Lock className="w-8 h-8 text-muted" />
            </div>
            {reason ? reason.render({ flag, featureName, fallback: ownWords }) : ownWords}
        </div>
    );
}
