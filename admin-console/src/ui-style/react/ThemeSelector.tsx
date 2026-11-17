/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { Contrast, Moon, Sun } from 'lucide-react';
import React from 'react';

import { THEME_LABELS, type ThemeName } from './theme';
import { useTheme } from './useTheme';

const THEME_ICONS: Record<ThemeName, React.FC<{ className?: string }>> = {
    light: Sun,
    dark: Moon,
    'high-contrast': Contrast,
};

export interface ThemeSelectorProps {
    /** Heading above the cards. Pass `null` to render the cards alone. */
    title?: string | null;
    description?: string;
    className?: string;
    /**
     * `compact` — icon, name and description (Admin Console).
     * `preview` — adds a miniature of the theme above the label (portal).
     */
    variant?: 'compact' | 'preview';
    /** Grid columns at the `sm` breakpoint. */
    columns?: 2 | 3 | 4;
    /**
     * Per-theme copy overrides. The two apps word the high-contrast option
     * differently; wording is a product decision, so the component takes it
     * as input instead of imposing one.
     */
    labels?: Partial<Record<ThemeName, { label?: string; description?: string }>>;
}

/**
 * A miniature of the theme being offered.
 *
 * The `data-theme` attribute is set on this element rather than on the
 * document, so the generated token sheet paints the preview in that theme
 * while the rest of the page stays in the current one. That means the
 * preview needs no colour values of its own: it cannot drift from the real
 * theme, and a theme added to the design system previews correctly here
 * without anyone editing this file. Preview swatches written as literal
 * hex values would be a second source of truth for every theme.
 */
const ThemePreview: React.FC<{ theme: ThemeName }> = ({ theme }) => (
    <div
        data-theme={theme}
        aria-hidden="true"
        className="w-full h-16 rounded mb-3 border border-default bg-surface flex items-center justify-center gap-2"
    >
        <div className="w-8 h-1.5 rounded bg-primary-500" />
        <div className="w-12 h-1.5 rounded bg-surface-inset" />
        <div className="w-6 h-1.5 rounded bg-primary-500" />
    </div>
);

/**
 * Theme picker. Renders one card per theme the app offers — the provider's
 * `availableThemes` decides, so the Admin Console shows two cards and the
 * portal three without either app owning a copy of this markup.
 *
 * Colours come from semantic tokens only, so the control themes itself.
 */
export const ThemeSelector: React.FC<ThemeSelectorProps> = ({
    title = 'Theme',
    description = 'Choose the appearance of the interface.',
    className = '',
    variant = 'compact',
    columns = 2,
    labels,
}) => {
    const { theme, setTheme, availableThemes } = useTheme();
    const gridCols = { 2: 'sm:grid-cols-2', 3: 'sm:grid-cols-3', 4: 'sm:grid-cols-4' }[columns];

    return (
        <div className={className}>
            {title !== null && (
                <>
                    <h3 className="text-lg font-semibold text-default mb-1">{title}</h3>
                    <p className="text-sm text-secondary mb-4">{description}</p>
                </>
            )}

            <div
                className={`grid grid-cols-1 ${gridCols} gap-3`}
                role="radiogroup"
                aria-label={title ?? 'Theme'}
            >
                {availableThemes.map((id) => {
                    const Icon = THEME_ICONS[id];
                    const override = labels?.[id];
                    const label = override?.label ?? THEME_LABELS[id].label;
                    const themeDescription = override?.description ?? THEME_LABELS[id].description;
                    const active = theme === id;
                    return (
                        <button
                            key={id}
                            type="button"
                            role="radio"
                            aria-checked={active}
                            onClick={() => setTheme(id)}
                            className={`rounded-lg border-2 p-4 text-left transition-all ${
                                variant === 'compact' ? 'flex items-start gap-3' : ''
                            } ${
                                active
                                    ? 'border-primary-500 bg-primary-50 ring-1 ring-primary-200'
                                    : 'border-default hover:border-strong hover:bg-surface-alt'
                            }`}
                        >
                            {variant === 'preview' ? (
                                <ThemePreview theme={id} />
                            ) : (
                                <div
                                    className={`p-2 rounded-lg ${
                                        active
                                            ? 'bg-primary-100 text-primary-600'
                                            : 'bg-surface-raised text-secondary'
                                    }`}
                                >
                                    <Icon className="w-5 h-5" />
                                </div>
                            )}
                            <div>
                                <div
                                    className={`font-medium flex items-center gap-2 ${
                                        active ? 'text-primary-700' : 'text-default'
                                    }`}
                                >
                                    {variant === 'preview' && <Icon className="w-4 h-4" />}
                                    {label}
                                </div>
                                <div className="text-xs text-muted mt-0.5">{themeDescription}</div>
                            </div>
                        </button>
                    );
                })}
            </div>
        </div>
    );
};
