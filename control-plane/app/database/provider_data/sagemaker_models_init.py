# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
AWS SageMaker Models — SageMaker Inference Endpoints (JumpStart + custom).

Model prefix: sagemaker/ (required)
Auth: AWS IAM (an access key, its secret and a region)
Cost values: 0.00 (billed per-instance-hour via AWS, not per-token)

NOTE: SageMaker models are deployed to dedicated endpoints. The model_name
corresponds to the SageMaker endpoint name, not a fixed model catalog.
These entries represent common JumpStart models that users deploy.
All models disabled by default; require SageMaker endpoint deployment.
"""



# COST NOTE (2026-08-23): deliberately no per-token cost — SageMaker
# JumpStart bills per INSTANCE-HOUR, not per token. Real spend exists
# but belongs to AWS cost allocation, not the per-token ledger;
# seeding a fake token rate would double-count it.

def get_models():
    return [
        # =====================================================================
        # JumpStart Chat Models (Representative Deployments)
        # =====================================================================
        {
            "model_name": "sagemaker/jumpstart-meta-llama-3-3-70b-instruct",
            "parameters": 70.0,
            "name": "SageMaker Llama 3.3 70B",
            "description": "Meta Llama 3.3 70B via SageMaker JumpStart. AWS-managed inference.",
            "usage": "Enterprise chat, coding, analysis — AWS-native deployment",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "aws", "sagemaker", "open-weight",
                     "enterprise", "self-hosted", "large"],
        },
        {
            "model_name": "sagemaker/jumpstart-meta-llama-3-1-8b-instruct",
            "parameters": 8.0,
            "name": "SageMaker Llama 3.1 8B",
            "description": "Meta Llama 3.1 8B via SageMaker JumpStart. Cost-effective.",
            "usage": "Simple tasks, classification, data processing — AWS-native",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["aws", "sagemaker", "open-weight",
                     "self-hosted", "cost-efficient", "small"],
        },
        {
            "model_name": "sagemaker/jumpstart-mistral-7b-instruct",
            "parameters": 7.0,
            "name": "SageMaker Mistral 7B",
            "description": "Mistral 7B Instruct via SageMaker JumpStart.",
            "usage": "General chat, simple coding, multilingual tasks",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 32768,
                "modalities": ["text"],
            },
            "tags": ["aws", "sagemaker", "open-weight",
                     "self-hosted", "cost-efficient", "small"],
        },
        {
            "model_name": "sagemaker/jumpstart-cohere-command-r-plus",
            "parameters": 104.0,
            "name": "SageMaker Cohere Command R+",
            "description": "Cohere Command R+ via SageMaker JumpStart. RAG-optimized.",
            "usage": "RAG, enterprise search, document analysis — AWS-native",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "aws", "sagemaker",
                     "enterprise", "self-hosted", "large"],
        },

        # =====================================================================
        # JumpStart Embedding Models
        # =====================================================================
        {
            "model_name": "sagemaker/jumpstart-bge-large-en-v1-5",
            "parameters": 0.33,
            "name": "SageMaker BGE Large EN v1.5",
            "description": "BAAI BGE Large English embeddings on SageMaker JumpStart.",
            "usage": "Text embeddings, semantic search, RAG — AWS-native",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 512,
                "modalities": ["text"],
            },
            "tags": ["embedding", "aws", "sagemaker",
                     "self-hosted", "small"],
        },
        {
            "model_name": "sagemaker/jumpstart-cohere-embed-english-v3",
            # Cohere doesn't publish exact param count for embed-v3; leave
            # null rather than carry a copy-paste from a chat-model entry.
            "parameters": None,
            "name": "SageMaker Cohere Embed v3 English",
            "description": "Cohere Embed v3 English on SageMaker. High-quality embeddings.",
            "usage": "Enterprise embeddings, semantic search, RAG on AWS",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 512,
                "modalities": ["text"],
            },
            "tags": ["embedding", "aws", "sagemaker",
                     "enterprise", "self-hosted"],
        },
    ]
