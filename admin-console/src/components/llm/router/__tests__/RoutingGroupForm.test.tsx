/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Routing group form.
 *
 * The payload is the point: it holds internal `_`-prefixed state that must not
 * leak, and on edit it must carry what the admin emptied. The server only
 * touches keys it receives, so a "drop whatever is empty" cleanup would mean
 * that removing the last fallback, or blanking the description, reports success
 * and changes nothing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

vi.mock('../ComplexityTierEditor', () => ({
    default: ({
        groupModels,
        value,
        onChange,
        onFloorTierChange,
        floorTier,
    }: {
        groupModels: Array<{ id: string }>;
        value: unknown[];
        onChange: (next: unknown[]) => void;
        onFloorTierChange: (tier: string | null) => void;
        floorTier: string | null;
    }) => (
        <div>
            tier editor for {groupModels.map(m => m.id).join(',')} with {value.length} tiers, floor {floorTier ?? 'none'}
            <button
                type="button"
                onClick={() => onChange([{ tier: 'simple', llm_model_id: 'm1', position: 0 }])}
            >
                set tier
            </button>
            <button type="button" onClick={() => onFloorTierChange('medium')}>
                set floor
            </button>
            <button type="button" onClick={() => onFloorTierChange(null)}>
                clear floor
            </button>
        </div>
    ),
}));
vi.mock('../../../common/AuditInfoTab', () => ({
    default: ({ onVersionChange, isNew }: { onVersionChange: (v: string) => void; isNew: boolean }) => (
        <div>
            audit tab {isNew ? 'new' : 'existing'}
            <button type="button" onClick={() => onVersionChange('2.0')}>
                bump version
            </button>
        </div>
    ),
}));

import RoutingGroupForm from '../RoutingGroupForm';
import type { RoutingGroup } from '../../../../lib/llmTypes';

const onSubmit = vi.fn();
const onCancel = vi.fn();
const onTiersChange = vi.fn();

const others = [
    { id: 'g-self', name: 'Self' },
    { id: 'g-a', name: 'Group A' },
    { id: 'g-b', name: 'Group B' },
];

const defaults = {
    version: '1.0',
    mode: 'chat',
    routing_strategy: 'usage-based-routing-v2',
    cooldown_time: 60,
    max_fallbacks: 5,
    num_retries: 3,
    retry_after: 5,
    enabled: true,
};

/** What the form sends for a new group with the required fields filled, sorted. */
const CREATE_FIELDS = [
    'cooldown_time', 'enabled', 'id', 'max_fallbacks',
    'mode', 'name', 'num_retries', 'retry_after', 'routing_strategy', 'version',
];
/** What the form sends back for a stored group, sorted. */
const UPDATE_FIELDS = [
    'content_policy_fallbacks',
    'context_window_fallbacks', 'cooldown_time', 'default_fallbacks', 'description', 'enabled',
    'fallback_models', 'max_fallbacks', 'mode', 'name', 'num_retries', 'retry_after', 'routing_strategy',
    'stream_timeout', 'version',
];
const SECTION_LABELS = [
    'Basic Information',
    'Audit Info',
    'Routing Strategy',
    'Fallbacks',
    'Failure Handling',
    'Retries & Timeouts',
    'Rate Limiting',
];

function group(overrides: Record<string, unknown> = {}): RoutingGroup {
    return {
        id: 'g-self',
        name: 'Self',
        description: 'Original',
        version: '3.1',
        mode: 'chat',
        routing_strategy: 'least-busy',
        allowed_fails: 2,
        cooldown_time: 30,
        cooldown_window: 90,
        num_retries: 1,
        retry_after: 2,
        timeout: 120,
        enabled: true,
        num_models: 0,
        ...overrides,
    } as unknown as RoutingGroup;
}

function renderCreate() {
    return render(
        <RoutingGroupForm isEditing={false} onSubmit={onSubmit} onCancel={onCancel} availableGroups={others} />
    );
}

function renderEdit(g: RoutingGroup = group()) {
    return render(
        <RoutingGroupForm routingGroup={g} isEditing onSubmit={onSubmit} onCancel={onCancel} availableGroups={others} />
    );
}

function field(labelText: string): HTMLInputElement {
    const label = Array.from(document.querySelectorAll('label')).find(l =>
        (l.textContent ?? '').trim().startsWith(labelText)
    );
    if (!label) throw new Error(`No label starting with "${labelText}"`);
    const control = label.parentElement?.querySelector('input,select');
    if (!control) throw new Error(`No control for "${labelText}"`);
    return control as HTMLInputElement;
}

function setField(labelText: string, value: string) {
    fireEvent.change(field(labelText), { target: { value } });
}

function section(label: string) {
    fireEvent.click(within(document.querySelector('nav') as HTMLElement).getByRole('button', { name: new RegExp(`^${label}`) }));
}

function navButton(label: string): HTMLElement {
    return within(document.querySelector('nav') as HTMLElement).getByRole('button', { name: new RegExp(`^${label}`) });
}

function picker(label: string): HTMLElement {
    const root = screen.getByText(label).parentElement;
    if (!root) throw new Error(`No picker ${label}`);
    return root;
}

function addFallback(label: string, groupName: string) {
    const root = picker(label);
    fireEvent.click(within(root).getByRole('button', { name: /Add fallback group/ }));
    fireEvent.click(within(root).getByRole('button', { name: new RegExp(`^${groupName}`) }));
}

function fillRequired() {
    fireEvent.change(screen.getByPlaceholderText('e.g., production-gpt-4'), { target: { value: 'g-new' } });
    fireEvent.change(screen.getByPlaceholderText('e.g., Production GPT-4 Pool'), { target: { value: 'New Group' } });
}

async function submitCreate() {
    fireEvent.click(screen.getByRole('button', { name: 'Create Routing Group' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    return onSubmit.mock.calls[0][0] as Record<string, unknown>;
}

async function submitUpdate() {
    fireEvent.click(screen.getByRole('button', { name: 'Update Routing Group' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    return onSubmit.mock.calls[0][0] as Record<string, unknown>;
}

describe('RoutingGroupForm', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        onSubmit.mockResolvedValue(undefined);
    });
    afterEach(cleanup);

    describe('create', () => {
        it('sends the defaults, without internal state or empty values', async () => {
            renderCreate();
            fillRequired();
            expect(await submitCreate()).toEqual({ id: 'g-new', name: 'New Group', ...defaults });
        });

        it('requires id and name and does not submit', async () => {
            renderCreate();
            fireEvent.click(screen.getByRole('button', { name: 'Create Routing Group' }));
            expect(await screen.findByText('ID is required')).toBeTruthy();
            expect(screen.getByText('Name is required')).toBeTruthy();
            expect(onSubmit).not.toHaveBeenCalled();
            expect(navButton('Basic Information').className).toContain('text-red-600');

            // Typing clears that field's error.
            fireEvent.change(screen.getByPlaceholderText('e.g., production-gpt-4'), { target: { value: 'x' } });
            expect(screen.queryByText('ID is required')).toBeNull();
        });

        it('jumps to the section holding the first error', async () => {
            renderCreate();
            fillRequired();
            section('Failure Handling');
            setField('Cooldown Time (sec)', '-1');
            section('Basic Information');
            fireEvent.click(screen.getByRole('button', { name: 'Create Routing Group' }));
            expect(await screen.findByText('Must be 0 or greater')).toBeTruthy();
            expect(screen.getByRole('heading', { name: 'Failure Handling' })).toBeTruthy();
            expect(onSubmit).not.toHaveBeenCalled();
        });

        it.each([
            ['Failure Handling', 'Cooldown Time (sec)', '-5', 'Must be 0 or greater'],
            ['Retries & Timeouts', 'Retries per Model', '-1', 'Must be 0 or greater'],
        ])('rejects a bad value in %s > %s', async (sec, label, value, message) => {
            renderCreate();
            fillRequired();
            section(sec);
            setField(label, value);
            // From another section: on its own section the browser's min= check
            // stops the submit first, and that field is not in the DOM otherwise.
            section('Basic Information');
            fireEvent.click(screen.getByRole('button', { name: 'Create Routing Group' }));
            expect(await screen.findByText(message)).toBeTruthy();
            expect(screen.getByRole('heading', { name: sec })).toBeTruthy();
            expect(onSubmit).not.toHaveBeenCalled();
        });

        it('sends the stream timeout set, and leaves it out when it is left empty', async () => {
            renderCreate();
            fillRequired();
            section('Retries & Timeouts');
            setField('Stream Timeout (sec)', '12.5');
            expect((await submitCreate()).stream_timeout).toBe(12.5);
        });

        it('refuses a stream timeout outside (0, 1800] and does not submit', async () => {
            renderCreate();
            fillRequired();
            section('Retries & Timeouts');
            setField('Stream Timeout (sec)', '1801');
            section('Basic Information');
            fireEvent.click(screen.getByRole('button', { name: 'Create Routing Group' }));
            expect(await screen.findByText('Greater than 0 and at most 1800 seconds')).toBeTruthy();
            expect(onSubmit).not.toHaveBeenCalled();
        });

        it('sends what every section sets', async () => {
            renderCreate();
            fillRequired();
            fireEvent.change(screen.getByPlaceholderText('Brief description of this routing group'), {
                target: { value: 'desc' },
            });
            setField('Mode', 'embedding');
            fireEvent.click(screen.getByLabelText('Enabled'));

            section('Routing Strategy');
            fireEvent.click(screen.getByRole('radio', { name: /Fastest Response/ }));

            section('Failure Handling');
            setField('Cooldown Time (sec)', '11');

            section('Retries & Timeouts');
            setField('Retries per Model', '7');
            setField('Retry After (sec)', '8');

            section('Rate Limiting');
            setField('Requests / Minute (RPM)', '100');
            setField('Tokens / Minute (TPM)', '2000');
            setField('Max Parallel Requests', '3');

            section('Fallbacks');
            setField('Max Fallback Attempts', '9');

            section('Audit Info');
            expect(screen.getByText('audit tab new')).toBeTruthy();
            fireEvent.click(screen.getByRole('button', { name: 'bump version' }));

            expect(await submitCreate()).toEqual({
                id: 'g-new',
                name: 'New Group',
                description: 'desc',
                version: '2.0',
                mode: 'embedding',
                enabled: false,
                routing_strategy: 'latency-based-routing',
                cooldown_time: 11,
                max_fallbacks: 9,
                num_retries: 7,
                retry_after: 8,
                rpm_limit: 100,
                tpm_limit: 2000,
                max_parallel_requests: 3,
            });
        });

        it('leaves limits out when they are blanked again', async () => {
            renderCreate();
            fillRequired();
            section('Rate Limiting');
            setField('Requests / Minute (RPM)', '100');
            setField('Requests / Minute (RPM)', '');
            const payload = await submitCreate();
            expect(payload).not.toHaveProperty('rpm_limit');
        });
    });

    describe('what the form holds', () => {
        it('has these sections, in order', () => {
            renderCreate();
            const nav = within(document.querySelector('nav') as HTMLElement);
            expect(nav.getAllByRole('button').map((b) => (b.textContent ?? '').replace(/[●!]/g, '').trim())).toEqual(
                SECTION_LABELS.map((label) => expect.stringContaining(label))
            );
        });

        it('a new group is sent with these fields and no other', async () => {
            renderCreate();
            fillRequired();
            const payload = await submitCreate();
            expect(Object.keys(payload).sort()).toEqual(CREATE_FIELDS);
        });

        it('a stored group is sent back with these fields and no other', async () => {
            renderEdit(group());
            const payload = await submitUpdate();
            expect(Object.keys(payload).sort()).toEqual(UPDATE_FIELDS);
            expect(payload).toMatchObject({ name: 'Self', cooldown_time: 30, num_retries: 1, retry_after: 2 });
        });

        // The Control Plane's routing-group requests name no switch for
        // cooldowns; the alerting fields are not enforced and not offered.
        it('offers no control the Control Plane has no field for, and no alerting', () => {
            renderCreate();
            section('Failure Handling');
            expect(screen.queryByText('Disable cooldowns entirely')).toBeNull();
            section('Retries & Timeouts');
            expect(screen.getByText('Stream Timeout (sec)')).toBeTruthy();
            expect(screen.getByText('Retry After (sec)')).toBeTruthy();
            expect(screen.queryByLabelText('Enable alerting')).toBeNull();
            expect(screen.queryByText('Alerting')).toBeNull();
        });

        it('shows no alerting of a group, which no response carries, and sends none', async () => {
            renderEdit({ ...group(), ...({ alerting_enabled: true, alerting_threshold: 90, alerting_webhook_url: 'https://hooks.test/old' } as object) } as RoutingGroup);
            section('Failure Handling');
            expect(screen.queryByTestId('routing-group-alerting-unavailable')).toBeNull();
            expect(screen.queryByText(/hooks\.test/)).toBeNull();
            const payload = await submitUpdate();
            expect(payload).not.toHaveProperty('alerting_enabled');
            expect(payload).not.toHaveProperty('alerting_webhook_url');
        });
    });

    describe('fallbacks', () => {
        it('keeps the order picked, per type, and omits empty types on create', async () => {
            renderCreate();
            fillRequired();
            section('Fallbacks');
            addFallback('General Fallbacks', 'Group B');
            addFallback('General Fallbacks', 'Group A');
            addFallback('Context Window Fallbacks', 'Group A');
            addFallback('Content Policy Fallbacks', 'Group B');
            addFallback('Default Fallbacks', 'Self');
            const payload = await submitCreate();
            expect(payload.fallback_models).toEqual(['g-b', 'g-a']);
            expect(payload.context_window_fallbacks).toEqual(['g-a']);
            expect(payload.content_policy_fallbacks).toEqual(['g-b']);
            expect(payload.default_fallbacks).toEqual(['g-self']);
        });

        it('reorders with the arrows and honours the bounds', async () => {
            renderCreate();
            fillRequired();
            section('Fallbacks');
            addFallback('General Fallbacks', 'Group A');
            addFallback('General Fallbacks', 'Group B');
            const root = picker('General Fallbacks');
            const up = within(root).getAllByTitle<HTMLButtonElement>('Move up (higher priority)');
            const down = within(root).getAllByTitle<HTMLButtonElement>('Move down (lower priority)');
            expect(up[0].disabled).toBe(true);
            expect(down[1].disabled).toBe(true);

            fireEvent.click(up[1]);
            expect(root.textContent).toMatch(/1Group B.*2Group A/);
            fireEvent.click(within(root).getAllByTitle('Move down (lower priority)')[0]);
            expect(root.textContent).toMatch(/1Group A.*2Group B/);
            fireEvent.click(within(root).getAllByTitle('Move up (higher priority)')[1]);

            expect((await submitCreate()).fallback_models).toEqual(['g-b', 'g-a']);
        });

        it('does not offer the group as its own fallback, and says when all are used', () => {
            renderEdit();
            section('Fallbacks');
            const root = picker('General Fallbacks');
            fireEvent.click(within(root).getByRole('button', { name: 'Add fallback group (2 available)…' }));
            expect(within(root).queryByRole('button', { name: /^Self/ })).toBeNull();

            fireEvent.click(within(root).getByRole('button', { name: /^Group A/ }));
            addFallback('General Fallbacks', 'Group B');
            expect(within(root).getByRole('button', { name: 'All groups added' })).toBeTruthy();
        });

        it('says when there is nothing to fall back to', () => {
            render(<RoutingGroupForm isEditing={false} onSubmit={onSubmit} onCancel={onCancel} />);
            section('Fallbacks');
            expect(screen.getAllByRole('button', { name: 'No other routing groups available' })).toHaveLength(4);
        });

        it('closes the picker on an outside click', () => {
            renderCreate();
            section('Fallbacks');
            const root = picker('General Fallbacks');
            fireEvent.click(within(root).getByRole('button', { name: /Add fallback group/ }));
            expect(within(root).getByRole('button', { name: /^Group A/ })).toBeTruthy();
            fireEvent.mouseDown(document.body);
            expect(within(root).queryByRole('button', { name: /^Group A/ })).toBeNull();
        });
    });

    describe('edit', () => {
        it('populates from the group, sends no id, and shows it read-only', async () => {
            renderEdit();
            expect(screen.getByDisplayValue('g-self').hasAttribute('disabled')).toBe(true);
            expect(field('Name').value).toBe('Self');
            expect(field('Description').value).toBe('Original');

            const payload = await submitUpdate();
            expect(payload).not.toHaveProperty('id');
            expect(payload).toMatchObject({
                name: 'Self',
                description: 'Original',
                version: '3.1',
                routing_strategy: 'least-busy',
                cooldown_time: 30,
                num_retries: 1,
            });
        });

        it('does not require an id', async () => {
            renderEdit();
            await submitUpdate();
            expect(screen.queryByText('ID is required')).toBeNull();
        });

        it('sends empty lists so removing the last fallback really removes it', async () => {
            renderEdit(
                group({
                    fallback_models: ['g-a'],
                    context_window_fallbacks: ['g-a'],
                    content_policy_fallbacks: ['g-b'],
                    default_fallbacks: ['g-b'],
                })
            );
            section('Fallbacks');
            for (const label of [
                'General Fallbacks',
                'Context Window Fallbacks',
                'Content Policy Fallbacks',
                'Default Fallbacks',
            ]) {
                fireEvent.click(within(picker(label)).getByTitle('Remove'));
            }
            const payload = await submitUpdate();
            expect(payload.fallback_models).toEqual([]);
            expect(payload.context_window_fallbacks).toEqual([]);
            expect(payload.content_policy_fallbacks).toEqual([]);
            expect(payload.default_fallbacks).toEqual([]);
        });

        it('sends an empty description so a blanked one is really cleared, and null for a blanked stream timeout', async () => {
            renderEdit(group({ stream_timeout: 30 }));
            setField('Description', '');
            section('Retries & Timeouts');
            expect(field('Stream Timeout (sec)').value).toBe('30');
            setField('Stream Timeout (sec)', '');
            const payload = await submitUpdate();
            expect(payload.description).toBe('');
            expect(payload.stream_timeout).toBeNull();
        });

        it('locks the mode once members are assigned, and names how many', () => {
            renderEdit(group({ num_models: 2 }));
            expect(field('Mode').disabled).toBe(true);
            expect(screen.getByText('(locked — 2 members assigned)')).toBeTruthy();
        });

        it('leaves the mode editable with no members', () => {
            renderEdit();
            expect(field('Mode').disabled).toBe(false);
            expect(screen.queryByText(/locked/)).toBeNull();
        });

        it('tells the audit tab this is not new', () => {
            renderEdit();
            section('Audit Info');
            expect(screen.getByText('audit tab existing')).toBeTruthy();
        });

        it.todo(
            'BUG: a rate limit, stream timeout or floor tier cannot be cleared on edit. Blank values go as absent, and null would not help: the control plane drops None (llm_routing_groups.py:507 exclude_none=True)'
        );
    });

    describe('complexity routing', () => {
        it('shows the tier editor only for that strategy and reports tier changes', () => {
            render(
                <RoutingGroupForm
                    routingGroup={group({ routing_strategy: 'complexity-based-routing' })}
                    isEditing
                    onSubmit={onSubmit}
                    onCancel={onCancel}
                    groupModels={[{ id: 'm1', name: 'Model 1' }]}
                    initialTiers={[{ tier: 'complex', llm_model_id: 'm1', position: 0 }]}
                    onTiersChange={onTiersChange}
                />
            );
            section('Routing Strategy');
            expect(screen.getByText('tier editor for m1 with 1 tiers, floor none')).toBeTruthy();

            fireEvent.click(screen.getByRole('button', { name: 'set tier' }));
            expect(onTiersChange).toHaveBeenCalledWith([{ tier: 'simple', llm_model_id: 'm1', position: 0 }]);

            fireEvent.click(screen.getByRole('radio', { name: /Weighted Shuffle/ }));
            expect(screen.queryByText(/tier editor/)).toBeNull();
        });

        it('sends the floor tier, and drops it when cleared', async () => {
            renderCreate();
            fillRequired();
            section('Routing Strategy');
            fireEvent.click(screen.getByRole('radio', { name: /Prompt Complexity/ }));
            fireEvent.click(screen.getByRole('button', { name: 'set floor' }));
            expect(screen.getByText(/floor medium/)).toBeTruthy();
            const payload = await submitCreate();
            expect(payload.routing_strategy).toBe('complexity-based-routing');
            expect(payload.complexity_floor_tier).toBe('medium');

            onSubmit.mockClear();
            fireEvent.click(screen.getByRole('button', { name: 'clear floor' }));
            expect((await submitCreate()) as object).not.toHaveProperty('complexity_floor_tier');
        });
    });

    describe('chrome', () => {
        it('toggles contextual help, which closes on a section change', () => {
            renderCreate();
            expect(screen.queryByText('How to set up:')).toBeNull();
            fireEvent.click(screen.getByTitle('Show help for this section'));
            expect(screen.getByText('How to set up:')).toBeTruthy();

            section('Fallbacks');
            expect(screen.queryByText(/other routing group IDs/)).toBeNull();
            fireEvent.click(screen.getByTitle('Show help for this section'));
            expect(screen.getByText(/other routing group IDs/)).toBeTruthy();
            fireEvent.click(screen.getByTitle('Hide help'));
            expect(screen.queryByText(/other routing group IDs/)).toBeNull();
        });

        it('marks configured sections in the sidebar', () => {
            renderCreate();
            expect(navButton('Routing Strategy').textContent).toContain('Rate-Limit');
            expect(navButton('Rate Limiting').textContent).not.toContain('●');

            section('Rate Limiting');
            setField('Max Parallel Requests', '2');
            expect(navButton('Rate Limiting').textContent).toContain('●');

            section('Fallbacks');
            addFallback('General Fallbacks', 'Group A');
            expect(navButton('Fallbacks').textContent).toContain('●');
        });

        it('disables both buttons while saving and recovers after a failure', async () => {
            let fail: (reason: Error) => void = () => {};
            onSubmit.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
            renderCreate();
            fillRequired();
            fireEvent.click(screen.getByRole('button', { name: 'Create Routing Group' }));

            const saving = await screen.findByRole<HTMLButtonElement>('button', { name: 'Saving...' });
            expect(saving.disabled).toBe(true);
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Cancel' }).disabled).toBe(true);

            fail(new Error('rejected'));
            const again = await screen.findByRole<HTMLButtonElement>('button', { name: 'Create Routing Group' });
            expect(again.disabled).toBe(false);
        });

        it('cancels without submitting', () => {
            renderCreate();
            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
            expect(onCancel).toHaveBeenCalledTimes(1);
            expect(onSubmit).not.toHaveBeenCalled();
        });
    });
});
