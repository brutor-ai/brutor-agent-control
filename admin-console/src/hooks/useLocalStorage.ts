/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState, useEffect, type Dispatch, type SetStateAction } from 'react';
import { errorKind } from '../lib/narrow';

/**
 * Custom hook for persisting state to localStorage
 *
 * @param key - localStorage key
 * @param defaultValue - default value if nothing in localStorage
 * @returns [value, setValue] - same as useState (supports updater functions)
 *
 * @example
 * const [layout, setLayout] = useLocalStorage('my-view-layout', 'card');
 * setLayout('list');                          // direct value
 * setLayout(prev => prev === 'card' ? 'list' : 'card');  // updater function
 */
export function useLocalStorage<T>(key: string, defaultValue: T): [T, Dispatch<SetStateAction<T>>] {
    // Initialize state from localStorage
    const [value, setValue] = useState<T>(() => {
        try {
            const item = localStorage.getItem(key);
            if (item === null) {
                return defaultValue;
            }
            return JSON.parse(item) as T;
        } catch (error) {
            console.error(`Failed to read ${key} from localStorage:`, errorKind(error));
            return defaultValue;
        }
    });

    // Save to localStorage whenever value changes
    useEffect(() => {
        try {
            localStorage.setItem(key, JSON.stringify(value));
        } catch (error) {
            console.error(`Failed to save ${key} to localStorage:`, errorKind(error));
        }
    }, [key, value]);

    return [value, setValue];
}

/**
 * Custom hook for persisting simple string state to localStorage
 * (no JSON parsing needed)
 *
 * @param key - localStorage key
 * @param defaultValue - default value if nothing in localStorage
 * @returns [value, setValue] - same as useState
 *
 * @example
 * const [layout, setLayout] = useLocalStorageString('my-view-layout', 'card');
 */
export function useLocalStorageString(key: string, defaultValue: string): [string, (value: string) => void] {
    // Initialize state from localStorage
    const [value, setValue] = useState<string>(() => {
        try {
            const item = localStorage.getItem(key);
            return item !== null ? item : defaultValue;
        } catch (error) {
            console.error(`Failed to read ${key} from localStorage:`, errorKind(error));
            return defaultValue;
        }
    });

    // Save to localStorage whenever value changes
    useEffect(() => {
        try {
            localStorage.setItem(key, value);
        } catch (error) {
            console.error(`Failed to save ${key} to localStorage:`, errorKind(error));
        }
    }, [key, value]);

    return [value, setValue];
}