# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the mode-aware health-check probe helpers in
`app.services.llm.model.llm_models_manager`.

Coverage:
  * `_PROBE_SKIP_MODES` matches the documented set (rerank,
    moderation, routing) — full proxy probe dispatch is keyed off
    this so a regression here would silently skip live modes.
  * `_looks_like_validation_400` recognises validation rejections
    (the image / video probes rely on this to distinguish
    "upstream rejected our intentional bad input" from real auth /
    quota failures).
  * `_SILENT_WAV_BYTES` is a well-formed RIFF/WAVE container —
    Whisper-style transcription probes upload it and need the
    upstream parser to accept the file before returning a (likely
    empty) transcription.
"""

import os
import struct

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.services.llm.model.llm_models_manager import (
    _PROBE_SKIP_MODES,
    _SILENT_WAV_BYTES,
    _build_silent_wav,
    _looks_like_validation_400,
)


class TestSkipModes:
    def test_skip_modes_exact_set(self):
        assert _PROBE_SKIP_MODES == {"rerank", "moderation", "routing"}

    def test_chat_mode_not_skipped(self):
        assert "chat" not in _PROBE_SKIP_MODES

    def test_image_audio_video_not_skipped(self):
        # These have proxy probes and must NOT be in the skip set.
        for mode in (
            "image_generation",
            "audio_speech",
            "audio_transcription",
            "video_generation",
            "embedding",
            "completion",
            "responses",
        ):
            assert mode not in _PROBE_SKIP_MODES, (
                f"{mode!r} must have a real probe path"
            )


class TestValidation400Heuristic:
    @pytest.mark.parametrize(
        "body",
        [
            "n must be greater than 0",
            '{"error":{"message":"n must be at least 1","type":"invalid_request_error"}}',
            "duration_seconds must be > 0",
            '{"error":{"code":"invalid_value","message":"out of range"}}',
            "Parameter 'size' is required",
            "between 1 and 10",
        ],
    )
    def test_recognises_validation_shapes(self, body):
        assert _looks_like_validation_400(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            # Auth failure — must NOT be classified as a healthy probe.
            "You didn't provide an API key. You need to provide your API key in an Authorization header.",
            "Incorrect API key provided",
            '{"error":{"message":"Unauthorized","type":"auth_error"}}',
            # Quota / rate-limit — also NOT healthy.
            "You have exceeded your monthly quota.",
        ],
    )
    def test_does_not_misclassify_auth_or_quota(self, body):
        assert _looks_like_validation_400(body) is False

    def test_empty_string_is_not_validation(self):
        assert _looks_like_validation_400("") is False

    def test_none_is_not_validation(self):
        assert _looks_like_validation_400(None) is False  # type: ignore[arg-type]


class TestSilentWav:
    def test_starts_with_riff_header(self):
        assert _SILENT_WAV_BYTES[:4] == b"RIFF"
        assert _SILENT_WAV_BYTES[8:12] == b"WAVE"

    def test_has_fmt_and_data_chunks(self):
        assert b"fmt " in _SILENT_WAV_BYTES
        assert b"data" in _SILENT_WAV_BYTES

    def test_chunk_size_field_consistent(self):
        # RIFF chunk size = 36 + data_size for PCM (8 fmt header +
        # 16 fmt body + 8 data header + N data bytes − 8 RIFF header).
        chunk_size = struct.unpack("<I", _SILENT_WAV_BYTES[4:8])[0]
        assert chunk_size == len(_SILENT_WAV_BYTES) - 8

    def test_pcm_format(self):
        # fmt chunk: 4 byte tag + 4 byte size + 2 byte format + 2 byte channels + ...
        # The format tag at offset 20 should be 1 (PCM).
        fmt_tag = struct.unpack("<H", _SILENT_WAV_BYTES[20:22])[0]
        assert fmt_tag == 1, "expected PCM format tag (1), got {}".format(fmt_tag)

    def test_compact_payload(self):
        # 1 second of 16-bit mono PCM at 16 kHz = 44-byte header +
        # 32,000 sample bytes. We need ≥0.1 s to clear OpenAI
        # Whisper's `audio_too_short` floor (see comment in
        # `_build_silent_wav` for the upgrade history); 1 s gives
        # comfortable headroom across providers without bloating the
        # multipart payload meaningfully (~32 KB inline). Anything
        # over ~64 KB would be strange for a silent probe clip.
        assert 1024 <= len(_SILENT_WAV_BYTES) <= 64 * 1024

    def test_module_constant_matches_builder(self):
        # Defensive: if someone refactors `_build_silent_wav` but
        # forgets to regenerate the cached constant, this test will
        # catch it before a probe runs against a malformed file.
        assert _SILENT_WAV_BYTES == _build_silent_wav()
