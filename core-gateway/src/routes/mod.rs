// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

pub mod a2a;
pub mod a2a_v1;
pub mod anthropic;
pub mod approvals;
pub mod audio_speech;
pub mod audio_transcribe;
pub mod files;
pub mod gateway;
pub mod gemini;
pub mod icons;
pub mod images;
pub mod llm;
pub mod llm_helpers;
pub mod mcp;
pub mod media;
pub mod metrics;
pub mod oauth;
// Portal self-service for kb_connector_identity_mappings.
// Restricted to the authenticated user's own rows; the POST stub
// returns 501 because real self-link requires per-connector OAuth.
pub mod request;
pub mod route_helpers;
// Explicit run finalization.
pub mod assurance_checks;
pub mod audit;
pub mod evidence;
// Optional AAC profile (flag-gated).
pub mod evidence_aac;
pub mod otel_ingest;
pub mod run_feedback;
pub mod runs;
pub mod skills;
pub mod video;
pub mod vmcp;
