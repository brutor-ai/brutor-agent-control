/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * End Users list: the actions that change an account. Listing, paging and search
 * are pinned in EndUsersListTab.test.tsx.
 *
 * Pinned: status changes send the inverse of what is on screen, delete needs the
 * name typed and reports failure inside the modal, and the create body. The
 * welcome-email box is in that body: the server defaults `send_welcome_email`
 * to true, so with the box left out, unticking it would still email the user.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

vi.mock('../../../hooks/useGateway', async () => (await import('../../../test/users/endUsersListTab')).useGatewayModule());

import EndUsersListTab from '../EndUsersListTab';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import { ALICE, BOB, listOf, mockGateway, request, resetEndUsersList } from '../../../test/users/endUsersListTab';

function rowOf(text: string): HTMLElement {
    const row = screen.getByText(text).closest('tr');
    if (!row) throw new Error(`no row for ${text}`);
    return row;
}

async function loaded() {
    // Send Message is contributed through a slot: rendered as the application does.
    render(<EndUsersListTab />, { wrapper: WithBuiltinSlots });
    await screen.findByText('Alice A');
}

function statCard(label: string): string | null | undefined {
    return screen.getByText(label).nextElementSibling?.textContent;
}

describe('EndUsersListTab actions', () => {
    beforeEach(() => resetEndUsersList([ALICE, BOB]));

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('what the table says', () => {
        it('reports the server\'s total, not the size of the page', async () => {
            mockGateway.fetchEndUsers.mockResolvedValue(listOf([ALICE, BOB], 240));
            await loaded();
            expect(statCard('Total Users')).toBe('240');
        });

        it('counts active, verified and inactive from the rows', async () => {
            await loaded();
            expect(statCard('Active Users')).toBe('1');
            expect(statCard('Verified Users')).toBe('1');
            expect(statCard('Inactive Users')).toBe('1');
        });

        it('shows status badges and last login per row', async () => {
            await loaded();
            const alice = within(rowOf('Alice A'));
            expect(alice.getByText('Active')).toBeInTheDocument();
            expect(alice.getByText('Verified')).toBeInTheDocument();
            expect(alice.getByText('@alice')).toBeInTheDocument();
            const bob = within(rowOf('bob'));
            expect(bob.getByText('Inactive')).toBeInTheDocument();
            expect(bob.getByText('Unverified')).toBeInTheDocument();
            expect(bob.getByText('Never')).toBeInTheDocument();
        });

        it('narrows the loaded page to inactive users', async () => {
            await loaded();
            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'inactive' } });

            expect(await screen.findByText('bob')).toBeInTheDocument();
            await waitFor(() => expect(screen.queryByText('Alice A')).not.toBeInTheDocument());
        });

        it('tells "nothing matches" apart from "nobody yet"', async () => {
            mockGateway.fetchEndUsers.mockResolvedValue(listOf([]));
            render(<EndUsersListTab />);
            expect(await screen.findByText('No end users yet')).toBeInTheDocument();

            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'active' } });
            expect(await screen.findByText('No users found')).toBeInTheDocument();
        });

        it('steps back a page when the last row of the last page is gone', async () => {
            mockGateway.fetchEndUsers.mockImplementation((q: { offset: number }) =>
                Promise.resolve(q.offset === 100 ? listOf([], 100) : listOf([ALICE], 101)),
            );
            await loaded();
            fireEvent.click(screen.getByRole('button', { name: 'Next' }));

            await waitFor(() =>
                expect(mockGateway.fetchEndUsers).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0 })),
            );
            expect(await screen.findByText('Alice A')).toBeInTheDocument();
        });

        it('shows a load failure', async () => {
            mockGateway.fetchEndUsers.mockRejectedValue(new Error('list failed'));
            render(<EndUsersListTab />);
            expect(await screen.findByText('list failed')).toBeInTheDocument();
        });
    });

    describe('details', () => {
        it('fetches the user clicked and shows the response', async () => {
            mockGateway.fetchEndUserDetails.mockResolvedValue({ ...BOB, display_name: null });
            await loaded();
            fireEvent.click(within(rowOf('bob')).getByTitle('View Details'));

            const heading = await screen.findByRole('heading', { name: 'User Details' });
            expect(mockGateway.fetchEndUserDetails).toHaveBeenCalledWith(2);
            const modal = heading.closest<HTMLElement>('.fixed');
            if (!modal) throw new Error('no modal');
            expect(within(modal).getByText('bob@acme.test')).toBeInTheDocument();
            expect(within(modal).getByText('Not set')).toBeInTheDocument();
            expect(within(modal).getByText('Inactive • Unverified')).toBeInTheDocument();

            fireEvent.click(within(modal).getByRole('button', { name: 'Close' }));
            expect(screen.queryByRole('heading', { name: 'User Details' })).not.toBeInTheDocument();
        });

        it('shows a failure, with no modal, and lets it be dismissed', async () => {
            mockGateway.fetchEndUserDetails.mockRejectedValue(new Error('details failed'));
            await loaded();
            fireEvent.click(within(rowOf('Alice A')).getByTitle('View Details'));

            const message = await screen.findByText('details failed');
            expect(screen.queryByRole('heading', { name: 'User Details' })).not.toBeInTheDocument();

            const box = message.parentElement;
            if (!box) throw new Error('no error box');
            fireEvent.click(within(box).getByRole('button', { name: '×' }));
            expect(screen.queryByText('details failed')).not.toBeInTheDocument();
        });
    });

    describe('status', () => {
        it('deactivates an active user and reloads', async () => {
            mockGateway.updateEndUserStatus.mockResolvedValue({});
            await loaded();
            const before = mockGateway.fetchEndUsers.mock.calls.length;

            fireEvent.click(within(rowOf('Alice A')).getByTitle('Deactivate'));

            await waitFor(() => expect(mockGateway.updateEndUserStatus).toHaveBeenCalledWith(1, false));
            await waitFor(() => expect(mockGateway.fetchEndUsers.mock.calls.length).toBeGreaterThan(before));
        });

        it('activates an inactive user', async () => {
            mockGateway.updateEndUserStatus.mockResolvedValue({});
            await loaded();
            fireEvent.click(within(rowOf('bob')).getByTitle('Activate'));
            await waitFor(() => expect(mockGateway.updateEndUserStatus).toHaveBeenCalledWith(2, true));
        });

        it('shows a failure and leaves the row as it was', async () => {
            mockGateway.updateEndUserStatus.mockRejectedValue(new Error('status refused'));
            await loaded();
            fireEvent.click(within(rowOf('Alice A')).getByTitle('Deactivate'));

            expect(await screen.findByText('status refused')).toBeInTheDocument();
            expect(within(rowOf('Alice A')).getByText('Active')).toBeInTheDocument();
        });
    });

    describe('delete', () => {
        const confirmButton = () => screen.getByRole('button', { name: 'Delete End User' });
        const confirmInput = () => screen.getByPlaceholderText('Enter "alice"');

        async function openDelete() {
            await loaded();
            fireEvent.click(within(rowOf('Alice A')).getByTitle('Delete User'));
            await screen.findByPlaceholderText('Enter "alice"');
        }

        it('needs the username typed, deletes that user, closes and reloads', async () => {
            mockGateway.deleteEndUser.mockResolvedValue({});
            await openDelete();
            const before = mockGateway.fetchEndUsers.mock.calls.length;

            expect(confirmButton()).toBeDisabled();
            fireEvent.change(confirmInput(), { target: { value: 'alic' } });
            expect(confirmButton()).toBeDisabled();
            fireEvent.change(confirmInput(), { target: { value: 'alice' } });
            fireEvent.click(confirmButton());

            await waitFor(() => expect(mockGateway.deleteEndUser).toHaveBeenCalledWith(1));
            await waitFor(() => expect(mockGateway.fetchEndUsers.mock.calls.length).toBeGreaterThan(before));
            await waitFor(() => expect(screen.queryByPlaceholderText('Enter "alice"')).not.toBeInTheDocument());
        });

        it('identifies who is being deleted', async () => {
            await openDelete();
            expect(screen.getByText('alice@acme.test - Alice A')).toBeInTheDocument();
        });

        it('reports a failure inside the modal and keeps it open', async () => {
            mockGateway.deleteEndUser.mockRejectedValue(new Error('has dependants'));
            await openDelete();
            fireEvent.change(confirmInput(), { target: { value: 'alice' } });
            fireEvent.click(confirmButton());

            const failed = (await screen.findByText('Delete failed')).parentElement;
            if (!failed) throw new Error('no error box');
            expect(within(failed).getByText('has dependants')).toBeInTheDocument();
            expect(confirmInput()).toBeInTheDocument();
        });

        it('prefers a server `detail` and falls back to a fixed message', async () => {
            mockGateway.deleteEndUser.mockRejectedValueOnce(
                Object.assign(new Error('HTTP 409'), { detail: 'Last admin of group' }),
            );
            await openDelete();
            fireEvent.change(confirmInput(), { target: { value: 'alice' } });
            fireEvent.click(confirmButton());
            expect(await screen.findByText('Last admin of group')).toBeInTheDocument();

            mockGateway.deleteEndUser.mockRejectedValueOnce({});
            fireEvent.click(confirmButton());
            expect(await screen.findByText('Failed to delete user')).toBeInTheDocument();
        });

        it('forgets the earlier failure when reopened', async () => {
            mockGateway.deleteEndUser.mockRejectedValue(new Error('has dependants'));
            await openDelete();
            fireEvent.change(confirmInput(), { target: { value: 'alice' } });
            fireEvent.click(confirmButton());
            await screen.findByText('has dependants');

            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
            expect(screen.queryByPlaceholderText('Enter "alice"')).not.toBeInTheDocument();

            fireEvent.click(within(rowOf('Alice A')).getByTitle('Delete User'));
            await screen.findByPlaceholderText('Enter "alice"');
            expect(screen.queryByText('has dependants')).not.toBeInTheDocument();
        });

        it('closes the details of a user who was just deleted', async () => {
            mockGateway.fetchEndUserDetails.mockResolvedValue(ALICE);
            mockGateway.deleteEndUser.mockResolvedValue({});
            await loaded();
            fireEvent.click(within(rowOf('Alice A')).getByTitle('View Details'));
            await screen.findByRole('heading', { name: 'User Details' });

            // '@alice' is only in the table: the open modal also shows 'Alice A'.
            fireEvent.click(within(rowOf('@alice')).getByTitle('Delete User'));
            fireEvent.change(await screen.findByPlaceholderText('Enter "alice"'), { target: { value: 'alice' } });
            fireEvent.click(confirmButton());

            await waitFor(() => expect(mockGateway.deleteEndUser).toHaveBeenCalledWith(1));
            await waitFor(() =>
                expect(screen.queryByRole('heading', { name: 'User Details' })).not.toBeInTheDocument(),
            );
        });
    });

    describe('export', () => {
        it('asks for CSV and saves the blob under a dated name', async () => {
            const blob = new Blob(['id,username\n'], { type: 'text/csv' });
            mockGateway.exportEndUsers.mockResolvedValue(blob);
            const created = vi.spyOn(window.URL, 'createObjectURL').mockReturnValue('blob:csv');
            const revoked = vi.spyOn(window.URL, 'revokeObjectURL').mockImplementation(() => {});
            const downloads: string[] = [];
            vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
                downloads.push(this.download);
            });
            await loaded();

            fireEvent.click(screen.getByRole('button', { name: 'Export' }));

            await waitFor(() => expect(revoked).toHaveBeenCalledWith('blob:csv'));
            expect(mockGateway.exportEndUsers).toHaveBeenCalledWith('csv');
            expect(created).toHaveBeenCalledWith(blob);
            expect(downloads).toHaveLength(1);
            expect(downloads[0]).toMatch(/^end-users-\d{4}-\d{2}-\d{2}T.*\.csv$/);
        });

        it('shows a failure', async () => {
            mockGateway.exportEndUsers.mockRejectedValue(new Error('export failed'));
            await loaded();
            fireEvent.click(screen.getByRole('button', { name: 'Export' }));
            expect(await screen.findByText('export failed')).toBeInTheDocument();
        });
    });

    describe('create', () => {
        function modal(): HTMLElement {
            const box = screen.getByRole('heading', { name: 'Create End User' }).closest<HTMLElement>('.fixed');
            if (!box) throw new Error('no modal');
            return box;
        }

        async function openCreate() {
            await loaded();
            fireEvent.click(screen.getByRole('button', { name: 'Create User' }));
            return modal();
        }

        function fillRequired() {
            fireEvent.change(screen.getByPlaceholderText('johndoe'), { target: { value: 'carol' } });
            fireEvent.change(screen.getByPlaceholderText('john@example.com'), { target: { value: 'carol@acme.test' } });
            fireEvent.change(screen.getByPlaceholderText('Min. 8 characters'), { target: { value: 'password-1' } });
        }

        const submit = (box: HTMLElement) =>
            fireEvent.click(within(box).getByRole('button', { name: 'Create User' }));

        function pickFile(box: HTMLElement, file: File) {
            const input = box.querySelector('input[type="file"]');
            if (!input) throw new Error('no file input');
            fireEvent.change(input, { target: { files: [file] } });
        }

        function photoBody(): FormData {
            const call = (request.mock.calls as unknown as Array<[string, { body: unknown }]>).find(([url]) =>
                url.endsWith('/photo'),
            );
            if (!call) throw new Error('no photo upload');
            return call[1].body as FormData;
        }

        const PNG = new File(['png-bytes'], 'me.png', { type: 'image/png' });

        it.each([
            ['eight spaces', '        '],
            ['seven characters', 'short12'],
        ])('refuses a password of %s before anything is sent, in the words of the rule', async (_name, password) => {
            const box = await openCreate();
            fillRequired();
            fireEvent.change(screen.getByPlaceholderText('Min. 8 characters'), { target: { value: password } });
            const form = box.querySelector('form');
            if (!form) throw new Error('no form');
            fireEvent.submit(form);

            expect(await within(box).findByText('Password must be at least 8 characters and not blank.')).toBeInTheDocument();
            expect(request).not.toHaveBeenCalled();
        });

        it('posts exactly the form, welcome email on by default, then closes and reloads', async () => {
            request.mockResolvedValue({ success: true, user: { id: 9 } });
            const box = await openCreate();
            fillRequired();
            fireEvent.change(screen.getByPlaceholderText('John Doe'), { target: { value: 'Carol C' } });
            const before = mockGateway.fetchEndUsers.mock.calls.length;
            submit(box);

            await waitFor(() =>
                expect(request).toHaveBeenCalledWith('/v1/admin/end-users', {
                    method: 'POST',
                    body: JSON.stringify({
                        username: 'carol',
                        email: 'carol@acme.test',
                        password: 'password-1',
                        display_name: 'Carol C',
                        role_id: '',
                        group_ids: [],
                        send_welcome_email: true,
                    }),
                }),
            );
            await waitFor(() => expect(screen.queryByRole('heading', { name: 'Create End User' })).not.toBeInTheDocument());
            await waitFor(() => expect(mockGateway.fetchEndUsers.mock.calls.length).toBeGreaterThan(before));
            // No photo chosen, so no second request.
            expect(request).toHaveBeenCalledTimes(1);
        });

        it('sends the welcome-email choice when it is unticked', async () => {
            request.mockResolvedValue({ success: true, user: { id: 9 } });
            const box = await openCreate();
            fillRequired();
            const box2 = within(box).getByRole('checkbox', { name: 'Send welcome email' });
            expect(box2).toBeChecked();
            fireEvent.click(box2);
            submit(box);

            await waitFor(() => expect(request).toHaveBeenCalled());
            const [, init] = request.mock.calls[0] as unknown as [string, { body: string }];
            expect(JSON.parse(init.body)).toMatchObject({ send_welcome_email: false });
        });

        it('does not promise to email login credentials, which the server never sends', async () => {
            const box = await openCreate();
            expect(within(box).queryByText(/login credentials/)).not.toBeInTheDocument();
        });

        it('reopens blank, including the welcome-email default', async () => {
            const box = await openCreate();
            fillRequired();
            fireEvent.click(within(box).getByRole('checkbox', { name: 'Send welcome email' }));
            fireEvent.click(within(box).getByRole('button', { name: 'Cancel' }));
            expect(screen.queryByRole('heading', { name: 'Create End User' })).not.toBeInTheDocument();

            fireEvent.click(screen.getByRole('button', { name: 'Create User' }));
            const again = modal();
            expect(screen.getByPlaceholderText('johndoe')).toHaveValue('');
            expect(within(again).getByRole('checkbox', { name: 'Send welcome email' })).toBeChecked();
            expect(request).not.toHaveBeenCalled();
        });

        it('shows a server rejection and keeps the form', async () => {
            request.mockRejectedValue(new Error("Username 'carol' already exists"));
            const box = await openCreate();
            fillRequired();
            submit(box);

            expect(await within(box).findByText("Username 'carol' already exists")).toBeInTheDocument();
            expect(screen.getByPlaceholderText('johndoe')).toHaveValue('carol');
        });

        it('does not report success on a response that does not say so', async () => {
            request.mockResolvedValueOnce({ success: false, detail: 'Quota reached' });
            const box = await openCreate();
            fillRequired();
            submit(box);
            expect(await within(box).findByText('Quota reached')).toBeInTheDocument();

            request.mockResolvedValueOnce({});
            submit(box);
            expect(await within(box).findByText('Failed to create user')).toBeInTheDocument();
        });

        it('uploads a chosen photo to the new user as a second step', async () => {
            request.mockResolvedValue({ success: true, user: { id: 9 } });
            const box = await openCreate();
            fillRequired();
            pickFile(box, PNG);
            expect(await within(box).findByRole('button', { name: 'Clear' })).toBeInTheDocument();
            submit(box);

            await waitFor(() =>
                expect(request).toHaveBeenCalledWith('/v1/admin/end-users/9/photo', expect.objectContaining({ method: 'POST' })),
            );
            expect(photoBody().get('file')).toBeInstanceOf(File);
            await waitFor(() => expect(screen.queryByRole('heading', { name: 'Create End User' })).not.toBeInTheDocument());
        });

        it('does not upload a photo that was cleared', async () => {
            request.mockResolvedValue({ success: true, user: { id: 9 } });
            const box = await openCreate();
            fillRequired();
            pickFile(box, PNG);
            fireEvent.click(await within(box).findByRole('button', { name: 'Clear' }));
            submit(box);

            await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
            await waitFor(() => expect(screen.queryByRole('heading', { name: 'Create End User' })).not.toBeInTheDocument());
        });

        it('says the user exists when only the photo failed, and keeps the modal up', async () => {
            request.mockImplementation((url: string) =>
                url.endsWith('/photo')
                    ? Promise.reject(new Error('too big'))
                    : Promise.resolve({ success: true, user: { id: 9 } }),
            );
            const box = await openCreate();
            fillRequired();
            pickFile(box, PNG);
            await within(box).findByRole('button', { name: 'Clear' });
            const before = mockGateway.fetchEndUsers.mock.calls.length;
            submit(box);

            // The user exists, and why the photo is not there, as the Control Plane words it.
            expect(await screen.findByText('User created, but photo upload failed — try again from the user detail screen: too big')).toBeInTheDocument();
            expect(screen.getByRole('heading', { name: 'Create End User' })).toBeInTheDocument();
            expect(mockGateway.fetchEndUsers.mock.calls.length).toBeGreaterThan(before);
        });

        it('refuses an oversized photo', async () => {
            const box = await openCreate();
            const big = new File(['x'], 'big.png', { type: 'image/png' });
            Object.defineProperty(big, 'size', { value: 5 * 1024 * 1024 + 1 });
            pickFile(box, big);

            expect(await within(box).findByText('Photo must be under 5 MB')).toBeInTheDocument();
            expect(within(box).queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument();
        });

        it('refuses a photo of an unsupported type', async () => {
            const box = await openCreate();
            pickFile(box, new File(['x'], 'evil.svg', { type: 'image/svg+xml' }));

            expect(await within(box).findByText('Unsupported image type: image/svg+xml')).toBeInTheDocument();
            expect(within(box).queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument();
        });

        it('reveals the password only on request', async () => {
            const box = await openCreate();
            const password = screen.getByPlaceholderText('Min. 8 characters');
            expect(password).toHaveAttribute('type', 'password');
            const eye = password.parentElement?.querySelector('button');
            if (!eye) throw new Error('no toggle');
            fireEvent.click(eye);
            expect(password).toHaveAttribute('type', 'text');
            expect(box).toBeInTheDocument();
        });
    });
});
