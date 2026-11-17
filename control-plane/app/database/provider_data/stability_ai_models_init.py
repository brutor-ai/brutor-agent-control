# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Stability AI Models — Image generation via Stable Diffusion.

        https://platform.stability.ai/pricing
Model prefix: stability/ (required)
Auth env var: STABILITY_API_KEY

Pricing: Credit-based, per image generated.
         Prices below land in the dedicated `cost_per_image` column
         — they are no longer overloaded onto
         `cost_per_input_token`. Stability also exposes some of these
         models via Bedrock (bedrock/stability.*).

The image-specific top-level columns are:
  * supported_image_sizes   — whitelist of resolutions the proxy
                              will accept; a request outside the list
                              gets a 400 before going upstream.
  * supported_image_qualities — Stability has no "hd" tier, so all
                              entries here are ["standard"].
  * max_images_per_request  — Stability accepts batch up to 4 on
                              v3.5 endpoints; older endpoints cap at 1.
"""

from app.constants.llm import LLModelMode


# Stability's text-to-image endpoints accept aspect-ratio-based sizes;
# we surface the most common pixel-shape variants so the admin UI's
# size dropdown is meaningful. The proxy translates these to the
# provider's aspect_ratio param when forwarding.
_T2I_SIZES = [
    "1024x1024",   # 1:1 square
    "1152x896",    # 9:7 landscape
    "1216x832",    # 19:13 landscape
    "1344x768",    # 7:4 landscape
    "1536x640",    # 12:5 ultra-wide
    "640x1536",    # 5:12 ultra-tall
    "768x1344",    # 4:7 portrait
    "832x1216",    # 13:19 portrait
    "896x1152",    # 7:9 portrait
]

# Editing endpoints (sketch / object-erase) keep the input size, so
# the size whitelist is descriptive of the supported *output*
# range. Conservative whitelist matching the typical input size.
_EDIT_SIZES = ["1024x1024"]


def get_models():
    return [
        {
            "model_name": "stability/stable-image-ultra",
            "parameters": 8000.0,  # Based on SD3.5 Large (8B)
            "name": "Stable Image Ultra",
            "description": (
                "Stability AI's state-of-the-art text-to-image model based on "
                "Stable Diffusion 3.5 Large. Highest quality with exceptional "
                "detail, multi-subject handling, and accurate text rendering. "
                "~$0.08/image."
            ),
            "usage": "Premium image generation, marketing assets, product photography",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,  # Billed per image
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.08,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text", "image"],
                "output_formats": ["png", "jpeg", "webp"],
                "negative_prompt": True,
                "seed_control": True,
            },
            "tags": ["image-gen", "flagship", "high-quality"],
        },
        {
            "model_name": "stability/sd3.5-large",
            "parameters": 8000.0,
            "name": "Stable Diffusion 3.5 Large",
            "description": (
                "Flagship 8B parameter base model. State-of-the-art image quality "
                "with exceptional prompt adherence and versatile style generation. "
                "~$0.065/image."
            ),
            "usage": "High-quality image generation, creative workflows, style exploration",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.065,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text", "image"],
                "output_formats": ["png", "jpeg", "webp"],
                "negative_prompt": True,
                "seed_control": True,
            },
            "tags": ["image-gen", "production", "sd3.5"],
        },
        {
            "model_name": "stability/sd3.5-large-turbo",
            "parameters": 8000.0,
            "name": "SD 3.5 Large Turbo",
            "description": (
                "Fast variant of SD 3.5 Large optimized for speed. "
                "Fewer inference steps with minimal quality loss. "
                "~$0.04/image."
            ),
            "usage": "Rapid prototyping, high-volume generation, iterative design",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.04,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text", "image"],
                "output_formats": ["png", "jpeg", "webp"],
                "negative_prompt": True,
                "seed_control": True,
            },
            "tags": ["image-gen", "fast", "sd3.5"],
        },
        {
            "model_name": "stability/sd3.5-medium",
            "parameters": 2500.0,
            "name": "SD 3.5 Medium",
            "description": (
                "Balanced 2.5B parameter model. Good quality at lower cost "
                "and faster inference. Runs on consumer GPUs. "
                "~$0.035/image."
            ),
            "usage": "Cost-effective generation, consumer-grade hardware, batch processing",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.035,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text", "image"],
                "output_formats": ["png", "jpeg", "webp"],
                "negative_prompt": True,
                "seed_control": True,
            },
            "tags": ["image-gen", "balanced", "sd3.5"],
        },
        {
            "model_name": "stability/stable-image-core",
            "parameters": 0.0,  # Undisclosed
            "name": "Stable Image Core",
            "description": (
                "Speed and affordability focused image generation. "
                "Designed for high-volume workflows where cost efficiency "
                "is paramount. ~$0.03/image."
            ),
            "usage": "High-volume image generation, prototyping, thumbnail creation",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.03,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "capabilities": {
                "modalities": ["text", "image"],
                "output_formats": ["png", "jpeg", "webp"],
                "negative_prompt": True,
            },
            "tags": ["image-gen", "cost-efficient", "fast"],
        },

        # =================================================================
        # IMAGE EDITING MODELS
        # =================================================================
    ]
