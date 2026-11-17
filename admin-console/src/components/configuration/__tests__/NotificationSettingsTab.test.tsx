/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Notifications.
 *
 * Pinned:
 *   * channels render masked summaries only — the component never sees or
 *     shows a full webhook URL after creation;
 *   * a failing channel wears its failure count (dead channels must not
 *     look healthy);
 *   * channel create sends kind-correct config; the signing secret is
 *     only included for generic webhooks;
 *   * subscription create wires channel/event/severity/categories;
 *   * test-send surfaces ok/error verbatim.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type {
  NotificationChannel,
  NotificationSubscription,
} from '../../../lib/notificationTypes';

const mockListChannels = vi.fn();
const mockListSubs = vi.fn();
const mockListDeliveries = vi.fn();
const mockCreateChannel = vi.fn();
const mockCreateSub = vi.fn();
const mockTestChannel = vi.fn();
const mockDeleteChannel = vi.fn();
const mockDeleteSub = vi.fn();

const gatewayClient = {
  listNotificationChannels: mockListChannels,
  listNotificationSubscriptions: mockListSubs,
  listNotificationDeliveries: mockListDeliveries,
  createNotificationChannel: mockCreateChannel,
  createNotificationSubscription: mockCreateSub,
  testNotificationChannel: mockTestChannel,
  deleteNotificationChannel: mockDeleteChannel,
  deleteNotificationSubscription: mockDeleteSub,
};

vi.mock('../../../hooks/useGateway', () => ({
  useGateway: () => ({ gatewayClient }),
}));

import NotificationSettingsTab from '../NotificationSettingsTab';

const channel = (over: Partial<NotificationChannel> = {}): NotificationChannel => ({
  id: 'nchan-1',
  tenant_id: 't1',
  name: 'Ops Slack',
  kind: 'slack_webhook',
  enabled: true,
  consecutive_failures: 0,
  last_success_at: null,
  last_failure_at: null,
  last_error: null,
  created_by: 'admin',
  created_at: '2026-08-31T10:00:00Z',
  config_summary: 'https://hooks.slack.com/…',
  ...over,
});

const subscription = (
  over: Partial<NotificationSubscription> = {}
): NotificationSubscription => ({
  id: 'nsub-1',
  tenant_id: 't1',
  channel_id: 'nchan-1',
  event_class: 'inbox_item_created',
  severity_floor: 'warning',
  categories: null,
  ai_system_id: null,
  coalesce_seconds: 900,
  enabled: true,
  created_by: 'admin',
  created_at: '2026-08-31T10:00:00Z',
  ...over,
});

describe('NotificationSettingsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListChannels.mockResolvedValue([channel()]);
    mockListSubs.mockResolvedValue([subscription()]);
    mockListDeliveries.mockResolvedValue({
      deliveries: [
        {
          id: 'ndel-1', subscription_id: 'nsub-1', channel_id: 'nchan-1',
          inbox_item_id: 'inbox-1', fingerprint: 'drift:sys-1:x',
          status: 'sent', attempts: 0, last_error: null,
          sent_at: '2026-08-31T10:05:00Z', created_at: '2026-08-31T10:05:00Z',
        },
      ],
      total: 1,
    });
  });

  it('renders channels with masked summaries and deliveries with status', async () => {
    render(<NotificationSettingsTab />);
    const row = await screen.findByTestId('channel-row');
    expect(row).toHaveTextContent('Ops Slack');
    expect(row).toHaveTextContent('https://hooks.slack.com/…');
    const delivery = screen.getByTestId('delivery-row');
    expect(delivery).toHaveTextContent('sent');
    expect(delivery).toHaveTextContent('drift:sys-1:x');
  });

  it('a failing channel wears its failure count', async () => {
    mockListChannels.mockResolvedValue([
      channel({ consecutive_failures: 5, last_error: 'HTTP 404' }),
    ]);
    render(<NotificationSettingsTab />);
    expect(await screen.findByTestId('channel-failing')).toHaveTextContent(
      '5 failures'
    );
  });

  it('creates a slack channel without a secret field', async () => {
    mockCreateChannel.mockResolvedValue(channel());
    render(<NotificationSettingsTab />);
    await screen.findByTestId('channel-row');
    fireEvent.click(screen.getByRole('button', { name: /add channel/i }));
    const form = await screen.findByTestId('channel-form');
    fireEvent.change(within(form).getByPlaceholderText('Ops Slack'), {
      target: { value: 'Alerts' },
    });
    fireEvent.change(
      within(form).getByPlaceholderText('https://hooks.slack.com/services/…'),
      { target: { value: 'https://hooks.slack.com/services/T/B/C' } }
    );
    fireEvent.click(within(form).getByRole('button', { name: /create channel/i }));
    await waitFor(() =>
      expect(mockCreateChannel).toHaveBeenCalledWith({
        name: 'Alerts',
        kind: 'slack_webhook',
        config: { webhook_url: 'https://hooks.slack.com/services/T/B/C' },
      })
    );
  });

  it('creates a generic webhook with the signing secret', async () => {
    mockCreateChannel.mockResolvedValue(channel({ kind: 'webhook' }));
    render(<NotificationSettingsTab />);
    await screen.findByTestId('channel-row');
    fireEvent.click(screen.getByRole('button', { name: /add channel/i }));
    const form = await screen.findByTestId('channel-form');
    fireEvent.change(within(form).getByLabelText('Channel kind'), {
      target: { value: 'webhook' },
    });
    fireEvent.change(within(form).getByPlaceholderText('Ops Slack'), {
      target: { value: 'SIEM' },
    });
    fireEvent.change(
      within(form).getByPlaceholderText('https://example.com/hooks/brutor'),
      { target: { value: 'https://siem.corp/hook' } }
    );
    const secretInput = form.querySelector('input[type="password"]');
    fireEvent.change(secretInput!, { target: { value: 'topsecret' } });
    fireEvent.click(within(form).getByRole('button', { name: /create channel/i }));
    await waitFor(() =>
      expect(mockCreateChannel).toHaveBeenCalledWith({
        name: 'SIEM',
        kind: 'webhook',
        config: { url: 'https://siem.corp/hook', secret: 'topsecret' },
      })
    );
  });

  it('creates a subscription with categories and floor', async () => {
    mockCreateSub.mockResolvedValue(subscription());
    render(<NotificationSettingsTab />);
    await screen.findByTestId('channel-row');
    fireEvent.click(screen.getByRole('button', { name: /add subscription/i }));
    const form = await screen.findByTestId('subscription-form');
    fireEvent.change(within(form).getByLabelText('Severity floor'), {
      target: { value: 'critical' },
    });
    fireEvent.click(within(form).getByRole('button', { name: /^drift$/i }));
    fireEvent.click(
      within(form).getByRole('button', { name: /create subscription/i })
    );
    await waitFor(() =>
      expect(mockCreateSub).toHaveBeenCalledWith({
        channel_id: 'nchan-1',
        event_class: 'inbox_item_created',
        severity_floor: 'critical',
        categories: ['drift'],
      })
    );
  });

  it('test-send surfaces the outcome verbatim', async () => {
    mockTestChannel.mockResolvedValue({ ok: false, error: 'HTTP 404: gone' });
    render(<NotificationSettingsTab />);
    const row = await screen.findByTestId('channel-row');
    fireEvent.click(within(row).getByTitle('Send test message'));
    expect(await screen.findByTestId('test-result')).toHaveTextContent(
      'HTTP 404: gone'
    );
  });
});
