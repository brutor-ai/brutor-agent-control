/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Notifications.
 *
 * Channels (Slack incoming webhook / signed generic webhook) +
 * subscriptions routing assurance-inbox events to them + the delivery
 * audit trail. Channel URLs and secrets are write-only: the API returns
 * masked summaries, and editing without re-entering a URL keeps the
 * stored (encrypted) one.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  Bell,
  CheckCircle,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  XCircle,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { INBOX_CATEGORIES } from '../../lib/assuranceInboxTypes';
import {
  NotificationChannel,
  NotificationDelivery,
  NotificationSubscription,
} from '../../lib/notificationTypes';

const EVENT_CLASS_LABELS: Record<string, string> = {
  inbox_item_created: 'New inbox item',
  inbox_item_reoccurred: 'Item re-occurred',
  digest_weekly: 'Weekly digest',
};

const STATUS_BADGE: Record<NotificationDelivery['status'], string> = {
  sent: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  coalesced: 'bg-surface-raised text-secondary',
  skipped: 'bg-surface-raised text-secondary',
};

const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '—');

export const NotificationSettingsTab: React.FC = () => {
  const { gatewayClient } = useGateway();

  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [subscriptions, setSubscriptions] = useState<NotificationSubscription[]>([]);
  const [deliveries, setDeliveries] = useState<NotificationDelivery[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Channel form
  const [showChannelForm, setShowChannelForm] = useState(false);
  const [chName, setChName] = useState('');
  const [chKind, setChKind] = useState<'slack_webhook' | 'webhook'>('slack_webhook');
  const [chUrl, setChUrl] = useState('');
  const [chSecret, setChSecret] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Subscription form
  const [showSubForm, setShowSubForm] = useState(false);
  const [subChannel, setSubChannel] = useState('');
  const [subEventClass, setSubEventClass] = useState<'inbox_item_created' | 'inbox_item_reoccurred'>('inbox_item_created');
  const [subFloor, setSubFloor] = useState<'info' | 'warning' | 'critical'>('warning');
  const [subCategories, setSubCategories] = useState<string[]>([]);

  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; error?: string | null }>>({});
  const [testing, setTesting] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!gatewayClient) return;
    setLoading(true);
    setError(null);
    try {
      const [ch, subs, dels] = await Promise.all([
        gatewayClient.listNotificationChannels(),
        gatewayClient.listNotificationSubscriptions(),
        gatewayClient.listNotificationDeliveries({ limit: 25 }),
      ]);
      setChannels(ch);
      setSubscriptions(subs);
      setDeliveries(dels.deliveries);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load notification settings');
    } finally {
      setLoading(false);
    }
  }, [gatewayClient]);

  useEffect(() => {
    void load();
  }, [load]);

  const createChannel = useCallback(async () => {
    if (!gatewayClient) return;
    setSaving(true);
    setFormError(null);
    try {
      await gatewayClient.createNotificationChannel({
        name: chName.trim(),
        kind: chKind,
        config:
          chKind === 'slack_webhook'
            ? { webhook_url: chUrl.trim() }
            : { url: chUrl.trim(), secret: chSecret.trim() || undefined },
      });
      setShowChannelForm(false);
      setChName('');
      setChUrl('');
      setChSecret('');
      await load();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : 'Failed to create channel');
    } finally {
      setSaving(false);
    }
  }, [gatewayClient, chName, chKind, chUrl, chSecret, load]);

  const createSubscription = useCallback(async () => {
    if (!gatewayClient) return;
    setSaving(true);
    setFormError(null);
    try {
      await gatewayClient.createNotificationSubscription({
        channel_id: subChannel,
        event_class: subEventClass,
        severity_floor: subFloor,
        categories: subCategories.length > 0 ? subCategories : null,
      });
      setShowSubForm(false);
      setSubCategories([]);
      await load();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : 'Failed to create subscription');
    } finally {
      setSaving(false);
    }
  }, [gatewayClient, subChannel, subEventClass, subFloor, subCategories, load]);

  const testChannel = useCallback(
    async (id: string) => {
      if (!gatewayClient) return;
      setTesting(id);
      try {
        const result = await gatewayClient.testNotificationChannel(id);
        setTestResults((prev) => ({ ...prev, [id]: result }));
      } catch (e) {
        setTestResults((prev) => ({
          ...prev,
          [id]: { ok: false, error: e instanceof Error ? e.message : 'failed' },
        }));
      } finally {
        setTesting(null);
      }
    },
    [gatewayClient]
  );

  const removeChannel = useCallback(
    async (id: string) => {
      if (!gatewayClient) return;
      await gatewayClient.deleteNotificationChannel(id);
      await load();
    },
    [gatewayClient, load]
  );

  const removeSubscription = useCallback(
    async (id: string) => {
      if (!gatewayClient) return;
      await gatewayClient.deleteNotificationSubscription(id);
      await load();
    },
    [gatewayClient, load]
  );

  const channelName = (id: string) =>
    channels.find((c) => c.id === id)?.name ?? id;

  return (
    <div className="space-y-6" data-testid="notification-settings">
      <div>
        <h3 className="text-lg font-semibold text-default flex items-center space-x-2">
          <Bell className="w-5 h-5" />
          <span>Assurance Notifications</span>
        </h3>
        <p className="text-sm text-secondary mt-1">
          Real-time alerts from the Assurance Inbox to Slack or any webhook.
          Alerts are a projection of the inbox — recurring findings coalesce
          into one message per window, and every delivery is recorded below.
        </p>
      </div>

      {error && (
        <div className="p-3 bg-red-50 text-red-700 rounded-md text-sm">{error}</div>
      )}

      {/* ── Channels ── */}
      <div className="bg-surface rounded-lg border">
        <div className="px-4 py-3 border-b flex items-center justify-between">
          <h4 className="font-medium text-default">Channels</h4>
          <button
            onClick={() => setShowChannelForm(true)}
            className="flex items-center space-x-1 px-3 py-1.5 bg-primary-500 text-white rounded-md text-sm hover:bg-primary-600"
          >
            <Plus className="w-4 h-4" />
            <span>Add channel</span>
          </button>
        </div>
        {loading && channels.length === 0 ? (
          <div className="p-6 text-center text-sm text-muted">Loading…</div>
        ) : channels.length === 0 ? (
          <div className="p-6 text-center text-sm text-secondary" data-testid="no-channels">
            No channels yet — add a Slack webhook to get assurance alerts where
            your operators live.
          </div>
        ) : (
          <div className="divide-y divide-default">
            {channels.map((ch) => (
              <div key={ch.id} className="px-4 py-3 flex items-center gap-3" data-testid="channel-row">
                <div className="flex-1 min-w-0 min-h-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm text-default">{ch.name}</span>
                    <span className="px-1.5 py-0.5 rounded text-xs bg-surface-raised text-secondary">
                      {ch.kind === 'slack_webhook' ? 'Slack' : 'Webhook'}
                    </span>
                    {!ch.enabled && (
                      <span className="px-1.5 py-0.5 rounded text-xs bg-surface-raised text-muted">
                        disabled
                      </span>
                    )}
                    {ch.consecutive_failures > 0 && (
                      <span
                        className="px-1.5 py-0.5 rounded text-xs bg-red-100 text-red-800 flex items-center gap-1"
                        title={ch.last_error ?? undefined}
                        data-testid="channel-failing"
                      >
                        <AlertTriangle className="w-3 h-3" />
                        {ch.consecutive_failures} failures
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted truncate">{ch.config_summary}</p>
                </div>
                {testResults[ch.id] && (
                  <span
                    className={`text-xs flex items-center gap-1 ${
                      testResults[ch.id].ok ? 'text-green-700' : 'text-red-700'
                    }`}
                    data-testid="test-result"
                  >
                    {testResults[ch.id].ok ? (
                      <CheckCircle className="w-3.5 h-3.5" />
                    ) : (
                      <XCircle className="w-3.5 h-3.5" />
                    )}
                    {testResults[ch.id].ok
                      ? 'Test delivered'
                      : (testResults[ch.id].error ?? 'Test failed')}
                  </span>
                )}
                <button
                  onClick={() => void testChannel(ch.id)}
                  disabled={testing === ch.id}
                  className="p-1.5 text-secondary hover:text-primary-600 rounded"
                  title="Send test message"
                >
                  {testing === ch.id ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Send className="w-4 h-4" />
                  )}
                </button>
                <button
                  onClick={() => void removeChannel(ch.id)}
                  className="p-1.5 text-secondary hover:text-red-600 rounded"
                  title="Delete channel"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Subscriptions ── */}
      <div className="bg-surface rounded-lg border">
        <div className="px-4 py-3 border-b flex items-center justify-between">
          <h4 className="font-medium text-default">Alert subscriptions</h4>
          <button
            onClick={() => {
              setSubChannel(channels[0]?.id ?? '');
              setShowSubForm(true);
            }}
            disabled={channels.length === 0}
            className="flex items-center space-x-1 px-3 py-1.5 bg-primary-500 text-white rounded-md text-sm hover:bg-primary-600 disabled:opacity-50"
          >
            <Plus className="w-4 h-4" />
            <span>Add subscription</span>
          </button>
        </div>
        {subscriptions.length === 0 ? (
          <div className="p-6 text-center text-sm text-secondary">
            No subscriptions — nothing is being alerted yet.
          </div>
        ) : (
          <div className="divide-y divide-default">
            {subscriptions.map((sub) => (
              <div key={sub.id} className="px-4 py-3 flex items-center gap-3" data-testid="subscription-row">
                <div className="flex-1 min-w-0 text-sm min-h-0">
                  <span className="font-medium text-default">
                    {EVENT_CLASS_LABELS[sub.event_class] ?? sub.event_class}
                  </span>
                  <span className="text-secondary">
                    {' '}→ {channelName(sub.channel_id)} · ≥{sub.severity_floor}
                    {sub.categories && sub.categories.length > 0 && (
                      <> · {sub.categories.join(', ')}</>
                    )}
                    {sub.ai_system_id && <> · system {sub.ai_system_id}</>}
                    {' '}· coalesce {Math.round(sub.coalesce_seconds / 60)}m
                  </span>
                </div>
                <button
                  onClick={() => void removeSubscription(sub.id)}
                  className="p-1.5 text-secondary hover:text-red-600 rounded"
                  title="Delete subscription"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Recent deliveries ── */}
      <div className="bg-surface rounded-lg border">
        <div className="px-4 py-3 border-b flex items-center justify-between">
          <h4 className="font-medium text-default">Recent deliveries</h4>
          <button
            onClick={() => void load()}
            className="p-1.5 text-muted hover:text-default rounded"
            title="Refresh"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
        {deliveries.length === 0 ? (
          <div className="p-6 text-center text-sm text-secondary">
            No deliveries yet.
          </div>
        ) : (
          <div className="divide-y divide-default">
            {deliveries.map((d) => (
              <div key={d.id} className="px-4 py-2 flex items-center gap-3 text-sm" data-testid="delivery-row">
                <span className={`px-1.5 py-0.5 rounded text-xs ${STATUS_BADGE[d.status]}`}>
                  {d.status}
                </span>
                <span className="text-secondary truncate flex-1 min-h-0 min-w-0">{d.fingerprint}</span>
                <span className="text-xs text-muted">{channelName(d.channel_id)}</span>
                {d.last_error && (
                  <span className="text-xs text-red-700 truncate max-w-[16rem]" title={d.last_error}>
                    {d.last_error}
                  </span>
                )}
                <span className="text-xs text-muted hidden md:inline">
                  {fmt(d.sent_at ?? d.created_at)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Channel form modal ── */}
      {showChannelForm && (
        <div className="modal-overlay">
          <div className="modal-content max-w-lg" data-testid="channel-form">
            <div className="px-6 py-4 border-b">
              <h3 className="font-semibold text-default">Add notification channel</h3>
            </div>
            <div className="px-6 py-4 space-y-3">
              {formError && (
                <div className="p-2 text-sm text-red-700 bg-red-50 rounded">{formError}</div>
              )}
              <label className="block text-sm font-medium text-default">Name</label>
              <input
                value={chName}
                onChange={(e) => setChName(e.target.value)}
                placeholder="Ops Slack"
                className="w-full px-3 py-2 border border-strong rounded-md text-sm"
              />
              <label className="block text-sm font-medium text-default">Kind</label>
              <select
                aria-label="Channel kind"
                value={chKind}
                onChange={(e) => setChKind(e.target.value as 'slack_webhook' | 'webhook')}
                className="w-full px-3 py-2 border border-strong rounded-md text-sm"
              >
                <option value="slack_webhook">Slack incoming webhook</option>
                <option value="webhook">Generic webhook (HMAC-signed)</option>
              </select>
              <label className="block text-sm font-medium text-default">
                {chKind === 'slack_webhook' ? 'Webhook URL' : 'Destination URL'}
              </label>
              <input
                value={chUrl}
                onChange={(e) => setChUrl(e.target.value)}
                placeholder={
                  chKind === 'slack_webhook'
                    ? 'https://hooks.slack.com/services/…'
                    : 'https://example.com/hooks/brutor'
                }
                className="w-full px-3 py-2 border border-strong rounded-md text-sm font-mono"
              />
              {chKind === 'webhook' && (
                <>
                  <label className="block text-sm font-medium text-default">
                    Signing secret{' '}
                    <span className="text-muted font-normal">
                      (optional — sent as X-Brutor-Signature: sha256=…)
                    </span>
                  </label>
                  <input
                    value={chSecret}
                    onChange={(e) => setChSecret(e.target.value)}
                    type="password"
                    className="w-full px-3 py-2 border border-strong rounded-md text-sm"
                  />
                </>
              )}
              <p className="text-xs text-muted">
                Encrypted at rest under the installation&apos;s encryption key and never shown
                again — the list displays a masked summary only.
              </p>
            </div>
            <div className="px-6 py-3 border-t flex justify-end gap-2">
              <button
                onClick={() => setShowChannelForm(false)}
                className="px-3 py-1.5 border border-strong text-secondary rounded-md text-sm"
              >
                Cancel
              </button>
              <button
                onClick={() => void createChannel()}
                disabled={saving || !chName.trim() || !chUrl.trim()}
                className="px-3 py-1.5 bg-primary-500 text-white rounded-md text-sm disabled:opacity-50"
              >
                Create channel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Subscription form modal ── */}
      {showSubForm && (
        <div className="modal-overlay">
          <div className="modal-content max-w-lg" data-testid="subscription-form">
            <div className="px-6 py-4 border-b">
              <h3 className="font-semibold text-default">Add alert subscription</h3>
            </div>
            <div className="px-6 py-4 space-y-3">
              {formError && (
                <div className="p-2 text-sm text-red-700 bg-red-50 rounded">{formError}</div>
              )}
              <label className="block text-sm font-medium text-default">Channel</label>
              <select
                aria-label="Subscription channel"
                value={subChannel}
                onChange={(e) => setSubChannel(e.target.value)}
                className="w-full px-3 py-2 border border-strong rounded-md text-sm"
              >
                {channels.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <label className="block text-sm font-medium text-default">Event</label>
              <select
                aria-label="Event class"
                value={subEventClass}
                onChange={(e) =>
                  setSubEventClass(
                    e.target.value as 'inbox_item_created' | 'inbox_item_reoccurred'
                  )
                }
                className="w-full px-3 py-2 border border-strong rounded-md text-sm"
              >
                <option value="inbox_item_created">New inbox item</option>
                <option value="inbox_item_reoccurred">Item re-occurred</option>
              </select>
              <label className="block text-sm font-medium text-default">
                Minimum severity
              </label>
              <select
                aria-label="Severity floor"
                value={subFloor}
                onChange={(e) =>
                  setSubFloor(e.target.value as 'info' | 'warning' | 'critical')
                }
                className="w-full px-3 py-2 border border-strong rounded-md text-sm"
              >
                <option value="info">info (everything)</option>
                <option value="warning">warning</option>
                <option value="critical">critical only</option>
              </select>
              <label className="block text-sm font-medium text-default">
                Categories{' '}
                <span className="text-muted font-normal">(none selected = all)</span>
              </label>
              <div className="flex flex-wrap gap-1.5">
                {INBOX_CATEGORIES.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() =>
                      setSubCategories((prev) =>
                        prev.includes(c)
                          ? prev.filter((x) => x !== c)
                          : [...prev, c]
                      )
                    }
                    className={`px-2 py-1 rounded text-xs border ${
                      subCategories.includes(c)
                        ? 'bg-primary-50 border-primary-300 text-primary-700'
                        : 'bg-surface border-strong text-secondary'
                    }`}
                  >
                    {c}
                  </button>
                ))}
              </div>
            </div>
            <div className="px-6 py-3 border-t flex justify-end gap-2">
              <button
                onClick={() => setShowSubForm(false)}
                className="px-3 py-1.5 border border-strong text-secondary rounded-md text-sm"
              >
                Cancel
              </button>
              <button
                onClick={() => void createSubscription()}
                disabled={saving || !subChannel}
                className="px-3 py-1.5 bg-primary-500 text-white rounded-md text-sm disabled:opacity-50"
              >
                Create subscription
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default NotificationSettingsTab;
