# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Jina AI Models — Search foundation models (embeddings + rerankers).

        https://jina.ai/embeddings/  |  https://jina.ai/reranker/
Model prefix: jina_ai/ (required)
Auth env var: JINA_AI_API_KEY

Pricing: Token-based (shared pool across all Jina APIs).
         10M free tokens per new API key.
         ~$0.02 per 1M tokens (varies by plan tier).
Cost values below: USD per 1M tokens.
"""

from app.constants.llm import LLModelMode


def get_models():
    return [
        # =================================================================
        # EMBEDDING MODELS
        # =================================================================
        {
            "model_name": "jina_ai/jina-embeddings-v4",
            "parameters": 3800.0,
            "name": "Jina Embeddings v4",
            "description": (
                "Universal multimodal multilingual embedding model. "
                "Supports text, images, and visually rich documents (PDFs, charts, tables). "
                "Built on Qwen2.5-VL-3B. SOTA on ViDoRe visual document retrieval. "
                "Dense (single-vector) and late-interaction (multi-vector) modes."
            ),
            "usage": "Multimodal RAG, visual document search, cross-lingual retrieval, PDF indexing",
            "mode": LLModelMode.EMBEDDING.value,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 32768,
                "dimensions": 2048,
                "min_dimensions": 128,
                "modalities": ["text", "image", "document"],
                "languages": 30,
            },
            "tags": ["embeddings", "multimodal", "multilingual", "flagship", "visual-document"],
        },
        {
            "model_name": "jina_ai/jina-embeddings-v5-text-small",
            "name": "Jina Embeddings v5 Text Small",
            "description": (
                "Jina Embeddings v5 Text Small, the model Jina names in place"
                " of the deprecated v3. 32,768-token input, 1,024 dimensions."
                " Jina publishes no per-token price: set the cost on import."
            ),
            "usage": "Text retrieval and RAG",
            "mode": "embedding",
            "cost_per_input_token": None,
            "cost_per_output_token": None,
            "capabilities": {
                "context_window": 32768,
                "modalities": ["text"],
                "dimensions": 1024,
            },
            "tags": ["embeddings"],
        },
        {
            "model_name": "jina_ai/jina-clip-v2",
            "parameters": 900.0,
            "name": "Jina CLIP v2",
            "description": (
                "Multimodal embedding model for text-image retrieval. "
                "900M parameters supporting 89 languages. "
                "Produces embeddings in a shared text-image semantic space."
            ),
            "usage": "Image search, multimodal RAG, cross-modal retrieval, visual similarity",
            "mode": LLModelMode.EMBEDDING.value,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 8192,
                "dimensions": 1024,
                "modalities": ["text", "image"],
                "languages": 89,
            },
            "tags": ["embeddings", "multimodal", "clip", "image-search"],
        },
        {
            "model_name": "jina_ai/jina-colbert-v2",
            "parameters": 560.0,
            "name": "Jina ColBERT v2",
            "description": (
                "Late-interaction embedding model for high-precision retrieval. "
                "Produces multi-vector embeddings (one per token) for fine-grained matching. "
                "Selectable dimensions: 128, 96, or 64."
            ),
            "usage": "High-precision reranking, document deduplication, fine-grained retrieval",
            "mode": LLModelMode.EMBEDDING.value,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 8192,
                "dimensions": 128,
                "modalities": ["text"],
                "languages": 89,
                "multi_vector": True,
            },
            "tags": ["embeddings", "colbert", "late-interaction", "multi-vector"],
        },

        # =================================================================
        # RERANKER MODELS
        # =================================================================
        {
            "model_name": "jina_ai/jina-reranker-v3",
            "parameters": 600.0,
            "name": "Jina Reranker v3",
            "description": (
                "SOTA listwise reranker with novel 'last but not late' interaction. "
                "Built on Qwen3-0.6B. Processes up to 64 documents simultaneously "
                "within 131K token context. BEIR nDCG@10: 61.94."
            ),
            "usage": "Search reranking, RAG precision, multi-document ranking, code retrieval",
            "mode": LLModelMode.RERANK.value,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 131072,
                "max_documents": 64,
                "modalities": ["text"],
                "languages": 18,
            },
            "tags": ["reranker", "flagship", "listwise", "multilingual"],
        },
        {
            "model_name": "jina_ai/jina-reranker-m0",
            "parameters": 2400.0,
            "name": "Jina Reranker m0",
            "description": (
                "Multimodal multilingual reranker for visual documents. "
                "Ranks text and/or image candidates against text queries. "
                "SOTA on ViDoRe benchmark (91.02 nDCG@5). "
                "Supports 29 languages."
            ),
            "usage": "Visual document reranking, PDF search, mixed-modal retrieval, slide search",
            "mode": LLModelMode.RERANK.value,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 10000,
                "modalities": ["text", "image"],
                "languages": 29,
            },
            "tags": ["reranker", "multimodal", "visual-document"],
        },
    ]
