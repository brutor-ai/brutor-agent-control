/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A horizontal tab bar for the single-body resource forms.
 *
 * The larger forms (model, MCP server, agent card…) already have their own
 * sidebar navigation. The smaller ones — provider, routing group, server
 * config — share this bar: each growing its own would give the same two
 * tabs three slightly different looks.
 *
 * Deliberately plain: these forms have two or three tabs, so this is a
 * segmented control rather than a scrollable nav with icons and badges.
 */

export interface FormTab<T extends string> {
    id: T;
    label: string;
    icon?: React.ReactNode;
}

interface FormTabBarProps<T extends string> {
    tabs: ReadonlyArray<FormTab<T>>;
    active: T;
    onChange: (id: T) => void;
}

export function FormTabBar<T extends string>({
    tabs,
    active,
    onChange,
}: FormTabBarProps<T>) {
    return (
        <div
            className="flex items-center gap-1 px-6 border-b border-default flex-shrink-0"
            role="tablist"
            data-testid="form-tab-bar"
        >
            {tabs.map((tab) => (
                <button
                    key={tab.id}
                    type="button"
                    role="tab"
                    aria-selected={active === tab.id}
                    onClick={() => onChange(tab.id)}
                    className={`flex items-center gap-2 px-3 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                        active === tab.id
                            ? 'border-primary-500 text-primary-700'
                            : 'border-transparent text-secondary hover:text-default'
                    }`}
                    data-testid={`form-tab-${tab.id}`}
                >
                    {tab.icon}
                    <span>{tab.label}</span>
                </button>
            ))}
        </div>
    );
}

export default FormTabBar;
