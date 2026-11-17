# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Replicate Models — Image generation (and selected video models).

Source: https://replicate.com/docs
        https://replicate.com/pricing
Model prefix: replicate/ (required when used through
                      legacy paths). The Brutor Rust adapter in
                      `routes/images.rs::call_replicate_images` calls
                      `https://api.replicate.com/v1/models/{model_name}/predictions`
                      directly with `Bearer <api_key>` and
                      `Prefer: wait=60` for sync-ish results.

Auth env var: REPLICATE_API_TOKEN

Pricing model: per second of GPU time (default), or per output for a
            handful of "official" models (FLUX, SDXL, etc.) with
            negotiated flat per-image pricing. The numbers below are
            the per-image flat-rate published values. Operators on
            per-second pricing should override `cost_per_image` with
            their measured average.

`model_name` should be `<owner>/<model>` (no version pin) so
Replicate routes to the latest version of the official model. If a
particular version pin is required, append `:version_hash` to the
model_name.

Whitelists are populated at the top level (`supported_image_sizes`,
`supported_image_qualities`, `max_images_per_request`,
`cost_per_image`) so the Brutor proxy validator in
`routes/images.rs` enforces them at the edge — see
`init_llms.py::create_llm_models` for the schema mapping.
"""

from app.constants.llm import LLModelMode

# Replicate text-to-image models accept WxH or aspect ratio. We
# expose the OpenAI-style WxH form so the admin Media-tab dropdown
# stays consistent across providers; the proxy translates these
# into the model-specific input schema (width/height for FLUX,
# aspect_ratio for some Imagen variants).
_T2I_SIZES = [
    "1024x1024",
    "1024x1792",
    "1792x1024",
    "1216x832",
    "832x1216",
]


def get_models():
    return [
        # =================================================================
        # FLUX (Black Forest Labs)
        # =================================================================
        {
            "model_name": "black-forest-labs/flux-schnell",
            "parameters": 12000.0,
            "name": "FLUX.1 [schnell] (via Replicate)",
            "description": (
                "Apache-2.0-licensed FLUX schnell — 4-step generation, "
                "fastest of the FLUX family. Replicate flat rate ~$0.003/image. "
                "Reasonable default for high-volume product/test workflows."
            ),
            "usage": "Fast image generation, prototyping, batch workflows, free-tier-friendly",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.003,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "timeout": 120.0,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["png", "jpeg", "webp"],
                "license": "Apache-2.0",
                "seed_control": True,
            },
            "tags": ["image-gen", "flux", "fast", "replicate", "low-cost"],
        },
        {
            "model_name": "black-forest-labs/flux-dev",
            "parameters": 12000.0,
            "name": "FLUX.1 [dev] (via Replicate)",
            "description": (
                "Open-weights FLUX dev — higher quality than schnell, "
                "non-commercial license. ~$0.025/image on Replicate."
            ),
            "usage": "Open-weights research workflows, fine-tuning baselines",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.025,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "timeout": 180.0,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["png", "jpeg", "webp"],
                "license": "FLUX.1-dev (non-commercial)",
                "seed_control": True,
            },
            "tags": ["image-gen", "flux", "high-quality", "replicate"],
        },
        {
            "model_name": "black-forest-labs/flux-1.1-pro",
            "parameters": 12000.0,
            "name": "FLUX 1.1 [pro] (via Replicate)",
            "description": (
                "Production-grade FLUX 1.1 pro variant on Replicate. "
                "Higher fidelity, commercial license. ~$0.04/image flat."
            ),
            "usage": "Production assets, commercial photography, marketing",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.04,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "timeout": 240.0,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["png", "jpeg", "webp"],
                "seed_control": True,
            },
            "tags": ["image-gen", "flux", "production", "commercial", "replicate"],
        },

        # =================================================================
        # STABLE DIFFUSION (Stability AI on Replicate)
        # =================================================================
        {
            "model_name": "stability-ai/stable-diffusion-3.5-large",
            "parameters": 8000.0,
            "name": "SD 3.5 Large (via Replicate)",
            "description": (
                "Stability's flagship SD 3.5 Large hosted on Replicate. "
                "~$0.07/image. Same model as direct Stability with "
                "Replicate-managed inference."
            ),
            "usage": "High-quality image generation, when Replicate's billing/audit is preferred",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.065,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "timeout": 240.0,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["png", "jpeg", "webp"],
                "seed_control": True,
            },
            "tags": ["image-gen", "stable-diffusion", "replicate", "production"],
        },
        {
            "model_name": "stability-ai/sdxl",
            "parameters": 3500.0,
            "name": "SDXL (via Replicate)",
            "description": (
                "Stable Diffusion XL hosted on Replicate. "
                "~$0.0095/image at flat-rate pricing. "
                "Solid balance of quality, cost, and ecosystem support."
            ),
            "usage": "General-purpose image generation, batch workflows, lower-cost runs",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "cost_per_image": 0.0043,
            "supported_image_sizes": _T2I_SIZES,
            "supported_image_qualities": ["standard"],
            "max_images_per_request": 4,
            "timeout": 180.0,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["png", "jpeg", "webp"],
                "seed_control": True,
                "negative_prompt": True,
            },
            "tags": ["image-gen", "sdxl", "replicate", "balanced"],
        },

        # =================================================================
        # GOOGLE IMAGEN (via Replicate)
        # =================================================================
        # Replicate hosts Google's Imagen series for non-Vertex
        # tenants — useful when an operator wants the Imagen
        # quality without setting up a GCP project.
        {
            "model_name": "google/nano-banana-2.1",
            "name": "Google Nano Banana 2.1 (via Replicate)",
            "description": (
                "Google Nano Banana 2.1 on Replicate (no Imagen model is "
                "listed any more). $0.0336 per image at 1K (seeded), $0.0504 "
                "at 2K, $0.1134 at 4K; the resolution follows the aspect "
                "ratio. One image per request; jpg or png."
            ),
            "usage": "Photorealistic image generation",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "timeout": 180.0,
            "cost_per_image": 0.0336,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
            "supported_image_sizes": ["1K", "2K", "4K"],
            "max_images_per_request": 1,
            "capabilities": {
                "modalities": ["text"],
                "output_formats": ["jpg", "png"],
            },
            "tags": ["image-gen", "google", "nano-banana"],
        },
    ]
