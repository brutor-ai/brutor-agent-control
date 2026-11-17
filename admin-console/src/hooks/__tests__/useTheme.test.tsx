/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ThemeProvider / useTheme
 *
 * The theme is applied by writing `data-theme` onto <html>, not by rendering a
 * class on some wrapper — every stylesheet in the console keys off that
 * attribute, so a provider that updates its context but not the DOM produces a
 * toggle that "works" in React devtools and does nothing on screen. That pair
 * (context value AND attribute) is what these tests hold together.
 *
 * The provider also has to survive a storage that throws. Chrome with site data
 * blocked throws on both read and write, and a console that white-screens
 * because of a theme preference is a much worse failure than a console that
 * silently renders light.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { useContext, type ReactNode } from 'react';
import { renderHook, render, screen, act } from '@testing-library/react';

import { ThemeProvider } from '../ThemeProvider';
import { useTheme, applyThemeToDOM, STORAGE_KEY, ThemeContext } from '../useTheme';

function wrapper({ children }: { children: ReactNode }) {
    return <ThemeProvider>{children}</ThemeProvider>;
}

describe('applyThemeToDOM', () => {
    afterEach(() => {
        document.documentElement.removeAttribute('data-theme');
    });

    it('writes the theme onto the document element', () => {
        // Takes the full settings object: the implementation is shared
        // with the portal, which also drives a font scale from it.
        applyThemeToDOM({ theme: 'dark', fontSize: 'medium' });
        expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

        applyThemeToDOM({ theme: 'light', fontSize: 'medium' });
        expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    });

    it('writes the font scale as well, so one call fully applies a theme', () => {
        applyThemeToDOM({ theme: 'light', fontSize: 'large' });
        expect(document.documentElement.style.getPropertyValue('--font-size-base')).toBe('15px');
    });
});

describe('useTheme', () => {
    afterEach(() => {
        document.documentElement.removeAttribute('data-theme');
        vi.restoreAllMocks();
    });

    it('throws outside a provider rather than silently rendering unthemed', () => {
        // Deliberate render error — React logs it, and the noise is not the point.
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

        expect(() => renderHook(() => useTheme())).toThrow(
            /must be used within ThemeProvider/
        );

        consoleError.mockRestore();
    });

    // Dark is the console's default theme: a first login lands on it.
    it('defaults to dark when nothing is stored', () => {
        const { result } = renderHook(() => useTheme(), { wrapper });

        expect(result.current.theme).toBe('dark');
        expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    });

    // An operator who chose light keeps it — the default never overrides a choice.
    it('restores the stored theme on mount', () => {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({ theme: 'light', fontSize: 'medium' }));

        const { result } = renderHook(() => useTheme(), { wrapper });

        expect(result.current.theme).toBe('light');
        expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    });

    /*
     * The shared provider ships a high-contrast theme that the console does
     * not offer. A value outside the offered list must not reach
     * `data-theme`, or the console renders against a theme its own picker
     * cannot undo.
     */
    it('clamps a stored theme the console does not offer', () => {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({ theme: 'high-contrast' }));

        const { result } = renderHook(() => useTheme(), { wrapper });

        expect(result.current.theme).toBe('dark');
    });

    it('setTheme updates the context, the DOM attribute and storage together', () => {
        const { result } = renderHook(() => useTheme(), { wrapper });

        act(() => result.current.setTheme('light'));

        expect(result.current.theme).toBe('light');
        expect(document.documentElement.getAttribute('data-theme')).toBe('light');
        expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toMatchObject({ theme: 'light' });
    });

    it('shares one theme across every consumer under the provider', () => {
        function Consumer({ id }: { id: string }) {
            const { theme, setTheme } = useTheme();
            return (
                <div>
                    <span data-testid={id}>{theme}</span>
                    <button data-testid={`${id}-light`} onClick={() => setTheme('light')}>
                        light
                    </button>
                </div>
            );
        }

        render(
            <ThemeProvider>
                <Consumer id="a" />
                <Consumer id="b" />
            </ThemeProvider>
        );

        expect(screen.getByTestId('a')).toHaveTextContent('dark');
        expect(screen.getByTestId('b')).toHaveTextContent('dark');

        act(() => screen.getByTestId('a-light').click());

        // Both surfaces move — a per-component theme would leave 'b' behind.
        expect(screen.getByTestId('a')).toHaveTextContent('light');
        expect(screen.getByTestId('b')).toHaveTextContent('light');
    });

    it('renders the dark default rather than crashing when storage reads throw', () => {
        vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
            throw new Error('site data blocked');
        });

        const { result } = renderHook(() => useTheme(), { wrapper });

        expect(result.current.theme).toBe('dark');
    });

    it('still applies the theme when storage writes throw', () => {
        vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
            throw new Error('site data blocked');
        });

        const { result } = renderHook(() => useTheme(), { wrapper });
        act(() => result.current.setTheme('light'));

        expect(result.current.theme).toBe('light');
        expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    });

    it('defaults the context to null so the missing-provider guard can fire', () => {
        // A `{}` default would make an unwrapped subtree silently render with
        // an inert setTheme instead of failing loudly.
        const { result } = renderHook(() => useContext(ThemeContext));

        expect(result.current).toBeNull();
    });
});
