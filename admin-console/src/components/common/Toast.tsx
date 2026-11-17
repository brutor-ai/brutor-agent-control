/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { ReactNode, createContext, useCallback, useContext, useMemo, useState } from 'react';
import { CheckCircle, XCircle, AlertTriangle, Info, X } from 'lucide-react';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface Toast {
    id: string;
    type: ToastType;
    title: string;
    message?: string;
    duration?: number;
}

interface ToastContextType {
    showToast: (toast: Omit<Toast, 'id'>) => void;
    success: (title: string, message?: string) => void;
    error: (title: string, message?: string) => void;
    warning: (title: string, message?: string) => void;
    info: (title: string, message?: string) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

export const useToast = () => {
    const context = useContext(ToastContext);
    if (!context) {
        throw new Error('useToast must be used within a ToastProvider');
    }
    return context;
};

interface ToastProviderProps {
    children: ReactNode;
}

export const ToastProvider: React.FC<ToastProviderProps> = ({ children }) => {
    const [toasts, setToasts] = useState<Toast[]>([]);

    const removeToast = useCallback((id: string) => {
        setToasts((prev) => prev.filter((toast) => toast.id !== id));
    }, []);

    const showToast = useCallback((toast: Omit<Toast, 'id'>) => {
        const id = Math.random().toString(36).substring(2, 9);
        const duration = toast.duration || 5000;

        const newToast: Toast = {
            ...toast,
            id,
        };

        setToasts((prev) => [...prev, newToast]);

        setTimeout(() => {
            removeToast(id);
        }, duration);
    }, [removeToast]);

    const success = useCallback((title: string, message?: string) => {
        showToast({ type: 'success', title, message });
    }, [showToast]);

    const error = useCallback((title: string, message?: string) => {
        showToast({ type: 'error', title, message, duration: 7000 });
    }, [showToast]);

    const warning = useCallback((title: string, message?: string) => {
        showToast({ type: 'warning', title, message, duration: 6000 });
    }, [showToast]);

    const info = useCallback((title: string, message?: string) => {
        showToast({ type: 'info', title, message });
    }, [showToast]);

    // Memoised deliberately. An inline object literal here mints a new context
    // value on every render — including every toast appearing or expiring — so
    // any consumer holding `toast` in a `useCallback`/`useEffect` dependency
    // chain re-runs its data load each time a toast fires. `PolicyTemplatesView`
    // and `PolicyBundlesView` both do exactly that.
    const value = useMemo(
        () => ({ showToast, success, error, warning, info }),
        [showToast, success, error, warning, info]
    );

    return (
        <ToastContext.Provider value={value}>
            {children}
            <ToastContainer toasts={toasts} onRemove={removeToast} />
        </ToastContext.Provider>
    );
};

interface ToastContainerProps {
    toasts: Toast[];
    onRemove: (id: string) => void;
}

const ToastContainer: React.FC<ToastContainerProps> = ({ toasts, onRemove }) => {
    return (
        <div className="fixed top-4 right-4 z-[9999] space-y-2 pointer-events-none">
            {toasts.map((toast) => (
                <ToastItem key={toast.id} toast={toast} onRemove={onRemove} />
            ))}
        </div>
    );
};

interface ToastItemProps {
    toast: Toast;
    onRemove: (id: string) => void;
}

const ToastItem: React.FC<ToastItemProps> = ({ toast, onRemove }) => {
    const getIcon = () => {
        switch (toast.type) {
            case 'success':
                return <CheckCircle className="w-5 h-5 text-green-600" />;
            case 'error':
                return <XCircle className="w-5 h-5 text-red-600" />;
            case 'warning':
                return <AlertTriangle className="w-5 h-5 text-amber-600" />;
            case 'info':
                return <Info className="w-5 h-5 text-blue-600" />;
        }
    };

    const getStyles = () => {
        switch (toast.type) {
            case 'success':
                return 'bg-green-50 border-green-200 text-green-900';
            case 'error':
                return 'bg-red-50 border-red-200 text-red-900';
            case 'warning':
                return 'bg-amber-50 border-amber-200 text-amber-900';
            case 'info':
                return 'bg-blue-50 border-blue-200 text-blue-900';
        }
    };

    return (
        <div
            className={`
                pointer-events-auto
                min-w-[320px] max-w-md
                rounded-lg border shadow-lg
                p-4
                animate-slide-in-right
                ${getStyles()}
            `}
        >
            <div className="flex items-start space-x-3">
                <div className="flex-shrink-0 mt-0.5">
                    {getIcon()}
                </div>
                <div className="flex-1 min-w-0 min-h-0">
                    <p className="font-semibold text-sm">
                        {toast.title}
                    </p>
                    {toast.message && (
                        <p className="text-sm mt-1 opacity-90">
                            {toast.message}
                        </p>
                    )}
                </div>
                <button
                    onClick={() => onRemove(toast.id)}
                    className="flex-shrink-0 p-1 hover:bg-black hover:bg-opacity-5 rounded transition-colors"
                >
                    <X className="w-4 h-4" />
                </button>
            </div>
        </div>
    );
};

