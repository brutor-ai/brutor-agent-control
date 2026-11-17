/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two screens of a TOTP enrolment, shared by the signed-in admin's own
 * enrolment (TwoFactorSetup) and the enrolment a sign-in can require
 * (TwoFactorEnrollmentStep): scan the QR code and enter a code; then the
 * backup codes, shown once.
 */

import { Copy, Download, Loader } from 'lucide-react';
import { runUserAction } from '../../lib/asyncSafe';

export interface TwoFactorQr {
    qr_code: string;
    secret: string;
}

interface TwoFactorQrStepProps {
    qr: TwoFactorQr;
    code: string;
    onCodeChange: (code: string) => void;
    onVerify: () => void;
    onCancel: () => void;
    loading: boolean;
    cancelLabel?: string;
}

export function TwoFactorQrStep({
    qr,
    code,
    onCodeChange,
    onVerify,
    onCancel,
    loading,
    cancelLabel = 'Cancel',
}: TwoFactorQrStepProps) {
    return (
        <div className="space-y-4">
            <p className="text-sm text-secondary">
                Scan this QR code with your authenticator app (Google Authenticator, Authy, 1Password):
            </p>
            <div className="flex justify-center">
                <img src={qr.qr_code} alt="2FA QR Code" className="w-48 h-48" />
            </div>
            <details className="text-sm">
                <summary className="text-muted cursor-pointer">Can't scan? Enter manually</summary>
                <code className="block mt-2 p-2 bg-surface-alt rounded text-xs break-all font-mono">{qr.secret}</code>
            </details>
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">Enter the 6-digit code from your app:</label>
                <div className="flex gap-2">
                    <input
                        type="text"
                        value={code}
                        onChange={e => onCodeChange(e.target.value.replace(/\D/g, '').slice(0, 6))}
                        className="flex-1 px-3 py-2 bg-surface border border-strong rounded-md text-default text-center text-lg tracking-widest font-mono focus:ring-2 focus:ring-primary min-h-0 min-w-0"
                        placeholder="000000"
                        maxLength={6}
                        autoFocus
                    />
                    <button
                        onClick={onVerify}
                        disabled={loading || code.length < 6}
                        className="px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700 disabled:opacity-50"
                    >
                        {loading ? <Loader className="w-4 h-4 animate-spin" /> : 'Verify'}
                    </button>
                </div>
            </div>
            <button onClick={onCancel} className="text-sm text-muted hover:text-default">{cancelLabel}</button>
        </div>
    );
}

interface BackupCodesPanelProps {
    codes: string[];
    onDone: () => void;
    doneLabel?: string;
}

export function BackupCodesPanel({ codes, onDone, doneLabel = 'Done' }: BackupCodesPanelProps) {
    const copyBackupCodes = () => {
        runUserAction(navigator.clipboard.writeText(codes.join('\n')), 'write text');
    };

    const downloadBackupCodes = () => {
        const blob = new Blob([codes.join('\n')], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'brutor-2fa-backup-codes.txt';
        a.click();
        URL.revokeObjectURL(url);
    };

    return (
        <div className="space-y-4">
            <div className="p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
                <p className="text-sm text-amber-800 dark:text-amber-300 font-medium">
                    Save these backup codes! They won't be shown again.
                </p>
                <p className="text-xs text-amber-700 dark:text-amber-400 mt-1">
                    Each code can only be used once. Store them somewhere safe.
                </p>
            </div>
            <div className="grid grid-cols-2 gap-2 p-3 bg-surface-alt rounded-lg font-mono text-sm">
                {codes.map((c, i) => (
                    <div key={i} className="text-default">{c}</div>
                ))}
            </div>
            <div className="flex gap-2">
                <button onClick={copyBackupCodes} className="flex items-center gap-1 px-3 py-1.5 text-sm border border-strong rounded-md hover:bg-surface-alt text-secondary">
                    <Copy className="w-3.5 h-3.5" /> Copy
                </button>
                <button onClick={downloadBackupCodes} className="flex items-center gap-1 px-3 py-1.5 text-sm border border-strong rounded-md hover:bg-surface-alt text-secondary">
                    <Download className="w-3.5 h-3.5" /> Download
                </button>
            </div>
            <button
                onClick={onDone}
                className="px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700 text-sm"
            >
                {doneLabel}
            </button>
        </div>
    );
}
