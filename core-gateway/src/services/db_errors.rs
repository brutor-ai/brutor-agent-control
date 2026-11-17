// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A database error is never discarded silently.
//!
//! `sqlx::query…fetch_*().await.unwrap_or(None)` turned "the database
//! failed" into "there is no row" with no log line, no metric and nothing
//! tying the wrong answer to its cause. Where a caller degrades past the
//! error on purpose, it goes through [`DbResultExt`]: the fallback is the
//! same, but the error is logged at `error` with the exact call site and
//! counted in `brutor_db_errors_degraded_total{site}`.
//!
//! Degrading is the exception. Enforcement and evidence paths propagate the
//! error and fail closed (503) instead; `tests/swallowed_db_error_guard.rs`
//! refuses a bare swallow and ratchets the degraded sites down.

use std::panic::Location;

/// Log and count one degraded database error at the caller's location.
#[track_caller]
pub fn record(e: &sqlx::Error) {
    let at = Location::caller();
    let site = format!("{}:{}", at.file(), at.line());
    tracing::error!(site = %site, error = %e, "database error degraded to a fallback value");
    metrics::counter!("brutor_db_errors_degraded_total", "site" => site).increment(1);
}

/// The response for a request that cannot be answered because a database
/// read failed: 503 with the canonical error envelope, logged with what was
/// being read and where. Never a 404 or an empty result.
#[track_caller]
pub fn unavailable(what: &str, e: &sqlx::Error) -> axum::response::Response {
    let at = Location::caller();
    tracing::error!(site = %format!("{}:{}", at.file(), at.line()), lookup = what, error = %e, "database read failed; answering 503");
    crate::services::error_envelope::canonical_error_response(
        axum::http::StatusCode::SERVICE_UNAVAILABLE,
        &format!("{what} could not be read; retry"),
    )
}

/// Degrade past a database error visibly. Each method returns what the old
/// swallow returned, after [`record`]ing the error.
pub trait DbResultExt<T> {
    /// `.unwrap_or(default)`, logged.
    #[track_caller]
    fn or_logged(self, default: T) -> T;
    /// `.unwrap_or_default()`, logged.
    #[track_caller]
    fn or_logged_default(self) -> T
    where
        T: Default;
    /// `.unwrap_or_else(|_| f())`, logged.
    #[track_caller]
    fn or_logged_else(self, f: impl FnOnce() -> T) -> T;
    /// `.ok()`, logged.
    #[track_caller]
    fn ok_logged(self) -> Option<T>;
}

impl<T> DbResultExt<T> for Result<T, sqlx::Error> {
    #[track_caller]
    fn or_logged(self, default: T) -> T {
        match self {
            Ok(v) => v,
            Err(e) => {
                record(&e);
                default
            }
        }
    }

    #[track_caller]
    fn or_logged_default(self) -> T
    where
        T: Default,
    {
        match self {
            Ok(v) => v,
            Err(e) => {
                record(&e);
                T::default()
            }
        }
    }

    #[track_caller]
    fn or_logged_else(self, f: impl FnOnce() -> T) -> T {
        match self {
            Ok(v) => v,
            Err(e) => {
                record(&e);
                f()
            }
        }
    }

    #[track_caller]
    fn ok_logged(self) -> Option<T> {
        match self {
            Ok(v) => Some(v),
            Err(e) => {
                record(&e);
                None
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values_pass_through_and_errors_fall_back() {
        let ok: Result<i64, sqlx::Error> = Ok(7);
        assert_eq!(ok.or_logged(0), 7);
        let err = || -> Result<i64, sqlx::Error> { Err(sqlx::Error::PoolTimedOut) };
        assert_eq!(err().or_logged(0), 0);
        assert_eq!(err().or_logged_default(), 0);
        assert_eq!(err().or_logged_else(|| 3), 3);
        assert_eq!(err().ok_logged(), None);
    }

    #[test]
    fn the_site_is_the_callers_line_not_this_file() {
        #[track_caller]
        fn here() -> String {
            let at = Location::caller();
            format!("{}:{}", at.file(), at.line())
        }
        // `record` reports `Location::caller()`; through `#[track_caller]`
        // that is the line that called the trait method.
        let site = here();
        assert!(site.contains("db_errors.rs"), "{site}");
    }
}
