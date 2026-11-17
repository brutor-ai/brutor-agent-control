/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Counters deep-link — presentational button that lands on the
 * System → Counters page with the right scope/subject/kind already
 * applied. Used from ProxyLogsViewer expanded rows and from Mission
 * Control system-alert / usage-alert rows. Two-clicks-to-Reset is the
 * north-star UX.
 *
 * Renders an <a> element so middle-click / cmd-click open a new tab —
 * SREs running through PagerDuty playbooks want this. SPA navigation
 * picks it up too (React Router intercepts in-app anchors).
 */

import { Gauge } from 'lucide-react';
import React from 'react';

import {
    CountersDeepLinkInput,
    buildCountersDeepLinkUrl,
} from './countersDeepLink';

export interface CountersDeepLinkButtonProps {
    /** When null, the button does not render. Lets callers drop the
     *  `shouldShow*` predicate inline. */
    input: CountersDeepLinkInput | null;
    /** Optional data-testid override for component-specific tests. */
    testId?: string;
    /** Title attribute (tooltip). Defaults to the canonical Reset /
     *  Release explainer. */
    title?: string;
    /** Visible button label. Defaults to "View counters". */
    label?: string;
    /** Optional className override. The default matches the surrounding
     *  toolbar styling on both call sites. */
    className?: string;
}

const DEFAULT_CLASS =
    'inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-default bg-surface hover:bg-surface-raised text-default transition-colors';

const DEFAULT_TITLE =
    'Open the matching rate-limit counter on System → Counters (Reset / Release available there)';

export function CountersDeepLinkButton({
    input,
    testId,
    title = DEFAULT_TITLE,
    label = 'View counters',
    className = DEFAULT_CLASS,
}: CountersDeepLinkButtonProps): React.ReactElement | null {
    if (!input) return null;
    const href = buildCountersDeepLinkUrl(input);
    return (
        <a
            href={href}
            data-testid={testId ?? 'counters-deep-link'}
            className={className}
            title={title}
        >
            <Gauge className="w-3.5 h-3.5" />
            {label}
        </a>
    );
}
