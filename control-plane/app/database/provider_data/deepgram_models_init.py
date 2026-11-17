# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Deepgram Models — Speech-to-Text (ASR).

        https://deepgram.com/pricing
Model prefix: deepgram/ (required)
Auth env var: DEEPGRAM_API_KEY

Pricing: Per audio minute (billed per-second).
         Nova-3: $0.0077/min streaming, ~$0.0059/min batch (PAYG).
         Nova-2: $0.0043/min batch.
         $200 free credits for new accounts (~25K–45K minutes).
Cost values below: Set to 0.0 (billed per audio minute, not tokens).
"""

from app.constants.llm import LLModelMode

# Deepgram accepts most common audio container formats. The proxy
# whitelist mirrors what Whisper-1 accepts so the portal MicRecorder
# blob (typically webm/opus) hits the validator regardless of which
# STT model the user picks.
DEEPGRAM_STT_FORMATS = ["mp3", "mp4", "m4a", "wav", "webm", "ogg", "flac"]

# Deepgram doesn't publish a hard duration cap on prerecorded
# uploads, but file uploads are capped at 2 GB ≈ ~5 hours of
# 16-bit PCM. We set a 6-hour validator-side cap as a sanity
# check; operators can raise it via the Media tab.
DEEPGRAM_MAX_DURATION_SECS = 6 * 3600


def get_models():
    return [
        {
            "model_name": "deepgram/nova-3",
            "parameters": 0.0,  # Undisclosed
            "name": "Nova-3",
            "description": (
                "Deepgram's latest and most accurate STT model. "
                "54% reduction in streaming WER vs Nova-2 (6.84% streaming, 5.26% batch). "
                "Real-time multilingual code-switching. "
                "~$0.0077/min streaming, ~$0.0059/min batch."
            ),
            "usage": "Contact centers, voice agents, live captioning, multilingual transcription",
            "mode": LLModelMode.AUDIO_TRANSCRIPTION.value,
            "cost_per_input_token": 0.0,  # Billed per audio minute
            "cost_per_output_token": 0.0,
            # Deepgram batch can take ~30 sec per hour of audio; allow
            # 5 min for long uploads. Streaming holds the connection
            # open for the duration of the input — separate machinery,
            # this timeout protects against hung HTTP for non-streaming.
            # Deepgram pay-as-you-go list rate (per input minute).
            "cost_per_audio_minute": 0.0043,
            "timeout": 300.0,
            "supported_input_formats": DEEPGRAM_STT_FORMATS,
            "max_audio_duration_seconds": DEEPGRAM_MAX_DURATION_SECS,
            "capabilities": {
                "languages": 7,
                "modalities": ["audio"],
                "speaker_diarization": True,
                "word_timestamps": True,
                "smart_formatting": True,
                "streaming": True,
                "latency_ms": 300,
            },
            "tags": ["stt", "flagship", "real-time", "low-latency"],
        },
        {
            "model_name": "deepgram/nova-2",
            "parameters": 0.0,
            "name": "Nova-2",
            "description": (
                "High-performance STT with excellent speed-per-dollar ratio. "
                "Median inference: 29.8s per hour of diarized audio. "
                "36 languages. Best for high-volume English batch processing. "
                "~$0.0043/min batch."
            ),
            "usage": "Batch transcription, podcast processing, call analytics, high-volume English",
            "mode": LLModelMode.AUDIO_TRANSCRIPTION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_audio_minute": 0.0043,
            # Deepgram pay-as-you-go list rate (per input minute).
            "cost_per_audio_minute": 0.0043,
            "timeout": 300.0,
            "supported_input_formats": DEEPGRAM_STT_FORMATS,
            "max_audio_duration_seconds": DEEPGRAM_MAX_DURATION_SECS,
            "capabilities": {
                "languages": 36,
                "modalities": ["audio"],
                "speaker_diarization": True,
                "word_timestamps": True,
                "smart_formatting": True,
                "streaming": True,
            },
            "tags": ["stt", "production", "cost-efficient", "high-volume"],
        },
        {
            "model_name": "deepgram/whisper-large",
            "parameters": 1550.0,  # Whisper Large v2
            "name": "Deepgram Whisper Large",
            "description": (
                "OpenAI Whisper Large v2 hosted on Deepgram infrastructure. "
                "3x faster than OpenAI's Whisper API with ~7.4% fewer word errors. "
                "~$0.0048/min. Good for multilingual workloads needing Whisper compatibility."
            ),
            "usage": "Multilingual transcription, Whisper-compatible workflows",
            "mode": LLModelMode.AUDIO_TRANSCRIPTION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_audio_minute": 0.0048,
            # Deepgram pay-as-you-go list rate (per input minute).
            "cost_per_audio_minute": 0.0048,
            "timeout": 300.0,
            "supported_input_formats": DEEPGRAM_STT_FORMATS,
            "max_audio_duration_seconds": DEEPGRAM_MAX_DURATION_SECS,
            "capabilities": {
                "languages": 99,
                "modalities": ["audio"],
                "streaming": False,
            },
            "tags": ["stt", "whisper", "multilingual"],
        },
    ]
