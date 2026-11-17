/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Enrolment in 2FA at sign-in.
 *
 * The tenant's two-factor policy requires 2FA and this admin has none, so the
 * password was answered with a temp token instead of a session. This step
 * spends it: the control plane generates a secret (QR code), a code from the
 * authenticator app is verified, and the answer to that carries the session
 * and the backup codes.
 *
 * The backup codes are returned once. They are shown here BEFORE the session
 * is handed to the caller: installing a session unmounts the sign-in form,
 * and the codes would never be seen.
 */

import React, { useEffect, useRef, useState } from 'react';
import { AlertCircle, Loader, ShieldCheck } from 'lucide-react';
import { useAuth } from '../hooks/useAuth';
import { accountLockMessage } from '../lib/accountLock';
import { errorMessage } from '../lib/narrow';
import { asyncHandler, runBackground } from '../lib/asyncSafe';
import type { TwoFactorEnrollmentSession, TwoFactorEnrollmentSetup } from '../lib/gatewayApi/auth';
import { BackupCodesPanel, TwoFactorQrStep } from './users/TwoFactorPieces';

interface TwoFactorEnrollmentStepProps {
    /** The `temp_token` of the sign-in answer that asked for enrolment. */
    tempToken: string;
    /** Whose policy it is, when the sign-in answer said. */
    tenantName?: string;
    /** Label of the way back to the password form. */
    backLabel: string;
    /** The enrolment is complete and the backup codes were shown. */
    onSignedIn: (session: TwoFactorEnrollmentSession) => void;
    onBack: () => void;
}

const TwoFactorEnrollmentStep: React.FC<TwoFactorEnrollmentStepProps> = ({
    tempToken,
    tenantName,
    backLabel,
    onSignedIn,
    onBack,
}) => {
    const { gatewayClient } = useAuth();
    const [setup, setSetup] = useState<TwoFactorEnrollmentSetup | null>(null);
    const [session, setSession] = useState<TwoFactorEnrollmentSession | null>(null);
    const [code, setCode] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    // One setup per temp token: every call replaces the pending secret, so a
    // second one (React runs effects twice in development) would invalidate
    // the QR code already on screen.
    const startedFor = useRef<string | null>(null);

    useEffect(() => {
        if (startedFor.current === tempToken) return;
        startedFor.current = tempToken;
        runBackground(
            gatewayClient
                .startTwoFactorEnrollment(tempToken)
                .then(setSetup)
                .catch((e: unknown) =>
                    setError(accountLockMessage(e) ?? errorMessage(e, 'Could not start two-factor setup. Please sign in again.')),
                ),
            'start 2FA enrolment',
        );
    }, [gatewayClient, tempToken]);

    const verify = async () => {
        setError('');
        setLoading(true);
        try {
            const resp = await gatewayClient.completeTwoFactorEnrollment(tempToken, code);
            if (resp?.access_token) {
                setSession(resp);
            } else {
                setError('The sign-in did not complete. Please sign in again.');
            }
        } catch (e) {
            setError(accountLockMessage(e) ?? errorMessage(e, 'Invalid code. Try again.'));
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="space-y-4" data-testid="two-factor-enrollment">
            <div className="text-center">
                <div className="flex items-center gap-2 text-primary-600 justify-center mb-2">
                    <ShieldCheck className="w-5 h-5" />
                    <h2 className="text-lg font-semibold text-secondary">Set up two-factor authentication</h2>
                </div>
                {!session && (
                    <p className="text-sm text-muted">
                        {tenantName ? `${tenantName} requires` : 'Your organization requires'} two-factor
                        authentication for administrators. Set it up now to finish signing in.
                    </p>
                )}
            </div>

            {error && (
                <div className="flex items-center space-x-2 p-3 bg-red-50 border border-red-200 rounded-md">
                    <AlertCircle className="h-4 w-4 text-red-600 flex-shrink-0" />
                    <span className="text-sm text-red-700">{error}</span>
                </div>
            )}

            {session ? (
                <>
                    <p className="text-sm text-secondary text-center">
                        Two-factor authentication is on. From now on you sign in with your password and a code.
                    </p>
                    <BackupCodesPanel
                        codes={session.backup_codes ?? []}
                        onDone={() => onSignedIn(session)}
                        doneLabel="Continue to the console"
                    />
                </>
            ) : setup ? (
                <TwoFactorQrStep
                    qr={setup}
                    code={code}
                    onCodeChange={(value) => { setCode(value); if (error) setError(''); }}
                    onVerify={asyncHandler(verify, 'verify 2FA enrolment')}
                    onCancel={onBack}
                    loading={loading}
                    cancelLabel={backLabel}
                />
            ) : (
                <div className="space-y-4 text-center">
                    {!error && (
                        <div className="flex items-center justify-center gap-2 text-muted text-sm">
                            <Loader className="w-4 h-4 animate-spin" /> Preparing your QR code...
                        </div>
                    )}
                    <button type="button" onClick={onBack} className="text-sm text-muted hover:text-secondary">
                        {backLabel}
                    </button>
                </div>
            )}
        </div>
    );
};

export default TwoFactorEnrollmentStep;
