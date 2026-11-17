// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

// ---------------------------------------------------------------------------
// LLM Proxy request/response types (OpenAI-compatible)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize, Serialize, ToSchema)]
pub struct ChatCompletionRequest {
    pub model: Option<String>,
    pub model_id: Option<String>,
    pub messages: Vec<ChatMessage>,
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default)]
    pub max_tokens: Option<i32>,
    #[serde(default)]
    pub stream: Option<bool>,
    #[serde(default)]
    pub top_p: Option<f64>,
    #[serde(default)]
    pub n: Option<i32>,
    #[serde(default)]
    pub stop: Option<serde_json::Value>,
    #[serde(default)]
    pub presence_penalty: Option<f64>,
    #[serde(default)]
    pub frequency_penalty: Option<f64>,
    #[serde(default)]
    pub tools: Option<serde_json::Value>,
    #[serde(default)]
    pub tool_choice: Option<serde_json::Value>,
    /// Catch-all for provider-specific params
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ChatMessage {
    pub role: String,
    #[serde(default)]
    pub content: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct ChatCompletionResponse {
    pub id: String,
    pub object: String,
    pub created: i64,
    pub model: String,
    pub choices: Vec<ChatChoice>,
    pub usage: Option<UsageInfo>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub system_fingerprint: Option<String>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct ChatChoice {
    pub index: i32,
    pub message: ChatMessage,
    pub finish_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct UsageInfo {
    pub prompt_tokens: i32,
    pub completion_tokens: i32,
    pub total_tokens: i32,
}

// ---------------------------------------------------------------------------
// SSE streaming chunk (for proper streaming support)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct ChatCompletionChunk {
    pub id: String,
    pub object: String,
    pub created: i64,
    pub model: String,
    pub choices: Vec<ChunkChoice>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<UsageInfo>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkChoice {
    pub index: i32,
    pub delta: ChatDelta,
    pub finish_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChatDelta {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Completions (legacy text mode)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize, Serialize, ToSchema)]
pub struct CompletionRequest {
    pub model: Option<String>,
    pub model_id: Option<String>,
    pub prompt: String,
    #[serde(default)]
    pub max_tokens: Option<i32>,
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default)]
    pub stream: Option<bool>,
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Embeddings
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize, Serialize, ToSchema)]
pub struct EmbeddingRequest {
    pub model: Option<String>,
    pub model_id: Option<String>,
    pub input: serde_json::Value,
    #[serde(default)]
    pub encoding_format: Option<String>,
    #[serde(default)]
    pub dimensions: Option<i32>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct EmbeddingResponse {
    pub object: String,
    pub data: Vec<EmbeddingData>,
    pub model: String,
    pub usage: UsageInfo,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct EmbeddingData {
    pub object: String,
    pub embedding: Vec<f64>,
    pub index: i32,
}

// ---------------------------------------------------------------------------
// Responses API (structured output)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize, Serialize, ToSchema)]
pub struct ResponsesRequest {
    pub model: Option<String>,
    pub model_id: Option<String>,
    pub input: serde_json::Value,
    #[serde(default)]
    pub max_output_tokens: Option<i32>,
    #[serde(default)]
    pub stream: Option<bool>,
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Skill execution
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize, Serialize, ToSchema)]
pub struct SkillExecuteRequest {
    pub skill_id: Option<String>,
    pub skill_name: Option<String>,
    #[serde(default)]
    pub input_params: Option<serde_json::Value>,
    #[serde(default)]
    pub group_id: Option<String>,
    #[serde(default)]
    pub correlation_id: Option<String>,
    #[serde(default)]
    pub parent_execution_id: Option<String>,
    /// One-time approval token(s) for retrying a skill execution that was
    /// held for a person, for a caller that cannot set the
    /// `X-Approval-Token` header. Several tokens are separated by commas.
    #[serde(default)]
    pub approval_token: Option<String>,
    /// The approvals of the request this execution is part of: the tokens
    /// it carries (headers, and `approval_token` above) and the questions
    /// its earlier gates had answered. Set by the surface that received the
    /// request, which shares it with its own gates; never deserialized.
    #[serde(skip)]
    pub approvals: std::sync::Arc<crate::services::agent_approval::RequestApprovals>,
    /// Keys this request tried to re-declare in the run's data
    /// scope (the binding is unchanged), captured by the route that read the
    /// `X-Brutor-Run-Scope` header and judged with the skill call. Never
    /// deserialized: a caller must not be able to clear its own conflict.
    #[serde(skip)]
    pub data_scope_conflicts: Vec<String>,
    /// The input as the caller sent it, when the surface that built the
    /// request passes it through an input guardrail first (an A2A message):
    /// `input_params` is then what the guardrail left, and an approval of
    /// the execution is bound to this (`skill_orchestrator`'s
    /// `approval_binding`). `None`: `input_params` is what the caller sent.
    /// Never deserialized.
    #[serde(skip)]
    pub input_as_sent: Option<serde_json::Value>,
    /// How an argument policy on the skill's input reads `input_params`.
    /// Set by the surface that built the request; never deserialized.
    #[serde(skip)]
    pub input_view: SkillInputView,
}

/// What `SkillExecuteRequest::input_params` is, for the argument policy on
/// the skill's input.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum SkillInputView {
    /// The caller's input object: its fields are addressed as they are.
    #[default]
    Whole,
    /// An A2A message: the policy judges the values of its data parts
    /// (`argument_policy::Arguments::of_a2a_message`).
    A2aMessage,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct SkillExecuteResponse {
    pub execution_id: String,
    pub skill_id: String,
    pub skill_name: String,
    pub skill_version: String,
    pub execution_status: String,
    pub correlation_id: String,
    pub message: String,
    pub group_id: String,
    pub group_name: String,
    /// Pre-approved tools for this skill execution.
    /// The calling agent should restrict LLM tool use to this list.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allowed_tools: Option<Vec<String>>,
    /// Recommended LLM model for this skill execution.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recommended_model: Option<String>,
    /// Expanded skill prompt (SKILL.md content).
    /// Inject this into the LLM conversation as system/user context.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skill_prompt: Option<String>,
    /// Skill description for context.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skill_description: Option<String>,
    /// Maximum tool calls allowed per execution (from group governance).
    /// The calling agent should enforce this limit during execution.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tool_calls_per_execution: Option<i64>,
    /// Output produced by the skill's scripts when the gateway dispatched the
    /// execution to the runner inline (REST path). Present only when the
    /// execution actually ran to completion; absent for a pure agent-handoff. Governed by skill_output
    /// guardrails before it reaches this field.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output: Option<String>,
}

// ---------------------------------------------------------------------------
// Gateway info
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct GatewayInfo {
    pub service: String,
    pub version: String,
    pub features: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct HealthResponse {
    pub status: String,
    pub service: String,
    pub components: serde_json::Value,
}

// ---------------------------------------------------------------------------
// Usage limit enforcement result
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct UsageEnforcementResult {
    pub blocked: bool,
    pub group_id: Option<String>,
    pub group_name: Option<String>,
    pub message: Option<String>,
    pub limit_type: Option<String>,
    pub current_usage: Option<String>,
    pub max_limit: Option<String>,
    pub warning_headers: Vec<(String, String)>,
}

impl UsageEnforcementResult {
    pub fn allowed() -> Self {
        Self {
            blocked: false,
            group_id: None,
            group_name: None,
            message: None,
            limit_type: None,
            current_usage: None,
            max_limit: None,
            warning_headers: Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_chat_completion_request_round_trip() {
        let json_str = json!({
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 100
        });
        let req: ChatCompletionRequest = serde_json::from_value(json_str).unwrap();
        assert_eq!(req.model, Some("gpt-4".to_string()));
        assert_eq!(req.messages.len(), 1);
        assert_eq!(req.messages[0].role, "user");
        assert_eq!(req.temperature, Some(0.7));
        assert_eq!(req.max_tokens, Some(100));
        assert!(req.stream.is_none());
    }

    #[test]
    fn test_chat_completion_request_minimal() {
        let json_str = json!({
            "messages": [{"role": "system", "content": "You are helpful."}]
        });
        let req: ChatCompletionRequest = serde_json::from_value(json_str).unwrap();
        assert!(req.model.is_none());
        assert!(req.model_id.is_none());
        assert!(req.temperature.is_none());
        assert!(req.max_tokens.is_none());
        assert_eq!(req.messages.len(), 1);
    }

    #[test]
    fn test_chat_completion_request_extra_fields_captured() {
        let json_str = json!({
            "messages": [{"role": "user", "content": "Hi"}],
            "custom_param": "custom_value"
        });
        let req: ChatCompletionRequest = serde_json::from_value(json_str).unwrap();
        assert_eq!(req.extra.get("custom_param").unwrap(), "custom_value");
    }

    #[test]
    fn test_chat_message_optional_fields_skipped_in_serialization() {
        let msg = ChatMessage {
            role: "assistant".to_string(),
            content: Some(json!("Hello!")),
            name: None,
            tool_calls: None,
            tool_call_id: None,
        };
        let serialized = serde_json::to_value(&msg).unwrap();
        // Optional fields with skip_serializing_if should be absent
        assert!(!serialized.as_object().unwrap().contains_key("name"));
        assert!(!serialized.as_object().unwrap().contains_key("tool_calls"));
        assert!(!serialized.as_object().unwrap().contains_key("tool_call_id"));
        // Required fields present
        assert_eq!(serialized["role"], "assistant");
        assert_eq!(serialized["content"], "Hello!");
    }

    #[test]
    fn test_usage_enforcement_result_allowed_defaults() {
        let result = UsageEnforcementResult::allowed();
        assert!(!result.blocked);
        assert!(result.group_id.is_none());
        assert!(result.group_name.is_none());
        assert!(result.message.is_none());
        assert!(result.limit_type.is_none());
        assert!(result.current_usage.is_none());
        assert!(result.max_limit.is_none());
        assert!(result.warning_headers.is_empty());
    }

    #[test]
    fn test_skill_execute_response_skips_none_optional_fields() {
        let resp = SkillExecuteResponse {
            execution_id: "exec-1".to_string(),
            skill_id: "skill-1".to_string(),
            skill_name: "test-skill".to_string(),
            skill_version: "1.0".to_string(),
            execution_status: "pending".to_string(),
            correlation_id: "corr-1".to_string(),
            message: "Started".to_string(),
            group_id: "group-1".to_string(),
            group_name: "Test Group".to_string(),
            allowed_tools: None,
            recommended_model: None,
            skill_prompt: None,
            skill_description: None,
            max_tool_calls_per_execution: None,
            output: None,
        };
        let serialized = serde_json::to_value(&resp).unwrap();
        let obj = serialized.as_object().unwrap();
        assert!(!obj.contains_key("allowed_tools"));
        assert!(!obj.contains_key("recommended_model"));
        assert!(!obj.contains_key("skill_prompt"));
        assert!(!obj.contains_key("skill_description"));
        assert!(!obj.contains_key("max_tool_calls_per_execution"));
        assert!(!obj.contains_key("output"));
        // Required fields present
        assert_eq!(obj["execution_id"], "exec-1");
        assert_eq!(obj["skill_name"], "test-skill");
    }

    #[test]
    fn test_skill_execute_response_includes_present_optional_fields() {
        let resp = SkillExecuteResponse {
            execution_id: "exec-2".to_string(),
            skill_id: "skill-2".to_string(),
            skill_name: "another-skill".to_string(),
            skill_version: "2.0".to_string(),
            execution_status: "running".to_string(),
            correlation_id: "corr-2".to_string(),
            message: "Running".to_string(),
            group_id: "group-2".to_string(),
            group_name: "Another Group".to_string(),
            allowed_tools: Some(vec!["tool_a".to_string(), "tool_b".to_string()]),
            recommended_model: Some("gpt-4".to_string()),
            skill_prompt: Some("Do the thing.".to_string()),
            skill_description: Some("A test skill".to_string()),
            max_tool_calls_per_execution: Some(10),
            output: Some("draft ready".to_string()),
        };
        let serialized = serde_json::to_value(&resp).unwrap();
        let obj = serialized.as_object().unwrap();
        assert_eq!(obj["allowed_tools"], json!(["tool_a", "tool_b"]));
        assert_eq!(obj["recommended_model"], "gpt-4");
        assert_eq!(obj["skill_prompt"], "Do the thing.");
        assert_eq!(obj["max_tool_calls_per_execution"], 10);
    }

    #[test]
    fn test_usage_info_deserialization() {
        let json_str = json!({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30});
        let usage: UsageInfo = serde_json::from_value(json_str).unwrap();
        assert_eq!(usage.prompt_tokens, 10);
        assert_eq!(usage.completion_tokens, 20);
        assert_eq!(usage.total_tokens, 30);
    }
}
