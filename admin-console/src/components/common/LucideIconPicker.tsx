/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo, useRef, useEffect } from 'react';
import { Search, X, ChevronDown, Folder } from 'lucide-react';
import {
    ICON_REGISTRY,
    ICON_CATEGORIES,
    resolveGroupIcon,
    getIconEntry
} from '../../lib/lucideIconMap';

interface LucideIconPickerProps {
    value: string;                              // Current icon name (e.g. 'brain')
    onChange: (iconName: string) => void;        // Callback with selected icon name
    groupType?: string;                         // For showing default icon when value is empty
    disabled?: boolean;
    className?: string;
}

const LucideIconPicker: React.FC<LucideIconPickerProps> = ({
    value,
    onChange,
    groupType,
    disabled = false,
    className = '',
}) => {
    const [isOpen, setIsOpen] = useState(false);
    const [search, setSearch] = useState('');
    const [activeCategory, setActiveCategory] = useState<string | null>(null);
    const dropdownRef = useRef<HTMLDivElement>(null);
    const searchRef = useRef<HTMLInputElement>(null);

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

    // Focus search on open
    useEffect(() => {
        if (isOpen && searchRef.current) {
            setTimeout(() => searchRef.current?.focus(), 50);
        }
    }, [isOpen]);

    // Filtered icons
    const filteredIcons = useMemo(() => {
        let entries = Object.entries(ICON_REGISTRY);

        if (search) {
            const term = search.toLowerCase();
            entries = entries.filter(([name, entry]) =>
                name.includes(term) ||
                entry.label.toLowerCase().includes(term) ||
                entry.category.toLowerCase().includes(term)
            );
        }

        if (activeCategory) {
            entries = entries.filter(([_, entry]) => entry.category === activeCategory);
        }

        return entries;
    }, [search, activeCategory]);

    // Resolve current display icon
    const CurrentIcon = value
        ? (ICON_REGISTRY[value]?.component || Folder)
        : resolveGroupIcon(undefined, groupType);
    const currentEntry = value ? getIconEntry(value) : null;
    const displayLabel = currentEntry?.label || (value ? value : 'Default');

    const handleSelect = (iconName: string) => {
        onChange(iconName);
        setIsOpen(false);
        setSearch('');
    };

    const handleClear = (e: React.MouseEvent) => {
        e.stopPropagation();
        onChange('');
        setIsOpen(false);
    };

    return (
        <div className={`relative ${className}`} ref={dropdownRef}>
            {/* Trigger Button */}
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
                    <div className="w-8 h-8 rounded-lg bg-surface-raised flex items-center justify-center flex-shrink-0">
                        <CurrentIcon className="w-4.5 h-4.5 text-secondary" />
                    </div>
                    <div className="min-w-0">
                        <span className="text-sm font-medium text-default block truncate">
                            {displayLabel}
                        </span>
                        {value && (
                            <span className="text-xs text-muted">{currentEntry?.category || 'Custom'}</span>
                        )}
                        {!value && (
                            <span className="text-xs text-muted">Uses group type icon</span>
                        )}
                    </div>
                </div>
                <div className="flex items-center space-x-1 flex-shrink-0">
                    {value && !disabled && (
                        <button
                            type="button"
                            onClick={handleClear}
                            className="p-1 text-muted hover:text-secondary rounded"
                            title="Clear custom icon"
                        >
                            <X className="w-3.5 h-3.5" />
                        </button>
                    )}
                    <ChevronDown className={`w-4 h-4 text-muted transition-transform ${isOpen ? 'rotate-180' : ''}`} />
                </div>
            </button>

            {/* Dropdown */}
            {isOpen && (
                <div className="absolute z-50 mt-1 w-full bg-surface border border-default rounded-lg shadow-lg overflow-hidden"
                     style={{ maxHeight: '420px' }}
                >
                    {/* Search */}
                    <div className="p-2 border-b border-default">
                        <div className="relative">
                            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted" />
                            <input
                                ref={searchRef}
                                type="text"
                                value={search}
                                onChange={(e) => setSearch(e.target.value)}
                                placeholder="Search icons..."
                                className="w-full pl-8 pr-3 py-1.5 text-sm border border-default rounded-md focus:outline-none focus:ring-1 focus:ring-primary-400 focus:border-primary-400"
                            />
                        </div>
                    </div>

                    {/* Category Pills */}
                    <div className="px-2 py-1.5 border-b border-default flex flex-wrap gap-1">
                        <button
                            type="button"
                            onClick={() => setActiveCategory(null)}
                            className={`px-2 py-0.5 text-xs rounded-full transition-colors ${
                                !activeCategory
                                    ? 'bg-primary-100 text-primary-700 font-medium'
                                    : 'bg-surface-raised text-secondary hover:bg-surface-inset'
                            }`}
                        >
                            All
                        </button>
                        {ICON_CATEGORIES.map(cat => {
                            return (
                                <button
                                    key={cat}
                                    type="button"
                                    onClick={() => setActiveCategory(activeCategory === cat ? null : cat)}
                                    className={`px-2 py-0.5 text-xs rounded-full transition-colors ${
                                        activeCategory === cat
                                            ? 'bg-primary-100 text-primary-700 font-medium'
                                            : 'bg-surface-raised text-secondary hover:bg-surface-inset'
                                    }`}
                                >
                                    {cat}
                                </button>
                            );
                        })}
                    </div>

                    {/* Icon Grid */}
                    <div className="p-2 overflow-y-auto" style={{ maxHeight: '280px' }}>
                        {filteredIcons.length === 0 ? (
                            <div className="text-center py-6">
                                <p className="text-sm text-muted">No icons found</p>
                            </div>
                        ) : (
                            <div className="grid grid-cols-6 gap-1">
                                {filteredIcons.map(([name, entry]) => {
                                    const Icon = entry.component;
                                    const isSelected = name === value;

                                    return (
                                        <button
                                            key={name}
                                            type="button"
                                            onClick={() => handleSelect(name)}
                                            title={entry.label}
                                            className={`
                                                flex flex-col items-center justify-center p-2 rounded-lg transition-all
                                                ${isSelected
                                                    ? 'bg-primary-100 ring-2 ring-primary-400 text-primary-700'
                                                    : 'hover:bg-surface-raised text-secondary hover:text-default'
                                                }
                                            `}
                                        >
                                            <Icon className="w-5 h-5" />
                                            <span className="text-[9px] mt-1 truncate w-full text-center leading-tight">
                                                {entry.label}
                                            </span>
                                        </button>
                                    );
                                })}
                            </div>
                        )}
                    </div>

                    {/* Footer */}
                    <div className="px-3 py-1.5 border-t border-default bg-surface-alt">
                        <p className="text-xs text-muted">
                            {filteredIcons.length} icon{filteredIcons.length !== 1 ? 's' : ''}
                            {value && ' · Clear to use default group type icon'}
                        </p>
                    </div>
                </div>
            )}
        </div>
    );
};

export default LucideIconPicker;
