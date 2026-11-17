// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! In-process stand-in for the Redis keyspace the limiters use.
//!
//! The gateway does not run without Redis (main refuses to start without
//! one), so this store backs exactly one thing: `UsageState::new(None)` in
//! tests. It keeps the SAME keys the Redis scripts use (window index and
//! all), so a test reads back precisely what production would have written,
//! and one mutex makes every multi-key operation atomic the way a Lua script
//! is. It is not evidence of cross-replica atomicity — that needs a real-Redis
//! test (`tests/integration/quota_counter_redis.rs`, `admission_redis.rs`).

use std::collections::HashMap;
use std::sync::Mutex;

/// One stored value. Mirrors the Redis types the limiter scripts touch.
#[derive(Debug, Clone)]
pub(crate) enum MemVal {
    Int(i64),
    /// Token bucket: `tokens` after the last refill at `ts_ms`.
    Bucket {
        tokens: f64,
        ts_ms: i64,
    },
    /// Sorted set: member -> score (lease expiry, epoch ms).
    Zset(HashMap<String, i64>),
    /// Set of distinct members.
    Set(std::collections::HashSet<String>),
    Str(String),
}

#[derive(Debug, Clone)]
pub(crate) struct MemEntry {
    pub val: MemVal,
    /// Absolute expiry, epoch ms. `None` = no TTL.
    pub expires_ms: Option<i64>,
}

#[derive(Debug, Default)]
pub(crate) struct MemKv {
    inner: Mutex<HashMap<String, MemEntry>>,
}

/// A locked view of the store; every method sees the same instant.
pub(crate) struct MemTx<'a> {
    map: std::sync::MutexGuard<'a, HashMap<String, MemEntry>>,
    pub now_ms: i64,
}

impl MemKv {
    pub fn lock(&self, now_ms: i64) -> MemTx<'_> {
        // A poisoned lock means a test panicked mid-update; the data is still
        // the best we have, and refusing to continue would only hide the
        // original failure behind a second one.
        let map = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        MemTx { map, now_ms }
    }
}

impl MemTx<'_> {
    fn live(&mut self, key: &str) -> Option<&mut MemEntry> {
        let expired = matches!(
            self.map.get(key),
            Some(MemEntry { expires_ms: Some(t), .. }) if *t <= self.now_ms
        );
        if expired {
            self.map.remove(key);
        }
        self.map.get_mut(key)
    }

    pub fn exists(&mut self, key: &str) -> bool {
        self.live(key).is_some()
    }

    pub fn get_int(&mut self, key: &str) -> i64 {
        match self.live(key) {
            Some(MemEntry {
                val: MemVal::Int(n),
                ..
            }) => *n,
            _ => 0,
        }
    }

    /// `INCRBY` + floor at zero; a fresh key gets `ttl_ms`, an existing key
    /// keeps its own unless `refresh_ttl`.
    pub fn incr_floor(
        &mut self,
        key: &str,
        delta: i64,
        ttl_ms: Option<i64>,
        refresh_ttl: bool,
    ) -> i64 {
        let now = self.now_ms;
        let fresh = self.live(key).is_none();
        let entry = self.map.entry(key.to_string()).or_insert(MemEntry {
            val: MemVal::Int(0),
            expires_ms: None,
        });
        let cur = match entry.val {
            MemVal::Int(n) => n,
            _ => 0,
        };
        let next = (cur + delta).max(0);
        entry.val = MemVal::Int(next);
        if fresh || refresh_ttl || entry.expires_ms.is_none() {
            if let Some(t) = ttl_ms {
                entry.expires_ms = Some(now + t);
            }
        }
        next
    }

    pub fn set_int(&mut self, key: &str, v: i64, ttl_ms: Option<i64>) {
        let now = self.now_ms;
        self.map.insert(
            key.to_string(),
            MemEntry {
                val: MemVal::Int(v),
                expires_ms: ttl_ms.map(|t| now + t),
            },
        );
    }

    /// Set, keeping an existing key's expiry (`SET … KEEPTTL`); a fresh key
    /// gets `ttl_ms`.
    pub fn set_int_keepttl(&mut self, key: &str, v: i64, ttl_ms: Option<i64>) {
        let now = self.now_ms;
        let keep = self.live(key).and_then(|e| e.expires_ms);
        self.map.insert(
            key.to_string(),
            MemEntry {
                val: MemVal::Int(v),
                expires_ms: keep.or(ttl_ms.map(|t| now + t)),
            },
        );
    }

    pub fn set_str(&mut self, key: &str, v: String, ttl_ms: Option<i64>) {
        let now = self.now_ms;
        self.map.insert(
            key.to_string(),
            MemEntry {
                val: MemVal::Str(v),
                expires_ms: ttl_ms.map(|t| now + t),
            },
        );
    }

    pub fn get_str(&mut self, key: &str) -> Option<String> {
        match self.live(key) {
            Some(MemEntry {
                val: MemVal::Str(s),
                ..
            }) => Some(s.clone()),
            _ => None,
        }
    }

    pub fn del(&mut self, key: &str) {
        self.map.remove(key);
    }

    /// Remaining TTL in ms: `None` when the key is absent or has no TTL.
    pub fn pttl(&mut self, key: &str) -> Option<i64> {
        let now = self.now_ms;
        self.live(key)
            .and_then(|e| e.expires_ms)
            .map(|t| (t - now).max(0))
    }

    pub fn expire(&mut self, key: &str, ttl_ms: i64) {
        let now = self.now_ms;
        if let Some(e) = self.live(key) {
            e.expires_ms = Some(now + ttl_ms);
        }
    }

    /// Bucket state after a lazy refill: `(tokens, ts)` as the bucket scripts
    /// compute them. A missing bucket starts full.
    pub fn bucket(&mut self, key: &str, capacity: f64, refill_per_sec: f64) -> f64 {
        let now = self.now_ms;
        match self.live(key) {
            Some(MemEntry {
                val: MemVal::Bucket { tokens, ts_ms },
                ..
            }) => {
                let elapsed = ((now - *ts_ms) as f64 / 1000.0).max(0.0);
                (*tokens + elapsed * refill_per_sec).min(capacity)
            }
            _ => capacity,
        }
    }

    pub fn set_bucket(&mut self, key: &str, tokens: f64, ttl_ms: i64) {
        let now = self.now_ms;
        self.map.insert(
            key.to_string(),
            MemEntry {
                val: MemVal::Bucket { tokens, ts_ms: now },
                expires_ms: Some(now + ttl_ms),
            },
        );
    }

    /// `SCARD`: 0 when the key is absent.
    pub fn set_len(&mut self, key: &str) -> i64 {
        match self.live(key) {
            Some(MemEntry {
                val: MemVal::Set(s),
                ..
            }) => s.len() as i64,
            _ => 0,
        }
    }

    /// `SISMEMBER`.
    pub fn set_has(&mut self, key: &str, member: &str) -> bool {
        match self.live(key) {
            Some(MemEntry {
                val: MemVal::Set(s),
                ..
            }) => s.contains(member),
            _ => false,
        }
    }

    /// `SADD` + `EXPIRE`: the key lives `ttl_ms` from this addition.
    pub fn set_add(&mut self, key: &str, member: &str, ttl_ms: i64) {
        let now = self.now_ms;
        if !matches!(self.live(key).map(|e| &e.val), Some(MemVal::Set(_))) {
            self.map.insert(
                key.to_string(),
                MemEntry {
                    val: MemVal::Set(std::collections::HashSet::new()),
                    expires_ms: None,
                },
            );
        }
        let entry = self.map.get_mut(key).expect("just inserted");
        if let MemVal::Set(s) = &mut entry.val {
            s.insert(member.to_string());
        }
        entry.expires_ms = Some(now + ttl_ms);
    }

    fn zset(&mut self, key: &str) -> &mut HashMap<String, i64> {
        if !matches!(self.live(key).map(|e| &e.val), Some(MemVal::Zset(_))) {
            self.map.insert(
                key.to_string(),
                MemEntry {
                    val: MemVal::Zset(HashMap::new()),
                    expires_ms: None,
                },
            );
        }
        match &mut self.map.get_mut(key).expect("just inserted").val {
            MemVal::Zset(z) => z,
            _ => unreachable!("zset() always leaves a zset at the key"),
        }
    }

    pub fn zadd(&mut self, key: &str, member: &str, score: i64) {
        self.zset(key).insert(member.to_string(), score);
    }

    /// `ZADD XX`: update only an existing member.
    pub fn zadd_xx(&mut self, key: &str, member: &str, score: i64) -> bool {
        match self.zset(key).get_mut(member) {
            Some(s) => {
                *s = score;
                true
            }
            None => false,
        }
    }

    pub fn zrem(&mut self, key: &str, member: &str) {
        self.zset(key).remove(member);
    }

    /// Drop members scored at or before `max` (`ZREMRANGEBYSCORE -inf max`).
    pub fn zrem_upto(&mut self, key: &str, max: i64) {
        self.zset(key).retain(|_, s| *s > max);
    }

    pub fn zcard(&mut self, key: &str) -> i64 {
        self.zset(key).len() as i64
    }

    /// Members scored at or before `max`, lowest first, at most `limit`.
    pub fn zrange_upto(&mut self, key: &str, max: i64, limit: usize) -> Vec<String> {
        let mut v: Vec<(i64, String)> = self
            .zset(key)
            .iter()
            .filter(|(_, s)| **s <= max)
            .map(|(m, s)| (*s, m.clone()))
            .collect();
        v.sort();
        v.into_iter().take(limit).map(|(_, m)| m).collect()
    }
}
