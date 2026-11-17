/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';

import { FONT_SIZES, FONT_SIZE_LABELS, FONT_SIZE_VALUES } from './theme';
import { useTheme } from './useTheme';

export interface FontSizeSelectorProps {
    title?: string | null;
    description?: string;
    className?: string;
}

/**
 * Font-scale picker. Drives `--font-size-base`, which `body` reads in
 * base.css, so the whole interface scales from one variable.
 *
 * Each option previews its own size, which is the only honest way to
 * present this control.
 */
export const FontSizeSelector: React.FC<FontSizeSelectorProps> = ({
    title = 'Text size',
    description = 'Scales the entire interface.',
    className = '',
}) => {
    const { fontSize, setFontSize } = useTheme();

    return (
        <div className={className}>
            {title !== null && (
                <>
                    <h3 className="text-lg font-semibold text-default mb-1">{title}</h3>
                    <p className="text-sm text-secondary mb-4">{description}</p>
                </>
            )}

            <div
                className="grid grid-cols-2 sm:grid-cols-4 gap-3"
                role="radiogroup"
                aria-label={title ?? 'Text size'}
            >
                {FONT_SIZES.map((size) => {
                    const active = fontSize === size;
                    return (
                        <button
                            key={size}
                            type="button"
                            role="radio"
                            aria-checked={active}
                            onClick={() => setFontSize(size)}
                            className={`px-3 py-3 rounded-lg border-2 text-center transition-all ${
                                active
                                    ? 'border-primary-500 bg-primary-50'
                                    : 'border-default hover:border-strong hover:bg-surface-alt'
                            }`}
                        >
                            <div
                                className={active ? 'text-primary-700 font-medium' : 'text-default'}
                                style={{ fontSize: `${FONT_SIZE_VALUES[size]}px` }}
                            >
                                Aa
                            </div>
                            <div className="text-xs text-muted mt-1">{FONT_SIZE_LABELS[size]}</div>
                        </button>
                    );
                })}
            </div>
        </div>
    );
};
