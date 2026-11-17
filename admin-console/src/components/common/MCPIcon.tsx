/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';

/**
 * MCP (Model Context Protocol) icon, matching the lucide-react interface
 * (`className` drives sizing).
 *
 * The path data is vendored here verbatim (Apache-2.0, lobehub/lobe-icons)
 * rather than imported from `@lobehub/icons`: that package is 55 MB and
 * declares a peer dependency on `@lobehub/ui`, which this project does not
 * use. One icon is not worth either cost.
 */
const MCPIcon: React.FC<{ className?: string; style?: React.CSSProperties }> = ({
    className,
    style,
}) => {
    const sizeMatch = className?.match(/w-(\d+)/);
    const size = sizeMatch?.[1] ? parseInt(sizeMatch[1], 10) * 4 : 20;
    return (
        <span className={className} style={{ display: 'inline-flex', lineHeight: 0, ...style }}>
            <svg
                fill="currentColor"
                fillRule="evenodd"
                height={size}
                width={size}
                viewBox="0 0 24 24"
                xmlns="http://www.w3.org/2000/svg"
                style={{ flex: 'none', lineHeight: 1 }}
                aria-hidden="true"
            >
                <title>MCP</title>
                <path d="M15.688 2.343a2.588 2.588 0 00-3.61 0l-9.626 9.44a.863.863 0 01-1.203 0 .823.823 0 010-1.18l9.626-9.44a4.313 4.313 0 016.016 0 4.116 4.116 0 011.204 3.54 4.3 4.3 0 013.609 1.18l.05.05a4.115 4.115 0 010 5.9l-8.706 8.537a.274.274 0 000 .393l1.788 1.754a.823.823 0 010 1.18.863.863 0 01-1.203 0l-1.788-1.753a1.92 1.92 0 010-2.754l8.706-8.538a2.47 2.47 0 000-3.54l-.05-.049a2.588 2.588 0 00-3.607-.003l-7.172 7.034-.002.002-.098.097a.863.863 0 01-1.204 0 .823.823 0 010-1.18l7.273-7.133a2.47 2.47 0 00-.003-3.537z" />
                <path d="M14.485 4.703a.823.823 0 000-1.18.863.863 0 00-1.204 0l-7.119 6.982a4.115 4.115 0 000 5.9 4.314 4.314 0 006.016 0l7.12-6.982a.823.823 0 000-1.18.863.863 0 00-1.204 0l-7.119 6.982a2.588 2.588 0 01-3.61 0 2.47 2.47 0 010-3.54l7.12-6.982z" />
            </svg>
        </span>
    );
};

export default MCPIcon;
