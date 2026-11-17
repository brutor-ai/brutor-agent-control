/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, Check, X } from 'lucide-react';
import {
    COLOR_PALETTE,
    resolveGroupColor,
} from '../../lib/lucideIconMap';
import { useTheme } from '../../hooks/useTheme';

interface IconColorPickerProps {
    value: string;                          // Color key (e.g. 'sky', 'emerald')
    onChange: (colorKey: string) => void;
    disabled?: boolean;
    className?: string;
}

const IconColorPicker: React.FC<IconColorPickerProps> = ({
    value,
    onChange,
    disabled = false,
    className = '',
}) => {
    const [isOpen, setIsOpen] = useState(false);
    const dropdownRef = useRef<HTMLDivElement>(null);
    const { theme } = useTheme();

    // Close on outside click
    useEffect(() => {
        const handleClick = (e: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
                setIsOpen(false);
            }
        };
        if (isOpen) {
            document.addEventListener('mousedown', handleClick);
            return () => document.removeEventListener('mousedown', handleClick);
        }
    }, [isOpen]);

    const resolved = resolveGroupColor(value, theme);
    const entries = Object.entries(COLOR_PALETTE);

    return (
        <div className={`relative ${className}`} ref={dropdownRef}>
            {/* Trigger */}
            <button
                type="button"
                onClick={() => !disabled && setIsOpen(!isOpen)}
                disabled={disabled}
                className={`
                    w-full flex items-center justify-between px-3 py-2 border rounded-lg transition-colors text-left
                    ${disabled
                        ? 'bg-surface-raised border-default cursor-not-allowed'
                        : isOpen
                            ? 'border-primary-400 ring-2 ring-primary-100'
                            : 'border-strong hover:border-gray-400'
                    }
                `}
            >
                <div className="flex items-center space-x-2.5">
                    {/* Color swatch */}
                    <div
                        className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
                        style={{ backgroundColor: resolved.bg }}
                    >
                        <div
                            className="w-4 h-4 rounded-full"
                            style={{ backgroundColor: resolved.fg }}
                        />
                    </div>
                    <div className="min-w-0">
                        <span className="text-sm font-medium text-default block">
                            {resolved.label}
                        </span>
                        {!value && (
                            <span className="text-xs text-muted">Default color</span>
                        )}
                    </div>
                </div>
                <div className="flex items-center space-x-1 flex-shrink-0">
                    {value && !disabled && (
                        <button
                            type="button"
                            onClick={(e) => {
                                e.stopPropagation();
                                onChange('');
                                setIsOpen(false);
                            }}
                            className="p-1 text-muted hover:text-secondary rounded"
                            title="Reset to default"
                        >
                            <X className="w-3.5 h-3.5" />
                        </button>
                    )}
                    <ChevronDown className={`w-4 h-4 text-muted transition-transform ${isOpen ? 'rotate-180' : ''}`} />
                </div>
            </button>

            {/* Dropdown */}
            {isOpen && (
                <div className="absolute z-50 mt-1 w-full bg-surface border border-default rounded-lg shadow-lg overflow-hidden">
                    <div className="p-3">
                        <div className="grid grid-cols-7 gap-2">
                            {entries.map(([key, entry]) => {
                                const isSelected = key === value || (!value && key === 'sky');

                                return (
                                    <button
                                        key={key}
                                        type="button"
                                        onClick={() => {
                                            onChange(key);
                                            setIsOpen(false);
                                        }}
                                        title={entry.label}
                                        className={`
                                            relative w-9 h-9 rounded-lg flex items-center justify-center transition-all
                                            ${isSelected ? 'ring-2 ring-offset-1 ring-gray-400 scale-110' : 'hover:scale-110'}
                                        `}
                                        style={{ backgroundColor: entry.bg }}
                                    >
                                        <div
                                            className="w-5 h-5 rounded-full"
                                            style={{ backgroundColor: entry.fg }}
                                        />
                                        {isSelected && (
                                            <div className="absolute inset-0 flex items-center justify-center">
                                                <Check className="w-3.5 h-3.5 text-white drop-shadow-sm" />
                                            </div>
                                        )}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {/* Footer */}
                    <div className="px-3 py-1.5 border-t border-default bg-surface-alt">
                        <p className="text-xs text-muted">
                            {value ? `${resolved.label} · Clear to use default` : 'Default (Sky)'}
                        </p>
                    </div>
                </div>
            )}
        </div>
    );
};

export default IconColorPicker;
