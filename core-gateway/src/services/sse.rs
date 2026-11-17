// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Incremental SSE (Server-Sent Events) decoding shared by the streaming
//! proxy forwarders (LLM chat-completions, LLM Responses, Anthropic Messages).
//!
//! It fixes two correctness bugs the ad-hoc per-forwarder code had:
//!
//! 1. **UTF-8 safety.** Network chunks split multibyte codepoints at arbitrary
//!    byte boundaries. The old code did
//!    `buffer.push_str(&String::from_utf8_lossy(&bytes))` per chunk, so a
//!    codepoint straddling two chunks decoded to `U+FFFD` replacement chars on
//!    BOTH sides — corrupting CJK / emoji output. Here we buffer raw bytes and
//!    decode only **complete events**: event separators are ASCII
//!    (`\n\n` / `\r\n\r\n`), so a complete event body never ends mid-codepoint.
//!
//! 2. **CRLF + `data:`-without-space.** The SSE spec allows `\r\n\r\n` event
//!    separators and a single optional space after a field's colon. The old
//!    `find("\n\n")` + `strip_prefix("data: ")` missed both, so a spec-legal
//!    upstream emitting CRLF never parsed (the buffer grew until the safety cap
//!    aborted the stream).

/// Incremental SSE byte buffer. Push raw network chunks; pop complete,
/// UTF-8-decoded event bodies.
#[derive(Default)]
pub struct SseByteBuffer {
    buf: Vec<u8>,
}

impl SseByteBuffer {
    pub fn new() -> Self {
        Self { buf: Vec::new() }
    }

    /// Append raw bytes from the wire.
    pub fn push(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
    }

    /// Pop the next complete event body (the text between separators), already
    /// UTF-8 decoded, or `None` if no full event is buffered yet. The trailing
    /// separator is consumed and not included in the returned body.
    pub fn next_event(&mut self) -> Option<String> {
        let (start, delim_len) = find_event_boundary(&self.buf)?;
        // Drain through the end of the separator; keep only the body.
        let drained: Vec<u8> = self.buf.drain(..start + delim_len).collect();
        // The body is complete (separator is ASCII), so this never splits a
        // codepoint; lossy decode only guards against a genuinely malformed
        // upstream rather than chunk-boundary artefacts.
        Some(String::from_utf8_lossy(&drained[..start]).into_owned())
    }

    /// Bytes currently buffered (an incomplete trailing event).
    pub fn buffered_len(&self) -> usize {
        self.buf.len()
    }
}

/// Find the first SSE event separator — `\n\n` or `\r\n\r\n` — returning
/// `(start_index, separator_len)` for the earliest-starting one.
fn find_event_boundary(buf: &[u8]) -> Option<(usize, usize)> {
    let lf = find_subslice(buf, b"\n\n");
    let crlf = find_subslice(buf, b"\r\n\r\n");
    match (lf, crlf) {
        (Some(a), Some(b)) => {
            if a <= b {
                Some((a, 2))
            } else {
                Some((b, 4))
            }
        }
        (Some(a), None) => Some((a, 2)),
        (None, Some(b)) => Some((b, 4)),
        (None, None) => None,
    }
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Extract the value of an SSE field line (`<name>: <value>`), stripping the
/// field name, the colon, and a single optional leading space per the SSE
/// spec. Returns `None` if the line is not that field.
///
/// Handles `data: x`, `data:x`, and `data:` (empty) — the old
/// `strip_prefix("data: ")` silently dropped the latter two.
///
/// Intra-event line endings (`\r\n`) are already handled by callers iterating
/// with `str::lines()`, which strips a trailing `\r`.
pub fn sse_field<'a>(line: &'a str, name: &str) -> Option<&'a str> {
    let rest = line.strip_prefix(name)?.strip_prefix(':')?;
    Some(rest.strip_prefix(' ').unwrap_or(rest))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_lf_separated_events() {
        let mut b = SseByteBuffer::new();
        b.push(b"data: one\n\ndata: two\n\n");
        assert_eq!(b.next_event().as_deref(), Some("data: one"));
        assert_eq!(b.next_event().as_deref(), Some("data: two"));
        assert_eq!(b.next_event(), None);
    }

    #[test]
    fn parses_crlf_separated_events() {
        // Spec-legal CRLF — the old find("\n\n") never matched these.
        let mut b = SseByteBuffer::new();
        b.push(b"data: one\r\n\r\ndata: two\r\n\r\n");
        assert_eq!(b.next_event().as_deref(), Some("data: one"));
        assert_eq!(b.next_event().as_deref(), Some("data: two"));
        assert_eq!(b.next_event(), None);
    }

    #[test]
    fn waits_for_complete_event() {
        let mut b = SseByteBuffer::new();
        b.push(b"data: par");
        assert_eq!(b.next_event(), None, "no separator yet");
        b.push(b"tial\n\n");
        assert_eq!(b.next_event().as_deref(), Some("data: partial"));
    }

    #[test]
    fn multibyte_codepoint_split_across_chunks_is_not_corrupted() {
        // "café 🚀" — the é (2 bytes) and 🚀 (4 bytes) are split mid-codepoint
        // across pushes. The old per-chunk from_utf8_lossy produced U+FFFD;
        // here the event only decodes once complete.
        let full = "data: café 🚀";
        let bytes = format!("{full}\n\n").into_bytes();
        let mut b = SseByteBuffer::new();
        // Split at a byte that lands inside the 🚀 (last 4 bytes).
        let split = bytes.len() - 4; // mid-emoji
        b.push(&bytes[..split]);
        assert_eq!(b.next_event(), None);
        b.push(&bytes[split..]);
        assert_eq!(b.next_event().as_deref(), Some(full));
    }

    #[test]
    fn multibyte_split_at_every_boundary_roundtrips() {
        let full = "data: 日本語のテスト 🎉🚀✨";
        let bytes = format!("{full}\n\n").into_bytes();
        for split in 1..bytes.len() {
            let mut b = SseByteBuffer::new();
            b.push(&bytes[..split]);
            let first = b.next_event();
            b.push(&bytes[split..]);
            let second = b.next_event();
            let got = first.or(second);
            assert_eq!(got.as_deref(), Some(full), "split at {split}");
        }
    }

    #[test]
    fn sse_field_handles_optional_space_and_empty() {
        assert_eq!(sse_field("data: x", "data"), Some("x"));
        assert_eq!(sse_field("data:x", "data"), Some("x"));
        assert_eq!(sse_field("data:", "data"), Some(""));
        assert_eq!(sse_field("data:  x", "data"), Some(" x")); // only ONE space stripped
        assert_eq!(sse_field("event: msg", "event"), Some("msg"));
        assert_eq!(sse_field("database: x", "data"), None);
        assert_eq!(sse_field(": comment", "data"), None);
    }

    #[test]
    fn earliest_boundary_wins_when_mixed() {
        let mut b = SseByteBuffer::new();
        b.push(b"a\n\nb\r\n\r\n");
        assert_eq!(b.next_event().as_deref(), Some("a"));
        assert_eq!(b.next_event().as_deref(), Some("b"));
    }
}
