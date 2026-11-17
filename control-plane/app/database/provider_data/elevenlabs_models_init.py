# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
ElevenLabs Models — Text-to-Speech and Speech-to-Text.

        https://elevenlabs.io/docs/overview/models
Model prefix: elevenlabs/ (required)
Auth env var: ELEVENLABS_API_KEY

TTS pricing: Per character (~$0.12–$0.30 per 1K chars depending on plan).
             Flash/Turbo models are 50% cheaper per character.
STT pricing: Per audio hour (~$0.22–$0.40/hr depending on plan).

Multimodal columns (`cost_per_audio_char`, `cost_per_audio_minute`,
`supported_voices`, `supported_audio_formats`,
`supported_input_formats`) are populated at the top level so the
proxy validators in `routes/audio_speech.rs` and
`routes/audio_transcribe.rs` can enforce them without parsing
JSONB. The seeded `supported_voices` list is the stock ElevenLabs
public voice set; operators with custom voice IDs can extend it
via the admin UI.
"""

from app.constants.llm import LLModelMode

# ElevenLabs publishes a small public voice catalogue that ships
# with every account. Seeding these so the portal voice picker
# shows something real out of the box. Operators can append their
# own voice ids via the admin UI (Media tab → "supported voices").
ELEVENLABS_STOCK_VOICES = [
    "21m00Tcm4TlvDq8ikWAM",  # Rachel
    "AZnzlk1XvdvUeBnXmlld",  # Domi
    "EXAVITQu4vr4xnSDxMaL",  # Bella
    "ErXwobaYiN019PkySvjV",  # Antoni
    "MF3mGyEYCl7XYWbV9V6O",  # Elli
    "TxGEqnHWrfWFTfGW9XjX",  # Josh
    "VR6AewLTigWG4xSOukaG",  # Arnold
    "pNInz6obpgDQGcFmaJgB",  # Adam
    "yoZ06aMxZJJ28mfd3POQ",  # Sam
]

# `routes/audio_speech.rs::elevenlabs_output_format` currently
# maps mp3 / opus / pcm. We list those at the top level so the
# proxy whitelist matches the adapter's reality.
ELEVENLABS_TTS_FORMATS = ["mp3", "opus", "pcm"]

# ElevenLabs Speech-to-Text accepts most common audio container
# formats. We mirror the same set Whisper-1 accepts so a user
# uploading from the portal MicRecorder hits the whitelist
# regardless of which STT model they chose.
ELEVENLABS_STT_FORMATS = ["mp3", "mp4", "m4a", "wav", "webm", "ogg", "flac"]


def get_models():
    return [
        # =================================================================
        # TEXT-TO-SPEECH MODELS
        # =================================================================
        {
            "model_name": "elevenlabs/eleven_v3",
            "parameters": 0.0,  # Undisclosed
            "name": "Eleven v3",
            "description": (
                "ElevenLabs' latest and most advanced speech synthesis model. "
                "Natural, life-like speech with high emotional range and contextual "
                "understanding. Supports audio tags for sound effects and pauses. "
                "70+ languages. 5,000 character limit per request."
            ),
            "usage": "Audiobooks, character voiceovers, emotional dialogue, multi-speaker content",
            "mode": LLModelMode.AUDIO_SPEECH.value,
            "cost_per_input_token": 0.0,  # Billed per character, not tokens
            "cost_per_output_token": 0.0,
            # Long-form TTS — 5000 chars can take ~30 sec to render.
            # ESTIMATE ~$0.10/1k chars (Pro-tier equivalent); ElevenLabs bills by subscription credits —
            # verify against your plan.
            "cost_per_audio_char": 0.00008,
            "timeout": 120.0,
            "supported_voices": ELEVENLABS_STOCK_VOICES,
            "supported_audio_formats": ELEVENLABS_TTS_FORMATS,
            "capabilities": {
                "languages": 70,
                "max_characters": 5000,
                "modalities": ["text"],
                "audio_tags": True,
            },
            "tags": ["tts", "flagship", "emotional", "multilingual"],
        },
        {
            "model_name": "elevenlabs/eleven_multilingual_v2",
            "parameters": 0.0,
            "name": "Eleven Multilingual v2",
            "description": (
                "Emotionally-aware speech synthesis with consistent voice quality. "
                "Best for long-form voiceovers, audiobooks, and professional content. "
                "29 languages. 10,000 character limit. Most stable on long-form."
            ),
            "usage": "Professional voiceovers, audiobooks, e-learning, corporate videos",
            "mode": LLModelMode.AUDIO_SPEECH.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            # 10 000 chars is long-form; render can take 60-90 sec.
            # ESTIMATE ~$0.10/1k chars; ElevenLabs bills by subscription credits —
            # verify against your plan.
            "cost_per_audio_char": 0.00008,
            "timeout": 180.0,
            "supported_voices": ELEVENLABS_STOCK_VOICES,
            "supported_audio_formats": ELEVENLABS_TTS_FORMATS,
            "capabilities": {
                "languages": 29,
                "max_characters": 10000,
                "modalities": ["text"],
            },
            "tags": ["tts", "production", "multilingual", "stable"],
        },
        {
            "model_name": "elevenlabs/eleven_flash_v2_5",
            "parameters": 0.0,
            "name": "Eleven Flash v2.5",
            "description": (
                "Ultra-low latency TTS model (~75ms). Designed for real-time "
                "conversational AI and voice agents. 32 languages. "
                "40,000 character limit. 50% lower cost per character than standard."
            ),
            "usage": "Conversational AI, voice agents, chatbots, real-time applications",
            "mode": LLModelMode.AUDIO_SPEECH.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            # Real-time TTS — short clips, but allow headroom for retries.
            # ESTIMATE ~$0.05/1k chars (0.5 credits/char tier); ElevenLabs bills by subscription credits —
            # verify against your plan.
            "cost_per_audio_char": 0.00004,
            "timeout": 60.0,
            "supported_voices": ELEVENLABS_STOCK_VOICES,
            "supported_audio_formats": ELEVENLABS_TTS_FORMATS,
            "capabilities": {
                "languages": 32,
                "max_characters": 40000,
                "latency_ms": 75,
                "modalities": ["text"],
            },
            "tags": ["tts", "low-latency", "real-time", "cost-efficient"],
        },

        # =================================================================
        # SPEECH-TO-TEXT MODELS
        # =================================================================
        {
            "model_name": "elevenlabs/scribe_v2",
            "parameters": 0.0,
            "name": "Scribe v2",
            "description": (
                "Next-generation speech recognition with improved accuracy "
                "across 90+ languages. Enhanced features including entity detection "
                "and keyterm prompting. Also available in realtime mode (~150ms latency)."
            ),
            "usage": "Real-time transcription, AI agents, live captioning, meeting documentation",
            "mode": LLModelMode.AUDIO_TRANSCRIPTION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            # Realtime STT primary use; allow batch fallback.
            # ESTIMATE ~$0.40/hour published Scribe rate.
            "cost_per_audio_minute": 0.003333,
            "timeout": 300.0,
            "supported_input_formats": ELEVENLABS_STT_FORMATS,
            "max_audio_duration_seconds": 7200,
            "capabilities": {
                "languages": 90,
                "modalities": ["audio"],
                "speaker_diarization": True,
                "word_timestamps": True,
                "audio_event_tagging": True,
                "entity_detection": True,
                "keyterm_prompting": True,
            },
            "tags": ["stt", "flagship", "multilingual", "realtime-capable"],
        },
    ]
