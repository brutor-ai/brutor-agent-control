# LLM provider adapters

The gateway's internal wire format for chat is the OpenAI Chat Completions
shape. An adapter translates that shape to one provider's API on the way out
and translates the provider's reply back on the way in. This directory holds
the adapters; `src/services/providers.rs` holds the pipeline that calls them.

## Where things are

| Path | Contents |
|---|---|
| `src/services/llm_adapter/mod.rs` | `LlmAdapter` and `StreamNormalizer` traits, `get_adapter()`, shared helpers, `make_test_model()` |
| `src/services/llm_adapter/<provider>.rs` | One adapter per file (table below) |
| `src/services/llm_adapter/openai_family.rs` | Chat Completions parameter rules of the OpenAI model families, shared by the OpenAI and Azure adapters |
| `src/services/providers.rs` | Request pipeline (`prepare_request` and its siblings), URL and header building, base URL discovery, parameter dropping, Claude version rules |
| `src/services/anthropic_adapter/` | A separate stack for the Anthropic Messages format: the native `/v1/messages` surface, and Claude on Bedrock / Vertex. Backends in `backend/` (`anthropic_direct.rs`, `bedrock.rs`, `vertex.rs`), client adapters in `client/` |

## Request pipeline

`providers::prepare_request(body, model)` is the chat path. Its steps, in
order:

1. Set `model` to `providers::wire_model_name(model)`; remove `model_id` and
   `extra`.
2. `inject_model_defaults`: top-level `null`s are removed first, then the
   model row's defaults fill what the caller left out (`temperature`,
   `max_tokens`, `top_p`, penalties, `seed`, `response_format`, `n`, `stop`,
   `logit_bias`, `logprobs`, `top_logprobs`, `stream_options`, `tools`,
   `tool_choice`, `parallel_tool_calls`, `user`).
3. `inject_system_prompt`: the row's `system_prompt` becomes the first
   message, before any system message of the caller.
4. `transform_for_provider`: `get_adapter(provider).transform_request(..)`.
   Then the `brutor_omit_reasoning` marker and any `thinking_blocks` left on
   messages are removed.
5. `drop_unsupported_params` (see "Parameter dropping").
6. Injected output parameters are clamped to the group's governance ceilings
   (`llm_governance::clamp_injected_params`), in the spelling the adapter
   uses for the output limit (`native_output_limit_key`).
7. Top-level `null` values are removed.

Other entry points in `providers.rs`: `prepare_text_completion` (converts
`prompt` to `messages` and runs steps 2 to 5 for every provider except
`openai`, `azure`, `azure_ai`, `text-completion-openai`),
`prepare_embedding`, and `prepare_responses`. The last two do not call an
adapter's `transform_request`.

URL and headers:

- `build_url(model, endpoint, stream)` = base URL + `adapter.chat_endpoint(..)`.
  The base is the row's `api_url` if set, else the known base of the row's
  own provider (`discover_base_url(provider)`). A row with neither has no
  destination: it is refused at dispatch (below) and `build_url` answers a
  `no-upstream-base://` URL that no HTTP client sends. There is no default
  provider. `providers::upstream_base_for` is the one place a base is chosen,
  for every provider API (LLM, images, speech, transcription, video, batch);
  `tests/provider_base_guard.rs` keeps OpenAI's host out of every other
  place.
- `build_headers_for(model, endpoint)` = `adapter.auth_headers_for_endpoint(..)`
  plus the row's `extra_headers`.

Response side: `providers::normalize_response(provider, body)` for a
non-streaming reply; `providers::needs_stream_normalization(provider)` and
`providers::create_stream_normalizer(provider)` for a stream. Read a provider
reply with `providers::upstream_json`, never `resp.json().unwrap_or_default()`
(`tests/upstream_body_guard.rs`).

### Azure

- `api-version` is a query parameter. `build_url` appends
  `api-version=<row.api_version>` for a provider whose name starts with
  `azure`, when the row has a non-empty `api_version`. It is not sent as a
  header.
- `wire_model_name` strips the catalog prefix: `azure/` for provider `azure`,
  `azure_ai/` for provider `azure_ai`. Every other provider's name is sent as
  the row has it.
- Authentication is the `api-key` header, not Bearer.

## The `LlmAdapter` trait

```rust
pub trait LlmAdapter: Send + Sync {
    fn provider_name(&self) -> &str;
    fn chat_endpoint(&self, model: &LlmModel, default: &str, stream: bool) -> String;
    fn auth_headers_for_endpoint(&self, model: &LlmModel, endpoint: &str) -> Vec<(String, String)>;
    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)>;
    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, model: &LlmModel);
    fn unsupported_params(&self) -> &'static [&'static str];
    fn needs_stream_normalization(&self) -> bool;
    fn create_stream_normalizer(&self) -> Box<dyn StreamNormalizer>;
    fn normalize_response(&self, body: &mut Value);
}
```

Only `provider_name` is required. Defaults:

| Method | Default |
|---|---|
| `chat_endpoint` | returns `default` unchanged |
| `auth_headers_for_endpoint` | calls `auth_headers` |
| `auth_headers` | `Authorization: Bearer <api_key>` when the row has a key |
| `transform_request` | no-op |
| `unsupported_params` | empty |
| `needs_stream_normalization` | `false` |
| `create_stream_normalizer` | pass-through normalizer |
| `normalize_response` | no-op |

`unsupported_params` is not read by the pipeline. Parameter dropping is driven
by the database (next section). Only `CloudflareAdapter` still overrides it,
and that list has no effect on a request.

```rust
pub trait StreamNormalizer: Send {
    /// One provider SSE data payload in, zero or more OpenAI-format
    /// SSE data strings out. `["[DONE]"]` ends the stream.
    fn convert(&mut self, data: &str) -> Vec<String>;
}
```

## Parameter dropping

`providers::drop_unsupported_params(body, model)` removes, in this order:

1. The provider row's `additional_drop_params`
   (`model.provider_additional_drop_params`, stamped onto the model from
   `llm_providers`), only when the model's `drop_params` is true. An unset
   `drop_params` counts as true.
2. The model row's own `additional_drop_params`, always.
3. `temperature` and `top_p` for provider `anthropic` when
   `anthropic_sampling_params_retired(model_name)` is true (Claude 4.8 and
   later).
4. `temperature` when `capabilities.temperature_locked` is true.
5. `tools`, `tool_choice`, `functions`, `function_call` when
   `capabilities.supports_function_calling` is `false`.

Both lists are JSON arrays of parameter names; non-string items are ignored.
The provider-wide defaults are seeded by the control plane
(`PROVIDER_SEED_CATALOG` in
`brutor-gateway-control/app/database/init_llm_providers.py`). A provider that
rejects a parameter gets it added there, not in an adapter.

## Adapter registry

`get_adapter(provider)` in `mod.rs`. An unknown provider name gets
`OpenAiAdapter`.

| Provider name(s) | Adapter (file) | What it overrides |
|---|---|---|
| `openai`, any unknown name | `OpenAiAdapter` (`openai.rs`) | `transform_request` = `openai_family::apply` |
| `azure`, `azure_ai` | `AzureAdapter` (`azure.rs`) | `api-key` auth header; `transform_request` = `openai_family::apply` |
| `anthropic` | `AnthropicAdapter` (`anthropic.rs`) | endpoint, auth, full request and response translation, stream normalizer |
| `gemini`, `google_ai_studio`, `vertex_ai` | `GeminiAdapter` (`gemini.rs`) | endpoint, auth, full request and response translation to the native Gemini REST API, stream normalizer |
| `cohere` | `CohereAdapter` (`cohere.rs`) | `n` to `num_generations`, `top_p` to `p`, `stop` to `stop_sequences`; removes `logprobs`, `top_logprobs`, `logit_bias`, `parallel_tool_calls`, `suffix`, `user`, `stream_options` |
| `mistral`, `codestral` | `MistralAdapter` (`mistral.rs`) | `max_completion_tokens` to `max_tokens`; `tool_choice` `required` or an object becomes `any`; removes `strict`, `$id`, `$schema`, `additionalProperties` from tools; removes `name` from non-tool messages; removes empty assistant messages |
| `xai` | `XaiAdapter` (`xai.rs`) | `max_completion_tokens` to `max_tokens`; removes `name` from messages and `strict` from tools; response: empty `finish_reason` with tool calls becomes `tool_calls` |
| `ollama`, `lm_studio` | `OllamaAdapter` (`ollama.rs`) | endpoint is `/v1` + default; `max_tokens` / `max_completion_tokens` to `num_predict` |
| `bedrock` | `BedrockAdapter` (`bedrock.rs`) | `reasoning_effort` to `thinking {type: enabled, budget_tokens}` (low/minimal 1024, medium 10000, high 15000, other 10000); `budget_tokens` raised to at least 1024. The Converse translation that follows sends neither (see "Amazon Bedrock") |
| `replicate` | `ReplicateAdapter` (`replicate.rs`) | `Authorization: Token <key>`; `max_tokens` to `max_length` (name contains `vicuna` or `flan`), kept (contains `codellama`), else `max_new_tokens`; `stop` to `stop_sequences` |
| `databricks` | `DatabricksAdapter` (`databricks.rs`) | `max_completion_tokens` to `max_tokens`; removes `name` from user messages |
| `openrouter` | `OpenRouterAdapter` (`openrouter.rs`) | `max_completion_tokens` to `max_tokens` |
| `groq` | `GroqAdapter` (`groq.rs`) | response and stream: `reasoning` renamed to `reasoning_content` |
| `cloudflare` | `CloudflareAdapter` (`cloudflare.rs`) | `unsupported_params` only (unused, see above) |
| `perplexity` | `PerplexityAdapter` (`perplexity.rs`) | nothing |
| `deepseek` | `DeepseekAdapter` (`deepseek.rs`) | nothing |
| `together_ai`, `fireworks_ai`, `deepinfra` | `TogetherFireworksAdapter` (`together.rs`) | nothing; `provider_name()` is `together_ai` for all three |
| `moonshot` | `MoonshotAdapter` (`moonshot.rs`) | nothing |
| `minimax` | `MinimaxAdapter` (`minimax.rs`) | nothing |
| `ai21` | `Ai21Adapter` (`ai21.rs`) | nothing |

`discover_base_url` in `providers.rs` knows more provider names than
`get_adapter` (for example `voyage`, `jina_ai`, `cerebras`, `sambanova`,
`huggingface`); those use `OpenAiAdapter`. It has no entry for `azure`,
`azure_ai`, `bedrock` (whose URL comes from the row's region), `vertex_ai`, `ollama`, `lm_studio`, `codestral`,
`cloudflare`, `replicate` or `databricks`: those rows need an `api_url`.

## Model families: the catalog row decides

`openai_family::chat_rules(model)` returns three flags for the OpenAI and
Azure adapters. The catalog row's `capabilities` JSON is read first; a name
prefix is the fallback.

| Rule | From the row | Fallback by name |
|---|---|---|
| Reasoning model: `temperature` and `top_p` are removed, the output limit is `max_completion_tokens` | `capabilities.supports_reasoning = true`, providers `openai` and `azure` only | name starts with `o1`, `o3`, `o4`, `gpt-5`, `gpt-6` |
| `max_tokens` is renamed to `max_completion_tokens` | a reasoning model, as above | name starts with `gpt-4o` |
| Function tools only with `reasoning_effort: "none"` | `capabilities.tools_require_reasoning_effort_none` (either value is final) | `gpt-6-sol`, `gpt-6-luna` and their `-`suffixed snapshots; `gpt-5.6-*` on Azure |

- The row can only switch the first two rules on. `supports_reasoning: false`
  does not switch a known family off.
- The name is lower-cased, and for `azure` / `azure_ai` the `azure/` prefix is
  removed before matching (`family_name`).
- `openai_family::apply` (the adapters' `transform_request`): renames
  `max_tokens`; with tools and no `reasoning_effort` on a tool-rule model it
  sets `reasoning_effort: "none"`; with the row's `thinking_enabled` it fills
  `reasoning_effort: "medium"` if absent; for `thinking_enabled` or a
  reasoning model it removes `temperature` and `top_p`. A caller's own
  `reasoning_effort` is never replaced.
- `openai_family::chat_request_conflict` is called by the chat route
  (`src/routes/llm.rs`) before sending: tools plus a caller-chosen
  `reasoning_effort` other than `"none"` on a tool-rule model is answered
  with 400.
- `prepare_responses` uses the same `chat_rules(model).reasoning` flag.

A new OpenAI or Azure model that follows an existing rule needs a catalog
row, not a code change.

## Anthropic adapter (`anthropic.rs`)

Endpoint `/messages`. Headers `x-api-key` and `anthropic-version: 2023-06-01`.

Request (`transform_request`):

- Top-level `null`s removed. `max_completion_tokens` becomes `max_tokens`; a
  missing or non-positive `max_tokens` is set to the row's
  `default_max_tokens`, else 4096.
- `stop` becomes `stop_sequences` (a string is wrapped in an array).
  `user` becomes `metadata.user_id`.
- System messages with string content move to top-level `system` as an array
  of `{type: "text", text}` blocks, one per message.
- `cache_control` is removed from messages and tools.
- `tool_choice`: `auto` to `{type: auto}`, `required` to `{type: any}`,
  `{function: {name}}` to `{type: tool, name}`; `none` removes `tools` and
  `tool_choice`. Tools become `{name, description, input_schema}`. An empty
  `tools` array is removed together with `tool_choice`.
- Messages: assistant `tool_calls` become `tool_use` blocks; `role: tool`
  becomes a `user` message with a `tool_result` block; `reasoning_content` is
  removed; `thinking_blocks` (`thinking` / `redacted_thinking` only) are put
  back first in the assistant content; consecutive messages of one role are
  merged.
- Removed: `n`, `logprobs`, `top_logprobs`, `logit_bias`,
  `frequency_penalty`, `presence_penalty`, `parallel_tool_calls`,
  `response_format`, `seed`, `suffix`, `stream_options`.

Sampling parameters and thinking. The version is parsed from the model name
by `providers::claude_version` on the first-party form of the id
(`claude_base_name` strips Bedrock and Vertex decorations):

| Rule | Applies to | Function |
|---|---|---|
| `temperature` removed | `claude-opus-4-7*` | inline in `transform_request` |
| `temperature` and `top_p` removed | Claude 4.8 and later, every Claude 5 | `anthropic_sampling_params_retired` |
| `top_p` removed when both `temperature` and `top_p` are present | `claude-opus-4*`, `claude-sonnet-4*`, `claude-haiku-4*` | inline |
| Thinking block is `{type: "adaptive"}` | Claude 4.6 and later | `anthropic_uses_adaptive_thinking` |
| Thinking block also carries `display: "summarized"` | Claude 4.7 and later | `anthropic_thinking_summary_is_opt_in` |
| Thinking block is `{type: "enabled", budget_tokens: 10000}` | before Claude 4.6 | inline |

- The thinking block is injected when the row's `thinking_enabled` is true
  and the request has no `thinking` of its own.
- With thinking on (any `thinking.type` except `disabled`): `top_p` and
  `top_k` are removed; `temperature` is removed where it is retired, else set
  to 1.0 if present.
- `inject_model_defaults` does not inject `temperature` / `top_p` for
  provider `anthropic` on Claude 4.8 and later, and
  `drop_unsupported_params` removes them again after the adapter.
- When the route sets `brutor_omit_reasoning: true` in the body
  (`providers::OMIT_REASONING_MARKER`), the adapter calls
  `withhold_anthropic_thinking`: on Claude 4.7 and later an active thinking
  block gets `display: "omitted"`. Older models are left as they are.

Response (`normalize_response`, only for a body with `type: "message"`):
`text` blocks are joined into `content`; `tool_use` becomes `tool_calls`;
`thinking` text becomes `reasoning_content`; the original `thinking` /
`redacted_thinking` blocks are returned as `thinking_blocks`; `stop_reason`
maps `end_turn` / `stop_sequence` to `stop`, `max_tokens` to `length`,
`tool_use` to `tool_calls`; usage maps to `prompt_tokens` /
`completion_tokens` / `total_tokens`, keeping non-zero
`cache_creation_input_tokens` and `cache_read_input_tokens`. The id becomes
`chatcmpl-<id>`.

## Structured output for Claude

A caller's `response_format: {type: "json_schema", json_schema: {schema}}`
is sent as the Messages API's `output_config.format: {type: "json_schema",
schema}` (`AnthropicAdapter::transform_request`), the schema without the
constraints the API does not take (`filter_schema_constraints` moves them
into the descriptions). A caller's own `output_config` is kept. A row whose
capabilities say `supports_response_schema: false` gets none, and
`{type: "json_object"}` has no equivalent: both are dropped, with a
warning in the log.

Why `json_object` has none: Claude's JSON output is
`output_config.format` with `type: "json_schema"` and a schema in which
`additionalProperties` must be `false`; an object with no listed keys can
only come back as `{}`
(<https://platform.claude.com/docs/en/build-with-claude/structured-outputs>,
"Objects with arbitrary keys"). There is no request that
means "any JSON object", so the rule below for a parameter a provider has
no field for applies: it is not sent. The caller's answer is not
constrained to JSON; a caller that needs that sends a `json_schema`.

## Amazon Bedrock: what is served and what is refused

Two stacks serve the providers `bedrock` and `bedrock_converse` alike; the
model id decides (the row's name without the catalog's `bedrock/` and
`converse/` prefixes, the foundation-model part of an inference-profile id
or ARN):

| Family (model id) | Served by | Bedrock Runtime operation | Route |
|---|---|---|---|
| Claude (`anthropic`, `claude` in the name) | `anthropic_adapter` (below) | `InvokeModel` with an Anthropic body | chat, Messages |
| `amazon.nova*` (not Canvas, Reel, Sonic, embeddings), `mistral.*`, `deepseek.*` | `services/bedrock_runtime/converse.rs` | `Converse`, `ConverseStream` | chat |
| `amazon.titan-embed-text*` | `services/bedrock_runtime/embeddings.rs` | `InvokeModel`, one text per call | embeddings |
| `cohere.embed-english-v3`, `cohere.embed-multilingual-v3` | the same | `InvokeModel`, up to 96 texts per call | embeddings |

Everything else is refused at dispatch with `unsupported_provider_api`,
501 (`providers::unsupported_provider_api(model, api)`): a Bedrock row of
any other family on every route, and the families above on every route but
theirs (a Nova row on embeddings, responses, completions, Messages or
batch; an embedding row on chat; a Claude row on responses, completions,
embeddings, the media routes, batch, or as a chat fallback). The check runs
in `llm_preflight::enforce_actor_planes` and again where a routing-group
member or fallback is sent.

**The rule for a cloud row** (
`providers::dispatch_refusal`, `providers::cloud_backend`): a row of
Amazon Bedrock (`bedrock`, `bedrock_converse`) or Vertex AI (`vertex_ai`,
`vertex`: one provider) is served by that provider's own backend on the
route in question, authenticated with the cloud credential in the row's
`provider_config`, or it is refused at dispatch. It never reaches a
forwarder that authenticates with an API key, with or without an
`api_url`. A credential in `api_key` (`KEY_ID:SECRET`, a base64 service
account) is not read: such a row has no credential
(`provider_credential_missing`).

| Vertex AI row | Served by | Route |
|---|---|---|
| Claude | `anthropic_adapter` (`rawPredict`, `streamRawPredict`) | chat, Messages |
| Gemini | `routes/gemini.rs` (`generateContent`) | chat, the Gemini route |
| another publisher model of Google | the same | the Gemini route |

`bedrock_runtime` (`src/services/bedrock_runtime/`):

- **URL.** The row's `api_url` if it has one, else
  `https://bedrock-runtime.<region>.amazonaws.com`, then
  `/model/<id>/converse`, `/converse-stream` or `/invoke`, the id as one
  percent-encoded path segment. The region is derived as the Claude backend
  derives it (`provider_config.aws_region`, `model_metadata.aws_region`, a
  Bedrock Runtime `api_url`) and must be a region name. There is no default
  region: a row that names none is refused (`provider_endpoint_invalid`,
  501), with or without an `api_url`, since the request is signed for a
  region. A Vertex AI row that names no location (`gcp_location`, or `region`
  in its metadata) and has no `api_url` is refused the same way. `providers::build_url`
  answers this URL for such a row.
- **Signature.** SigV4 by the Claude backend's signing code, service
  `bedrock`, over the canonical form of the path as sent, with the row's
  own credential (`provider_config`, and nothing else). A row
  with no credential is refused (`provider_credential_missing`, 501):
  nothing is sent unsigned and no credential is read from the environment.
  The credential is never put in a header.
- **Chat.** `forward_chat_completion{,_streaming}` run `prepare_request` as
  for every provider, then `converse::to_converse` turns the prepared body
  into the Converse body (the table in `converse.rs`). A parameter Converse
  has no field for is not sent. A feature AWS does not document for the
  model (`bedrock_runtime::features`: tools, a forced tool choice, tools
  with streaming, images, structured output) is refused with 400 naming it
  (`converse::request_conflict`, called by the chat route); as a fallback or
  a routing-group member such a candidate is skipped. The reply comes back
  through `converse::response_to_openai`; a stream through
  `converse::openai_sse_response`, which decodes the event stream and hands
  the route the SSE of an OpenAI-compatible provider (usage from the
  `metadata` event, `[DONE]` only after `messageStop`).
- **Embeddings.** `embeddings::embed`, called by the embeddings route
  through `llm_router::send_first_success_with`. At most
  `MAX_CONCURRENT_CALLS` (4) calls in flight; one failed call fails the
  request, starts no further call, and the calls that succeeded are
  recorded: their tokens and cost on the audit row of the failed request
  and as one usage row with the failure's status; the error body
  carries them as `usage`. Titan reports `inputTextTokenCount`; AWS documents no token count
  for Cohere Embed v3, so the gateway records AWS's approximation for the
  model, one token per four characters, rounded up per text.
- **Errors.** Bedrock's HTTP status is kept (429 throttling, 400
  validation, 403 access denied, …) and the body becomes the OpenAI error
  shape (`bedrock_runtime::error_body`).

`BedrockAdapter` in this directory is what `prepare_request` runs for
provider `bedrock` before that translation; none of what it writes
(`thinking`) is a Converse field.

## Rows with no destination

A row with no `api_url`, of a provider the gateway knows no base URL for on
the API in question, is refused at dispatch with `provider_api_url_missing`,
501, and an audit row; nothing is connected to and the row's credential
stays in the gateway (it is never sent to
OpenAI). Both refusals are one check, `providers::dispatch_refusal(model,
api)`: on the requested model in `llm_preflight::enforce_actor_planes`
(every LLM, media and batch route), on every chat fallback and routing-group
member in `llm_router::forward_chat_completion{,_streaming}`, and on every
model-level fallback candidate in `llm_router::resolve_fallback_chain`
(embeddings, speech, transcription, images, streaming chat), which drops a
candidate it refuses. The rows whose URL a native backend builds from the
region or project are exempt where that backend serves them: Claude on
Bedrock or Vertex and Gemini on Vertex on the chat route, Bedrock and Vertex
rows on the Messages route, Vertex rows on the Gemini route, and the
Bedrock rows `bedrock_runtime` serves on the chat and embeddings routes. On
the other routes (responses, completions, embeddings) those rows need an
`api_url`.

## Claude on Bedrock and Vertex

The chat route does not send these through `prepare_request`.
`anthropic_adapter::should_route_via_openai_surface(model)` is true for
provider `bedrock` with a model name containing `anthropic` or `claude`, and
for `vertex_ai` / `vertex` with a name containing `claude`. Such a request is
translated by `translate_openai_to_anthropic` in
`src/services/anthropic_adapter/mod.rs` (`reasoning_effort` to a thinking
block, then `AnthropicAdapter::transform_request`, then
`drop_unsupported_params`) and forwarded through the native pipeline. The
backend (`anthropic_adapter::backend::get_backend`: `anthropic`, `bedrock`,
`vertex_ai`) builds the URL, the authentication (SigV4 for Bedrock, an access
token for Vertex) and the stream conversion. The reply is translated back
with `AnthropicAdapter::normalize_response` or `AnthropicStreamNormalizer`.

What holds for these rows:

- **Hosts.** A host of AWS or Google that has a configured part is built in
  `services/cloud_hosts.rs` and nowhere else (`tests/cloud_host_guard.rs`).
  The AWS region and the Google Cloud location are names (`a-z 0-9 -`) or
  the row is refused at dispatch, 501 `provider_endpoint_invalid`, before
  any connection. `BedrockBackend::endpoint_url` is the one builder of a
  Bedrock Runtime URL (Claude, Converse, embeddings): the model id is one
  percent-encoded path segment. `VertexBackend::publisher_model_url` is the
  one for Vertex (Claude, and Gemini on the Gemini route); the catalog's
  `vertex_ai/` prefix is not part of the model id.
- **Bedrock credential.** A Claude row is signed with its own credential
  (`backend::bedrock::sign_request`: SigV4 over the URI-encoded path, the
  host as sent and the bytes that are sent, once) or refused, 501
  `provider_credential_missing`. Nothing is sent unsigned.
- **Vertex credential.** The token provider is the row's own service
  account (`provider_config.gcp_credentials_json`, cached per credential).
  A row that carries none is refused, 501 `provider_credential_missing`:
the gateway process's own Google identity (Application Default
  Credentials) is the operator's and is never used for a row. A row that
  names no project is refused, 501 `provider_endpoint_invalid`.
- **The gateway's own cloud identity.** No tenant traffic is authenticated
  with it (`tests/cloud_identity_guard.rs`). The Bedrock guardrail signs
  with the credential and region of its own configuration, or it is a
  check that cannot be evaluated and the policy's `fail_open` decides the
  call. The media store's S3 client is the operator's own infrastructure
  and uses the operator's AWS identity (`BRUTOR_MEDIA_BUCKET`, `BRUTOR_MEDIA_S3_REGION`).
- **No credential in a header.** `cloud_auth.rs` is the shape of
  `provider_config` and nothing else. A row with `auth_type` `aws_iam` or
  `gcp_service_account` on a provider that is not a cloud (it would go to
  a forwarder that authenticates with an API key) is refused, 501
  `provider_auth_unsupported`; `azure_ad`
  likewise unless the row also has an API key (the client-credentials flow
  is not implemented).
- **Bedrock streams.** `InvokeModelWithResponseStream` answers AWS's binary
  event stream. `BedrockEventStreamConverter` decodes it with the one
  decoder of the gateway (`next_event_stream_message`, also Converse's):
  lengths checked before anything is sliced, prelude and message CRC-32
  verified. A malformed frame or an exception message ends the stream with
  an Anthropic `error` event (on the chat route, an `{"error": …}` chunk).
  An upstream that answers `text/event-stream` is read as SSE.

`BedrockAdapter` in this directory therefore never sees a Claude row (see
"Amazon Bedrock" above for the rows it does see). Gemini on Vertex goes
through
`routes::gemini::forward_via_openai_surface`, which uses `GeminiAdapter`.

## Gemini adapter (`gemini.rs`)

Targets the native REST API, not Google's OpenAI-compatible endpoint.

- Endpoint: `/v1beta/models/<name>:generateContent`, or
  `:streamGenerateContent?alt=sse` when streaming. A leading `models/` in the
  model name is removed. Embeddings are the exception:
  `/v1beta/openai/embeddings`.
- Auth: `x-goog-api-key`; `Authorization: Bearer` for `/embeddings`.
- Request: `messages` to `contents` and `systemInstruction`; `tools` to
  `functionDeclarations`; `tool_choice` to `toolConfig.functionCallingConfig`;
  `response_format` to `responseMimeType` / `responseSchema`; `temperature`,
  `top_p`, `top_k`, `max_tokens` / `max_completion_tokens`, `n`, `stop` to
  `generationConfig` (`temperature`, `topP`, `topK`, `maxOutputTokens`,
  `candidateCount`, `stopSequences`); `thinking_config`, or the row's
  `thinking_enabled` (`thinkingBudget: 10000`), to
  `generationConfig.thinkingConfig`; `web_search_options` to a `googleSearch`
  tool; `audio.voice` to `speechConfig` and `responseModalities`. `model`,
  `stream` and the OpenAI-only parameters are removed from the body.
- Response: `candidates` become `choices`; `functionCall` parts become
  `tool_calls` with a synthesised id; `finishReason` maps `STOP` to `stop`,
  `MAX_TOKENS` to `length`, `SAFETY` / `RECITATION` to `content_filter`;
  `usageMetadata` maps to `usage` and is also kept on the body.

## Streaming normalisation

Three adapters return `needs_stream_normalization() == true`:

| Adapter | Normalizer | What it does |
|---|---|---|
| `AnthropicAdapter` | `AnthropicStreamNormalizer` | Stateful. `message_start` to a role chunk (with usage if present); `text_delta` to `delta.content`; `tool_use` / `server_tool_use` blocks and `input_json_delta` to `delta.tool_calls`; `thinking_delta` to `delta.reasoning_content`; at `content_block_stop` the rebuilt thinking block (text and signature) is emitted as `delta.thinking_blocks`; `message_delta` to a chunk with `finish_reason` and usage; `message_stop` to `[DONE]`. Unparseable or unknown events produce nothing. |
| `GeminiAdapter` | `GeminiStreamNormalizer` | Stateful. Native `candidates` chunks to `chat.completion.chunk`; a usage chunk when `usageMetadata` is present. Unparseable payloads are forwarded unchanged. |
| `GroqAdapter` | `GroqStreamNormalizer` | Renames `choices[].delta.reasoning` to `reasoning_content`. Everything else is forwarded unchanged. |

All other adapters use the pass-through normalizer.

## Shared helpers (`mod.rs`, `pub(crate)`)

| Function | Purpose |
|---|---|
| `filter_schema_constraints(schema)` | Recursively removes `minLength`, `maxLength`, `minItems`, `maxItems`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` and appends them to `description`. Recurses into `properties`, `items`, `$defs`, `anyOf` / `allOf` / `oneOf`. |
| `strip_name_from_messages(obj)` | Removes `name` from every message. |
| `strip_strict_from_tools(obj)` | Removes `strict` from each tool and its `function`. |
| `content_to_blocks(content)` | String, null or array content to a `Vec<Value>` of content blocks. |

## Adding a provider

A provider with an OpenAI-compatible API needs no adapter: an unknown
provider name gets `OpenAiAdapter`. Add its base URL to `discover_base_url`
(or set `api_url` on the rows) and put the parameters it rejects in the
provider's `additional_drop_params`.

When the provider needs its own translation:

1. Create `src/services/llm_adapter/<provider>.rs` with a unit struct that
   implements `LlmAdapter`. Override only what differs.
2. In `mod.rs`: add `mod <provider>;`, the `use`, and the match arm in
   `get_adapter()`.
3. Add the base URL to `discover_base_url()` in `src/services/providers.rs`
   if the provider has a fixed one.
4. Add the provider name to `ALL_PROVIDERS` in the tests of
   `src/services/providers.rs`. That test
   (`the_output_ceiling_reaches_every_provider_in_its_own_spelling`) checks
   that every adapter sends exactly one output limit within the governance
   ceiling. An adapter that renames the output limit to a new key must also
   add that key to `llm_governance::OUTPUT_LIMIT_KEYS`.
5. Add unit tests in the adapter file.
6. Optional: add `("<provider>", "<model>")` to the `providers` array in
   `benches/adapter_transform.rs`.

Rules for `transform_request`:

- When renaming a parameter, do not overwrite a target key the caller already
  set (`if !obj.contains_key(target)`).
- Do not hard-code a list of parameters to drop; use `additional_drop_params`.
- Do not key behaviour on a model name where a catalog capability can carry
  it. Where a name fallback is needed, keep one definition
  (`openai_family.rs` is the example).

## Tests and benchmarks

Unit tests live in a `#[cfg(test)] mod tests` block in each adapter file, in
`mod.rs`, and in `providers.rs`.

```bash
cargo test --lib llm_adapter            # all adapter unit tests
cargo test --lib llm_adapter::ollama    # one adapter
cargo test --lib services::providers    # pipeline, URL, headers, parameter dropping
```

`make_test_model(provider, model_name)` (`mod.rs`, `#[cfg(test)]`,
`pub(crate)`) builds an `LlmModel` with `enabled: true`, the API key
`sk-test-key` and every optional field `None`.

Integration tests (feature `db-tests`, need Postgres and Redis; see
`tests/integration/README.md`) that check what reaches a mock upstream:

- `tests/integration/llm_model_family_rules.rs`: the OpenAI / Azure family
  rules and the Claude sampling-parameter rule.
- `tests/integration/bedrock_claude.rs`: Claude on Bedrock (hosts,
  credential, signature, event stream), Vertex hosts and credentials, and
  the cloud credentials of a row never reaching an upstream.
- `tests/integration/bedrock_runtime.rs`: Converse, ConverseStream and
  InvokeModel embeddings against a mock Bedrock, the SigV4 signature
  checked by the test's own implementation.
- `tests/integration/azure_request_shape.rs`
- `tests/integration/anthropic_native.rs`, `gemini_native.rs`,
  `gemini_surface.rs`
- `tests/integration/upstream_error_bodies.rs`

Benchmarks (criterion, `[[bench]]` entries in `Cargo.toml`):

```bash
cargo bench --bench adapter_transform    # transform_request per provider, Anthropic normalize_response
cargo bench --bench stream_normalizer    # AnthropicStreamNormalizer
cargo bench --bench anthropic_adapter    # the anthropic_adapter stack
```
