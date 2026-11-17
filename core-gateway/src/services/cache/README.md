# Cache layer (`src/services/cache`)

In-process caches of the configuration the gateway reads from Postgres, the
listener that keeps them fresh, and the Redis-backed usage counters. The
config caches are per process; a Community gateway is one process (see
[Instances](#instances)).

## Files

| File | Contents |
|------|----------|
| `mod.rs` | `ResourceCache` trait, `Generation` / `insert_fresh`, `InvalidationMessage`, `INVALIDATION_CHANNEL`, `CacheInvalidator`, `CacheStatsSnapshot` |
| `invalidation.rs` | `InvalidationHandler`, `InvalidationRegistry`, `BUILTIN_INVALIDATION_HANDLERS`, `unregistered_type` |
| `layers.rs` | `CacheLayers` / `CacheLayer`: the layer registry of the System Operations console |
| `model_cache.rs` | `ModelCache` |
| `provider_cache.rs` | `ProviderCache`, `LlmProvider` |
| `group_cache.rs` | `ResourceGroupCache`, `EffectiveGroupData`, `GroupExtras`, `GroupLoader` |
| `mcp_cache.rs` | `McpServerCache` |
| `skill_cache.rs` | `AgentSkillCache` |
| `agent_card_cache.rs` | `AgentCardCache`, `CachedAgentCard`, `AgentCardCapability` |
| `usage_state.rs`, `usage_state/` | `UsageState` (Redis counters), the admission engine, `mem_kv` (test store) |
| `invalidation_rig.rs` | Seeded `CacheInvalidator` for tests; compiled only under `test` or the `db-tests` feature |

Caches the listener also keeps fresh but that live elsewhere:
`GuardrailsCache` (`services/guardrails/mod.rs`), `ArgumentPolicyCache`
(`services/argument_policy/cache.rs`), `SemanticPolicyCache`
(`services/semantic_policy.rs`), `MaintenanceCache`
(`middleware/maintenance.rs`) and the run-ceiling cache
(`services/run_budget.rs`).

All of them are fields of `AppState` (`src/services/mod.rs`), built in
`AppState::new`.

## Redis is required

`GatewayBuilder` (`src/builder.rs`) connects to Redis before the state exists
and refuses to start when it cannot (`"Redis is required: ..."`). There is no
mode without Redis: `REDIS_URL` is a required variable with no default
(`src/env_vars.rs`), so a start without it is refused before the builder runs.
`AppConfig::redis_url` is unset only in a configuration a test builds
(`AppConfig::for_test`), and the builder refuses that one the same way.

## Config caches

moka (`moka::future::Cache`), `Arc<T>` values, built with compile-time
constants. No environment variable changes their TTL or capacity.

| Cache | Sub-cache | Key | Value | TTL | Max entries |
|-------|-----------|-----|-------|-----|-------------|
| `ModelCache` | `models` | `{tenant_id}:{id \| model_name \| name}` | `Arc<LlmModel>` | 300 s | 10 000 |
| | `routing_groups` | `{tenant_id}:{group_name}` | `Arc<LlmRoutingGroup>` | 300 s | 1 000 |
| `ProviderCache` | `providers` | `{tenant_id}:{provider_id}` | `Arc<LlmProvider>` | 600 s | 10 000 |
| `ResourceGroupCache` | `groups` | `{tenant_id}:{group_id}` | `Arc<EffectiveGroupData>` | 300 s | 5 000 |
| | `user_groups` | `{tenant_id}:{user_id}` | `Arc<Vec<String>>` (group ids) | 300 s | 50 000 |
| | `api_key_groups` | `{api_key_hash}` (no tenant prefix) | `Arc<(tenant_id, Option<group_id>)>` | 300 s | 50 000 |
| | `card_services` | `{tenant_id}` | `Arc<HashMap<card_id, ai_system_id>>` | 300 s | 5 000 |
| `McpServerCache` | `servers` | `{tenant_id}:{server_id}` and `{tenant_id}:{server_name}` | `Arc<McpServer>` | 300 s | 5 000 |
| | `configs` | `{tenant_id}:{config_id}` | `Arc<ServerConfig>` | 300 s | 5 000 |
| `AgentSkillCache` | `skills` | `{tenant_id}:{skill_id}` and `{tenant_id}:{skill_name}` | `Arc<AgentSkill>` | 300 s | 5 000 |
| `AgentCardCache` | `by_id` | `{tenant_id}:id:{card_id}` | `Arc<CachedAgentCard>` | 300 s | 5 000 |
| | `by_name` | `{tenant_id}:name:{card_name}` | same `Arc` | 300 s | 5 000 |

Notes per cache:

- **ModelCache.** `get_model` inserts a loaded model under the lookup key,
  its id, its `model_name` and (when it differs from `model_name`) its `name`; `get_model_by_id` under its id and
  `model_name`. On load the `provider` slug is stamped from
  `ProviderCache`, and `api_key` / `provider_config` are decrypted, so cached
  models hold plaintext
  credentials. `invalidate(tenant, id)` evicts every lookup key of the model
  (one `SELECT` on `llm_model` when the entry is not cached). The members of
  a routing group are not cached: the router reads them per request, with the
  availability predicate, so a member that retires or whose provider is
  switched off stops being picked at once.
- **ProviderCache.** Held as `Arc<ProviderCache>` (the struct is not `Clone`);
  `ModelCache` holds the same `Arc`. `slug`, `get`, `stamp_one`,
  `stamp_models`, `slugs_for`, `providers_for`.
- **ResourceGroupCache.** `EffectiveGroupData` is the group plus
  `effective_model_ids`, `effective_mcp_ids`, `effective_skill_ids`,
  `effective_agent_card_ids`, `effective_a2a_limits`,
  `a2a_governance_levels` (each level's own A2A policy document, unmerged),
  `member_user_ids` (direct only), `ancestor_ids`
  and `extras`. `extras` (`GroupExtras`) holds what registered `GroupLoader`s
  added; it is empty in a Community gateway. A loader runs inside the group
  load and an error from it fails the load. `invalidate_tenant` clears
  `api_key_groups` for every tenant, because its keys carry no tenant prefix.
- **McpServerCache / AgentSkillCache / AgentCardCache.** `invalidate(tenant,
  id)` also evicts the name-keyed entry that holds the same row, by a
  predicate scan within the tenant prefix.

Other caches (not moka; `HashMap` or `DashMap` with a timestamp per entry):

| Cache | Key | TTL |
|-------|-----|-----|
| `GuardrailsCache` | `{tenant_id}:u:{user_id}`, `{tenant_id}:g:{group_id}`, `{tenant_id}:__all__` | 60 s (`config::fixed::GUARDRAIL_CACHE_TTL_SECS`) |
| `ArgumentPolicyCache` | prefixed `{tenant_id}:` | same setting |
| `SemanticPolicyCache` | prefixed `{tenant_id}:` | same setting |
| `MaintenanceCache` | per tenant | 1 s |
| run-ceiling cache (`run_budget`) | process-global `DashMap` | 60 s |

`GuardrailsCache` decrypts provider `api_key` values on load. Its
`invalidate(tenant, id)` drops the whole tenant.

## The `ResourceCache` trait

```rust
fn name(&self) -> &str;
async fn invalidate(&self, tenant_id: &str, resource_id: &str);
async fn invalidate_tenant(&self, tenant_id: &str);
async fn invalidate_all(&self);
```

Implemented by the six caches of this directory, `GuardrailsCache` and
`ArgumentPolicyCache`.

Three rules every moka-backed cache here follows:

1. **`.support_invalidation_closures()` on the builder.** Without it
   `invalidate_entries_if` returns an error, which the call sites discard, and
   a tenant-wide eviction does nothing. (`card_services` is built without it;
   it is only ever evicted by key or whole.)
2. **`run_pending_tasks().await` after `invalidate_entries_if`.** moka applies
   a predicate lazily; the explicit pass makes the next lookup miss.
3. **`Generation` and `insert_fresh`.** Every invalidation calls
   `generation.bump()` before it evicts. A load reads `generation.current()`
   before it queries and inserts through `insert_fresh`, which drops the value
   if the generation moved. A load that started before an invalidation cannot
   re-insert what it read.

## Behaviour on a load error

No cache stores a value produced by an error.

- `ResourceGroupCache::try_get_effective_group`, `get_user_group_ids`,
  `get_api_key_tenant`, `card_service_system`, `McpServerCache::get_server` /
  `get_config`, `AgentSkillCache::get_skill` and `AgentCardCache::get_by_id` /
  `get_by_name` return `Result<_, sqlx::Error>`. Enforcement paths use
  `try_get_effective_group` and refuse on `Err`.
  `get_effective_group` logs the error and returns `None`.
- `ModelCache::get_model` / `get_model_by_id` and `ProviderCache::get` log the
  error and return `None`. A model whose provider cannot be resolved is
  returned with an empty `provider`.
- `GuardrailsCache::get_configs`: when a reload fails and an entry (even an
  expired one) is cached, that entry is served; with nothing cached the error
  is returned.

## Invalidation

### Redis pub/sub: configuration changes

The control plane publishes an `InvalidationMessage` on the Redis channel
`brutor:cache:invalidate` (`INVALIDATION_CHANNEL`) after a configuration
write:

```json
{ "type": "model", "action": "update", "tenant_id": "...", "id": "..." }
```

`CacheInvalidator` is built in the `cache_invalidation_listener` task
(`src/tasks.rs`) and subscribes to that channel. Its cache fields must be
clones of the `AppState` fields (a moka `Cache` clone shares the store), never
freshly constructed; `tests/route_pipeline_audit.rs::cache_invalidator_holds_shared_caches`
checks the construction block.

`handle_invalidation` runs every handler registered for the message's `type`
in `InvalidationRegistry`, in registration order (built-in first, then those
of extensions; `Extension::invalidation_handlers` in `src/builder.rs`). The
match on `type` is exact. A handler can be added, never removed or replaced.

Built-in handlers (`BUILTIN_INVALIDATION_HANDLERS`):

| `type` | Evicts |
|--------|--------|
| `model`, `routing_group` | `ModelCache` entry; `ResourceGroupCache` tenant |
| `llm_provider`, `provider` | `ProviderCache` entry; `ModelCache` tenant |
| `mcp_server`, `server_config` | `McpServerCache` entry; `ResourceGroupCache` tenant |
| `skill`, `agent_skill` | `AgentSkillCache` entry; `ResourceGroupCache` tenant |
| `resource_group` | `ResourceGroupCache` tenant; run-ceiling cache (all) |
| `ai_system_dependency` | `ResourceGroupCache` tenant |
| `argument_policy` | `ArgumentPolicyCache` tenant; `ResourceGroupCache` tenant |
| `guardrail`, `guardrail_config` | `GuardrailsCache` tenant; `ResourceGroupCache` tenant |
| `agent_card` | `AgentCardCache` entry; `ResourceGroupCache` tenant |
| `semantic_policy` | `SemanticPolicyCache` tenant |
| `membership`, `assign` | `ResourceGroupCache`, `ModelCache`, `McpServerCache`, `AgentSkillCache` tenant |
| any other type | `unregistered_type`: `ModelCache`, `ResourceGroupCache`, `McpServerCache`, `AgentSkillCache`, `GuardrailsCache`, `AgentCardCache` tenant |

The fallback does not reach `ProviderCache`, `ArgumentPolicyCache` or
`SemanticPolicyCache`, so a type the control plane publishes needs a handler
of its own.

Listener behaviour:

- After every successful subscribe, including the first,
  `flush_config_caches` drops every config cache whole (pub/sub does not
  replay missed messages), then calls the `flush` of each handler that has
  one.
- When the connection or subscription fails, or the stream ends, it retries
  with backoff from 2 s doubling to 30 s. Until it is subscribed again entries
  live out their TTL.
- A payload that cannot be read or parsed is logged and skipped.

### Postgres LISTEN/NOTIFY: System Operations console

A second listener, `services/system_notify.rs` (task `system_notify_listener`),
holds a `PgListener`. It serves operator actions from the console, not
configuration writes. On the Postgres channel `brutor:cache:invalidate` (same
name as the Redis channel, different payload: `layer`, `scope`, `id`, optional
`tenant_id`) it flushes one layer:

| Layer | Cache |
|-------|-------|
| `policy` | `ArgumentPolicyCache` |
| `group` | `ResourceGroupCache` |
| `tool` | `McpServerCache` |
| other | the `CacheLayer` registered under that name in `state.cache_layers`; unknown names are logged and ignored |

No `tenant_id` flushes the whole layer; `scope == "tenant"` flushes the
tenant; anything else flushes the entry `id`. After every (re)LISTEN the three
Community layers, each registered layer with `flush_after_listen`, and the
maintenance cache are dropped whole. `brutor:maintenance` drops the cached
maintenance mode.

## Statistics

There is no Prometheus metric for these caches. The only consumer of cache
counters is `brutor:cache:stats:request` (Postgres NOTIFY): the gateway
replies on `brutor:cache:stats:reply` with `hits`, `misses`, `entries`
(`CacheStatsSnapshot`) for `policy`, `group`, `tool` and each registered layer
that has a `stats` function. `ModelCache::stats`, `AgentSkillCache::stats` and
`AgentCardCache::stats` return entry counts only and have no caller outside
tests.

Debug logs: `RUST_LOG=brutor_gateway_core::services::cache=debug`.

## Other behaviour

- Caches start empty and fill on demand; the config caches of this directory
  are not warmed at start.
- Concurrent misses on one key each run their own load (`get` then
  `insert_fresh`; moka's `get_with` is not used).

## UsageState (`usage_state.rs`)

The Redis counters that usage limits are enforced on, admitted atomically by
`usage_state::admission`. A Redis error refuses the requests whose limits it
holds; there is no per-process fallback. `UsageState::new(None)` is the
in-process store (`mem_kv`) that tests run on, under the same keys.

Key families: `brutor:rpm:{model_id}:{minute}` and
`brutor:tpm:{model_id}:{minute}` (120 s TTL), `brutor:cooldown:{model_id}`
(TTL = cooldown), `brutor:active:{model_id}` (300 s TTL),
`brutor:run:{tenant_id}:{run_id}:{field}`, `brutor:toolc:{group_id}:h|d:{index}`,
`brutor:tool_hour:{group_id}:{hour}` (2 h TTL),
`brutor:tool_day:{group_id}:{day}` (48 h TTL),
`brutor:uq:{h|d|m}:{counter_key}:{window_index}`, and the admission holds
(`brutor:hold:{id}`, the sorted set `brutor:holds`).

## Instances

A Community gateway runs as one instance. At start it takes the lease
`brutor:instance:core` in Redis (`services/instance_lease.rs`); a second
instance that finds a live lease does not start. Running several instances is
the Enterprise `Clustering` extension (below). Each instance has its own
config caches and its own subscription to the invalidation channel.

## Enterprise extensions

The response cache, the prompt cache, the knowledge base and clustering are
Enterprise only (`enterprise/src/`, a separate workspace). None of their code
is in this directory; they attach through the registries above:

| Extension | File | Registers |
|-----------|------|-----------|
| Response cache | `enterprise/src/semantic_response_cache.rs` | handlers `guardrail_cached_responses` (`guardrail`, `guardrail_config`), `semantic_cache`, `router_cache_config`; layer `semantic` (no counters, not flushed on re-listen); group loader `response_cache_group_config` |
| Prompt cache | `enterprise/src/prompt_cache.rs` | handlers `prompt_cache_policy` (with a `flush`), `prompt_cache_binding`; layer `response` (counters, flushed on re-listen) |
| Knowledge base | `enterprise/src/knowledge_base.rs` | handler `knowledge_base`; group loader `knowledge_base_group_config` |
| Clustering | `enterprise/src/clustering.rs` | `allows_multiple_instances() == true` (no instance lease is taken); a statistics responder whose reply carries `replica_id` (the instance's name in the deployment, else the host name) |

In a Community gateway the LLM routes call `NoResponseCache`
(`services/response_cache.rs`), the layers `semantic` and `response` do not
exist, and the message types only these extensions handle (`semantic_cache`,
`router_cache_config`, `prompt_cache_policy`, `prompt_cache_binding`,
`knowledge_base`) fall to `unregistered_type`.

## Adding a cache

1. Add `src/services/cache/<name>_cache.rs`: keys prefixed `{tenant_id}:`,
   `Arc<T>` values, a builder with `max_capacity`, `time_to_live` and
   `support_invalidation_closures`, a `Generation` field, inserts through
   `insert_fresh`.
2. Derive `Clone` (or hold it in an `Arc`) so the invalidator shares the
   instance.
3. Implement `ResourceCache`: bump the generation first in every method, scope
   every predicate to the tenant prefix, call `run_pending_tasks` after each
   predicate.
4. Load functions return `Result` and never cache an error.
5. Add the field to `AppState` and `AppState::new` (`src/services/mod.rs`), to
   `CacheInvalidator` (`mod.rs`), to `flush_config_caches`, and to the
   construction in `cache_invalidation_listener` (`src/tasks.rs`) as
   `state.<name>.clone()`.
6. Add a handler to `BUILTIN_INVALIDATION_HANDLERS` for each message type, and
   evict `ResourceGroupCache` for the tenant when the resource is bound to
   groups. An Enterprise cache registers its handlers from its extension
   instead.
7. Tests: `#[cfg(test)]` / `db-tests` helpers (`_peek_*`, `insert_*_for_test`)
   on the cache, per-cache tests (tenant isolation, prefix collision such as
   `acme` vs `acme-corp`, per-entry eviction, `invalidate_all`), dispatch
   tests in `mod.rs` using `invalidation_rig`, and the lists in
   `tests/route_pipeline_audit.rs::cache_invalidator_holds_shared_caches`.
8. Update this file.
