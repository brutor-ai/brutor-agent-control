# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OpenAI Multimodal Models — Image generation, Text-to-Speech, Speech-to-Text.

Source: https://platform.openai.com/docs/models
Pricing: https://openai.com/api/pricing/

These models share the tenant's existing OpenAI API key (the same one
used for chat/responses/embedding), which makes them the cheapest path
to a working test baseline for image + audio + transcribe modalities.

One model per modality is seeded with `enabled=True` so the portal /
admin UI / health-check harness see a usable baseline out of the box —
matching the way `gpt-5.2` (chat), `gpt-5.5` (responses), and
`text-embedding-3-small` (embedding) are enabled by default in
`openai_models_init.py` / `openai_embedding_models_init.py`.

Cost values are in USD.
  * Image cost lives in the dedicated `cost_per_image` column added
    in migration a021_llm_model_image_columns. Sizes and qualities
    live in `supported_image_sizes` / `supported_image_qualities`
    so the proxy can validate at the edge without parsing JSONB.
  * Audio cost is per-character for TTS (OpenAI bills per character)
    and per-second for STT (Whisper bills per second of audio input).
"""

from datetime import date

from app.constants.llm import LLModelMode


def get_models():
    return [
        # =================================================================
        # IMAGE GENERATION
        # =================================================================
        # NOTE: DALL-E 3 and DALL-E 2 were removed from this seed on
        # 2026-05-13 — OpenAI retired both models from the API on
        # 2026-05-12. The replacement is `gpt-image-1-mini`, a native
        # image-gen model integrated with GPT-4o. Existing tenant rows
        # for `dall-e-3` / `dall-e-2` are NOT auto-migrated; operators
        # who want to drop them can do so via the admin UI. The new baseline below auto-seeds
        # on fresh installs only.

        # The default-enabled image model (one model per modality is seeded
        # enabled). Replaces gpt-image-2, which OpenAI shuts down on
        # 2027-02-10.
        {
            "model_name": "gpt-image-2.6-flare",
            "name": "OpenAI GPT-Image-2.6 Flare",
            "description": (
                "OpenAI GPT-Image-2.6 Flare, the fast-iteration model and the"
                " default-enabled image model. Endpoints "
                "v1/images/generations and v1/images/edits. Token-priced: "
                "text input $5.00, image input $8.00, image output $30.00 per"
                " 1M tokens. OpenAI publishes no per-image price for the 2.6 "
                "models, so cost_per_image is empty and the cost comes from "
                "the response's token usage."
            ),
            "usage": "Image generation and editing",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "supports_vision": False,
            "default_max_tokens": 0,
            "enabled": True,
            "timeout": 180.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 30.0,
            "supported_image_sizes": ["1024x1024", "1536x1024", "1024x1536", "auto"],
            "supported_image_qualities": ["low", "medium", "high", "xhigh", "max", "auto"],
            "max_images_per_request": 10,
            "capabilities": {
                "modalities": ["text", "image"],
                "token_priced": True,
                "cost_per_image_input_token": 8.0,
            },
            "additional_drop_params": ["response_format", "style"],
            "tags": ["image-gen", "openai", "default-baseline"],
        },
        {
            "model_name": "gpt-image-2.6-sunburst",
            "name": "OpenAI GPT-Image-2.6 Sunburst",
            "description": (
                "OpenAI GPT-Image-2.6 Sunburst, for detailed visual work. "
                "Endpoints v1/images/generations and v1/images/edits. Token-"
                "priced: text input $5.00, image input $8.00, image output "
                "$30.00 per 1M tokens. OpenAI publishes no per-image price "
                "for the 2.6 models, so cost_per_image is empty and the cost "
                "comes from the response's token usage."
            ),
            "usage": "Image generation and editing",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 180.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 30.0,
            "supported_image_sizes": ["1024x1024", "1536x1024", "1024x1536", "auto"],
            "supported_image_qualities": ["low", "medium", "high", "xhigh", "max", "auto"],
            "max_images_per_request": 10,
            "capabilities": {
                "modalities": ["text", "image"],
                "token_priced": True,
                "cost_per_image_input_token": 8.0,
            },
            "additional_drop_params": ["response_format", "style"],
            "tags": ["image-gen", "openai"],
        },

        {
            "model_name": "tts-1",
            "name": "OpenAI TTS-1",
            "description": (
                "Lower-latency OpenAI text-to-speech model. Nine built-in "
                "voices; mp3, opus, aac, flac, wav and pcm output. Default-"
                "enabled speech model. OpenAI retires it on 2027-01-06 with "
                "no successor on v1/audio/speech: see the retirement fields."
            ),
            "usage": "Voice replies, accessibility, real-time read-aloud, voice agents",
            "mode": LLModelMode.AUDIO_SPEECH.value,
            "enabled": True,
            "parameters": 0.0,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 60.0,
            # OpenAI bills tts-1 at $15 per 1M input characters →
            # $0.000015/char. The proxy multiplies this against
            # `req.input.chars().count()` for per-request cost roll-up.
            "cost_per_audio_char": 0.000015,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            # Top-level whitelists are what the proxy validator
            # consults — duplicating into `capabilities` would just
            # drift over time. Anything inside `capabilities` is
            # documentation, not enforcement.
            "supported_voices": [
                "alloy",
                "ash",
                "coral",
                "echo",
                "fable",
                "onyx",
                "nova",
                "sage",
                "shimmer",
            ],
            "supported_audio_formats": ["mp3", "opus", "aac", "flac", "wav", "pcm"],
            "capabilities": {
                "modalities": ["text"],
                "supports_streaming": True,
            },
            "tags": ["audio-tts", "openai", "real-time", "default-baseline"],
            "retirement_notes": (
                "OpenAI retires tts-1, tts-1-hd and gpt-4o-mini-tts on "
                "2027-01-06. Its named replacement, gpt-realtime-2.1-mini, is"
                " served on v1/realtime only and is not an audio_speech "
                "model. Choose a successor before this date."
            ),
            "retirement_announced_at": date(2026, 10, 1),
            "retire_date": date(2027, 1, 6),
        },

        # =================================================================
        # SPEECH-TO-TEXT (AUDIO_TRANSCRIPTION)
        # =================================================================
        {
            "model_name": "gpt-transcribe",
            "name": "OpenAI GPT Transcribe",
            "description": (
                "OpenAI's file-transcription model (v1/audio/transcriptions), "
                "the first of the models OpenAI's API reference lists for "
                "that endpoint. Default-enabled transcription model. It takes "
                "`language` (ISO-639-1), as the endpoint's other models do; "
                "the reference also lists `languages` (a list of the possible "
                "languages) and `keywords` as supported by this model."
            ),
            "usage": "Speech-to-text for uploaded audio files",
            "mode": LLModelMode.AUDIO_TRANSCRIPTION.value,
            "supports_vision": False,
            "default_max_tokens": 0,
            "enabled": True,
            "timeout": 120.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_audio_minute": 0.0045,
            "supported_input_formats": ["flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"],
            "supported_response_formats": ["json"],
            "capabilities": {
                "modalities": ["audio"],
                "max_file_size_mb": 25,
            },
            "tags": ["audio-stt", "openai", "default-baseline"],
        },

        # =================================================================
        # VIDEO GENERATION (Sora 2 family)
        # =================================================================
        # OpenAI's Sora 2 hosts a queue-based async API on the same
        # base URL as the rest of OpenAI's catalogue (the route is
        # /v1/videos for submit; /v1/videos/{id} for poll;
        # /v1/videos/{id}/content for the rendered bytes). The
        # tenant's existing OPENAI_API_KEY is reused — no per-model
        # key plumbing — but Sora is access-gated on the OpenAI
        # account, so both entries ship `enabled=False`. Operators
        # whose key has been granted Sora access flip them on via
        # Admin UI.
        #
        # Pricing reference (Q2 2026, USD):
        #   sora-2:        ~$0.10 / second @ 720p
        #   sora-2-pro:    ~$0.30 / second @ 720p
        #                  ~$0.50 / second @ 1080p
        # The catalogue stores a single `cost_per_video_second` per
        # row (no resolution-tiering yet), so we use the lower
        # (720p) figure for `sora-2-pro` and rely on operators to
        # override via Admin UI if they're shipping at 1080p.
    ]
