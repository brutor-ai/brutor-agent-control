// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! # Multimodal Files API — object storage abstraction
//!
//! Media storage.
//! Generated and
//! uploaded media (images, audio, video) is persisted to object
//! storage; chat messages reference the asset by stable
//! `brutor_file_id` and the portal asks the gateway for a fresh
//! signed URL when it needs to render the bytes.
//!
//! ## Layers
//!
//! - [`path`] — pure path composition + validation. Independent of
//!   the storage backend; lets us test the isolation rules
//!   (tenant-prefix, no path traversal, extension allowlist) without
//!   spinning up MinIO.
//! - [`storage`] — the `Storage` trait + `InMemoryStorage` test
//!   double. Higher layers depend on the trait, not on a concrete
//!   backend, so the route handlers in
//!   `services::media_storage::endpoints` can be
//!   exercised against in-process bytes.
//! - [`filesystem`] — `FilesystemStorage`: files under a directory (a
//!   volume) for one gateway instance; the gateway serves its signed
//!   download links itself (`routes::media`). [`replicas`] reports a
//!   second instance that uses one.
//! - [`s3`] — `S3Storage`, the implementation for several instances. Uses
//!   `aws-sdk-s3` so the same code drives both AWS S3 (SaaS) and
//!   MinIO (trial bundle) — MinIO is S3-API compatible, the only
//!   difference is the endpoint URL and the credential source.
//!
//! ## Why this lives in `services::media_storage`, not as its own
//! crate
//!
//! Every other gateway-core service (KB ingestion, A2A, MCP) sits
//! under `src/services/`. Promoting media-storage to a workspace
//! crate would buy us a faster compile-edit cycle on the storage
//! code in isolation — but then every other consumer (route
//! handlers, the GC sweep job) has to declare it as a dep, the
//! workspace's release/version dance gets one more entry, and the
//! shared `AppState` needs an external trait import. The cost
//! outweighs the win at this size; if media-storage grows past a
//! few thousand lines or starts shipping its own binary
//! (background workers, etc.), we can extract it then.

pub mod filesystem;
pub mod path;
pub mod replicas;
pub mod s3;
pub mod storage;
pub mod sweep;

pub use filesystem::{FilesystemConfig, FilesystemStorage, LinkSigner};
pub use path::{KeyError, MediaKind, ObjectKey};
pub use s3::{S3Config, S3Credentials, S3Storage};
pub use storage::{InMemoryStorage, ObjectMetadata, Storage, StorageError};
pub use sweep::{SweepReport, run_once as run_sweep_once, spawn_loop as spawn_sweep_loop};
