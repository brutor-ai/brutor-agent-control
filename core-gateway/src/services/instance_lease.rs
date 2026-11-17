// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The instance lease
//!
//! The Community Edition runs as one gateway instance. At start the gateway
//! takes a lease in Redis; a second instance that finds a live lease held by
//! another does not start and says why, naming the Enterprise Edition. A
//! composition that makes several instances safe (an extension whose
//! `allows_multiple_instances()` is true) takes no lease: the builder skips
//! this step.
//!
//! This is a boundary of the edition, not a control. Nothing that enforces a
//! limit or a policy depends on it: two instances started by someone who
//! removed the check still admit every limit atomically on the shared
//! counters. That is why a lease lost while serving does not stop the
//! gateway (below).
//!
//! ## The design
//!
//! - **Key** [`LEASE_KEY`], one lease for the core gateway. **Value**: an
//!   instance id minted at process start ([`mint_instance_id`]).
//! - **Take**: `SET key id NX PX 15000` ([`LEASE_TTL`]).
//! - **Renew** every 5 s ([`LEASE_RENEW_EVERY`]): the TTL is extended only
//!   while the stored value is still this instance's (a Lua compare-and-set).
//! - **At start**, when the key is held by another id: retry each second
//!   ([`START_RETRY`]) for up to 20 s ([`START_WAIT`], the TTL plus a
//!   margin). That is what makes an ordinary restart work: a restarted
//!   process is a new id, and the previous process's lease lapses within the
//!   TTL of its last renewal. Still held after the wait: another instance is
//!   alive, and the start is refused with [`AnotherInstance`].
//! - **While running**, when a renewal finds the key held by another id
//!   (this instance was paused long enough to lose the lease and a second
//!   one started): **keep serving**, log at error level and raise a system
//!   alert. Stopping a serving gateway would turn a licence boundary into an
//!   outage.
//! - **Redis unreachable at start**: the gateway already refuses to start
//!   without Redis; an error while taking the lease is a refusal too.
//!
//! One addition the design does not state: a graceful shutdown gives the
//! lease back ([`InstanceLease::release`], compare-and-delete), so that the
//! next start need not wait out the TTL. A process that is killed releases
//! nothing and the wait above covers it.
//!
//! The key and the timings have one definition, here
//! (`tests/clustering_guard.rs`). A test passes its own [`LeaseSettings`]
//! (a key of its own, short timings) through
//! `GatewayBuilder::instance_lease`.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use redis::aio::ConnectionManager;
use sqlx::PgPool;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use crate::services::system_alerts::{self, AlertCategory, Severity, SystemAlertInput};

/// The lease of the core gateway. The control plane's is its own key; the
/// two services do not compete with each other.
pub const LEASE_KEY: &str = "brutor:instance:core";
/// How long a lease lives without a renewal.
pub const LEASE_TTL: Duration = Duration::from_millis(15_000);
/// How often the holder renews it.
pub const LEASE_RENEW_EVERY: Duration = Duration::from_secs(5);
/// How long a start waits for a lease held by another instance to lapse:
/// the TTL plus a margin.
pub const START_WAIT: Duration = Duration::from_secs(20);
/// How often a waiting start tries again.
pub const START_RETRY: Duration = Duration::from_secs(1);

/// The `source` of the system alert a lost lease raises.
pub const ALERT_SOURCE: &str = "instance_lease";

/// The key and the timings of a lease.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeaseSettings {
    pub key: String,
    pub ttl: Duration,
    pub renew_every: Duration,
    pub start_wait: Duration,
    pub start_retry: Duration,
}

impl LeaseSettings {
    /// The lease of a Community gateway: the one definition.
    pub fn community() -> Self {
        Self {
            key: LEASE_KEY.to_string(),
            ttl: LEASE_TTL,
            renew_every: LEASE_RENEW_EVERY,
            start_wait: START_WAIT,
            start_retry: START_RETRY,
        }
    }
}

impl Default for LeaseSettings {
    fn default() -> Self {
        Self::community()
    }
}

/// An id for this process: the host's name, so that an operator can tell
/// which machine or container holds the lease, and a random part, so that
/// two processes on one host (and a process and its restart) differ.
pub fn mint_instance_id() -> String {
    let host = hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "gateway".to_string());
    let random = uuid::Uuid::new_v4().simple().to_string();
    format!("{host}/{}", &random[..12])
}

/// The refusal of a start: a live lease is held by another instance.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnotherInstance {
    /// The instance id that holds the lease.
    pub holder: String,
    /// Seconds since that instance last renewed it.
    pub last_seen_secs: u64,
}

impl std::fmt::Display for AnotherInstance {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "Another Core Gateway instance is already running against this Redis and database \
             (instance {}, last seen {} s ago). The Community Edition runs as a single instance. \
             Running several instances for capacity or availability needs the Enterprise Edition. \
             If the other instance has just been stopped, start this one again in a few seconds.",
            self.holder, self.last_seen_secs
        )
    }
}

impl std::error::Error for AnotherInstance {}

/// Why a lease was not taken.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TakeError {
    AnotherInstance(AnotherInstance),
    /// Redis did not answer the lease commands. The reason names no address.
    Redis(String),
}

impl std::fmt::Display for TakeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::AnotherInstance(other) => other.fmt(f),
            Self::Redis(reason) => write!(
                f,
                "The instance lease could not be taken: Redis did not answer ({reason}). Redis \
                 is required; refusing to start."
            ),
        }
    }
}

impl std::error::Error for TakeError {}

/// What one renewal found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Renewal {
    /// The lease was this instance's and its TTL was extended.
    Held,
    /// The lease had lapsed and nobody held it: taken again.
    Retaken,
    /// Another instance holds it. Nothing was written.
    Lost { holder: String },
    /// Redis did not answer. The reason names no address.
    Unreachable(String),
}

/// Extend the TTL while the value is still ours; take the key again when it
/// lapsed and nobody holds it; otherwise say who does. `1` = held, `2` =
/// retaken, a string = the other holder.
const RENEW_LUA: &str = r#"
local holder = redis.call('GET', KEYS[1])
if holder == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
elseif not holder then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 2
else
  return holder
end
"#;

/// Delete the key only while the value is still ours.
const RELEASE_LUA: &str = r#"
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"#;

/// A lease this process holds (or held: see [`InstanceLease::is_lost`]).
pub struct InstanceLease {
    redis: ConnectionManager,
    settings: LeaseSettings,
    id: String,
    lost: AtomicBool,
}

impl InstanceLease {
    /// Take the lease for a new instance id, waiting out a lease another
    /// instance holds for up to `settings.start_wait`.
    pub async fn take(
        redis: ConnectionManager,
        settings: LeaseSettings,
    ) -> Result<Self, TakeError> {
        Self::take_as(redis, settings, mint_instance_id()).await
    }

    /// [`Self::take`] with the instance id given.
    pub async fn take_as(
        mut redis: ConnectionManager,
        settings: LeaseSettings,
        id: String,
    ) -> Result<Self, TakeError> {
        let ttl_ms = settings.ttl.as_millis() as u64;
        let deadline = tokio::time::Instant::now() + settings.start_wait;
        let mut waited = false;
        loop {
            let taken: Option<String> = redis::cmd("SET")
                .arg(&settings.key)
                .arg(&id)
                .arg("NX")
                .arg("PX")
                .arg(ttl_ms)
                .query_async(&mut redis)
                .await
                .map_err(|e| TakeError::Redis(e.category().to_string()))?;
            if taken.is_some() {
                break;
            }
            // Held. By whom, and how long ago was it renewed?
            let (holder, remaining_ms): (Option<String>, i64) = redis::pipe()
                .cmd("GET")
                .arg(&settings.key)
                .cmd("PTTL")
                .arg(&settings.key)
                .query_async(&mut redis)
                .await
                .map_err(|e| TakeError::Redis(e.category().to_string()))?;
            match holder {
                // Ours already: the SET was applied and its answer lost.
                Some(holder) if holder == id => break,
                // It lapsed between the two commands: take it now.
                None => continue,
                Some(holder) => {
                    if tokio::time::Instant::now() >= deadline {
                        let since_renewal = ttl_ms.saturating_sub(remaining_ms.max(0) as u64);
                        return Err(TakeError::AnotherInstance(AnotherInstance {
                            holder,
                            last_seen_secs: since_renewal / 1000,
                        }));
                    }
                    if !waited {
                        waited = true;
                        tracing::info!(
                            holder = %holder,
                            wait_secs = settings.start_wait.as_secs(),
                            "The instance lease is held by another instance; waiting for it to \
                             lapse (an ordinary restart lapses within the lease's lifetime)"
                        );
                    }
                    tokio::time::sleep(settings.start_retry).await;
                }
            }
        }
        Ok(Self {
            redis,
            settings,
            id,
            lost: AtomicBool::new(false),
        })
    }

    /// This instance's id: the value of the lease.
    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn settings(&self) -> &LeaseSettings {
        &self.settings
    }

    /// Whether the last renewal found the lease held by another instance.
    pub fn is_lost(&self) -> bool {
        self.lost.load(Ordering::Relaxed)
    }

    /// One renewal.
    pub async fn renew(&self) -> Renewal {
        let mut redis = self.redis.clone();
        let answer: Result<redis::Value, redis::RedisError> = redis::Script::new(RENEW_LUA)
            .key(&self.settings.key)
            .arg(&self.id)
            .arg(self.settings.ttl.as_millis() as u64)
            .invoke_async(&mut redis)
            .await;
        let renewal = match answer {
            Ok(redis::Value::Int(1)) => Renewal::Held,
            Ok(redis::Value::Int(2)) => Renewal::Retaken,
            Ok(other) => match redis::from_redis_value::<String>(&other) {
                Ok(holder) => Renewal::Lost { holder },
                Err(_) => Renewal::Unreachable("an unexpected answer".to_string()),
            },
            Err(e) => Renewal::Unreachable(e.category().to_string()),
        };
        match &renewal {
            Renewal::Held | Renewal::Retaken => self.lost.store(false, Ordering::Relaxed),
            Renewal::Lost { .. } => self.lost.store(true, Ordering::Relaxed),
            // Nothing is known: the last answer stands.
            Renewal::Unreachable(_) => {}
        }
        renewal
    }

    /// Give the lease back, if it is still this instance's. For a graceful
    /// shutdown; best effort.
    pub async fn release(&self) {
        let mut redis = self.redis.clone();
        let released: Result<i64, redis::RedisError> = redis::Script::new(RELEASE_LUA)
            .key(&self.settings.key)
            .arg(&self.id)
            .invoke_async(&mut redis)
            .await;
        match released {
            Ok(1) => tracing::info!(instance = %self.id, "Instance lease released"),
            Ok(_) => {}
            Err(e) => tracing::warn!(
                reason = %e.category(),
                "Instance lease not released (it lapses by itself)"
            ),
        }
    }

    /// Renew until `shutdown`: at once, then every `renew_every`. A lease
    /// found held by another instance is logged and alerted once per loss
    /// and the gateway keeps serving; nothing here stops anything.
    pub fn spawn_renewal(
        self: Arc<Self>,
        db: PgPool,
        shutdown: CancellationToken,
    ) -> JoinHandle<()> {
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(self.settings.renew_every);
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            let mut reported_lost = false;
            loop {
                tokio::select! {
                    _ = shutdown.cancelled() => return,
                    _ = interval.tick() => {}
                }
                match self.renew().await {
                    Renewal::Held => {
                        if reported_lost {
                            tracing::info!(instance = %self.id, "Instance lease held again");
                        }
                        reported_lost = false;
                    }
                    Renewal::Retaken => {
                        tracing::warn!(
                            instance = %self.id,
                            "The instance lease had lapsed (this instance was paused, or Redis \
                             was out of reach, for longer than the lease lives) and nobody held \
                             it: taken again"
                        );
                        reported_lost = false;
                    }
                    Renewal::Lost { holder } => {
                        if !reported_lost {
                            reported_lost = true;
                            report_lost(&db, &self.id, &holder).await;
                        }
                    }
                    Renewal::Unreachable(reason) => tracing::warn!(
                        reason = %reason,
                        "Instance lease renewal: Redis did not answer; trying again"
                    ),
                }
            }
        })
    }
}

/// A second instance holds the lease while this one serves: say so, at
/// error level and as a system alert. The gateway keeps serving.
async fn report_lost(db: &PgPool, id: &str, holder: &str) {
    let message = format!(
        "This gateway instance ({id}) lost its instance lease to another instance ({holder}) and \
         keeps serving. The Community Edition runs as a single instance: stop one of the two. \
         Running several instances for capacity or availability needs the Enterprise Edition."
    );
    tracing::error!(instance = %id, holder = %holder, "{message}");
    let alert = SystemAlertInput {
        tenant_id: None,
        severity: Severity::High,
        category: AlertCategory::InfrastructureThreshold,
        source: ALERT_SOURCE.to_string(),
        resource_type: Some("system".to_string()),
        resource_id: None,
        resource_name: None,
        error_code: Some("second_instance".to_string()),
        title: "A second gateway instance is running".to_string(),
        message,
        context: Some(serde_json::json!({ "instance": id, "holder": holder })),
    };
    if let Err(e) = system_alerts::raise(db, alert).await {
        tracing::error!(error = %e, "The second-instance alert could not be raised");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The refusal is the message the design gives, with the Enterprise
    /// Edition named.
    #[test]
    fn the_refusal_names_the_other_instance_and_the_enterprise_edition() {
        let refusal = AnotherInstance {
            holder: "host-a/0123456789ab".to_string(),
            last_seen_secs: 3,
        }
        .to_string();
        assert_eq!(
            refusal,
            "Another Core Gateway instance is already running against this Redis and database \
             (instance host-a/0123456789ab, last seen 3 s ago). The Community Edition runs as a \
             single instance. Running several instances for capacity or availability needs the \
             Enterprise Edition. If the other instance has just been stopped, start this one \
             again in a few seconds."
        );
        assert_eq!(
            TakeError::AnotherInstance(AnotherInstance {
                holder: "h".into(),
                last_seen_secs: 0
            })
            .to_string(),
            AnotherInstance {
                holder: "h".into(),
                last_seen_secs: 0
            }
            .to_string()
        );
    }

    /// The lease of a Community gateway is the decided one.
    #[test]
    fn the_community_lease_is_the_decided_key_and_timings() {
        let settings = LeaseSettings::default();
        assert_eq!(settings, LeaseSettings::community());
        assert_eq!(settings.key, "brutor:instance:core");
        assert_eq!(settings.ttl, Duration::from_secs(15));
        assert_eq!(settings.renew_every, Duration::from_secs(5));
        assert_eq!(settings.start_wait, Duration::from_secs(20));
        assert_eq!(settings.start_retry, Duration::from_secs(1));
        // A renewal can be missed twice before the lease lapses, and a
        // start outwaits a lease that was renewed just before its holder
        // stopped.
        assert!(settings.renew_every * 2 < settings.ttl);
        assert!(settings.start_wait > settings.ttl);
    }

    #[test]
    fn two_minted_ids_differ() {
        let (a, b) = (mint_instance_id(), mint_instance_id());
        assert_ne!(a, b);
        assert!(a.contains('/') && !a.starts_with('/'), "{a}");
    }
}
