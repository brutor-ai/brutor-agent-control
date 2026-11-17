/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Provider metadata + lookup. Kept apart from providerLogos.tsx: a file that
// exports both components and non-components opts out of Fast Refresh
// (react-refresh/only-export-components).

export interface ProviderInfo {
    displayName: string;
    fallbackColor: string;
}

export const PROVIDER_INFO: Record<string, ProviderInfo> = {
    // ----- Tier 1: Major Direct API Providers -----
    openai: { displayName: 'OpenAI', fallbackColor: '#10A37F' },
    anthropic: { displayName: 'Anthropic', fallbackColor: '#D97757' },
    gemini: { displayName: 'Google Gemini', fallbackColor: '#4285F4' },
    vertex_ai: { displayName: 'Google Vertex AI', fallbackColor: '#4285F4' },
    azure: { displayName: 'Azure OpenAI', fallbackColor: '#0078D4' },
    azure_ai: { displayName: 'Azure AI Foundry', fallbackColor: '#0078D4' },
    bedrock: { displayName: 'AWS Bedrock', fallbackColor: '#FF9900' },
    bedrock_converse: { displayName: 'AWS Bedrock Converse', fallbackColor: '#FF9900' },
    mistral: { displayName: 'Mistral AI', fallbackColor: '#F2A93B' },
    cohere: { displayName: 'Cohere', fallbackColor: '#39594D' },
    xai: { displayName: 'xAI', fallbackColor: '#000000' },
    deepseek: { displayName: 'DeepSeek', fallbackColor: '#4D6BFE' },
    moonshot: { displayName: 'Moonshot AI', fallbackColor: '#0F172A' },
    minimax: { displayName: 'MiniMax', fallbackColor: '#F037A5' },

    // ----- Tier 2: Inference Platforms -----
    groq: { displayName: 'Groq', fallbackColor: '#F55036' },
    together_ai: { displayName: 'Together AI', fallbackColor: '#6366F1' },
    fireworks_ai: { displayName: 'Fireworks AI', fallbackColor: '#FF6B35' },
    perplexity: { displayName: 'Perplexity', fallbackColor: '#20808D' },
    openrouter: { displayName: 'OpenRouter', fallbackColor: '#6366F1' },
    sambanova: { displayName: 'SambaNova', fallbackColor: '#FF5722' },
    cerebras: { displayName: 'Cerebras', fallbackColor: '#0066FF' },
    ai21: { displayName: 'AI21 Labs', fallbackColor: '#6C5CE7' },
    deepinfra: { displayName: 'DeepInfra', fallbackColor: '#FF4081' },

    // ----- Tier 3: Self-Hosted / Specialized -----
    ollama: { displayName: 'Ollama', fallbackColor: '#000000' },
    vllm: { displayName: 'vLLM', fallbackColor: '#00BCD4' },
    nvidia_nim: { displayName: 'NVIDIA NIM', fallbackColor: '#76B900' },
    databricks: { displayName: 'Databricks', fallbackColor: '#FF3621' },
    huggingface: { displayName: 'Hugging Face', fallbackColor: '#FFD21E' },
    sagemaker: { displayName: 'AWS SageMaker', fallbackColor: '#FF9900' },
    lm_studio: { displayName: 'LM Studio', fallbackColor: '#2196F3' },

    // ----- Specialized Services -----
    voyage: { displayName: 'Voyage AI', fallbackColor: '#00897B' },
    jina_ai: { displayName: 'Jina AI', fallbackColor: '#FF6F00' },
    elevenlabs: { displayName: 'ElevenLabs', fallbackColor: '#000000' },
    deepgram: { displayName: 'Deepgram', fallbackColor: '#13EF93' },
    stability_ai: { displayName: 'Stability AI', fallbackColor: '#A855F7' },
    fal_ai: { displayName: 'Fal AI', fallbackColor: '#EC4899' },

    // ----- OpenAI-Compatible Generic -----
    openai_compatible: { displayName: 'OpenAI Compatible', fallbackColor: '#6B7280' },

    // ----- Internal -----
    routing_group: { displayName: 'Routing Group', fallbackColor: '#F97316' },

    // ----- Fallback -----
    custom: { displayName: 'Custom', fallbackColor: '#6B7280' },
};

export const getProviderInfo = (provider: string): ProviderInfo => {
    return PROVIDER_INFO[provider] || PROVIDER_INFO.custom;
};
