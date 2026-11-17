/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * TOTP enrolment and removal for the signed-in admin.
 *
 * The thing worth pinning is the status read. Run from a render-phase guard
 * (`status === null && !loading`) that swallows failures, one failing request
 * would clear `loading` with `status` still null and fire the next: a tight request
 * loop against the control plane, behind a spinner that never resolves. Unknown
 * status also has to stay unknown, never rendered as "2FA is off, enable it".
 *
 * Beyond that: the code sent on verify and disable, that a rejected code changes
 * nothing, and that backup codes are shown, copyable and downloadable once.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const request = vi.fn();
// The client's own methods, issued through `request`.
const gateway = { gatewayClient: clientOver(request) };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import { TwoFactorSetup } from '../TwoFactorSetup';
import { clientOver } from '../../../test/clientOver';

const STATUS = '/v1/admin/users/me/2fa/status';
const SETUP = '/v1/admin/users/me/2fa/setup';
const VERIFY = '/v1/admin/users/me/2fa/verify';
const DISABLE = '/v1/admin/users/me/2fa/disable';

const QR = { qr_code: 'data:image/png;base64,QQ==', secret: 'JBSWY3DPEHPK3PXP' };
const CODES = ['aaaa-1111', 'bbbb-2222'];

type Handler = () => unknown;

function serve(routes: Partial<Record<string, Handler>>) {
    request.mockImplementation((url: string) => {
        const handler = routes[url];
        if (!handler) return Promise.reject(new Error(`unexpected ${url}`));
        return Promise.resolve().then(handler);
    });
}

function statusCalls(): number {
    return (request.mock.calls as unknown as Array<[string]>).filter(([url]) => url === STATUS).length;
}

const enterCode = (value: string) =>
    fireEvent.change(screen.getByPlaceholderText('000000'), { target: { value } });

async function toQrStep(routes: Partial<Record<string, Handler>> = {}) {
    serve({ [STATUS]: () => ({ enabled: false }), [SETUP]: () => QR, ...routes });
    render(<TwoFactorSetup onStatusChange={onStatusChange} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Enable 2FA' }));
    await screen.findByAltText('2FA QR Code');
}

async function toDisableStep(routes: Partial<Record<string, Handler>> = {}) {
    serve({ [STATUS]: () => ({ enabled: true }), ...routes });
    render(<TwoFactorSetup onStatusChange={onStatusChange} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Disable 2FA' }));
    await screen.findByText(/Enter your current 6-digit code/);
}

const onStatusChange = vi.fn();

describe('TwoFactorSetup', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('status', () => {
        it('offers enable when 2FA is off', async () => {
            serve({ [STATUS]: () => ({ enabled: false }) });
            render(<TwoFactorSetup />);

            expect(await screen.findByRole('button', { name: 'Enable 2FA' })).toBeInTheDocument();
            expect(screen.getByText(/Add an extra layer/)).toBeInTheDocument();
            expect(screen.queryByRole('button', { name: 'Disable 2FA' })).not.toBeInTheDocument();
        });

        it('offers disable when 2FA is on', async () => {
            serve({ [STATUS]: () => ({ enabled: true }) });
            render(<TwoFactorSetup />);

            expect(await screen.findByRole('button', { name: 'Disable 2FA' })).toBeInTheDocument();
            expect(screen.getByText(/your account is protected/)).toBeInTheDocument();
            expect(screen.queryByRole('button', { name: 'Enable 2FA' })).not.toBeInTheDocument();
        });

        it('asks once, however often it re-renders', async () => {
            serve({ [STATUS]: () => ({ enabled: false }) });
            render(<TwoFactorSetup />);
            await screen.findByRole('button', { name: 'Enable 2FA' });
            expect(statusCalls()).toBe(1);
        });

        it('does not hammer the server when the status read fails', async () => {
            serve({
                [STATUS]: () => {
                    throw new Error('status unavailable');
                },
            });
            render(<TwoFactorSetup />);

            expect(await screen.findByText('status unavailable')).toBeInTheDocument();
            await new Promise((resolve) => setTimeout(resolve, 100));
            expect(statusCalls()).toBe(1);
        });

        it('keeps a failed read unknown: no enable or disable on a guess, and Retry asks again', async () => {
            let healthy = false;
            serve({
                [STATUS]: () => {
                    if (!healthy) throw new Error('status unavailable');
                    return { enabled: true };
                },
            });
            render(<TwoFactorSetup />);
            await screen.findByText('status unavailable');

            expect(screen.queryByRole('button', { name: 'Enable 2FA' })).not.toBeInTheDocument();
            expect(screen.queryByRole('button', { name: 'Disable 2FA' })).not.toBeInTheDocument();

            healthy = true;
            fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

            expect(await screen.findByRole('button', { name: 'Disable 2FA' })).toBeInTheDocument();
            expect(statusCalls()).toBe(2);
            expect(screen.queryByText('status unavailable')).not.toBeInTheDocument();
        });
    });

    describe('enrolment', () => {
        it('POSTs setup and shows the QR code and manual secret from the response', async () => {
            await toQrStep();

            expect(request).toHaveBeenCalledWith(SETUP, { method: 'POST' });
            expect(screen.getByAltText('2FA QR Code')).toHaveAttribute('src', QR.qr_code);
            expect(screen.getByText(QR.secret)).toBeInTheDocument();
        });

        it('shows why setup failed and stays on the enable button', async () => {
            serve({
                [STATUS]: () => ({ enabled: false }),
                [SETUP]: () => {
                    throw new Error('2FA is already enabled');
                },
            });
            render(<TwoFactorSetup />);
            fireEvent.click(await screen.findByRole('button', { name: 'Enable 2FA' }));

            expect(await screen.findByText('2FA is already enabled')).toBeInTheDocument();
            expect(screen.queryByAltText('2FA QR Code')).not.toBeInTheDocument();
        });

        it('takes digits only, at most six, and will not verify fewer', async () => {
            await toQrStep();
            const input = screen.getByPlaceholderText('000000');

            enterCode('12a4');
            expect(input).toHaveValue('124');
            expect(screen.getByRole('button', { name: 'Verify' })).toBeDisabled();

            enterCode('12345678');
            expect(input).toHaveValue('123456');
            expect(screen.getByRole('button', { name: 'Verify' })).toBeEnabled();
        });

        it('sends the code, then shows backup codes and reports enabled', async () => {
            await toQrStep({ [VERIFY]: () => ({ enabled: true, backup_codes: CODES }) });

            enterCode('123456');
            fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

            expect(await screen.findByText('aaaa-1111')).toBeInTheDocument();
            expect(screen.getByText('bbbb-2222')).toBeInTheDocument();
            expect(request).toHaveBeenCalledWith(VERIFY, {
                method: 'POST',
                body: JSON.stringify({ code: '123456' }),
            });
            expect(onStatusChange).toHaveBeenCalledWith(true);

            fireEvent.click(screen.getByRole('button', { name: 'Done' }));
            expect(screen.queryByText('aaaa-1111')).not.toBeInTheDocument();
            expect(screen.getByText(/your account is protected/)).toBeInTheDocument();
        });

        it('stays on the QR step when the code is rejected, and reports nothing as enabled', async () => {
            await toQrStep({
                [VERIFY]: () => {
                    throw new Error('Invalid verification code');
                },
            });
            enterCode('000000');
            fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

            expect(await screen.findByText('Invalid verification code')).toBeInTheDocument();
            expect(screen.getByAltText('2FA QR Code')).toBeInTheDocument();
            expect(onStatusChange).not.toHaveBeenCalled();
        });

        it('Cancel returns to idle with the code and the last error gone', async () => {
            await toQrStep({
                [VERIFY]: () => {
                    throw new Error('Invalid verification code');
                },
            });
            enterCode('000000');
            fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
            await screen.findByText('Invalid verification code');

            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

            expect(screen.getByRole('button', { name: 'Enable 2FA' })).toBeInTheDocument();
            expect(screen.queryByText('Invalid verification code')).not.toBeInTheDocument();
        });
    });

    describe('backup codes', () => {
        async function toBackup() {
            await toQrStep({ [VERIFY]: () => ({ enabled: true, backup_codes: CODES }) });
            enterCode('123456');
            fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
            await screen.findByText('aaaa-1111');
        }

        it('copies them one per line', async () => {
            const writeText = vi.fn().mockResolvedValue(undefined);
            Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
            await toBackup();

            fireEvent.click(screen.getByRole('button', { name: 'Copy' }));

            expect(writeText).toHaveBeenCalledWith('aaaa-1111\nbbbb-2222');
        });

        it('downloads them as a named text file', async () => {
            const blobs: Blob[] = [];
            vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => {
                blobs.push(b as Blob);
                return 'blob:codes';
            });
            const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
            const downloads: string[] = [];
            vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
                downloads.push(this.download);
            });
            await toBackup();

            fireEvent.click(screen.getByRole('button', { name: 'Download' }));

            expect(downloads).toEqual(['brutor-2fa-backup-codes.txt']);
            expect(await blobs[0].text()).toBe('aaaa-1111\nbbbb-2222');
            expect(revoke).toHaveBeenCalledWith('blob:codes');
        });
    });

    describe('removal', () => {
        it('needs a full code', async () => {
            await toDisableStep();
            enterCode('123');
            expect(screen.getByRole('button', { name: 'Disable' })).toBeDisabled();
        });

        it('sends the code and reports disabled', async () => {
            await toDisableStep({ [DISABLE]: () => ({ enabled: false }) });
            enterCode('654321');
            fireEvent.click(screen.getByRole('button', { name: 'Disable' }));

            expect(await screen.findByRole('button', { name: 'Enable 2FA' })).toBeInTheDocument();
            expect(request).toHaveBeenCalledWith(DISABLE, {
                method: 'POST',
                body: JSON.stringify({ code: '654321' }),
            });
            expect(onStatusChange).toHaveBeenCalledWith(false);
        });

        it('stays protected when the code is rejected', async () => {
            await toDisableStep({
                [DISABLE]: () => {
                    throw new Error('Invalid TOTP code');
                },
            });
            enterCode('000000');
            fireEvent.click(screen.getByRole('button', { name: 'Disable' }));

            expect(await screen.findByText('Invalid TOTP code')).toBeInTheDocument();
            expect(onStatusChange).not.toHaveBeenCalled();
            expect(screen.getByText(/your account is protected/)).toBeInTheDocument();
        });

        it("a 409 shows the policy's reason, closes the code entry and stays protected", async () => {
            // The tenant's two-factor policy requires 2FA of its
            // administrators, so the control plane refuses to switch it off.
            const reason =
                "Two-factor authentication is required for administrators by your organization's security policy and cannot be disabled.";
            await toDisableStep({
                [DISABLE]: () => {
                    throw Object.assign(new Error(reason), { status: 409 });
                },
            });
            enterCode('654321');
            fireEvent.click(screen.getByRole('button', { name: 'Disable' }));

            expect(await screen.findByText(reason)).toBeInTheDocument();
            // Another code would not change the answer: no code entry left open.
            await waitFor(() => expect(screen.queryByPlaceholderText('000000')).not.toBeInTheDocument());
            expect(screen.getByText(/your account is protected/)).toBeInTheDocument();
            expect(screen.getByRole('button', { name: 'Disable 2FA' })).toBeInTheDocument();
            expect(onStatusChange).not.toHaveBeenCalled();
        });

        it('Cancel backs out without a request', async () => {
            await toDisableStep();
            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

            expect(screen.getByRole('button', { name: 'Disable 2FA' })).toBeInTheDocument();
            expect(request).not.toHaveBeenCalledWith(DISABLE, expect.anything());
        });

        it.todo(
            'BUG: a rejected TOTP code reads "Authentication failed", not the server\'s "Invalid TOTP code". gatewayApiClient.ts:656 builds the 401 error from errorData.message and ignores FastAPI\'s errorData.detail. The fix belongs in the shared client',
        );
    });

    it('disables the enable button while setup is in flight', async () => {
        let release: (v: unknown) => void = () => {};
        const pending = new Promise((resolve) => {
            release = resolve;
        });
        serve({ [STATUS]: () => ({ enabled: false }), [SETUP]: () => pending });
        render(<TwoFactorSetup />);
        fireEvent.click(await screen.findByRole('button', { name: 'Enable 2FA' }));

        const button = await screen.findByRole('button', { name: 'Setting up...' });
        expect(button).toBeDisabled();

        release(QR);
        await waitFor(() => expect(screen.getByAltText('2FA QR Code')).toBeInTheDocument());
    });
});
