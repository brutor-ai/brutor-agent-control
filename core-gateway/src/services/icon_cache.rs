// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! In-memory icon cache with TTL.
//!
//! Caches MCP server icon binary data to avoid repeated DB queries.
//! Icons rarely change, so a 1-hour TTL is generous. Cache is bounded
//! to prevent unbounded memory growth.

use std::collections::HashMap;
use std::sync::RwLock;
use std::time::{Duration, Instant};

const MAX_ENTRIES: usize = 500;
const TTL: Duration = Duration::from_secs(3600); // 1 hour

pub struct CachedIcon {
    pub data: Vec<u8>,
    pub mime_type: String,
    pub etag: String,
    inserted_at: Instant,
}

pub struct IconCache {
    entries: RwLock<HashMap<String, CachedIcon>>,
}

impl IconCache {
    pub fn new() -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
        }
    }

    /// Get a cached icon if it exists and hasn't expired.
    pub fn get(&self, server_id: &str) -> Option<(Vec<u8>, String, String)> {
        let entries = self.entries.read().ok()?;
        let entry = entries.get(server_id)?;
        if entry.inserted_at.elapsed() > TTL {
            return None; // Expired
        }
        Some((
            entry.data.clone(),
            entry.mime_type.clone(),
            entry.etag.clone(),
        ))
    }

    /// Insert an icon into the cache.
    pub fn insert(&self, server_id: String, data: Vec<u8>, mime_type: String, etag: String) {
        if let Ok(mut entries) = self.entries.write() {
            // Evict expired entries if we're at capacity
            if entries.len() >= MAX_ENTRIES {
                entries.retain(|_, v| v.inserted_at.elapsed() <= TTL);
            }
            // If still at capacity, evict oldest
            if entries.len() >= MAX_ENTRIES {
                if let Some(oldest_key) = entries
                    .iter()
                    .min_by_key(|(_, v)| v.inserted_at)
                    .map(|(k, _)| k.clone())
                {
                    entries.remove(&oldest_key);
                }
            }
            entries.insert(
                server_id,
                CachedIcon {
                    data,
                    mime_type,
                    etag,
                    inserted_at: Instant::now(),
                },
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_cache_is_empty() {
        let cache = IconCache::new();
        assert!(cache.get("any-key").is_none());
        assert!(cache.get("").is_none());
        assert!(cache.get("server-123").is_none());
    }

    #[test]
    fn insert_and_get() {
        let cache = IconCache::new();
        cache.insert(
            "server-1".into(),
            vec![0x89, 0x50, 0x4E, 0x47],
            "image/png".into(),
            "etag-abc".into(),
        );
        let result = cache.get("server-1");
        assert!(result.is_some());
    }

    #[test]
    fn get_returns_correct_data() {
        let cache = IconCache::new();
        let data = vec![0xFF, 0xD8, 0xFF, 0xE0];
        let mime = "image/jpeg".to_string();
        let etag = "\"abc123\"".to_string();

        cache.insert("srv-42".into(), data.clone(), mime.clone(), etag.clone());

        let (ret_data, ret_mime, ret_etag) = cache.get("srv-42").unwrap();
        assert_eq!(ret_data, data);
        assert_eq!(ret_mime, mime);
        assert_eq!(ret_etag, etag);
    }

    #[test]
    fn cache_miss_returns_none() {
        let cache = IconCache::new();
        cache.insert(
            "exists".into(),
            vec![1, 2, 3],
            "image/png".into(),
            "e1".into(),
        );
        assert!(cache.get("does-not-exist").is_none());
        assert!(cache.get("EXISTS").is_none()); // case sensitive
        assert!(cache.get("").is_none());
    }

    #[test]
    fn insert_overwrites_existing() {
        let cache = IconCache::new();
        cache.insert(
            "srv-1".into(),
            vec![1, 2, 3],
            "image/png".into(),
            "etag-v1".into(),
        );
        cache.insert(
            "srv-1".into(),
            vec![4, 5, 6],
            "image/svg+xml".into(),
            "etag-v2".into(),
        );

        let (data, mime, etag) = cache.get("srv-1").unwrap();
        assert_eq!(data, vec![4, 5, 6]);
        assert_eq!(mime, "image/svg+xml");
        assert_eq!(etag, "etag-v2");
    }

    #[test]
    fn max_entries_eviction() {
        let cache = IconCache::new();

        // Fill to MAX_ENTRIES
        for i in 0..MAX_ENTRIES {
            cache.insert(
                format!("server-{i}"),
                vec![i as u8],
                "image/png".into(),
                format!("etag-{i}"),
            );
        }

        // All entries should be accessible
        assert!(cache.get("server-0").is_some());
        assert!(cache.get(&format!("server-{}", MAX_ENTRIES - 1)).is_some());

        // Insert one more — should evict the oldest
        cache.insert(
            "server-new".into(),
            vec![0xFF],
            "image/png".into(),
            "etag-new".into(),
        );

        // The new entry should exist
        let (data, _, etag) = cache.get("server-new").unwrap();
        assert_eq!(data, vec![0xFF]);
        assert_eq!(etag, "etag-new");

        // Total entries should not exceed MAX_ENTRIES
        let entries = cache.entries.read().unwrap();
        assert!(entries.len() <= MAX_ENTRIES);
    }

    #[test]
    fn expired_entry_structure() {
        // We can't easily test TTL expiration without mocking time,
        // but we can verify that the CachedIcon stores inserted_at correctly
        // and that the cache correctly uses Instant for timing.
        let cache = IconCache::new();
        cache.insert(
            "srv-ttl".into(),
            vec![10, 20],
            "image/gif".into(),
            "etag-ttl".into(),
        );

        // Entry should be valid immediately (well within TTL)
        let result = cache.get("srv-ttl");
        assert!(result.is_some());

        // Verify the internal entry has a reasonable inserted_at
        let entries = cache.entries.read().unwrap();
        let entry = entries.get("srv-ttl").unwrap();
        assert!(entry.inserted_at.elapsed() < Duration::from_secs(5));
        assert_eq!(entry.data, vec![10, 20]);
        assert_eq!(entry.mime_type, "image/gif");
        assert_eq!(entry.etag, "etag-ttl");
    }

    #[test]
    fn multiple_distinct_entries() {
        let cache = IconCache::new();
        cache.insert("a".into(), vec![1], "image/png".into(), "etag-a".into());
        cache.insert("b".into(), vec![2], "image/jpeg".into(), "etag-b".into());
        cache.insert("c".into(), vec![3], "image/svg+xml".into(), "etag-c".into());

        let (data_a, mime_a, _) = cache.get("a").unwrap();
        let (data_b, mime_b, _) = cache.get("b").unwrap();
        let (data_c, mime_c, _) = cache.get("c").unwrap();

        assert_eq!(data_a, vec![1]);
        assert_eq!(data_b, vec![2]);
        assert_eq!(data_c, vec![3]);
        assert_eq!(mime_a, "image/png");
        assert_eq!(mime_b, "image/jpeg");
        assert_eq!(mime_c, "image/svg+xml");
    }

    #[test]
    fn empty_data_icon() {
        let cache = IconCache::new();
        cache.insert(
            "empty-icon".into(),
            vec![],
            "image/png".into(),
            "etag-empty".into(),
        );
        let (data, _, _) = cache.get("empty-icon").unwrap();
        assert!(data.is_empty());
    }
}
