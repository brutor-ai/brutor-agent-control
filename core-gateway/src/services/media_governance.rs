// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Multimodal (media) governance + limits enforcement.
//!
//! This module reads the per-mode JSONB columns
//! (`media_global_governance`, `media_global_limits`) from the
//! tenant's resource group and enforces request-shape rules
//! before the upstream provider call dispatches. The schema
//! mirrors `PolicyMediaGovernance` / `PolicyMediaLimits` on the
//! Python side — keys are LLModelMode values
//! (`audio_speech`, `audio_transcription`, `image_generation`,
//! `video_generation`, `embedding`).
//!
//! Why a single JSONB column per category instead of one column
//! per modality: schema stays put when we add a new modality,
//! same DB-shape works for both partial (per-mode) and full
//! (whole-column) writes from the policy engine. Cost: every
//! enforcement helper has to do a `.get(mode_key)` indirection
//! before it can read a setting. That's cheap.
//!
//! Each `enforce_*` helper returns `Ok(())` on pass or a
//! `MediaGovernanceError` on a violation that the route handler
//! converts to an HTTP 400 / 403. Routes are responsible for
//! fetching the group governance once per request and threading
//! it into the helper — keeping the read out of every helper
//! avoids N DB roundtrips per request.

use serde_json::Value;

#[derive(Debug, thiserror::Error)]
pub enum MediaGovernanceError {
    #[error("voice {voice:?} not in whitelist {whitelist:?}")]
    VoiceNotWhitelisted {
        voice: String,
        whitelist: Vec<String>,
    },
    #[error("response_format {format:?} not in whitelist {whitelist:?}")]
    FormatNotWhitelisted {
        format: String,
        whitelist: Vec<String>,
    },
    #[error("input is {chars} chars, exceeds policy cap of {cap}")]
    InputTooLong { chars: usize, cap: usize },
    #[error("speed {speed} outside policy range [{min}, {max}]")]
    SpeedOutOfRange { speed: f32, min: f32, max: f32 },
    #[error("language {lang:?} not in allowlist {allowlist:?}")]
    LanguageNotAllowed {
        lang: String,
        allowlist: Vec<String>,
    },
    #[error("audio is {size_mb:.2} MB, exceeds policy cap of {cap_mb:.2} MB")]
    AudioTooLarge { size_mb: f64, cap_mb: f64 },
    #[error("image size {size:?} not in whitelist {whitelist:?}")]
    SizeNotWhitelisted {
        size: String,
        whitelist: Vec<String>,
    },
    #[error("image quality {quality:?} not in whitelist {whitelist:?}")]
    QualityNotWhitelisted {
        quality: String,
        whitelist: Vec<String>,
    },
    #[error("image style {style:?} not in whitelist {whitelist:?}")]
    StyleNotWhitelisted {
        style: String,
        whitelist: Vec<String>,
    },
    #[error("requested {n} images, policy max is {max}")]
    ImageCountExceeded { n: u32, max: u32 },
    #[error("prompt is {chars} chars, exceeds policy cap of {cap}")]
    PromptTooLong { chars: usize, cap: usize },
    #[error("video duration {duration}s exceeds policy max of {max}s")]
    DurationExceeded { duration: u32, max: u32 },
    #[error("video resolution {resolution:?} not in whitelist {whitelist:?}")]
    ResolutionNotWhitelisted {
        resolution: String,
        whitelist: Vec<String>,
    },
    #[error("video policy requires an image input but none was provided")]
    ImageInputRequired,
    #[error("embedding dimensions {dim} not in whitelist {whitelist:?}")]
    DimNotWhitelisted { dim: u32, whitelist: Vec<u32> },
    #[error("embedding batch size {n} exceeds policy max of {max}")]
    BatchTooLarge { n: u32, max: u32 },
}

/// Extract the per-mode governance dict for a given mode.
/// Returns `None` if no governance is set on the group, or if
/// no entry exists for this mode.
pub fn governance_for_mode<'a>(
    media_global_governance: Option<&'a Value>,
    mode: &str,
) -> Option<&'a Value> {
    media_global_governance.and_then(|v| v.get(mode))
}

/// Field-name → restrictive-merge rule for a single per-mode media
/// governance field. Each field's "most restrictive" direction differs, so a
/// generic numeric-min merge (`usage::restrictive_merge`) would be WRONG here
/// (it can't intersect whitelists and would min a floor like `speed_min`).
enum MediaMergeRule {
    /// Allow-list: most-restrictive = INTERSECTION. Empty list means
    /// "unconstrained", so it's the identity element (empty ∩ X = X).
    Whitelist,
    /// Ceiling (max_*): most-restrictive = MIN.
    Ceiling,
    /// Floor (min bound): most-restrictive = MAX.
    Floor,
    /// Bool toggle where `true` is stricter (e.g. require_image_input): OR.
    BoolOr,
}

fn media_field_rule(field: &str) -> Option<MediaMergeRule> {
    match field {
        "voice_whitelist"
        | "response_format_whitelist"
        | "language_allowlist"
        | "size_whitelist"
        | "quality_whitelist"
        | "style_whitelist"
        | "resolution_whitelist"
        | "dim_whitelist" => Some(MediaMergeRule::Whitelist),
        "max_input_characters"
        | "speed_max"
        | "max_audio_size_mb"
        | "max_n"
        | "max_prompt_characters"
        | "max_duration_seconds"
        | "max_batch_size" => Some(MediaMergeRule::Ceiling),
        "speed_min" => Some(MediaMergeRule::Floor),
        "require_image_input" => Some(MediaMergeRule::BoolOr),
        _ => None,
    }
}

/// Merge a parent group's `media_global_governance` into the (child-rooted)
/// accumulator, MOST-RESTRICTIVE wins, per the per-field rules above. Operates
/// on the full mode→fields dict. A field/mode present only on the parent is
/// inherited as-is. A level's `null` (a document, a mode or a field) states
/// nothing: the other level's stays.
pub fn merge_media_governance(base: &mut Value, parent: &Value) {
    let Some(parent_modes) = parent.as_object() else {
        return;
    };
    let Some(base_modes) = base.as_object_mut() else {
        *base = parent.clone();
        return;
    };
    for (mode, parent_mode_gov) in parent_modes {
        let Some(parent_fields) = parent_mode_gov.as_object() else {
            continue;
        };
        let entry = base_modes
            .entry(mode.clone())
            .or_insert_with(|| Value::Object(serde_json::Map::new()));
        let Some(base_fields) = entry.as_object_mut() else {
            // The child's slot for this mode is not an object: the parent's.
            *entry = parent_mode_gov.clone();
            continue;
        };
        for (field, pv) in parent_fields {
            match media_field_rule(field) {
                Some(MediaMergeRule::Whitelist) => {
                    // Intersect on raw Value equality so string AND int
                    // whitelists (e.g. dim_whitelist) both work.
                    let p_arr: Vec<Value> = pv.as_array().cloned().unwrap_or_default();
                    match base_fields.get(field) {
                        Some(bv) => {
                            let b_arr: Vec<Value> = bv.as_array().cloned().unwrap_or_default();
                            // empty = unconstrained → identity element
                            let merged: Vec<Value> = if b_arr.is_empty() {
                                p_arr
                            } else if p_arr.is_empty() {
                                b_arr
                            } else {
                                b_arr.into_iter().filter(|x| p_arr.contains(x)).collect()
                            };
                            base_fields.insert(field.clone(), Value::Array(merged));
                        }
                        None => {
                            base_fields.insert(field.clone(), pv.clone());
                        }
                    }
                }
                Some(MediaMergeRule::Ceiling) | Some(MediaMergeRule::Floor) => {
                    let take_max = matches!(media_field_rule(field), Some(MediaMergeRule::Floor));
                    // A stated bound is a number that is not negative (`0`
                    // is a ceiling that admits nothing). `null`, a negative
                    // number or a non-number is no bound at its level (the
                    // enforce_* readers take integer caps with `as_u64`):
                    // the other level's stays.
                    let stated = |v: Option<&Value>| {
                        v.and_then(Value::as_f64)
                            .filter(|f| f.is_finite() && *f >= 0.0)
                    };
                    if let Some(pf) = stated(Some(pv)) {
                        let parent_wins = match stated(base_fields.get(field)) {
                            Some(bf) if take_max => pf > bf,
                            Some(bf) => pf < bf,
                            None => true,
                        };
                        // The winning value as stated: an integer cap read
                        // with `as_u64` stays an integer.
                        if parent_wins {
                            base_fields.insert(field.clone(), pv.clone());
                        }
                    }
                }
                Some(MediaMergeRule::BoolOr) => {
                    let merged = base_fields
                        .get(field)
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false)
                        || pv.as_bool().unwrap_or(false);
                    base_fields.insert(field.clone(), Value::Bool(merged));
                }
                // Unknown field: inherit parent's only if child doesn't set it.
                None => {
                    if base_fields.get(field).is_none_or(Value::is_null) {
                        base_fields.insert(field.clone(), pv.clone());
                    }
                }
            }
        }
    }
}

fn as_string_array(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

fn as_int_array(value: &Value) -> Vec<u32> {
    value
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_u64().map(|n| n as u32))
                .collect()
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Mode: audio_speech (TTS)
// ---------------------------------------------------------------------------

pub struct AudioSpeechRequest<'a> {
    pub voice: &'a str,
    pub response_format: &'a str,
    pub input_chars: usize,
    pub speed: f32,
}

pub fn enforce_audio_speech(
    governance: Option<&Value>,
    req: &AudioSpeechRequest<'_>,
) -> Result<(), MediaGovernanceError> {
    let Some(g) = governance else {
        return Ok(());
    };

    if let Some(wl) = g.get("voice_whitelist") {
        let whitelist = as_string_array(wl);
        if !whitelist.is_empty() && !whitelist.iter().any(|v| v == req.voice) {
            return Err(MediaGovernanceError::VoiceNotWhitelisted {
                voice: req.voice.to_string(),
                whitelist,
            });
        }
    }

    if let Some(wl) = g.get("response_format_whitelist") {
        let whitelist = as_string_array(wl);
        if !whitelist.is_empty()
            && !whitelist
                .iter()
                .any(|f| f.eq_ignore_ascii_case(req.response_format))
        {
            return Err(MediaGovernanceError::FormatNotWhitelisted {
                format: req.response_format.to_string(),
                whitelist,
            });
        }
    }

    if let Some(cap) = g.get("max_input_characters").and_then(|v| v.as_u64()) {
        let cap = cap as usize;
        if req.input_chars > cap {
            return Err(MediaGovernanceError::InputTooLong {
                chars: req.input_chars,
                cap,
            });
        }
    }

    let min = g
        .get("speed_min")
        .and_then(|v| v.as_f64())
        .map(|f| f as f32);
    let max = g
        .get("speed_max")
        .and_then(|v| v.as_f64())
        .map(|f| f as f32);
    if let (Some(min_v), Some(max_v)) = (min, max) {
        if req.speed < min_v || req.speed > max_v {
            return Err(MediaGovernanceError::SpeedOutOfRange {
                speed: req.speed,
                min: min_v,
                max: max_v,
            });
        }
    } else if let Some(min_v) = min {
        if req.speed < min_v {
            return Err(MediaGovernanceError::SpeedOutOfRange {
                speed: req.speed,
                min: min_v,
                max: f32::MAX,
            });
        }
    } else if let Some(max_v) = max {
        if req.speed > max_v {
            return Err(MediaGovernanceError::SpeedOutOfRange {
                speed: req.speed,
                min: 0.0,
                max: max_v,
            });
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Mode: audio_transcription (STT)
// ---------------------------------------------------------------------------

pub struct AudioTranscriptionRequest<'a> {
    pub language: Option<&'a str>,
    pub response_format: Option<&'a str>,
    pub audio_size_bytes: u64,
}

pub fn enforce_audio_transcription(
    governance: Option<&Value>,
    req: &AudioTranscriptionRequest<'_>,
) -> Result<(), MediaGovernanceError> {
    let Some(g) = governance else {
        return Ok(());
    };

    if let (Some(lang), Some(allowlist_v)) = (req.language, g.get("language_allowlist")) {
        let allowlist = as_string_array(allowlist_v);
        if !allowlist.is_empty() && !allowlist.iter().any(|l| l == lang) {
            return Err(MediaGovernanceError::LanguageNotAllowed {
                lang: lang.to_string(),
                allowlist,
            });
        }
    }

    if let (Some(fmt), Some(wl_v)) = (req.response_format, g.get("response_format_whitelist")) {
        let whitelist = as_string_array(wl_v);
        if !whitelist.is_empty() && !whitelist.iter().any(|f| f.eq_ignore_ascii_case(fmt)) {
            return Err(MediaGovernanceError::FormatNotWhitelisted {
                format: fmt.to_string(),
                whitelist,
            });
        }
    }

    if let Some(cap_mb) = g.get("max_audio_size_mb").and_then(|v| v.as_f64()) {
        let size_mb = req.audio_size_bytes as f64 / (1024.0 * 1024.0);
        if size_mb > cap_mb {
            return Err(MediaGovernanceError::AudioTooLarge { size_mb, cap_mb });
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Mode: image_generation
// ---------------------------------------------------------------------------

pub struct ImageGenerationRequest<'a> {
    pub size: Option<&'a str>,
    pub quality: Option<&'a str>,
    pub style: Option<&'a str>,
    pub n: u32,
    pub prompt_chars: usize,
}

pub fn enforce_image_generation(
    governance: Option<&Value>,
    req: &ImageGenerationRequest<'_>,
) -> Result<(), MediaGovernanceError> {
    let Some(g) = governance else {
        return Ok(());
    };

    if let (Some(size), Some(wl_v)) = (req.size, g.get("size_whitelist")) {
        let whitelist = as_string_array(wl_v);
        if !whitelist.is_empty() && !whitelist.iter().any(|s| s == size) {
            return Err(MediaGovernanceError::SizeNotWhitelisted {
                size: size.to_string(),
                whitelist,
            });
        }
    }

    if let (Some(q), Some(wl_v)) = (req.quality, g.get("quality_whitelist")) {
        let whitelist = as_string_array(wl_v);
        if !whitelist.is_empty() && !whitelist.iter().any(|s| s == q) {
            return Err(MediaGovernanceError::QualityNotWhitelisted {
                quality: q.to_string(),
                whitelist,
            });
        }
    }

    if let (Some(s), Some(wl_v)) = (req.style, g.get("style_whitelist")) {
        let whitelist = as_string_array(wl_v);
        if !whitelist.is_empty() && !whitelist.iter().any(|x| x == s) {
            return Err(MediaGovernanceError::StyleNotWhitelisted {
                style: s.to_string(),
                whitelist,
            });
        }
    }

    if let Some(max) = g.get("max_n").and_then(|v| v.as_u64()) {
        if req.n as u64 > max {
            return Err(MediaGovernanceError::ImageCountExceeded {
                n: req.n,
                max: max as u32,
            });
        }
    }

    if let Some(cap) = g.get("max_prompt_characters").and_then(|v| v.as_u64()) {
        let cap = cap as usize;
        if req.prompt_chars > cap {
            return Err(MediaGovernanceError::PromptTooLong {
                chars: req.prompt_chars,
                cap,
            });
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Mode: video_generation
// ---------------------------------------------------------------------------

pub struct VideoGenerationRequest<'a> {
    pub duration_seconds: Option<u32>,
    pub resolution: Option<&'a str>,
    pub has_image_input: bool,
    pub prompt_chars: usize,
}

pub fn enforce_video_generation(
    governance: Option<&Value>,
    req: &VideoGenerationRequest<'_>,
) -> Result<(), MediaGovernanceError> {
    let Some(g) = governance else {
        return Ok(());
    };

    if let Some(max) = g.get("max_duration_seconds").and_then(|v| v.as_u64()) {
        if let Some(dur) = req.duration_seconds {
            if dur as u64 > max {
                return Err(MediaGovernanceError::DurationExceeded {
                    duration: dur,
                    max: max as u32,
                });
            }
        }
    }

    if let (Some(res), Some(wl_v)) = (req.resolution, g.get("resolution_whitelist")) {
        let whitelist = as_string_array(wl_v);
        if !whitelist.is_empty() && !whitelist.iter().any(|r| r == res) {
            return Err(MediaGovernanceError::ResolutionNotWhitelisted {
                resolution: res.to_string(),
                whitelist,
            });
        }
    }

    if g.get("require_image_input")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
        && !req.has_image_input
    {
        return Err(MediaGovernanceError::ImageInputRequired);
    }

    if let Some(cap) = g.get("max_prompt_characters").and_then(|v| v.as_u64()) {
        let cap = cap as usize;
        if req.prompt_chars > cap {
            return Err(MediaGovernanceError::PromptTooLong {
                chars: req.prompt_chars,
                cap,
            });
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Mode: embedding
// ---------------------------------------------------------------------------

pub struct EmbeddingRequest {
    pub dim: Option<u32>,
    pub batch_size: u32,
}

pub fn enforce_embedding(
    governance: Option<&Value>,
    req: &EmbeddingRequest,
) -> Result<(), MediaGovernanceError> {
    let Some(g) = governance else {
        return Ok(());
    };

    if let (Some(dim), Some(wl_v)) = (req.dim, g.get("dim_whitelist")) {
        let whitelist = as_int_array(wl_v);
        if !whitelist.is_empty() && !whitelist.contains(&dim) {
            return Err(MediaGovernanceError::DimNotWhitelisted { dim, whitelist });
        }
    }

    if let Some(max) = g.get("max_batch_size").and_then(|v| v.as_u64()) {
        if req.batch_size as u64 > max {
            return Err(MediaGovernanceError::BatchTooLarge {
                n: req.batch_size,
                max: max as u32,
            });
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn no_governance_passes_everything() {
        let req = AudioSpeechRequest {
            voice: "alloy",
            response_format: "mp3",
            input_chars: 1000,
            speed: 1.0,
        };
        assert!(enforce_audio_speech(None, &req).is_ok());
    }

    #[test]
    fn audio_speech_voice_whitelist_rejects_outside() {
        let gov = json!({"voice_whitelist": ["alloy", "echo"]});
        let req = AudioSpeechRequest {
            voice: "fable",
            response_format: "mp3",
            input_chars: 100,
            speed: 1.0,
        };
        assert!(matches!(
            enforce_audio_speech(Some(&gov), &req),
            Err(MediaGovernanceError::VoiceNotWhitelisted { .. })
        ));
    }

    #[test]
    fn audio_speech_voice_whitelist_accepts_inside() {
        let gov = json!({"voice_whitelist": ["alloy", "echo"]});
        let req = AudioSpeechRequest {
            voice: "alloy",
            response_format: "mp3",
            input_chars: 100,
            speed: 1.0,
        };
        assert!(enforce_audio_speech(Some(&gov), &req).is_ok());
    }

    #[test]
    fn audio_speech_format_whitelist_case_insensitive() {
        let gov = json!({"response_format_whitelist": ["mp3", "wav"]});
        let req = AudioSpeechRequest {
            voice: "alloy",
            response_format: "MP3",
            input_chars: 100,
            speed: 1.0,
        };
        assert!(enforce_audio_speech(Some(&gov), &req).is_ok());
    }

    #[test]
    fn audio_speech_input_cap_enforced() {
        let gov = json!({"max_input_characters": 4000});
        let req = AudioSpeechRequest {
            voice: "alloy",
            response_format: "mp3",
            input_chars: 5000,
            speed: 1.0,
        };
        assert!(matches!(
            enforce_audio_speech(Some(&gov), &req),
            Err(MediaGovernanceError::InputTooLong {
                chars: 5000,
                cap: 4000
            })
        ));
    }

    #[test]
    fn audio_speech_speed_range_enforced() {
        let gov = json!({"speed_min": 0.5, "speed_max": 1.5});
        for (speed, ok) in [
            (0.4, false),
            (0.5, true),
            (1.0, true),
            (1.5, true),
            (1.6, false),
        ] {
            let req = AudioSpeechRequest {
                voice: "alloy",
                response_format: "mp3",
                input_chars: 100,
                speed,
            };
            assert_eq!(
                enforce_audio_speech(Some(&gov), &req).is_ok(),
                ok,
                "speed={speed}",
            );
        }
    }

    #[test]
    fn empty_whitelist_means_unconstrained() {
        // An explicit empty list shouldn't reject everything;
        // operators use it as "no restriction at this layer".
        let gov = json!({"voice_whitelist": []});
        let req = AudioSpeechRequest {
            voice: "alloy",
            response_format: "mp3",
            input_chars: 100,
            speed: 1.0,
        };
        assert!(enforce_audio_speech(Some(&gov), &req).is_ok());
    }

    #[test]
    fn audio_transcription_language_allowlist() {
        let gov = json!({"language_allowlist": ["en", "sv"]});
        let req_ok = AudioTranscriptionRequest {
            language: Some("en"),
            response_format: None,
            audio_size_bytes: 1024,
        };
        let req_bad = AudioTranscriptionRequest {
            language: Some("fr"),
            response_format: None,
            audio_size_bytes: 1024,
        };
        assert!(enforce_audio_transcription(Some(&gov), &req_ok).is_ok());
        assert!(matches!(
            enforce_audio_transcription(Some(&gov), &req_bad),
            Err(MediaGovernanceError::LanguageNotAllowed { .. })
        ));
    }

    #[test]
    fn audio_transcription_audio_size_cap() {
        let gov = json!({"max_audio_size_mb": 5.0});
        let small = AudioTranscriptionRequest {
            language: None,
            response_format: None,
            audio_size_bytes: 4 * 1024 * 1024, // 4 MB
        };
        let big = AudioTranscriptionRequest {
            language: None,
            response_format: None,
            audio_size_bytes: 6 * 1024 * 1024, // 6 MB
        };
        assert!(enforce_audio_transcription(Some(&gov), &small).is_ok());
        assert!(matches!(
            enforce_audio_transcription(Some(&gov), &big),
            Err(MediaGovernanceError::AudioTooLarge { .. })
        ));
    }

    #[test]
    fn image_generation_size_whitelist() {
        let gov = json!({"size_whitelist": ["1024x1024"]});
        let bad = ImageGenerationRequest {
            size: Some("512x512"),
            quality: None,
            style: None,
            n: 1,
            prompt_chars: 10,
        };
        assert!(matches!(
            enforce_image_generation(Some(&gov), &bad),
            Err(MediaGovernanceError::SizeNotWhitelisted { .. })
        ));
    }

    #[test]
    fn image_generation_max_n_enforced() {
        let gov = json!({"max_n": 1});
        let bad = ImageGenerationRequest {
            size: None,
            quality: None,
            style: None,
            n: 4,
            prompt_chars: 10,
        };
        assert!(matches!(
            enforce_image_generation(Some(&gov), &bad),
            Err(MediaGovernanceError::ImageCountExceeded { n: 4, max: 1 })
        ));
    }

    #[test]
    fn video_generation_duration_cap() {
        let gov = json!({"max_duration_seconds": 9});
        let ok = VideoGenerationRequest {
            duration_seconds: Some(5),
            resolution: None,
            has_image_input: false,
            prompt_chars: 10,
        };
        let bad = VideoGenerationRequest {
            duration_seconds: Some(20),
            resolution: None,
            has_image_input: false,
            prompt_chars: 10,
        };
        assert!(enforce_video_generation(Some(&gov), &ok).is_ok());
        assert!(matches!(
            enforce_video_generation(Some(&gov), &bad),
            Err(MediaGovernanceError::DurationExceeded {
                duration: 20,
                max: 9
            })
        ));
    }

    #[test]
    fn video_generation_require_image_input() {
        let gov = json!({"require_image_input": true});
        let bad = VideoGenerationRequest {
            duration_seconds: Some(5),
            resolution: None,
            has_image_input: false,
            prompt_chars: 10,
        };
        let ok = VideoGenerationRequest {
            duration_seconds: Some(5),
            resolution: None,
            has_image_input: true,
            prompt_chars: 10,
        };
        assert!(matches!(
            enforce_video_generation(Some(&gov), &bad),
            Err(MediaGovernanceError::ImageInputRequired)
        ));
        assert!(enforce_video_generation(Some(&gov), &ok).is_ok());
    }

    #[test]
    fn embedding_dim_whitelist() {
        let gov = json!({"dim_whitelist": [512, 1024, 1536]});
        assert!(
            enforce_embedding(
                Some(&gov),
                &EmbeddingRequest {
                    dim: Some(1024),
                    batch_size: 1,
                }
            )
            .is_ok()
        );
        assert!(matches!(
            enforce_embedding(
                Some(&gov),
                &EmbeddingRequest {
                    dim: Some(768),
                    batch_size: 1,
                }
            ),
            Err(MediaGovernanceError::DimNotWhitelisted { .. })
        ));
    }

    #[test]
    fn embedding_batch_size_cap() {
        let gov = json!({"max_batch_size": 32});
        assert!(matches!(
            enforce_embedding(
                Some(&gov),
                &EmbeddingRequest {
                    dim: None,
                    batch_size: 100,
                }
            ),
            Err(MediaGovernanceError::BatchTooLarge { n: 100, max: 32 })
        ));
    }

    #[test]
    fn governance_for_mode_returns_per_mode_dict() {
        let global = json!({
            "audio_speech": {"voice_whitelist": ["alloy"]},
            "video_generation": {"max_duration_seconds": 9}
        });
        let mode = governance_for_mode(Some(&global), "audio_speech").unwrap();
        assert_eq!(mode.get("voice_whitelist").unwrap()[0], "alloy");
        assert!(governance_for_mode(Some(&global), "missing_mode").is_none());
    }

    #[test]
    fn merge_media_governance_applies_per_field_rules() {
        // child (base) ⟵ parent. Most-restrictive per field type.
        let mut base = json!({
            "audio_speech": {
                "voice_whitelist": ["alloy", "nova", "echo"],
                "max_input_characters": 5000,
                "speed_max": 2.0,
                "speed_min": 0.5
            },
            "video_generation": { "require_image_input": false }
        });
        let parent = json!({
            "audio_speech": {
                "voice_whitelist": ["alloy", "nova"], // intersection → [alloy, nova]
                "max_input_characters": 1000,         // ceiling → min(5000,1000)=1000
                "speed_max": 1.5,                     // ceiling → min(2.0,1.5)=1.5
                "speed_min": 0.8                      // floor → max(0.5,0.8)=0.8
            },
            "video_generation": { "require_image_input": true }, // bool OR → true
            "image_generation": { "max_n": 3 }                   // parent-only mode → inherited
        });
        merge_media_governance(&mut base, &parent);

        let sp = &base["audio_speech"];
        let voices: Vec<&str> = sp["voice_whitelist"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(voices, vec!["alloy", "nova"], "whitelist intersection");
        assert_eq!(sp["max_input_characters"], 1000, "ceiling = min");
        assert_eq!(sp["speed_max"], 1.5, "ceiling = min");
        assert_eq!(sp["speed_min"], 0.8, "floor = max");
        assert_eq!(
            base["video_generation"]["require_image_input"], true,
            "bool OR"
        );
        assert_eq!(
            base["image_generation"]["max_n"], 3,
            "parent-only mode inherited"
        );
    }
}
