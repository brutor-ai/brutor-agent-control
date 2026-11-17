// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The skill size limit, and the request limit derived from it.
//!
//! A skill is at most [`SKILL_MAX_BYTES`] of files, uncompressed, as
//! Anthropic's Agent Skills (30 MB per skill). The Control Plane checks it at
//! upload (`SKILL_MAX_BYTES` in `app/constants/agent_skill.py`, given to the
//! Admin Console as `limits.skill_max_bytes` by `GET /v1/admin/agent-skills`).
//! The gateway sends a skill's files to the skill runner in one JSON request,
//! a binary file base64-encoded, so the largest request is the largest skill
//! × 4/3 plus the JSON around the files: [`MAX_REQUEST_BYTES`]. The skill
//! runner (`src/config.rs`) writes the same three definitions;
//! `tests/skill_limits_contract.rs` holds all three repositories together.

/// The largest skill, in bytes of its files uncompressed: the Control Plane's
/// `SKILL_MAX_BYTES`, checked at upload.
pub const SKILL_MAX_BYTES: usize = 30_000_000;

/// What a request carries besides the files: the JSON around them (field
/// names, file names, the script's arguments, the quoting of text files).
pub const REQUEST_FRAMING_BYTES: usize = 1_048_576;

/// The largest request the gateway sends the skill runner, and the largest
/// the runner accepts (a larger one gets 413 there): the largest skill
/// base64-encoded (4 bytes for every 3, rounded up) plus the framing,
/// 30,000,000 × 4/3 = 40,000,000, + 1,048,576 = 41,048,576 bytes (about
/// 41 MB).
pub const MAX_REQUEST_BYTES: usize = SKILL_MAX_BYTES.div_ceil(3) * 4 + REQUEST_FRAMING_BYTES;

/// A request body for the runner, refused here when the runner would refuse
/// it for its size: the caller gets a reason that names the limit instead of
/// a 413 after the upload.
pub fn runner_request_body<T: serde::Serialize>(request: &T) -> Result<Vec<u8>, String> {
    body_within(request, MAX_REQUEST_BYTES)
}

fn body_within<T: serde::Serialize>(request: &T, limit: usize) -> Result<Vec<u8>, String> {
    let body = serde_json::to_vec(request).map_err(|e| format!("encode runner request: {e}"))?;
    if body.len() > limit {
        return Err(format!(
            "the skill is too large to run: its request to the skill runner is {} bytes, \
             more than the {limit} bytes the runner accepts (a skill is at most \
             {SKILL_MAX_BYTES} bytes of files)",
            body.len()
        ));
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_request_limit_is_the_largest_skill_base64_encoded_plus_framing() {
        assert_eq!(MAX_REQUEST_BYTES, 41_048_576);
    }

    #[test]
    fn a_request_up_to_the_limit_is_sent_one_byte_more_is_refused() {
        // `"…"`: the JSON string of n-2 characters is n bytes.
        let at_limit = "a".repeat(98);
        let body = body_within(&at_limit, 100).expect("at the limit");
        assert_eq!(body.len(), 100);

        let over = "a".repeat(99);
        let refusal = body_within(&over, 100).expect_err("one byte over");
        assert!(refusal.contains("101 bytes"), "{refusal}");
        assert!(refusal.contains("too large"), "{refusal}");
    }

    #[test]
    fn the_largest_skill_base64_encoded_fits() {
        // 30,000,000 bytes of one binary file, as the gateway sends it.
        let encoded = "A".repeat(SKILL_MAX_BYTES.div_ceil(3) * 4);
        let request = serde_json::json!({"assets": [{"filename": "big.bin", "content": encoded}]});
        assert!(runner_request_body(&request).is_ok());
    }
}
