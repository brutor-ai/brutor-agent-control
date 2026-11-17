/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Assurance notifications — wire types for
 * /v1/admin/notifications/*. Channel configs are write-only: the API
 * returns only `config_summary` (masked), never the URL or secret.
 */

export type NotificationChannelKind = 'slack_webhook' | 'webhook';
export type NotificationEventClass =
  | 'inbox_item_created'
  | 'inbox_item_reoccurred'
  | 'digest_weekly';

export interface NotificationChannel {
  id: string;
  tenant_id: string;
  name: string;
  kind: NotificationChannelKind;
  enabled: boolean;
  consecutive_failures: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  created_by: string | null;
  created_at: string | null;
  /** Masked destination, e.g. "https://hooks.slack.com/…" — never the URL. */
  config_summary: string;
}

export interface NotificationChannelConfigInput {
  webhook_url?: string;
  url?: string;
  secret?: string;
}

export interface NotificationSubscription {
  id: string;
  tenant_id: string;
  channel_id: string;
  event_class: NotificationEventClass;
  severity_floor: 'info' | 'warning' | 'critical';
  categories: string[] | null;
  ai_system_id: string | null;
  coalesce_seconds: number;
  enabled: boolean;
  created_by: string | null;
  created_at: string | null;
}

export interface NotificationDelivery {
  id: string;
  subscription_id: string;
  channel_id: string;
  inbox_item_id: string;
  fingerprint: string;
  status: 'sent' | 'failed' | 'coalesced' | 'skipped';
  attempts: number;
  last_error: string | null;
  sent_at: string | null;
  created_at: string | null;
}

export interface NotificationDeliveryListResponse {
  deliveries: NotificationDelivery[];
  total: number;
}

export interface NotificationTestSendResponse {
  ok: boolean;
  error: string | null;
}
