#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

#
# governance_smoke.sh — Governance WIRING smoke-test for the Brutor stack.
#
# Proves that the per-surface INPUT guardrail (G1–G13 wiring) actually FIRES at
# runtime. The decisive, dependency-free probe is the INPUT GUARDRAIL BLOCK:
# input guardrails run BEFORE the upstream provider call, so a request whose
# input carries a seeded banned word MUST return a guardrail BLOCK (HTTP 403
# with a guardrail-shaped error) even though no real provider keys are
# configured.
#
#   * 403 + guardrail reason       => PASS  (surface is wired, fired pre-upstream)
#   * 200 OR provider/upstream err => FAIL  (request reached upstream => wiring gap)
#     (e.g. 401 "obtain an API key from platform.openai.com", 502, model unreachable)
#   * surface needs creds/model    => SKIP  (clearly noted)
#
# The script is idempotent + re-runnable. It mutates the dev stack:
#   - PATCHes the seeded default guardrail config (adds a sentinel banned word,
#     enables every input surface + the banned_words_surfaces list)
#   - creates (or reuses) a shared LLM API key on the default group
#
# Usage:  ./governance_smoke.sh
# Override anything via env vars at the top.
#
set -uo pipefail

# ---------------------------------------------------------------------------
# Parameters (override via environment)
# ---------------------------------------------------------------------------
CONTROL_URL="${CONTROL_URL:-http://localhost:5050}"   # Python control plane
PROXY_URL="${PROXY_URL:-http://localhost:8100}"       # Rust core proxy
TENANT_ID="${TENANT_ID:-default}"

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-Admin123!}"

PORTAL_USER="${PORTAL_USER:-trial}"
PORTAL_PASS="${PORTAL_PASS:-Trial123!}"

GROUP_ID="${GROUP_ID:-group-01KVYXSCSDN6TDPTPMSPHQVRDJ}"
CONFIG_ID="${CONFIG_ID:-guardrail-default-default}"
API_KEY_NAME="${API_KEY_NAME:-governance-smoke-key}"

# Sentinel banned word — vanishingly unlikely to appear in real traffic.
BANNED="${BANNED:-ZZBANNEDXYZZY}"

# Seeded resources discovered from the dev DB.
MCP_SERVER="${MCP_SERVER:-shared-mcp-map-default}"
MCP_TOOL="${MCP_TOOL:-show_location}"
A2A_CARD="${A2A_CARD:-agent-card-hello-world-a2a-default}"
A2A_CAPABILITY="${A2A_CAPABILITY:-hello}"

# Model names per mode (override if your seed differs).
MODEL_CHAT="${MODEL_CHAT:-gpt-5.2}"
MODEL_RESPONSES="${MODEL_RESPONSES:-gpt-5.5}"
MODEL_COMPLETIONS="${MODEL_COMPLETIONS:-gpt-5.2}"
MODEL_EMBEDDING="${MODEL_EMBEDDING:-text-embedding-3-small}"
MODEL_IMAGE="${MODEL_IMAGE:-gpt-image-2.6-flare}"
MODEL_TTS="${MODEL_TTS:-tts-1}"
MODEL_STT="${MODEL_STT:-gpt-transcribe}"
MODEL_VIDEO="${MODEL_VIDEO:-luma/agent/ray/v3.2/text-to-video}"
MODEL_ANTHROPIC="${MODEL_ANTHROPIC:-}"   # empty => no native Anthropic model seeded => SKIP

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "FATAL: '$1' is required" >&2; exit 1; }; }
need curl
need python3

jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)" 2>/dev/null; }

# Results table (parallel arrays).
declare -a R_SURFACE R_STATUS R_VERDICT R_REASON

record() { # surface  http_status  verdict  reason
  R_SURFACE+=("$1"); R_STATUS+=("$2"); R_VERDICT+=("$3"); R_REASON+=("$4")
}

# Classify a guardrail-probe response.
#   $1 surface  $2 http_status  $3 body
# A guardrail block = 403 whose body mentions guardrail/policy/banned/blocked.
classify_block() {
  local surface="$1" code="$2" body="$3"
  local low; low="$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')"
  if [[ "$code" == "403" ]] && printf '%s' "$low" | grep -Eq 'guardrail|policy|banned|blocked|violat'; then
    record "$surface" "$code" "PASS" "guardrail block (pre-upstream)"
  elif [[ "$code" == "403" ]]; then
    # 403 but not obviously guardrail-shaped — could be access control. Mark FAIL-ish.
    record "$surface" "$code" "FAIL" "403 but not guardrail-shaped: $(printf '%s' "$body" | head -c 80)"
  elif [[ "$code" =~ ^2 ]]; then
    record "$surface" "$code" "FAIL" "200/2xx — request was allowed (no block)"
  elif printf '%s' "$low" | grep -Eq 'platform.openai.com|provide an api key|upstream|unreachable|bad gateway|502|connection'; then
    record "$surface" "$code" "FAIL" "reached upstream provider (wiring gap)"
  else
    record "$surface" "$code" "FAIL" "non-block status: $(printf '%s' "$body" | head -c 80)"
  fi
}

# POST helper -> sets globals HTTP_CODE + HTTP_BODY.
post() { # url  [curl args...]
  local url="$1"; shift
  local out; out="$(curl -s -m 30 -w $'\n__HTTP__%{http_code}' "$url" "$@")"
  HTTP_CODE="${out##*__HTTP__}"
  HTTP_BODY="${out%$'\n'__HTTP__*}"
}

hr() { printf '%s\n' "------------------------------------------------------------------------"; }

# ===========================================================================
# 1. LOGIN
# ===========================================================================
echo "==> Logging in (tenant admin @ $CONTROL_URL)"
post "$CONTROL_URL/v1/admin/users/tenant/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\",\"tenant_id\":\"$TENANT_ID\"}"
ADMIN_TOKEN="$(printf '%s' "$HTTP_BODY" | jget "['access_token']")"
if [[ -z "$ADMIN_TOKEN" ]]; then
  echo "FATAL: tenant-admin login failed ($HTTP_CODE): $HTTP_BODY" >&2; exit 1
fi
echo "    tenant-admin OK"

echo "==> Logging in (portal user @ $PROXY_URL)"
post "$PROXY_URL/v1/portal/auth/login" -H 'Content-Type: application/json' -H "X-Tenant-ID: $TENANT_ID" \
  -d "{\"username\":\"$PORTAL_USER\",\"password\":\"$PORTAL_PASS\"}"
PORTAL_TOKEN="$(printf '%s' "$HTTP_BODY" | jget "['access_token']")"
if [[ -z "$PORTAL_TOKEN" ]]; then
  echo "    WARN: portal login failed ($HTTP_CODE) — portal surfaces will SKIP"
  PORTAL_TOKEN=""
else
  echo "    portal-user OK"
fi

AUTH_ADMIN=(-H "Authorization: Bearer $ADMIN_TOKEN" -H "X-Tenant-ID: $TENANT_ID")

# ===========================================================================
# 2. CONFIGURE GOVERNANCE — PATCH the seeded default config.
#    PATCH is a FULL replacement, so we read the current config and merge.
# ===========================================================================
echo "==> Fetching current guardrail config '$CONFIG_ID'"
post "$CONTROL_URL/v1/admin/guardrails/configs/$CONFIG_ID" "${AUTH_ADMIN[@]}" -X GET
CUR="$HTTP_BODY"
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "FATAL: could not GET config ($HTTP_CODE): $CUR" >&2; exit 1
fi

echo "==> Building PATCH (sentinel banned word + all input surfaces)"
# All input surfaces we will probe (config-level *_enabled toggles) plus the
# matching per-check banned_words_surfaces list. Both must include a surface
# for a check to fire there (config map ∩ per-check list).
PATCH_PY="$(mktemp)"
cat > "$PATCH_PY" <<'PY'
import sys, json, os
cur = json.load(sys.stdin)["config"]
banned = os.environ["BANNED"]

input_surfaces = [
    "chat_input", "embeddings_input", "image_gen_input",
    "audio_tts_input", "audio_stt_input", "video_gen_input",
    "batch_input", "mcp_input", "skill_input", "a2a_inbound",
]
# Output surfaces we toggle on too (dependency-free output probes are rare, but
# enable them so the wiring is exercised where a block can fire pre-upstream).
output_surfaces = ["chat_output", "mcp_output", "skill_output", "a2a_outbound"]

surfaces = dict(cur.get("surfaces", {}))
for s in input_surfaces + output_surfaces:
    surfaces[s] = True
# keep registration default
surfaces.setdefault("mcp_registration", True)
# drop response-only keys not accepted by the request model
surfaces.pop("mcp_registration", None) if False else None

b = cur.get("builtin", {})
existing_banned = list(b.get("banned_words") or [])
if banned not in existing_banned:
    existing_banned.append(banned)

# banned_words_surfaces: union of existing + every input surface + outputs.
bw_surfaces = set(b.get("banned_words_surfaces") or [])
bw_surfaces |= set(input_surfaces) | set(output_surfaces)

builtin = {
    "banned_words": existing_banned,
    "banned_patterns": list(b.get("banned_patterns") or []),
    "banned_words_match_mode": b.get("banned_words_match_mode") or "substring",
    "banned_words_surfaces": sorted(bw_surfaces),
    # Preserve the other checks as-is (enabled flags + actions) so we don't
    # weaken the existing default config; banned_words alone is our probe.
    "pii_enabled": b.get("pii", {}).get("enabled", True),
    "pii_action": b.get("pii", {}).get("action", "redact"),
    "prompt_injection_enabled": b.get("prompt_injection", {}).get("enabled", True),
    "prompt_injection_action": b.get("prompt_injection", {}).get("action", "block"),
    "jailbreak_enabled": b.get("jailbreak", {}).get("enabled", True),
    "jailbreak_action": b.get("jailbreak", {}).get("action", "block"),
    "toxic_content_enabled": b.get("toxic_content", {}).get("enabled", False),
    # Force built_in to avoid the "openai_moderation requires api_key in
    # providers[]" save-time validation — we don't ship a real provider key
    # in the dev stack and toxic_content is irrelevant to the banned-word probe.
    "toxic_content_provider": "built_in",
    "secrets_enabled": (b.get("secrets") or {}).get("enabled", False),
}

patch = {
    "name": cur.get("name", "Default Guardrails"),
    "enabled": True,
    "group_ids": cur.get("group_ids") or [],
    "surfaces": {k: v for k, v in surfaces.items()
                 if k in {
                    "chat_input","chat_output","embeddings_input","image_gen_input",
                    "image_gen_output","audio_tts_input","audio_tts_output",
                    "audio_stt_input","audio_stt_output","video_gen_input",
                    "video_gen_output","batch_input","batch_output","moderation_input",
                    "mcp_input","mcp_output","mcp_registration","skill_input",
                    "skill_output","a2a_inbound","a2a_outbound",
                 }},
    "builtin": builtin,
    "fail_open": cur.get("fail_open", True),
    "log_all_checks": cur.get("log_all_checks", False),
    "max_check_timeout_ms": cur.get("max_check_timeout_ms", 2000.0),
}
print(json.dumps(patch))
PY
PATCH_BODY="$(printf '%s' "$CUR" | BANNED="$BANNED" python3 "$PATCH_PY")"
rm -f "$PATCH_PY"
if [[ -z "$PATCH_BODY" ]]; then
  echo "FATAL: failed to build PATCH body" >&2; exit 1
fi

post "$CONTROL_URL/v1/admin/guardrails/configs/$CONFIG_ID" "${AUTH_ADMIN[@]}" \
  -X PATCH -H 'Content-Type: application/json' -d "$PATCH_BODY"
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "FATAL: PATCH failed ($HTTP_CODE): $HTTP_BODY" >&2; exit 1
fi
echo "    PATCH OK"

echo "==> Verifying round-trip (banned word + surfaces present)"
post "$CONTROL_URL/v1/admin/guardrails/configs/$CONFIG_ID" "${AUTH_ADMIN[@]}" -X GET
RT_PY="$(mktemp)"
cat > "$RT_PY" <<'PY'
import sys, json, os
d = json.load(sys.stdin)["config"]
b = d["builtin"]
banned = os.environ["BANNED"]
ok = banned in (b.get("banned_words") or [])
ok = ok and "chat_input" in (b.get("banned_words_surfaces") or [])
ok = ok and d["surfaces"].get("a2a_inbound") is True
print("yes" if ok else "no")
PY
RT_OK="$(printf '%s' "$HTTP_BODY" | BANNED="$BANNED" python3 "$RT_PY")"
rm -f "$RT_PY"
if [[ "$RT_OK" != "yes" ]]; then
  echo "FATAL: round-trip verification failed. Config did not persist." >&2
  echo "$HTTP_BODY" | head -c 600 >&2; echo >&2; exit 1
fi
echo "    round-trip OK (banned word + surfaces persisted)"

# Give the proxy a moment to pick up the config change (cache invalidation).
sleep 2

# ===========================================================================
# 3. CREATE / REUSE an LLM API key on the default group.
# ===========================================================================
# The plaintext key is only returned at creation, so we mint a fresh, uniquely
# named key on every run (keeps the script re-runnable; old keys can be revoked
# in the admin UI). Override API_KEY_NAME to pin a name.
KEY_NAME_RUN="${API_KEY_NAME}-$(date +%s)"
echo "==> Creating LLM API key '$KEY_NAME_RUN' on group $GROUP_ID"
post "$CONTROL_URL/v1/admin/resource-groups/$GROUP_ID/api-keys" "${AUTH_ADMIN[@]}" \
  -X POST -H 'Content-Type: application/json' \
  -d "{\"name\":\"$KEY_NAME_RUN\",\"access_mode\":\"shared\",\"enabled\":true}"
LLM_KEY="$(printf '%s' "$HTTP_BODY" | jget "['key_value']")"
if [[ -z "$LLM_KEY" ]]; then
  echo "FATAL: API key creation failed ($HTTP_CODE): $HTTP_BODY" >&2; exit 1
fi
echo "    API key created: ${LLM_KEY:0:24}..."
sleep 1

AUTH_LLM=(-H "Authorization: Bearer $LLM_KEY" -H "X-Tenant-ID: $TENANT_ID" -H 'Content-Type: application/json')
[[ -n "$PORTAL_TOKEN" ]] && AUTH_PORTAL=(-H "Authorization: Bearer $PORTAL_TOKEN" -H "X-Tenant-ID: $TENANT_ID")

# ===========================================================================
# 4 & 5. FIRE ONE REQUEST PER SURFACE with the banned word in the input.
# ===========================================================================
hr; echo "Firing per-surface probes (banned word = '$BANNED')"; hr

# --- chat (API key) ----------------------------------------------------------
post "$PROXY_URL/v1/proxy/llm/chat/completions" "${AUTH_LLM[@]}" \
  -d "{\"model\":\"$MODEL_CHAT\",\"messages\":[{\"role\":\"user\",\"content\":\"$BANNED please\"}]}"
classify_block "chat (chat_input)" "$HTTP_CODE" "$HTTP_BODY"

# --- responses ---------------------------------------------------------------
post "$PROXY_URL/v1/proxy/llm/responses" "${AUTH_LLM[@]}" \
  -d "{\"model\":\"$MODEL_RESPONSES\",\"input\":\"$BANNED please\"}"
classify_block "responses (chat_input)" "$HTTP_CODE" "$HTTP_BODY"

# --- completions -------------------------------------------------------------
post "$PROXY_URL/v1/proxy/llm/completions" "${AUTH_LLM[@]}" \
  -d "{\"model\":\"$MODEL_COMPLETIONS\",\"prompt\":\"$BANNED please\"}"
classify_block "completions (chat_input)" "$HTTP_CODE" "$HTTP_BODY"

# --- embeddings --------------------------------------------------------------
post "$PROXY_URL/v1/proxy/llm/embeddings" "${AUTH_LLM[@]}" \
  -d "{\"model\":\"$MODEL_EMBEDDING\",\"input\":\"$BANNED\"}"
classify_block "embeddings (embeddings_input)" "$HTTP_CODE" "$HTTP_BODY"

# --- Anthropic-native /v1/messages -------------------------------------------
if [[ -n "$MODEL_ANTHROPIC" ]]; then
  post "$PROXY_URL/v1/messages" "${AUTH_LLM[@]}" \
    -d "{\"model\":\"$MODEL_ANTHROPIC\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"$BANNED please\"}]}"
  classify_block "anthropic /v1/messages (chat_input)" "$HTTP_CODE" "$HTTP_BODY"
else
  record "anthropic /v1/messages (chat_input)" "-" "SKIP" "no Anthropic model seeded"
fi

# --- batch (API key) ---------------------------------------------------------
post "$PROXY_URL/v1/proxy/batch/jobs" "${AUTH_LLM[@]}" \
  -d "{\"model\":\"$MODEL_CHAT\",\"requests\":[{\"custom_id\":\"r1\",\"body\":{\"messages\":[{\"role\":\"user\",\"content\":\"$BANNED please\"}]}}]}"
# Batch rejects the whole job at submit with HTTP 400 ("Batch rejected: N
# request item(s) violated an input guardrail") rather than 403 — that IS a
# guardrail block (the job never reaches the provider), so accept it.
BATCH_LOW="$(printf '%s' "$HTTP_BODY" | tr '[:upper:]' '[:lower:]')"
if printf '%s' "$BATCH_LOW" | grep -Eq 'guardrail|policy|banned|blocked|violat'; then
  record "batch (batch_input)" "$HTTP_CODE" "PASS" "batch rejected by input guardrail (whole job)"
else
  classify_block "batch (batch_input)" "$HTTP_CODE" "$HTTP_BODY"
fi

# --- mcp tools/call (API key; banned word in tool argument) ------------------
post "$PROXY_URL/v1/proxy/mcp/$MCP_SERVER" \
  -H "Authorization: Bearer $LLM_KEY" -H "X-Tenant-ID: $TENANT_ID" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$MCP_TOOL\",\"arguments\":{\"name\":\"$BANNED\",\"lat\":48.8,\"lng\":2.3}}}"
# MCP returns 200 with a JSON-RPC error envelope on a guardrail block, so
# treat an in-body guardrail/policy mention as a block regardless of HTTP code.
MCP_LOW="$(printf '%s' "$HTTP_BODY" | tr '[:upper:]' '[:lower:]')"
if printf '%s' "$MCP_LOW" | grep -Eq 'guardrail|policy|banned|blocked|violat'; then
  record "mcp tools/call (mcp_input)" "$HTTP_CODE" "PASS" "guardrail block in JSON-RPC/HTTP response"
else
  classify_block "mcp tools/call (mcp_input)" "$HTTP_CODE" "$HTTP_BODY"
fi

# --- a2a inbound (G2) --------------------------------------------------------
post "$PROXY_URL/v1/proxy/a2a/inbound" \
  -H "Authorization: Bearer $LLM_KEY" -H "X-Tenant-ID: $TENANT_ID" -H 'Content-Type: application/json' \
  -d "{\"target_card_id\":\"$A2A_CARD\",\"capability\":\"$A2A_CAPABILITY\",\"message\":{\"text\":\"$BANNED please\"}}"
A2A_LOW="$(printf '%s' "$HTTP_BODY" | tr '[:upper:]' '[:lower:]')"
if [[ "$HTTP_CODE" == "403" ]] && printf '%s' "$A2A_LOW" | grep -Eq 'guardrail|policy|banned|blocked|violat'; then
  record "a2a inbound (a2a_inbound)" "$HTTP_CODE" "PASS" "guardrail block (pre-dispatch)"
else
  classify_block "a2a inbound (a2a_inbound)" "$HTTP_CODE" "$HTTP_BODY"
fi

# --- PORTAL surfaces (image / tts / stt / video) — need portal JWT -----------
if [[ -n "$PORTAL_TOKEN" ]]; then
  # image generation
  post "$PROXY_URL/v1/proxy/llm/images/generations" "${AUTH_PORTAL[@]}" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL_IMAGE\",\"prompt\":\"$BANNED please\"}"
  classify_block "image gen (image_gen_input)" "$HTTP_CODE" "$HTTP_BODY"

  # text-to-speech
  post "$PROXY_URL/v1/proxy/llm/audio/speech" "${AUTH_PORTAL[@]}" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL_TTS\",\"voice\":\"alloy\",\"input\":\"$BANNED please\"}"
  classify_block "audio tts (audio_tts_input)" "$HTTP_CODE" "$HTTP_BODY"

  # speech-to-text (multipart; banned word in optional prompt field)
  TMP_WAV="$(mktemp).wav"
  # minimal 44-byte WAV header (silence) so the multipart 'file' part is valid.
  # base64 of the header avoids embedding raw NUL bytes in the script.
  printf '%s' "UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=" | base64 -d > "$TMP_WAV" 2>/dev/null
  post "$PROXY_URL/v1/proxy/llm/audio/transcriptions" "${AUTH_PORTAL[@]}" \
    -F "model=$MODEL_STT" -F "prompt=$BANNED please" -F "file=@$TMP_WAV;type=audio/wav"
  classify_block "audio stt (audio_stt_input)" "$HTTP_CODE" "$HTTP_BODY"
  rm -f "$TMP_WAV"

  # video generation
  post "$PROXY_URL/v1/proxy/llm/video/generations" "${AUTH_PORTAL[@]}" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL_VIDEO\",\"prompt\":\"$BANNED please\"}"
  classify_block "video gen (video_gen_input)" "$HTTP_CODE" "$HTTP_BODY"
else
  for s in "image gen (image_gen_input)" "audio tts (audio_tts_input)" \
           "audio stt (audio_stt_input)" "video gen (video_gen_input)"; do
    record "$s" "-" "SKIP" "no portal JWT"
  done
fi

# ===========================================================================
# 6. RESULTS TABLE
# ===========================================================================
echo
hr
printf '%-34s | %-6s | %-6s | %s\n' "SURFACE" "HTTP" "VERDICT" "REASON"
hr
PASS=0; FAIL=0; SKIP=0
for i in "${!R_SURFACE[@]}"; do
  printf '%-34s | %-6s | %-6s | %s\n' \
    "${R_SURFACE[$i]}" "${R_STATUS[$i]}" "${R_VERDICT[$i]}" "${R_REASON[$i]}"
  case "${R_VERDICT[$i]}" in
    PASS) PASS=$((PASS+1));;
    FAIL) FAIL=$((FAIL+1));;
    SKIP) SKIP=$((SKIP+1));;
  esac
done
hr
echo "Totals: PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
hr

# Non-zero exit if any surface FAILED (reached upstream / not blocked).
[[ "$FAIL" -eq 0 ]]
