/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState } from 'react';
import { Shield, ShieldCheck, ShieldOff, Loader, AlertTriangle } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { errorMessage, errorStatus } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { BackupCodesPanel, TwoFactorQrStep } from './TwoFactorPieces';

interface TwoFactorSetupProps {
    onStatusChange?: (enabled: boolean) => void;
}

export function TwoFactorSetup({ onStatusChange }: TwoFactorSetupProps) {
    const { gatewayClient } = useGateway();
    const [status, setStatus] = useState<{ enabled: boolean; verified_at?: string } | null>(null);
    const [loading, setLoading] = useState(false);
    const [step, setStep] = useState<'idle' | 'qr' | 'backup' | 'disable'>('idle');
    const [qrData, setQrData] = useState<{ qr_code: string; secret: string } | null>(null);
    const [backupCodes, setBackupCodes] = useState<string[]>([]);
    const [code, setCode] = useState('');
    const [error, setError] = useState('');
    const [statusFailed, setStatusFailed] = useState(false);

    const loadStatus = async () => {
        try {
            setStatus((await gatewayClient?.getTwoFactorStatus()) ?? null);
        } catch (e) {
            // Must stop the guard below from re-firing: with `status` still null
            // and `loading` back to false, a failed read re-requested forever.
            setStatusFailed(true);
            setError(errorMessage(e, 'Failed to load 2FA status'));
        }
    };

    // Load status on mount
    if (status === null && !loading && !statusFailed) {
        setLoading(true);
        runBackground(loadStatus().finally(() => setLoading(false)), 'status');
    }

    const startSetup = async () => {
        setError('');
        setLoading(true);
        try {
            setQrData((await gatewayClient?.startTwoFactorSetup()) ?? null);
            setStep('qr');
        } catch (e) {
            setError(errorMessage(e, 'Failed to start 2FA setup'));
        } finally {
            setLoading(false);
        }
    };

    const verifyCode = async () => {
        setError('');
        if (code.length < 6) { setError('Enter a 6-digit code'); return; }
        setLoading(true);
        try {
            const resp = await gatewayClient?.verifyTwoFactorSetup(code);
            setBackupCodes(resp?.backup_codes || []);
            setStep('backup');
            setStatus({ enabled: true });
            onStatusChange?.(true);
        } catch (e) {
            setError(errorMessage(e, 'Invalid code. Try again.'));
        } finally {
            setLoading(false);
        }
    };

    const disable2FA = async () => {
        setError('');
        if (code.length < 6) { setError('Enter your current 6-digit code'); return; }
        setLoading(true);
        try {
            await gatewayClient?.disableTwoFactor(code);
            setStatus({ enabled: false });
            setStep('idle');
            setCode('');
            onStatusChange?.(false);
        } catch (e) {
            setError(errorMessage(e, 'Invalid code'));
            // 409: the tenant's two-factor policy requires 2FA of its
            // administrators, so it cannot be switched off. The message is
            // the control plane's; another code would not change the answer,
            // so the code entry is closed rather than left inviting a retry.
            if (errorStatus(e) === 409) {
                setStep('idle');
                setCode('');
            }
        } finally {
            setLoading(false);
        }
    };

    if (loading && status === null) {
        return (
            <div className="flex items-center gap-2 text-muted">
                <Loader className="w-4 h-4 animate-spin" /> Loading 2FA status...
            </div>
        );
    }

    // Unknown is not "off": offering "Enable 2FA" here would misreport an account
    // that may already be protected.
    if (status === null && statusFailed) {
        return (
            <div className="bg-surface border border-default rounded-lg p-5 space-y-3">
                <div className="p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4 text-red-500" />
                    <p className="text-sm text-red-700 dark:text-red-400">{error}</p>
                </div>
                <button
                    onClick={() => { setError(''); setStatusFailed(false); }}
                    className="px-4 py-2 border border-strong rounded-md hover:bg-surface-alt text-sm text-secondary"
                >
                    Retry
                </button>
            </div>
        );
    }

    return (
        <div className="bg-surface border border-default rounded-lg p-5">
            <div className="flex items-center gap-3 mb-4">
                {status?.enabled ? (
                    <ShieldCheck className="w-6 h-6 text-green-500" />
                ) : (
                    <Shield className="w-6 h-6 text-muted" />
                )}
                <div>
                    <h3 className="text-base font-semibold text-default">Two-Factor Authentication</h3>
                    <p className="text-sm text-muted">
                        {status?.enabled
                            ? 'Enabled — your account is protected with TOTP'
                            : 'Add an extra layer of security to your account'}
                    </p>
                </div>
            </div>

            {error && (
                <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4 text-red-500" />
                    <p className="text-sm text-red-700 dark:text-red-400">{error}</p>
                </div>
            )}

            {/* Idle — show enable/disable button */}
            {step === 'idle' && !status?.enabled && (
                <button
                    onClick={asyncHandler(startSetup, 'click')}
                    disabled={loading}
                    className="px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700 disabled:opacity-50 text-sm"
                >
                    {loading ? 'Setting up...' : 'Enable 2FA'}
                </button>
            )}

            {step === 'idle' && status?.enabled && (
                <button
                    onClick={() => { setStep('disable'); setCode(''); setError(''); }}
                    className="px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 text-sm flex items-center gap-2"
                >
                    <ShieldOff className="w-4 h-4" /> Disable 2FA
                </button>
            )}

            {/* QR Code step */}
            {step === 'qr' && qrData && (
                <TwoFactorQrStep
                    qr={qrData}
                    code={code}
                    onCodeChange={setCode}
                    onVerify={asyncHandler(verifyCode, 'click')}
                    onCancel={() => { setStep('idle'); setCode(''); setError(''); }}
                    loading={loading}
                />
            )}

            {/* Backup codes step */}
            {step === 'backup' && backupCodes.length > 0 && (
                <BackupCodesPanel
                    codes={backupCodes}
                    onDone={() => { setStep('idle'); setBackupCodes([]); setCode(''); }}
                />
            )}

            {/* Disable confirmation */}
            {step === 'disable' && (
                <div className="space-y-3">
                    <p className="text-sm text-secondary">Enter your current 6-digit code to disable 2FA:</p>
                    <div className="flex gap-2">
                        <input
                            type="text"
                            value={code}
                            onChange={e => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                            className="flex-1 px-3 py-2 bg-surface border border-strong rounded-md text-default text-center text-lg tracking-widest font-mono focus:ring-2 focus:ring-primary min-h-0 min-w-0"
                            placeholder="000000"
                            maxLength={6}
                            autoFocus
                        />
                        <button onClick={asyncHandler(disable2FA, 'click')} disabled={loading || code.length < 6} className="px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50">
                            {loading ? <Loader className="w-4 h-4 animate-spin" /> : 'Disable'}
                        </button>
                    </div>
                    <button onClick={() => { setStep('idle'); setCode(''); setError(''); }} className="text-sm text-muted hover:text-default">Cancel</button>
                </div>
            )}
        </div>
    );
}
