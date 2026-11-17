# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Fal AI Models — Fast, scalable image generation API + video models.

        https://fal.ai/pricing
Model prefix: fal_ai/ (required)
Auth env var: FAL_AI_API_KEY

Pricing: Per image or per megapixel for image models.
         Per second of output for video models.
         Supports FLUX, Stable Diffusion, Google Imagen, Recraft,
         Luma Dream Machine, Kling, Runway Gen-3, and more.
Cost values below: Set to 0.0 (billed per image/megapixel/second,
not tokens).

Video models share Fal's queue-based async API: submit returns a
request id, status is polled until COMPLETED, then the result URL
is fetched. Brutor's video proxy route (of the multimodal
plan) wraps this in a `video_generation_jobs` row + per-tenant SSE
channel.
"""

from app.constants.llm import LLModelMode


def get_models():
    return [
        # =================================================================
        # FLUX 2 MODELS (Latest generation)
        # =================================================================
        {
            "model_name": "fal-ai/flux-2-pro",
            "parameters": 12000.0,  # 12B flow transformer
            "name": "FLUX.2 [pro]",
            "description": (
                "Latest FLUX generation from Black Forest Labs. "
                "Zero-configuration pipeline — professional quality without "
                "tuning steps or guidance parameters. Multi-reference editing "
                "with up to 9 input images. ~$0.03/MP + $0.015/extra MP."
            ),
            "usage": "Production image generation, e-commerce editing, automated asset pipelines",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,  # Billed per megapixel
            "cost_per_output_token": 0.0,
            # ~$0.03/MP for the first MP. The proxy applies a multiplier
            # by output area; this is the floor for a 1024^2 generation.
            "cost_per_image": 0.03,
            "supported_image_sizes": [
                "512x512", "768x768", "1024x1024",
                "1024x1792", "1792x1024",
            ],
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text", "image"],
                "output_formats": ["jpeg", "png"],
                "seed_control": True,
                "multi_reference": True,
                "max_reference_images": 9,
            },
            "tags": ["image-gen", "flagship", "flux", "zero-config"],
        },

        # =================================================================
        # FLUX 1.1 MODELS
        # =================================================================
        {
            "model_name": "fal-ai/flux-pro/v1.1-ultra",
            "parameters": 12000.0,
            "name": "FLUX 1.1 [pro] Ultra",
            "description": (
                "High-resolution FLUX variant delivering up to 2K (4MP) output. "
                "Enhanced photorealism with ~10 second generation times. "
                "Highest Elo score on Artificial Analysis image arena. "
                "~$0.06/image."
            ),
            "usage": "High-res marketing assets, product photography, editorial illustrations",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.06,
            "supported_image_sizes": [
                "1024x1024", "1024x1792", "1792x1024", "2048x2048",
            ],
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["jpeg", "png"],
                "max_resolution": "4MP",
                "seed_control": True,
            },
            "tags": ["image-gen", "high-resolution", "flux", "premium"],
        },
        {
            "model_name": "fal-ai/flux-pro/v1.1",
            "parameters": 12000.0,
            "name": "FLUX 1.1 [pro]",
            "description": (
                "Enhanced FLUX Pro with 6x faster generation than predecessor. "
                "Superior composition, detail, and artistic fidelity. "
                "~$0.04/MP (1MP = 1024×1024)."
            ),
            "usage": "Professional image generation, creative development, batch workflows",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.04,
            "supported_image_sizes": [
                "512x512", "768x768", "1024x1024", "1024x1792", "1792x1024",
            ],
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["jpeg", "png"],
                "seed_control": True,
            },
            "tags": ["image-gen", "production", "flux", "fast"],
        },

        # =================================================================
        # THIRD-PARTY MODELS ON FAL
        # =================================================================
        {
            "model_name": "fal-ai/stable-diffusion-v35-medium",
            "parameters": 2500.0,
            "name": "SD 3.5 Medium (via Fal)",
            "description": (
                "Stable Diffusion 3.5 Medium hosted on Fal AI. "
                "Balanced quality and speed with Fal's optimized inference. "
                "Supports guidance_scale and num_inference_steps."
            ),
            "usage": "Cost-effective generation, batch processing, rapid prototyping",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.02,
            "supported_image_sizes": [
                "512x512", "768x768", "1024x1024", "1024x1792", "1792x1024",
            ],
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["jpeg", "png"],
                "seed_control": True,
                "guidance_scale": True,
            },
            "tags": ["image-gen", "stable-diffusion", "balanced"],
        },
        {
            "model_name": "fal-ai/recraft/v3/text-to-image",
            "parameters": 0.0,
            "name": "Recraft v3 (via Fal)",
            "description": (
                "Recraft v3 text-to-image on Fal AI. Supports style presets "
                "(realistic_image, digital_illustration, etc.) and custom "
                "image sizes. Strong at design-oriented generation."
            ),
            "usage": "Design assets, illustrations, styled image generation",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.04,
            "supported_image_sizes": [
                "1024x1024", "1024x1792", "1792x1024",
            ],
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["jpeg", "png"],
                "style_presets": True,
            },
            "tags": ["image-gen", "recraft", "design", "styled"],
        },

        # =================================================================
        # IDEOGRAM v3 — the text-rendering specialist
        # =================================================================
        # Ideogram is the only image model that reliably renders text
        # including long strings, brand names, and complex layouts —
        # the "best in class" for anything with words in it.
        {
            "model_name": "fal-ai/ideogram/v3",
            "name": "Ideogram v3 (via Fal)",
            "description": (
                "Ideogram v3 hosted on Fal. Specialises in legible "
                "in-image text — long strings, brand names, and "
                "complex typographic layouts. Pick this over FLUX / "
                "SD when the output MUST contain readable text "
                "(posters, ads, infographics, mock-ups). Default-"
                "disabled; operators enable per tenant."
            ),
            "usage": "Posters, ads, packaging, infographics, anything text-heavy",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "enabled": False,
            "parameters": 0.0,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 120.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            # ~$0.08 per 1024² image at v3 default quality. Higher
            # tiers (`turbo`/`quality`) re-price at request time
            # via the FAL response's billed-units field.
            "cost_per_image": 0.06,
            "supported_image_sizes": [
                "1024x1024", "1024x1792", "1792x1024",
            ],
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 8,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["jpeg", "png"],
                "text_rendering": "specialist",
            },
            "tags": ["image-gen", "ideogram", "fal", "text-rendering",
                     "typography"],
        },

        # =================================================================
        # VIDEO GENERATION
        # =================================================================
        # Video models on Fal use a queue-based async API. Submission
        # returns a request id; status is polled via the Fal queue
        # endpoint until COMPLETED (or FAILED). Brutor's video
        # proxy route wraps this in a `video_generation_jobs` row +
        # tenant SSE channel.
        #
        # Seeded default is Luma Dream Machine — the cheapest of
        # the credible Fal video models ($0.10/sec → $0.50 per
        # 5-second clip), which means a trial user with the
        # default $10 of credits Fal hands out can run ~20 clips
        # before hitting their wallet.
        #
        # An earlier seed shipped `fal-ai/fast-svd` default-enabled
        # on the (incorrect) assumption it was free-tier
        # accessible. It isn't — `fast-svd` is either deprecated
        # on the current Fal catalogue or gated behind a per-app
        # enable step on the dashboard, and trial users with a
        # valid key + credits hit:
        #
        #     "Cannot access application 'fal-ai/fast-svd'.
        #      Authentication is required to access this application."
        #
        # The lesson: don't ship "free tier" claims that can't be
        # independently verified against the provider's current
        # dashboard. Luma works on any Fal key with credits.
        # =================================================================
        # CURRENT FLAGSHIPS (refreshed 2026-08-23)
        # Removed: fal-ai/imagen4/preview and fal-ai/runway-gen3/* are
        # hard-404 on fal (Imagen replaced by the Nano Banana family;
        # Runway retired Gen-3 upstream with no fal successor).
        # NOTE: fal is migrating vendor endpoints OUT of the fal-ai/
        # namespace (ideogram/v4, bytedance/seedance-2.5/*, luma/agent/*)
        # — do not assume the prefix for new additions.
        # =================================================================
        {
            "model_name": "fal-ai/flux-2-max",
            "name": "FLUX.2 Max (via Fal)",
            "description": "Black Forest Labs' flagship FLUX.2 tier on Fal. $0.07 per megapixel.",
            "usage": "Highest-quality image generation",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "enabled": False,
            "parameters": 0.0,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 300.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.07,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["png", "jpeg"],
                "async_queue": True,
            },
            "tags": ["image-gen", "flux", "fal", "premium", "latest"],
        },
        {
            "model_name": "fal-ai/nano-banana-2",
            "name": "Google Nano Banana 2 (via Fal)",
            "description": "Gemini 3.1 Flash Image on Fal (1K/2K/4K). The successor to the removed Imagen 4 endpoints.",
            "usage": "Fast high-quality image generation and editing",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "enabled": False,
            "parameters": 0.0,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 300.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.08,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["png", "jpeg"],
                "async_queue": True,
            },
            "tags": ["image-gen", "google", "fal", "latest"],
        },
        {
            "model_name": "fal-ai/kling-video/v3/pro/text-to-video",
            "name": "Kling v3 Pro Text-to-Video (via Fal)",
            "description": "Kuaishou Kling v3 Pro on Fal. $0.168 per second with audio, the default (seeded); $0.112 without. 3 to 15 second clips; the endpoint has no resolution parameter.",
            "usage": "High-quality text-to-video",
            "mode": LLModelMode.VIDEO_GENERATION.value,
            "enabled": False,
            "parameters": 0.0,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 900.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_video_second": 0.168,
            "supported_video_durations": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "max_video_duration_seconds": 15,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["mp4"],
                "async_queue": True,
            },
            "tags": ["video-gen", "kling", "fal", "premium", "latest"],
        },
        {
            "model_name": "bytedance/seedance-2.5/text-to-video",
            "name": "ByteDance Seedance 2.5 Text-to-Video (via Fal)",
            "description": "Seedance 2.5 on Fal (non-fal-ai namespace). $0.473 per second at 720p (seeded); $0.2205 at 480p, $1.164 at 1080p. 4 to 30 second clips.",
            "usage": "Long-form (30s) text-to-video at strong quality/price",
            "mode": LLModelMode.VIDEO_GENERATION.value,
            "enabled": False,
            "parameters": 0.0,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 900.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_video_second": 0.473,
            "supported_video_resolutions": ["480p", "720p", "1080p"],
            "supported_video_durations": [
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
                28,
                29,
                30,
            ],
            "max_video_duration_seconds": 30,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["mp4"],
                "async_queue": True,
            },
            "tags": ["video-gen", "seedance", "bytedance", "fal", "latest"],
        },
        {
            "model_name": "luma/agent/ray/v3.2/text-to-video",
            "name": "Luma Ray 3.2 Text to Video (via Fal)",
            "description": (
                "Luma Ray 3.2 text-to-video on Fal, the only Luma text-to-"
                "video endpoint Fal lists (the Ray 2 Flash endpoint answers "
                "404). Default-enabled video model. $0.10 per second at 540p,"
                " the default (seeded); $0.20 at 720p, $0.40 at 1080p. 5 or "
                "10 second clips. Async queue API."
            ),
            "usage": (
                "Short-form video generation, animated thumbnails, motion "
                "design"
            ),
            "mode": LLModelMode.VIDEO_GENERATION.value,
            "supports_vision": False,
            "default_max_tokens": 0,
            "enabled": True,
            "timeout": 600.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_video_second": 0.1,
            "supported_video_resolutions": ["540p", "720p", "1080p"],
            "supported_video_durations": [5, 10],
            "max_video_duration_seconds": 10,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["mp4"],
                "async_queue": True,
                "supported_aspect_ratios": ["3:4", "4:3", "1:1", "9:16", "16:9", "21:9"],
            },
            "tags": ["video-gen", "luma", "fal", "async-queue", "default-baseline"],
        },


        # =================================================================
        # SEEDANCE v1 PRO — ByteDance's video model.
        # Strong price/performance for non-premium creative work.
        # =================================================================
        {
            "model_name": "fal-ai/bytedance/seedance/v1/pro/text-to-video",
            "name": "Seedance v1 Pro (via Fal)",
            "description": (
                "ByteDance Seedance v1 Pro text-to-video on Fal. About "
                "$0.124 per second at 1080p (Fal prints roughly $0.62 "
                "per 5-second clip). 480p, 720p or 1080p; 2 to 12 "
                "second clips. Default-disabled."
            ),
            "usage": "Everyday video generation, social/short-form, marketing iteration",
            "mode": LLModelMode.VIDEO_GENERATION.value,
            "enabled": False,
            "parameters": 0.0,
            "supports_vision": False,
            "default_max_tokens": 0,
            "timeout": 900.0,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            # ~$0.18/sec at 1080p per FAL — verify at deploy.
            "cost_per_video_second": 0.124,
            "supported_video_resolutions": ["480p", "720p", "1080p"],
            "supported_video_durations": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
            "max_video_duration_seconds": 12,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["mp4"],
                "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
                "async_queue": True,
            },
            "tags": ["video-gen", "seedance", "bytedance", "fal",
                     "mid-tier", "async-queue"],
        },
    ]
