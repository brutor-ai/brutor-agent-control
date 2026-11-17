# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
AssemblyAI Models — Speech-to-Text (ASR) with diarization, sentiment,
                  and entity-detection extras.

Source: https://www.assemblyai.com/docs/
        https://www.assemblyai.com/pricing
Model prefix: assemblyai/ (legacy string-mode IDs).
                      The Brutor Rust adapter in
                      `routes/audio_transcribe.rs::call_assemblyai_transcribe`
                      drives a 3-step async flow (upload → submit →
                      poll) directly against `api.assemblyai.com`
                      with `Authorization: <api_key>` (raw, not
                      "Bearer").

Auth env var: ASSEMBLYAI_API_KEY

Pricing (AssemblyAI pricing page, read 2026-11-11):
  * "universal-3-5-pro" — $0.21/audio hour = $0.0035/minute
  * "universal-2"       — $0.15/audio hour = $0.0025/minute

The older values "best", "nano" and "slam-1" are deprecated and no longer
seeded.

The proxy reads `cost_per_audio_minute` directly; we surface both
the per-minute and per-hour numbers in the description for
operators reading the catalog.

`supported_input_formats` mirrors what the portal MicRecorder is
likely to upload (webm/opus from MediaRecorder by default; mp3/m4a
from file uploads). `max_audio_duration_seconds` is set to 6 hours,
matching AssemblyAI's published prerecorded-upload limit.
"""

from app.constants.llm import LLModelMode

# AssemblyAI accepts a wide set of containers; we list the ones a
# portal MicRecorder upload or a typical file picker would produce.
ASSEMBLYAI_INPUT_FORMATS = [
    "mp3", "mp4", "m4a", "wav", "webm", "ogg", "flac", "opus",
]

ASSEMBLYAI_MAX_DURATION_SECS = 6 * 3600  # 6 h prerecorded cap


def get_models():
    return [
        {
            "model_name": "universal-3-5-pro",
            "name": "AssemblyAI Universal-3.5 Pro",
            "description": (
                "AssemblyAI Universal-3.5 Pro, the replacement for the "
                "deprecated `best` and `slam-1` values. $0.21 per hour; 18 "
                "languages. AssemblyAI now takes the model as `speech_models`"
                " (a list); the singular `speech_model` is deprecated."
            ),
            "usage": "Transcription of recorded audio",
            "mode": LLModelMode.AUDIO_TRANSCRIPTION.value,
            "timeout": 600.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_audio_minute": 0.0035,
            "supported_input_formats": ASSEMBLYAI_INPUT_FORMATS,
            "max_audio_duration_seconds": ASSEMBLYAI_MAX_DURATION_SECS,
            "capabilities": {
                "modalities": ["audio"],
                "languages": 18,
            },
            "tags": ["audio-stt", "assemblyai"],
        },
        {
            "model_name": "universal-2",
            "name": "AssemblyAI Universal-2",
            "description": (
                "AssemblyAI Universal-2, the replacement for the deprecated "
                "`nano` value. $0.15 per hour; 99 languages."
            ),
            "usage": "Transcription of recorded audio",
            "mode": LLModelMode.AUDIO_TRANSCRIPTION.value,
            "timeout": 600.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_audio_minute": 0.0025,
            "supported_input_formats": ASSEMBLYAI_INPUT_FORMATS,
            "max_audio_duration_seconds": ASSEMBLYAI_MAX_DURATION_SECS,
            "capabilities": {
                "modalities": ["audio"],
                "languages": 99,
            },
            "tags": ["audio-stt", "assemblyai"],
        },
    ]
