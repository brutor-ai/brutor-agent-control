/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { createContext, useCallback, useContext, useRef, useState } from 'react';
import { AlertTriangle, HelpCircle, X, Loader2, Info } from 'lucide-react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ConfirmationVariant = 'danger' | 'warning' | 'info';

export interface ConfirmationOptions {
    /** Dialog title — e.g. "Delete registry". */
    title: string;
    /** Main body message. Supports React nodes for richer content. */
    message: React.ReactNode;
    /**
     * Optional additional note shown in a subtle box below the message.
     * Use for "this cannot be undone" / "may take a while" caveats.
     */
    note?: React.ReactNode;
    /**
     * Color/icon treatment.
     *  - `danger` (default): red header, destructive semantics.
     *  - `warning`: amber header, cautionary but not irreversible.
     *  - `info`: neutral header, plain yes/no prompts.
     */
    variant?: ConfirmationVariant;
    /** Confirm button label. Defaults match variant. */
    confirmLabel?: string;
    /** Cancel button label. Default: "Cancel". */
    cancelLabel?: string;
}

// ---------------------------------------------------------------------------
// Context / Provider
// ---------------------------------------------------------------------------

/**
 * Returns a promise-based confirm function. Replaces `window.confirm`
 * with a proper in-app modal:
 *
 *     const confirm = useConfirm();
 *     if (!(await confirm({ title: 'Delete?', message: '…' }))) return;
 */
type ConfirmFn = (opts: ConfirmationOptions) => Promise<boolean>;

const ConfirmationContext = createContext<ConfirmFn | null>(null);

export const useConfirm = (): ConfirmFn => {
    const fn = useContext(ConfirmationContext);
    if (!fn) {
        throw new Error('useConfirm must be used within <ConfirmationProvider>');
    }
    return fn;
};

interface ProviderState {
    opts: ConfirmationOptions | null;
    loading: boolean;
}

export const ConfirmationProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [state, setState] = useState<ProviderState>({ opts: null, loading: false });
    const resolverRef = useRef<((v: boolean) => void) | null>(null);

    const confirm = useCallback<ConfirmFn>((opts) => {
        return new Promise((resolve) => {
            resolverRef.current = resolve;
            setState({ opts, loading: false });
        });
    }, []);

    const handleCancel = () => {
        resolverRef.current?.(false);
        resolverRef.current = null;
        setState({ opts: null, loading: false });
    };

    const handleConfirm = () => {
        resolverRef.current?.(true);
        resolverRef.current = null;
        setState({ opts: null, loading: false });
    };

    return (
        <ConfirmationContext.Provider value={confirm}>
            {children}
            {state.opts && (
                <ConfirmationDialog
                    {...state.opts}
                    onCancel={handleCancel}
                    onConfirm={handleConfirm}
                />
            )}
        </ConfirmationContext.Provider>
    );
};

// ---------------------------------------------------------------------------
// Standalone dialog (exported so callers can embed without the provider)
// ---------------------------------------------------------------------------

const VARIANT_CONFIG: Record<ConfirmationVariant, {
    Icon: React.FC<{ className?: string }>;
    headerBg: string;
    headerBorder: string;
    iconColor: string;
    titleColor: string;
    confirmClasses: string;
    defaultConfirmLabel: string;
}> = {
    danger: {
        Icon: AlertTriangle,
        headerBg: 'bg-red-50',
        headerBorder: 'border-red-100',
        iconColor: 'text-red-600',
        titleColor: 'text-red-900',
        confirmClasses: 'bg-red-600 text-white hover:bg-red-700',
        defaultConfirmLabel: 'Delete',
    },
    warning: {
        Icon: AlertTriangle,
        headerBg: 'bg-amber-50',
        headerBorder: 'border-amber-100',
        iconColor: 'text-amber-600',
        titleColor: 'text-amber-900',
        confirmClasses: 'bg-amber-600 text-white hover:bg-amber-700',
        defaultConfirmLabel: 'Continue',
    },
    info: {
        Icon: HelpCircle,
        headerBg: 'bg-surface-alt',
        headerBorder: 'border-default',
        iconColor: 'text-primary-500',
        titleColor: 'text-default',
        confirmClasses: 'bg-primary-500 text-white hover:bg-primary-600',
        defaultConfirmLabel: 'OK',
    },
};

interface StandaloneProps extends ConfirmationOptions {
    onCancel: () => void;
    onConfirm: () => void;
    loading?: boolean;
}

export const ConfirmationDialog: React.FC<StandaloneProps> = ({
    title,
    message,
    note,
    variant = 'danger',
    confirmLabel,
    cancelLabel = 'Cancel',
    onCancel,
    onConfirm,
    loading = false,
}) => {
    const cfg = VARIANT_CONFIG[variant];
    const { Icon } = cfg;

    // Keyboard shortcuts: Enter confirms, Escape cancels.
    React.useEffect(() => {
        const handler = (e: KeyboardEvent) => {
            if (loading) return;
            if (e.key === 'Enter') onConfirm();
            else if (e.key === 'Escape') onCancel();
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, [loading, onConfirm, onCancel]);

    return (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[70] p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-md w-full animate-scale-in">
                {/* Header */}
                <div className={`flex items-center justify-between p-5 border-b ${cfg.headerBg} ${cfg.headerBorder}`}>
                    <div className="flex items-center gap-3">
                        <div className={`w-10 h-10 rounded-full ${cfg.headerBg} flex items-center justify-center`}>
                            <Icon className={`w-5 h-5 ${cfg.iconColor}`} />
                        </div>
                        <h2 className={`text-lg font-semibold ${cfg.titleColor}`}>{title}</h2>
                    </div>
                    <button
                        onClick={onCancel}
                        disabled={loading}
                        className="p-1.5 hover:bg-black/5 rounded-md transition-colors disabled:opacity-50"
                        aria-label="Close"
                    >
                        <X className={`w-5 h-5 ${cfg.iconColor}`} />
                    </button>
                </div>

                {/* Body */}
                <div className="p-5 space-y-3">
                    <div className="text-sm text-default whitespace-pre-line">{message}</div>
                    {note && (
                        <div className="flex items-start gap-2 text-xs text-secondary bg-surface-alt p-3 rounded-md">
                            <Info className="w-4 h-4 text-muted mt-0.5 flex-shrink-0" />
                            <div>{note}</div>
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="flex justify-end gap-3 p-4 border-t border-default bg-surface-alt rounded-b-lg">
                    <button
                        onClick={onCancel}
                        disabled={loading}
                        className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors disabled:opacity-50"
                    >
                        {cancelLabel}
                    </button>
                    <button
                        onClick={onConfirm}
                        disabled={loading}
                        className={`inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-md transition-colors disabled:opacity-50 ${cfg.confirmClasses}`}
                    >
                        {loading && <Loader2 className="w-4 h-4 animate-spin" />}
                        {confirmLabel ?? cfg.defaultConfirmLabel}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default ConfirmationDialog;
