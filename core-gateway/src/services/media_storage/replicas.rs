// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Whether a filesystem media store is used by more than one gateway
//! instance.
//!
//! The filesystem store is for ONE instance: each instance sees only the
//! files on its own volume, and the size limit is counted in the process.
//! A Community gateway is one instance by construction (its instance lease).
//! A composition that allows several (the Enterprise Edition's clustering)
//! takes no lease, so each instance that uses the filesystem store says
//! where it is in Redis every [`BEAT`] and counts the instances that said so
//! within [`STALE`]: more than one is logged at error level, each time the
//! count changes, and never silently ignored. Starting such a composition on
//! the filesystem store also logs a warning (the builder), before a second
//! instance exists.

use std::time::Duration;

use redis::aio::ConnectionManager;

/// The sorted set of the instances that use a filesystem media store:
/// member the instance id, score the time of its last beat (ms).
pub const PRESENCE_KEY: &str = "brutor:media:filesystem:instances";
/// How often an instance says it is there.
pub const BEAT: Duration = Duration::from_secs(30);
/// How long an instance counts after its last beat.
pub const STALE: Duration = Duration::from_secs(90);

/// The presence of the instances on one key.
#[derive(Clone, Debug)]
pub struct Presence {
    key: String,
    stale: Duration,
}

impl Default for Presence {
    fn default() -> Self {
        Self::new(PRESENCE_KEY, STALE)
    }
}

impl Presence {
    /// Presence on `key`, an instance counting for `stale` after its beat.
    pub fn new(key: impl Into<String>, stale: Duration) -> Self {
        Self {
            key: key.into(),
            stale,
        }
    }

    /// Record that `instance` is there at `now_ms`, forget the instances
    /// silent for longer than the stale window, and return how many are
    /// there (this one included).
    pub async fn beat(
        &self,
        redis: &mut ConnectionManager,
        instance: &str,
        now_ms: u64,
    ) -> redis::RedisResult<usize> {
        let oldest = now_ms.saturating_sub(self.stale.as_millis() as u64);
        let _: () = redis::cmd("ZADD")
            .arg(&self.key)
            .arg(now_ms)
            .arg(instance)
            .query_async(redis)
            .await?;
        let _: () = redis::cmd("ZREMRANGEBYSCORE")
            .arg(&self.key)
            .arg("-inf")
            .arg(format!("({oldest}"))
            .query_async(redis)
            .await?;
        let _: () = redis::cmd("PEXPIRE")
            .arg(&self.key)
            .arg(self.stale.as_millis() as u64)
            .query_async(redis)
            .await?;
        let count: usize = redis::cmd("ZCARD")
            .arg(&self.key)
            .query_async(redis)
            .await?;
        Ok(count)
    }
}

/// What is logged when `count` instances use a filesystem media store.
/// `None` for one.
pub fn warning(count: usize) -> Option<String> {
    (count > 1).then(|| {
        format!(
            "{count} gateway instances use a filesystem media store \
             (BRUTOR_MEDIA_STORAGE_BACKEND=filesystem): it serves ONE instance. Each \
             instance sees only the files on its own volume, so a file one instance stored \
             is missing on the others, and the size limit is counted per instance. Use \
             BRUTOR_MEDIA_STORAGE_BACKEND=s3 with more than one instance."
        )
    })
}
