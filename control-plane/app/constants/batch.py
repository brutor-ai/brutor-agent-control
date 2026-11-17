# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Batch Processing Constants and Enums

Defines enumerations for the unified batch API that normalizes
provider-specific batch workflows (Anthropic, OpenAI, Google, xAI)
into a single lifecycle.
"""

from enum import Enum


class BatchJobStatus(str, Enum):
    """
    Batch job lifecycle states

    Flow: CREATED → VALIDATING → IN_PROGRESS → COMPLETED | FAILED | CANCELLED | EXPIRED
                                              → PARTIALLY_COMPLETED (some requests failed)

    - CREATED: Job record created in gateway, not yet submitted to provider
    - VALIDATING: Provider is validating the batch (file format, model access, etc.)
    - IN_PROGRESS: Provider is processing requests
    - COMPLETED: All requests processed successfully
    - PARTIALLY_COMPLETED: Some requests succeeded, some failed
    - FAILED: Batch failed (provider error, validation failure, or all requests failed)
    - CANCELLING: Cancel requested, waiting for provider confirmation
    - CANCELLED: Cancelled by user or admin
    - EXPIRED: Provider expired the batch (e.g., exceeded 24h completion window)
    """
    CREATED = "created"
    VALIDATING = "validating"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class BatchRequestStatus(str, Enum):
    """
    Individual request status within a batch

    - PENDING: Not yet processed by the provider
    - SUCCEEDED: Request completed successfully
    - FAILED: Request failed (error in response)
    - CANCELLED: Parent batch was cancelled before this request was processed
    - EXPIRED: Parent batch expired before this request was processed
    """
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class BatchEndpoint(str, Enum):
    """
    Supported batch endpoint types

    Each maps to the underlying provider's batch-compatible endpoint.
    """
    CHAT_COMPLETIONS = "/v1/chat/completions"
    MESSAGES = "/v1/messages"
    EMBEDDINGS = "/v1/embeddings"
    RESPONSES = "/v1/responses"
