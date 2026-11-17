# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Manager Service 
Handles business logic for LLM operations with group-based access control
"""

import asyncio
import httpx
import re
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.constants.proxy import proxy_internal_url, gateway_service_api_key
from app.constants.llm import LLModelMode
from app.constants.server import ServerStatusType
from app.database.models.llm_model import LLModel
from app.core.logging_system import BrutorLogger
from app.services.llm import provider_config as provider_config_rules
from app.services.oauth.encryption_service import EncryptionService


# Modes for which a synchronous probe doesn't make sense — either
# there's no proxy route yet (RERANK / MODERATION) or it's a virtual
# construct that has no upstream (ROUTING). The probe reports the
# model as healthy with an explanatory note rather than firing an
# arbitrary chat/completions request that would 4xx every time.
_PROBE_SKIP_MODES = {
    LLModelMode.RERANK.value,
    LLModelMode.MODERATION.value,
    LLModelMode.ROUTING.value,
}

# (provider_key, mode) combinations where a synchronous probe is a
# bad fit — typically because the upstream uses an async submit /
# poll pipeline that exceeds the probe's 5s budget, or because each
# probe materially costs the operator. We mark the model healthy
# with a clear "skipped" note rather than emit a misleading
# `httpx.ReadTimeout` every cycle.
#
# AssemblyAI's STT is upload → submit → poll-at-1Hz; minimum
# successful round-trip is ~5–10s plus per-second poll latency.
# The `Test Connection` button still works (it shares this code
# path) so operators see "probe skipped, run a real upload to
# verify" rather than a hard timeout.
_PROBE_SKIP_PROVIDER_MODE = {
    ("assemblyai", LLModelMode.AUDIO_TRANSCRIPTION.value),
}

# Validation 4xx responses that we treat as "upstream is reachable
# and auth was accepted" rather than as a hard failure. Used by the
# image / video probes which intentionally send invalid params (e.g.
# `n=0`, `duration_seconds=-1`) so the upstream rejects the request
# *before* generating billable output.
#
# Note: the Brutor Rust proxy now does its own param validation
# (e.g. `routes/images.rs` rejects `n` outside `[1, max_images]`
# with the message ``"n=0 outside [1,10] for this model"``). That
# means the probe's intentionally-bad request is rejected by our
# OWN proxy before reaching the provider — so we still need to
# treat it as a healthy signal. Hence ``outside`` is on the list:
# it's the canonical token in our proxy's validation messages.
_VALIDATION_OK_KEYWORDS = (
    "must be",
    "required",
    "invalid_request_error",
    "invalid value",
    "invalid_value",
    "out of range",
    "greater than",
    "less than",
    "between",
    "must be greater",
    "must be at least",
    "n must",
    "duration",
    # Brutor proxy validation phrasing: "n=0 outside [1,10] for this model".
    "outside",
    # Whisper's `audio_too_short` (HTTP 400) — auth + reachability
    # confirmed; the upstream simply demands ≥0.1s. We send 1s now,
    # but keep the keyword in case a stricter provider raises the
    # floor in the future.
    "audio_too_short",
    "too short",
    # Silence-rejection vocabulary across STT providers:
    #  - Deepgram: returns 200 with empty transcript (no keyword)
    #  - AssemblyAI: status="error" with `error: "no speech detected"`
    #  - Google STT: empty `results: []` (200 OK, no keyword)
    #  - ElevenLabs Scribe: 400 with `no_audio_detected`
    # Treating any of these as healthy makes the probe robust to
    # the empty/silent test clip we deliberately upload.
    "no speech",
    "no_speech",
    "no audio",
    "no_audio",
    "silent",
    "silence detected",
)


def _looks_like_validation_400(body_text: str) -> bool:
    """Heuristic: does this 400 body read like a parameter-validation
    rejection rather than an auth / billing failure?

    Used by the image and video probes — both deliberately send
    invalid sentinel parameters so the upstream rejects the request
    BEFORE running the model. A validation-shaped 400 confirms the
    request reached the provider with valid auth, which is enough
    to declare the model healthy.

    Conservative: returns False on empty / non-string input so the
    caller falls through to the generic error path.
    """
    if not body_text:
        return False
    haystack = body_text.lower()
    return any(needle in haystack for needle in _VALIDATION_OK_KEYWORDS)


async def _attempt_catalog_probe(
    *,
    proxy_url: str,
    model_id: str,
    headers: Dict[str, str],
    timeout: float,
) -> Optional[Dict[str, Any]]:
    """Attempt the catalog-probe via the proxy's
    POST /v1/proxy/llm/admin/{model_id}/probe-catalog endpoint.

    Returns a `result`-shaped dict on success or genuine failure
    (so the caller can return it directly). Returns ``None`` when
    the provider has no `models_api_url` configured (HTTP 422 from
    the proxy), telling the caller to fall back to whatever
    mode-specific probe the file already implements.

    Tags `probe_type="catalog"` on every dict it returns. The
    caller's outer `result` keeps `probe_type="synthetic"` if this
    helper falls back, so the admin UI's badge reflects which
    probe actually ran.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout + 5) as client:
            resp = await client.post(
                f"{proxy_url}/v1/proxy/llm/admin/{model_id}/probe-catalog",
                headers=headers,
            )
    except Exception as e:  # noqa: BLE001 — probe must never raise
        return {
            "success": False,
            "model_id": model_id,
            "response_time_ms": 0.0,
            "response": None,
            "error": f"catalog probe dispatch failed: {e}",
            "tokens_used": None,
            "probe_type": "catalog",
        }

    # 422 → provider has no catalog URL configured. Caller falls
    # through to its mode-specific probe path. We deliberately do
    # NOT log here — the caller's path may succeed and an extra log
    # would just add noise.
    if resp.status_code == 422:
        return None

    if resp.status_code != 200:
        return {
            "success": False,
            "model_id": model_id,
            "response_time_ms": 0.0,
            "response": None,
            "error": (
                f"catalog probe returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            ),
            "tokens_used": None,
            "probe_type": "catalog",
        }

    try:
        body = resp.json()
    except Exception:
        return {
            "success": False,
            "model_id": model_id,
            "response_time_ms": 0.0,
            "response": None,
            "error": "catalog probe returned non-JSON body",
            "tokens_used": None,
            "probe_type": "catalog",
        }

    success = bool(body.get("success", False))
    latency_ms = body.get("latency_ms")
    status_code = body.get("status_code")
    error_message = body.get("error_message") or ""
    models_count = body.get("models_count")
    model_name_found = body.get("model_name_found")
    resolved_url = body.get("resolved_url")

    # Compose the human-readable response/error so the admin UI
    # can show useful detail without the operator having to expand
    # the raw response.
    if success:
        if model_name_found:
            response_text = (
                f"Catalog probe OK — model present in provider's "
                f"listing of {models_count} models "
                f"(resolved URL: {resolved_url})."
            )
        else:
            response_text = (
                f"Catalog probe reached provider — "
                f"{models_count if models_count is not None else 'unknown'} "
                f"models in listing (resolved URL: {resolved_url})."
            )
        error_text = None
    else:
        response_text = None
        error_text = error_message or (
            f"Catalog probe returned status {status_code} "
            f"(resolved URL: {resolved_url})."
        )

    return {
        "success": success,
        "model_id": model_id,
        "response_time_ms": latency_ms or 0.0,
        "response": response_text,
        "error": error_text,
        "tokens_used": None,
        "probe_type": "catalog",
        # The failure is evidence about the LISTING, not the model: the
        # key reached a 200 listing that simply omits the name (DeepSeek
        # omits deepseek-v4-flash, which generates fine), or the listing
        # endpoint refused the key (Groq /models → 403 for a key that
        # generates). Background sweeps fall back to the endpoint probe
        # on these instead of painting the model red. Mirrors the core's
        # CatalogProbeResult: name absence = success=false,
        # status_code=200, model_name_found=false.
        "listing_only_failure": (not success) and (
            (status_code == 200 and model_name_found is False)
            or status_code in (401, 403)
        ),
    }


# ---------------------------------------------------------------------------
# Health-check cost profile (background vs operator-initiated)
# ---------------------------------------------------------------------------
#
# ``probe="catalog"`` is what the Admin UI's timer-driven sweep asks for: the
# catalog probe (provider /models listing, zero tokens) for EVERY mode whose
# provider has a catalog URL, falling back to the endpoint probe only when
# the core answers 422. ``probe="endpoint"`` (default) is an operator's
# explicit "Test connection" / "Health check" click and keeps the real
# end-to-end generation probe. Regression context: a 5-minute background
# sweep ran a real generation against every chat / embedding / audio model
# (~8,000 provider calls and ~$3.70 overnight on the dev stack).
PROBE_ENDPOINT = "endpoint"
PROBE_CATALOG = "catalog"
HEALTH_CHECK_PROBES = (PROBE_ENDPOINT, PROBE_CATALOG)

# Background back-off: a model that keeps failing for a configuration /
# model reason is re-checked at ``ai_interval_seconds * BACKOFF_MULTIPLIER``
# instead of every interval. (The tenant's ``ai_recovery_check_interval`` is
# deliberately NOT used: it is documented and validated as a FASTER
# recovery cadence — it must not exceed the regular interval — so using it
# here would probe a broken model more often, not less.)
HEALTH_CHECK_BACKOFF_MULTIPLIER = 6

# Status codes that are transient by nature — never a reason to back off.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})

_STATUS_IN_ERROR_RE = re.compile(
    r"(?:proxy returned|provider returned|returned http|returned status|"
    r"failed with)\s+(\d{3})\b",
    re.IGNORECASE,
)

# Failure texts that name a configuration / model problem without an HTTP
# status. Matched case-insensitively against the stored error.
_NON_TRANSIENT_ERROR_MARKERS = (
    "api key not configured",
    "not found in provider listing",
    "requires ",  # "Requires <Tier> tier" (restricted-tier key rejection)
    "not chat-compatible",
)


def is_non_transient_probe_failure(error_message: Optional[str]) -> bool:
    """Is this stored probe failure a configuration / model error (4xx-class)
    rather than a transient one (timeout, 429, 5xx, network)?

    Conservative by design: anything it cannot positively classify as a
    4xx-class config/model failure is treated as transient, so an
    unrecognised failure keeps being probed at the normal cadence rather
    than silently backed off.
    """
    if not error_message:
        return False
    text = error_message.lower()
    match = _STATUS_IN_ERROR_RE.search(text)
    if match:
        status = int(match.group(1))
        return 400 <= status < 500 and status not in _TRANSIENT_HTTP_STATUSES
    if "timeout" in text or "timed out" in text:
        return False
    return any(marker in text for marker in _NON_TRANSIENT_ERROR_MARKERS)


def should_back_off_background_check(
    model: Any,
    *,
    failure_threshold: int,
    backoff_interval_seconds: float,
    now: Optional[datetime] = None,
) -> bool:
    """Should a BACKGROUND sweep skip this model this time round?

    True when its last ``failure_threshold`` consecutive checks failed, the
    latest failure is non-transient (see ``is_non_transient_probe_failure``)
    and it was checked less than ``backoff_interval_seconds`` ago.

    Limitation (no schema change): ``llm_model`` stores the consecutive
    failure COUNT and only the LATEST error text, so the transient /
    non-transient test is applied to the latest failure, not to each of the
    N. A transient failure in the run still resets nothing — but the latest
    one being transient always keeps the model on the normal cadence.
    Operator-initiated checks never back off.
    """
    if (model.health_status or "") not in (
        ServerStatusType.UNHEALTHY.value,
        ServerStatusType.REQUIRES_TIER.value,
    ):
        return False
    if (model.consecutive_health_failures or 0) < max(1, int(failure_threshold)):
        return False
    if not is_non_transient_probe_failure(model.health_check_error):
        return False
    last = model.last_health_check
    if last is None:
        return False
    if last.tzinfo is None:  # SQLite round-trips drop tzinfo
        last = last.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() < float(backoff_interval_seconds)


# Valid 16-bit mono PCM WAV containing 1 second of silence. The STT
# probe uploads this so Whisper-style models accept the multipart
# and return a (typically empty) transcription, confirming auth +
# reachability. OpenAI's `/v1/audio/transcriptions` rejects anything
# under 0.1 seconds with `audio_too_short`, so a single-frame WAV
# (which we used to ship) failed every probe — generate a full
# second of silence instead.
#
# 16 kHz / 16-bit / mono is the canonical "speech ASR" PCM shape:
# OpenAI Whisper, Groq Whisper, Deepgram, ElevenLabs Scribe, and
# Google STT all accept it without an explicit encoding hint.
# 8 kHz "telephone" sample rate works for OpenAI but is below
# Google STT's recommended floor — 16 kHz is universally happy at
# the cost of one extra ~16 KB per probe.
#
# Constructed at module load to avoid synthesising it on every
# probe.
def _build_silent_wav() -> bytes:
    import struct
    sample_rate = 16000  # universally-accepted ASR sample rate
    bits_per_sample = 16
    channels = 1
    num_samples = sample_rate  # 1.0s — well above OpenAI's 0.1s floor
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = num_samples * channels * bits_per_sample // 8
    chunk_size = 36 + data_size
    return (
        b"RIFF"
        + struct.pack("<I", chunk_size)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH",
            16,                # fmt chunk size
            1,                 # PCM
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
        )
        + b"data"
        + struct.pack("<I", data_size)
        + b"\x00" * data_size  # 1s of zero-amplitude 16-bit samples
    )


_SILENT_WAV_BYTES = _build_silent_wav()


def _extract_responses_text(body: Dict[str, Any]) -> str:
    """
    Pull a short readable excerpt from a Responses API response body.

    The Responses API shape is a list under `output`, where each item
    can be a message, reasoning step, or tool call. Messages carry
    the user-visible text in `content[*].text`. We walk the list for
    the first assistant message with text and return the first ~80
    chars. Purely cosmetic — if the shape changes, return "" and let
    the caller render the status alone.
    """
    try:
        for item in body.get("output", []) or []:
            if item.get("type") == "message":
                for part in item.get("content", []) or []:
                    if isinstance(part, dict) and part.get("type") in (
                        "output_text",
                        "text",
                    ):
                        text = (part.get("text") or "").strip()
                        if text:
                            return text[:80] + ("…" if len(text) > 80 else "")
        # Newer SDKs also expose `output_text` as a top-level string.
        text = (body.get("output_text") or "").strip()
        if text:
            return text[:80] + ("…" if len(text) > 80 else "")
    except Exception:
        pass
    return ""


class LLModelsManager:
    """
    Service layer for LLM operations
    """

    def __init__(self, logger: BrutorLogger, encryption_service: EncryptionService):
        """
        Initialize LLM Manager

        Args:
            logger: Logger instance for operation tracking
            encryption_service: Encryption service for decrypting model API keys
        """
        self.logger = logger
        self.encryption_service = encryption_service
        # Pooled client for admin → proxy side-channel calls
        # (record-probe-result, reset-resilience). Bulk health-check
        # fires ~2 of these per model in a tight loop, and the prior
        # "fresh AsyncClient per call" pattern produced 100+ concurrent
        # TCP SYNs to 127.0.0.1:8100 that overran the accept queue
        # within the 5 s connect window, surfacing as ConnectTimeout
        # warnings even though the proxy was healthy. A shared
        # keep-alive pool reuses sockets and eliminates the
        # accept-queue bottleneck. Lazy-init on first use so import
        # order and event-loop binding stay simple.
        self._proxy_internal_client: Optional[httpx.AsyncClient] = None
        self._proxy_internal_client_lock = asyncio.Lock()

    async def _get_proxy_internal_client(self) -> httpx.AsyncClient:
        """
        Return the lazily-initialized shared httpx.AsyncClient used
        for admin → proxy side-channel calls. The client maintains a
        small keep-alive pool so a bulk health-check sweep reuses
        sockets instead of opening a fresh TCP connection per probe.
        Connect timeout sits at 10 s — generous headroom over the
        previous 5 s without slowing single-model checks because
        warm sockets skip the connect step entirely.
        """
        if self._proxy_internal_client is None:
            async with self._proxy_internal_client_lock:
                if self._proxy_internal_client is None:
                    self._proxy_internal_client = httpx.AsyncClient(
                        timeout=httpx.Timeout(connect=10.0, read=8.0, write=5.0, pool=5.0),
                        limits=httpx.Limits(
                            max_keepalive_connections=20,
                            max_connections=40,
                            keepalive_expiry=60.0,
                        ),
                    )
        return self._proxy_internal_client

    async def _test_connection_via_proxy(
        self,
        model: LLModel,
        timeout: float = 5.0,
        db: Optional[Session] = None,
        probe: str = PROBE_ENDPOINT,
    ) -> Dict[str, Any]:
        """
        Test connection to a specific LLM model by routing through the Rust proxy.

        ``probe="catalog"`` (background sweep) tries the free catalog probe
        for every mode first; ``probe="endpoint"`` (default, operator click)
        only uses it for image / video, where a real generation would bill.

        The proxy handles all provider-specific details (URL building, auth, adapters).

        Args:
            model: LLModel instance to test
            timeout: Test timeout in seconds (default: 5.0)
            db: Database session for looking up an API key

        Returns:
            Dictionary with test results including success, response_time_ms, error, tokens_used
        """
        start_time = datetime.now(timezone.utc)

        result = {
            "success": False,
            "model_id": model.id,
            "model_name": model.model_name,
            # Provider is a relationship; surface the
            # slug from `.key`. Legacy clients of this dict expect
            # a string here, not a nested object.
            "provider": model.provider.key if model.provider else None,
            "response_time_ms": 0.0,
            "response": None,
            "error": None,
            "tokens_used": None,
            # Probe-type taxonomy — drives the admin UI's
            # "Catalog Probe" / "Endpoint Probe" / "Synthetic Probe"
            # badge next to Healthy/Unhealthy:
            #   - "catalog"   → hit provider's /models endpoint (free,
            #                   verifies key + reachability + that
            #                   our model_name is in their catalog)
            #   - "endpoint"  → real (cheap) inference call to the
            #                   provider's actual mode endpoint
            #   - "synthetic" → Brutor-side validation 400 only;
            #                   doesn't reach the upstream. Reserved
            #                   for skip-path modes that intentionally
            #                   never call out.
            # Default to synthetic — branches below override as they
            # take a real path so the eventual badge tells the truth
            # about the last probe.
            "probe_type": "synthetic",
        }

        try:
            proxy_url = proxy_internal_url()

            # Build auth headers — use a Brutor API key for proxy authentication.
            # The proxy requires a valid API key or JWT to resolve user/group access.
            # Set BRUTOR_GATEWAY_SERVICE_API_KEY to a Brutor API key (brk_...) that has
            # access to all models in the Default resource group.
            headers = {"Content-Type": "application/json"}

            health_check_key = gateway_service_api_key()
            if health_check_key:
                headers["Authorization"] = f"Bearer {health_check_key}"
                headers["X-API-Key"] = health_check_key
            # Pass the model's tenant context so the proxy resolves the right model
            headers["X-Tenant-ID"] = getattr(model, 'tenant_id', 'default')
            # W3C traceparent. Lets a Grafana operator click
            # from the control plane's "Test Connection" trace into the
            # proxy-side span without manually correlating timestamps.
            from app.services.observability import make_traceparent
            headers["traceparent"] = make_traceparent()
            # Tell the proxy this is an internal health probe so it skips the
            # "model must be bound to a group" access check. A freshly-added
            # catalog model has no group bindings yet — without this header
            # the admin sees a confusing 403 before they've had a chance to
            # wire access control. The proxy only honours this header when
            # combined with a global-scope service key.
            headers["X-Brutor-Internal-Health-Check"] = "1"

            # Pre-flight: if the model demands an API key but doesn't
            # have one, refuse to claim Healthy. We previously ran
            # the synthetic probe here for image / video models on
            # providers without `models_api_url` (e.g. fal_ai). The
            # probe sent a deliberately-invalid `n=0` /
            # `duration_seconds=-1` and the proxy's OWN validation
            # rejected with 400 — before authenticating, before
            # touching the upstream. We then declared the model
            # Healthy on that 400. That was dishonest: a model
            # with no API key would never serve a real request,
            # but the badge was green.
            #
            # The honest report is "Unhealthy — API key not
            # configured". Operators see immediately what's
            # missing instead of being lulled by a green badge.
            # Probes for `auth_type='none'` providers (Ollama,
            # vLLM, LM Studio when run locally) skip this check
            # because they intentionally don't need a key.
            requires_key = (
                getattr(model, "auth_type", "api_key") or "api_key"
            ).lower() == "api_key"
            if requires_key:
                has_key = bool(
                    (getattr(model, "api_key", None) or "").strip()
                )
                if not has_key:
                    end_time = datetime.now(timezone.utc)
                    result["response_time_ms"] = (
                        (end_time - start_time).total_seconds() * 1000
                    )
                    result["success"] = False
                    result["upstream_called"] = False
                    result["error"] = (
                        "API key not configured — set the provider "
                        "API key on this model to enable health checks "
                        "and real traffic."
                    )
                    # Tag as synthetic so the badge accurately
                    # reflects "we did not actually verify the
                    # upstream"; without a key we couldn't have.
                    result["probe_type"] = "synthetic"
                    return result

            # Modes that don't have a synchronous proxy probe — the
            # admin gets a "configured but not actively probed" state
            # rather than a misleading 4xx from the wrong endpoint.
            #
            # These synthetic-skip paths set
            # `upstream_called=False` so `test_connection` can skip
            # the follow-up `_reset_proxy_resilience` POST. There's
            # no resilience state to reset when the proxy never made
            # an upstream call, and the round trip just adds latency
            # to the bulk health check.
            if model.mode in _PROBE_SKIP_MODES:
                end_time = datetime.now(timezone.utc)
                result["response_time_ms"] = (
                    (end_time - start_time).total_seconds() * 1000
                )
                result["success"] = True
                result["upstream_called"] = False
                result["response"] = (
                    f"Probe skipped — mode='{model.mode}' has no synchronous "
                    "health-check endpoint. Health is implicitly verified by "
                    "live request traffic."
                )
                return result

            # Provider+mode combinations whose upstream shape doesn't
            # fit a synchronous probe (e.g. AssemblyAI's async
            # upload→submit→poll STT pipeline always exceeds the 5s
            # probe budget, and each probe would cost real money).
            provider_key = (
                model.provider.key.lower() if model.provider and model.provider.key else ""
            )
            if (provider_key, model.mode) in _PROBE_SKIP_PROVIDER_MODE:
                end_time = datetime.now(timezone.utc)
                result["response_time_ms"] = (
                    (end_time - start_time).total_seconds() * 1000
                )
                result["success"] = True
                result["upstream_called"] = False
                result["response"] = (
                    f"Probe skipped — {provider_key} {model.mode} uses an "
                    "async pipeline incompatible with synchronous probing. "
                    "Health is verified by live request traffic; use the "
                    "portal to test end-to-end."
                )
                return result

            # Catalog-probe shortcut — preferred for modes where a
            # real-inference probe would either cost money (image,
            # video) or return uninformative bytes. The probe hits
            # the provider's /models endpoint with the model's API
            # key, verifies the configured model_name appears, and
            # reports back. Free, honest, fast.
            #
            # Modes that have cheap real-inference probes (chat,
            # embeddings, TTS at 1 char, STT with a silent WAV)
            # skip this block — those branches below run the real
            # endpoint probe so the badge reads "Endpoint Probe"
            # instead of "Catalog Probe", proving the actual
            # generation path works end-to-end.
            #
            # Returns 422 when the provider has no `models_api_url`
            # configured — caller falls through to whichever
            # mode-specific path exists below.
            # Background sweeps (``probe="catalog"``) take this path for
            # EVERY mode — see the module-level note on the cost profile.
            if probe == PROBE_CATALOG or model.mode in (
                LLModelMode.IMAGE_GENERATION.value,
                LLModelMode.VIDEO_GENERATION.value,
            ):
                catalog_outcome = await _attempt_catalog_probe(
                    proxy_url=proxy_url,
                    model_id=model.id,
                    headers=headers,
                    timeout=timeout,
                )
                if (
                    catalog_outcome is not None
                    and probe == PROBE_CATALOG
                    and catalog_outcome.get("listing_only_failure")
                ):
                    # Background sweep: the listing can't speak for this
                    # model — let the mode's own probe decide (as for 422).
                    catalog_outcome = None
                if catalog_outcome is not None:
                    catalog_outcome.pop("listing_only_failure", None)
                    catalog_outcome["upstream_called"] = catalog_outcome.get(
                        "success", False
                    )
                    return catalog_outcome
                # Fall through — the provider has no catalog URL.
                # The mode's own probe runs below (endpoint for chat /
                # embedding / audio; the synthetic-validation stop-gap
                # for image / video) and tags result["probe_type"]
                # itself, so the badge reflects what actually ran.

            if model.is_embedding:
                # Real-inference probe: actually generates an
                # embedding. Tag accordingly so the badge reads
                # "Endpoint Probe" not the default synthetic.
                result["probe_type"] = "endpoint"
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/embeddings",
                        json={"model": model.model_name, "input": ["health check"]},
                        headers=headers,
                    )

                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                result["response_time_ms"] = response_time

                if resp.status_code == 200:
                    body = resp.json()
                    dims = len(body.get("data", [{}])[0].get("embedding", [])) if body.get("data") else "unknown"
                    result["success"] = True
                    result["response"] = f"Embedding generated (dimensions: {dims})"
                    usage = body.get("usage", {})
                    result["tokens_used"] = usage.get("total_tokens")
                else:
                    result["error"] = f"Proxy returned {resp.status_code}: {resp.text[:500]}"

            elif model.mode == LLModelMode.IMAGE_GENERATION.value:
                # Image probe — DALL-E / SDXL / fal video models bill
                # per-generation, so a "real" probe would cost ~$0.04
                # per check on every Test Connection click. We instead
                # send a deliberately-invalid `n=0` so the upstream
                # validates the request and rejects it BEFORE running
                # the model. A 400 with a validation-shaped body proves
                # auth + reachability without charging the tenant.
                #
                # Layered status mapping:
                #   200  → upstream actually generated something (rare;
                #          some providers ignore `n=0` and default to 1).
                #          Treat as healthy + note unexpected billing.
                #   400  → validation rejected our intentional bad input.
                #          Healthy. The response body should mention
                #          "must be" / "invalid" / similar.
                #   401  → real auth failure. Unhealthy.
                #   404  → bad model id. Unhealthy.
                #   other → unhealthy.
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/images/generations",
                        json={
                            "model": model.model_name,
                            "prompt": "ping",
                            "n": 0,
                        },
                        headers=headers,
                    )
                end_time = datetime.now(timezone.utc)
                result["response_time_ms"] = (
                    (end_time - start_time).total_seconds() * 1000
                )
                if resp.status_code == 200:
                    result["success"] = True
                    # Provider DID call upstream (n=0 was
                    # ignored and a real generation happened). Reset
                    # is meaningful.
                    result["upstream_called"] = True
                    result["response"] = (
                        "Image endpoint reachable (probe billed — provider "
                        "ignored n=0 sentinel)."
                    )
                elif resp.status_code == 400 and _looks_like_validation_400(resp.text):
                    result["success"] = True
                    # Synthetic-validation 400: the proxy
                    # short-circuited at the validator; no upstream
                    # call → no resilience state to reset.
                    result["upstream_called"] = False
                    result["response"] = (
                        "Image endpoint reachable (auth OK; upstream rejected "
                        "the n=0 sentinel as expected — no generation billed)."
                    )
                else:
                    result["error"] = (
                        f"Proxy returned {resp.status_code}: "
                        f"{resp.text[:500]}"
                    )

            elif model.mode == LLModelMode.AUDIO_SPEECH.value:
                # TTS probe — billed per character, so a single-char
                # input is essentially free (~$0.000015 on OpenAI). We
                # use the model's configured `default_voice` if set,
                # otherwise fall back to "alloy" (OpenAI's most
                # universally-available voice). Other providers
                # accept arbitrary voice strings; the upstream returns
                # a clean 400 if the voice is unrecognised, which we
                # report verbatim.
                result["probe_type"] = "endpoint"
                voice = (
                    getattr(model, "default_voice", None) or "alloy"
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/audio/speech",
                        json={
                            "model": model.model_name,
                            "voice": voice,
                            "input": "ping",
                        },
                        headers=headers,
                    )
                end_time = datetime.now(timezone.utc)
                result["response_time_ms"] = (
                    (end_time - start_time).total_seconds() * 1000
                )
                # OpenAI returns audio bytes (audio/mpeg etc.) on success
                # rather than JSON. Any 2xx with non-empty body is healthy.
                if 200 <= resp.status_code < 300:
                    result["success"] = True
                    ctype = resp.headers.get("content-type", "")
                    nbytes = len(resp.content) if resp.content else 0
                    result["response"] = (
                        f"Audio synthesised ({nbytes} bytes, {ctype or 'unknown type'})"
                    )
                else:
                    result["error"] = (
                        f"Proxy returned {resp.status_code}: "
                        f"{resp.text[:500]}"
                    )

            elif model.mode == LLModelMode.AUDIO_TRANSCRIPTION.value:
                # STT probe — multipart with a 1-second silent WAV.
                # OpenAI Whisper bills per minute (rounded up to the
                # nearest second), so this costs ~$0.0001 per probe.
                # The silent file ensures we get a deterministic
                # successful round-trip even if the underlying model
                # returns an empty transcription.
                result["probe_type"] = "endpoint"
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/audio/transcriptions",
                        data={"model": model.model_name},
                        files={"file": ("silence.wav", _SILENT_WAV_BYTES, "audio/wav")},
                        headers={
                            k: v for k, v in headers.items()
                            if k.lower() != "content-type"  # let httpx set boundary
                        },
                    )
                end_time = datetime.now(timezone.utc)
                result["response_time_ms"] = (
                    (end_time - start_time).total_seconds() * 1000
                )
                if resp.status_code == 200:
                    result["success"] = True
                    body = resp.json() if "json" in resp.headers.get(
                        "content-type", ""
                    ) else {}
                    text = (body.get("text") or "").strip()
                    result["response"] = (
                        f"Transcription returned (text='{text[:40]}')"
                        if text else "Transcription returned (empty — silent input)"
                    )
                elif resp.status_code == 400 and _looks_like_validation_400(resp.text):
                    # Provider accepted auth but rejected the audio
                    # payload itself (e.g. `audio_too_short`). The
                    # round-trip proves reachability + valid key, so
                    # treat the model as healthy.
                    result["success"] = True
                    result["response"] = (
                        "Upstream reachable; rejected probe payload "
                        f"({resp.status_code})"
                    )
                else:
                    result["error"] = (
                        f"Proxy returned {resp.status_code}: "
                        f"{resp.text[:500]}"
                    )

            elif model.mode == LLModelMode.VIDEO_GENERATION.value:
                # Video probe — generation costs $$+ per clip, so we
                # never actually submit a job. Instead we send an
                # intentionally-invalid `duration_seconds=-1` so the
                # proxy's video route validates our input and 400s
                # before forwarding to the upstream. A 400 means the
                # proxy was reachable + auth was accepted; that's
                # enough to declare the model healthy.
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/video/generations",
                        json={
                            "model": model.model_name,
                            "prompt": "ping",
                            "duration_seconds": -1,
                        },
                        headers=headers,
                    )
                end_time = datetime.now(timezone.utc)
                result["response_time_ms"] = (
                    (end_time - start_time).total_seconds() * 1000
                )
                if resp.status_code == 200:
                    result["success"] = True
                    result["upstream_called"] = True
                    result["response"] = (
                        "Video job submitted (probe billed — proxy accepted "
                        "the invalid duration sentinel)."
                    )
                elif resp.status_code == 400 and _looks_like_validation_400(resp.text):
                    result["success"] = True
                    # Synthetic 400: validator caught the
                    # duration_seconds=-1 sentinel before any upstream
                    # call. No reset needed.
                    result["upstream_called"] = False
                    result["response"] = (
                        "Video endpoint reachable (auth OK; upstream rejected "
                        "the duration_seconds=-1 sentinel as expected — no "
                        "generation billed)."
                    )
                elif resp.status_code == 401:
                    result["error"] = (
                        f"Proxy returned 401: {resp.text[:500]}"
                    )
                else:
                    # Async submit endpoints often return 202 with a
                    # job id when a real job lands. We accept 2xx as
                    # healthy and surface the body for debugging.
                    if 200 <= resp.status_code < 300:
                        result["success"] = True
                        result["response"] = (
                            f"Video endpoint accepted probe ({resp.status_code})"
                        )
                    else:
                        result["error"] = (
                            f"Proxy returned {resp.status_code}: "
                            f"{resp.text[:500]}"
                        )

            elif model.is_completion:
                # The proxy nests every LLM route under ``/v1/proxy/llm`` —
                # ``/v1/proxy/completions`` (without ``/llm/``) is unmapped
                # and returns an empty 404, which the probe was reporting
                # as "Proxy returned 404:" with no body. Same prefix the
                # embedding probe above already uses correctly.
                result["probe_type"] = "endpoint"
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/completions",
                        json={"model": model.model_name, "prompt": "Say hello", "max_tokens": 5},
                        headers=headers,
                    )

                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                result["response_time_ms"] = response_time

                if resp.status_code == 200:
                    body = resp.json()
                    result["success"] = True
                    result["response"] = body.get("choices", [{}])[0].get("text", "").strip() if body.get("choices") else None
                    usage = body.get("usage", {})
                    result["tokens_used"] = usage.get("total_tokens")
                else:
                    result["error"] = f"Proxy returned {resp.status_code}: {resp.text[:500]}"

            elif model.is_responses:
                # Responses API probe — designed for reasoning-only
                # models (o-series, gpt-5-*-pro). The request body
                # here matches what OpenAI's Responses API expects:
                #   - `input` is a minimal user turn
                #   - `max_output_tokens` caps billed output; reasoning
                #     models burn tokens fast on their internal CoT,
                #     even a 2-token reply can cost thousands. 64 is a
                #     safe cap that stops runaway probe spend while
                #     still letting the model produce *something*.
                #   - `reasoning.effort: "low"` is the cheapest tier
                #     accepted by *both* model families:
                #       · gpt-5.x supports {minimal, low, medium, high}
                #       · o-series (o1, o3, o4) supports {low, medium, high}
                #         and returns 400 'unsupported_value' on "minimal".
                #     Picking "low" gives us the same lightweight probe
                #     across both families without per-model dispatch.
                result["probe_type"] = "endpoint"
                probe_body = {
                    "model": model.model_name,
                    "input": "ping",
                    "max_output_tokens": 64,
                    "reasoning": {"effort": "low"},
                }
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/responses",
                        json=probe_body,
                        headers=headers,
                    )

                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                result["response_time_ms"] = response_time

                if resp.status_code == 200:
                    body = resp.json()
                    usage = body.get("usage", {})
                    total = usage.get("total_tokens") or (
                        (usage.get("input_tokens") or 0)
                        + (usage.get("output_tokens") or 0)
                    )

                    # Responses API returns `status: completed` when
                    # the model finished; `incomplete` if it hit the
                    # max_output_tokens cap (which is expected for
                    # our 64-token probe). Both are "upstream is
                    # reachable + auth works" — the only real fail
                    # mode at HTTP 200 is `status: failed`.
                    status_str = body.get("status", "completed")
                    if status_str == "failed":
                        err = body.get("error", {}).get(
                            "message", "unknown error"
                        )
                        result["error"] = (
                            f"Responses API returned status=failed: {err}"
                        )
                    else:
                        # Pull a short text excerpt from the first
                        # output message so operators see the model
                        # actually responded, not just a 200. Fall
                        # back to status string if structure differs.
                        excerpt = _extract_responses_text(body)
                        result["success"] = True
                        result["response"] = (
                            f"Responses API ({status_str})"
                            + (f" — {excerpt}" if excerpt else "")
                        )
                        result["tokens_used"] = total if total else None
                else:
                    # Surface the provider body so admins can see
                    # whether it was an auth (401) / quota (429) /
                    # bad-model (404) failure without guessing.
                    result["error"] = (
                        f"Proxy returned {resp.status_code}: "
                        f"{resp.text[:500]}"
                    )

            else:
                # Chat completion health check via proxy (default).
                #
                # Reasoning / "pro" variants (o1/o3 family, gpt-5-*-pro,
                # etc.) are NOT chat-completions compatible at the
                # provider and return a 404 with a body like:
                #   "This is not a chat model and thus not supported
                #    in the v1/chat/completions endpoint."
                # They require the Responses API. Operators often seed
                # these with the default mode="chat" and then blame us
                # when the health check fails — so on that specific
                # 404 shape, transparently fall back to /responses
                # before declaring the model unhealthy. A successful
                # fallback reports healthy and annotates the response
                # text so the operator knows to flip the mode.
                result["probe_type"] = "endpoint"
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{proxy_url}/v1/proxy/llm/chat/completions",
                        json={"model": model.model_name, "messages": [{"role": "user", "content": "test"}], "max_tokens": 5},
                        headers=headers,
                    )

                    probe_body_text = resp.text
                    looks_like_reasoning_model = (
                        resp.status_code == 404
                        and (
                            "not a chat model" in probe_body_text.lower()
                            or "v1/responses" in probe_body_text.lower()
                        )
                    )
                    if looks_like_reasoning_model:
                        fallback_resp = await client.post(
                            f"{proxy_url}/v1/proxy/llm/responses",
                            json={"model": model.model_name, "input": "health check"},
                            headers=headers,
                        )
                        end_time = datetime.now(timezone.utc)
                        response_time = (end_time - start_time).total_seconds() * 1000
                        result["response_time_ms"] = response_time

                        if fallback_resp.status_code == 200:
                            body = fallback_resp.json()
                            usage = body.get("usage", {})
                            total = usage.get("total_tokens") or (
                                (usage.get("input_tokens") or 0)
                                + (usage.get("output_tokens") or 0)
                            )
                            result["success"] = True
                            result["response"] = (
                                "Reasoning model detected — verified via Responses "
                                "API. Change this model's mode to 'responses' so "
                                "normal traffic skips the chat/completions probe."
                            )
                            result["tokens_used"] = total
                        else:
                            result["error"] = (
                                f"Chat completions rejected (404 — model is not "
                                f"chat-compatible). Responses API fallback also "
                                f"failed with {fallback_resp.status_code}: "
                                f"{fallback_resp.text[:300]}"
                            )
                        return result

                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                result["response_time_ms"] = response_time

                if resp.status_code == 200:
                    body = resp.json()
                    result["success"] = True
                    result["response"] = body.get("choices", [{}])[0].get("message", {}).get("content") if body.get("choices") else None
                    usage = body.get("usage", {})
                    result["tokens_used"] = usage.get("total_tokens")
                else:
                    result["error"] = f"Proxy returned {resp.status_code}: {resp.text[:500]}"

        except Exception as e:
            end_time = datetime.now(timezone.utc)
            response_time = (end_time - start_time).total_seconds() * 1000
            result["response_time_ms"] = response_time
            result["error"] = str(e)

        return result

    async def _reset_proxy_resilience(self, model: LLModel) -> None:
        """
        Tell the Rust proxy to clear in-memory resilience state for this
        model — circuit breaker, probe throttle, and router cooldown.

        Called after a successful `test_connection` to unstick a model
        the operator just verified is healthy.

        Why short-timeout fire-and-forget:
            This call is **advisory**. The proxy's natural probe cadence
            resets breaker state on its own; calling reset-resilience
            here is just a latency optimisation to avoid the operator
            seeing a one-cycle 503 immediately after their "Test
            Connection" press. If the call fails, the next live request
            triggers a probe and the breaker recovers — nothing is broken.

            Previously this used a 10-second read timeout. Under a
            bulk-health sweep, that meant 70 models × 10s worst-case =
            12 minutes of dead time when the proxy was momentarily
            unable to respond. With the actual single-UPDATE endpoint
            running in <100ms, a 2s read timeout is comfortably above
            normal latency and surfaces real proxy outages 5× faster.
        """
        proxy_url = proxy_internal_url()
        tenant_id = getattr(model, "tenant_id", "default")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
        }
        key = gateway_service_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
            headers["X-API-Key"] = key
        # Propagate a W3C traceparent so a slow reset
        # surfaces in the same Grafana trace tree as the
        # `_test_connection_via_proxy` call that triggered it.
        from app.services.observability import make_traceparent
        headers["traceparent"] = make_traceparent()

        url = f"{proxy_url}/v1/proxy/llm/admin/{model.id}/reset-resilience"
        # 8 s now that the proxy's reset
        # endpoint is a single UPDATE on the model row that no
        # longer contends with per-request `total_requests++`
        # writes (those go through the in-process aggregator).
        # 8 s is a sane upper bound for "the proxy is briefly slow";
        # 2 s hid real slowness
        # and 10 s was so loose it stretched
        # bulk health checks into multi-minute exercises.
        #
        # Use the pooled side-channel client so a bulk
        # sweep reuses keep-alive sockets to the proxy instead of
        # opening a fresh TCP connection per call. The pool config
        # carries its own timeouts (connect=10 s, read=8 s).
        client = await self._get_proxy_internal_client()
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()

    async def _record_probe_result(
        self,
        *,
        model: LLModel,
        success: bool,
        status_code: int,
        latency_ms: float,
        probe_type: str,
    ) -> None:
        """Centralized probe-outcome accounting.

        Posts the probe result to the proxy's
        `record-probe-result` endpoint, which writes ALL THREE
        counters (proxy_metrics, usage_logs, model_metrics) in a
        single place. This is the only metric-write site for
        probe traffic — the routes themselves skip writes when
        the `X-Brutor-Internal-Health-Check` header is present.
        Net effect: 1 probe → +1 in every operator-facing view.

        Best-effort: a network blip here only loses metric
        visibility for one cycle, which is preferable to crashing
        the health-check sweep. The caller wraps this in
        ``try/except`` and logs a WARN.
        """
        proxy_url = proxy_internal_url()
        tenant_id = getattr(model, "tenant_id", "default")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
            # Tell the proxy this is a probe so request-handler
            # paths (including the auth middleware's group lookup)
            # treat it consistently with the actual probe traffic.
            "X-Brutor-Internal-Health-Check": "1",
        }
        key = gateway_service_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
            headers["X-API-Key"] = key
        from app.services.observability import make_traceparent
        headers["traceparent"] = make_traceparent()

        url = f"{proxy_url}/v1/proxy/llm/admin/{model.id}/record-probe-result"
        payload = {
            "success": success,
            "status_code": int(status_code),
            "latency_ms": float(latency_ms or 0.0),
            "probe_type": probe_type,
        }
        # Pooled side-channel client — see _get_proxy_internal_client.
        # Eliminates the burst of fresh TCP connects that surfaced as
        # ConnectTimeout warnings during bulk health-check sweeps.
        client = await self._get_proxy_internal_client()
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()

    async def test_connection(
        self,
        model: LLModel,
        db: Session,
        timeout: Optional[int] = None,
        probe: str = PROBE_ENDPOINT,
    ) -> Dict[str, Any]:
        """
        Test connection to an LLM by routing through the Rust proxy.

        The Rust proxy handles all provider-specific details (URL building,
        auth handling, adapter logic).

        Args:
            model: LLM to test
            db: Database session for updating health status
            timeout: Optional timeout override (seconds, default: 5)
            probe: ``"endpoint"`` (default — operator-initiated, real
                end-to-end probe) or ``"catalog"`` (background sweep —
                free catalog probe where the provider has a catalog URL,
                endpoint probe otherwise). The result's ``probe_type``
                reports which probe actually ran.

        Returns:
            Dictionary matching HealthCheckResponse Pydantic model:
            {
                "model_id": str,
                "model_name": str,
                "status": "healthy" | "unhealthy" | "timeout",
                "response_time_ms": float | None,
                "error_message": str | None,
                "timestamp": str
            }
        """
        if probe not in HEALTH_CHECK_PROBES:
            raise ValueError(
                f"probe must be one of {HEALTH_CHECK_PROBES}, got {probe!r}"
            )
        timestamp = datetime.now(timezone.utc).isoformat()
        timeout_value = float(timeout) if timeout else 5.0

        # Virtual routing group models don't have real connections — always healthy
        if model.is_routing:
            self.logger.debug("Skipping health check for virtual routing group model", context={
                "model_id": model.id
            })
            return {
                "model_id": model.id,
                "model_name": model.model_name,
                "status": "healthy",
                "response_time_ms": 0.0,
                "error_message": None,
                "timestamp": timestamp,
                "probe_type": None,
            }

        try:
            self.logger.debug("Testing LLM connection via proxy", context={
                "model_id": model.id,
                "model_name": model.model_name,
                # Slug for the log; relationship may be unloaded
                # if the caller passed a freshly-constructed model
                # (e.g. the test-connection path).
                "provider": model.provider.key if model.provider else None,
            })

            # Test connection through the Rust proxy
            adapter_result = await self._test_connection_via_proxy(
                model, timeout=timeout_value, db=db, probe=probe
            )

            # Map adapter result to HealthCheckResponse format
            capabilities = model.capabilities or {}
            is_restricted = bool(capabilities.get("restricted_tier"))
            required_tier = capabilities.get("required_tier") or "Pro"

            if adapter_result["success"]:
                status = ServerStatusType.HEALTHY.value
                error_message = None
                alert = None

                self.logger.debug("Connection test successful", context={
                    "model_id": model.id,
                    "response_time_ms": adapter_result["response_time_ms"],
                    "tokens_used": adapter_result.get("tokens_used")
                })
            else:
                error = adapter_result.get("error", "").lower()
                error_message = adapter_result.get("error", "Unknown error")
                alert = None
                # Restricted-tier models (OpenAI Codex preview, Pro,
                # o1-pro, ...) only respond with a tier-eligible API
                # key. When the configured key isn't eligible, surface
                # a dedicated ``requires_tier`` status so dashboards
                # render "Requires <Tier>" instead of red "Unhealthy".
                # This is a key-eligibility signal, not a real failure.
                if is_restricted:
                    status = ServerStatusType.REQUIRES_TIER.value
                    alert = (
                        f"Requires {required_tier} tier — "
                        f"'{model.model_name}' is a restricted-tier "
                        f"model. Your configured API key doesn't have "
                        f"access; the provider rejected the health "
                        f"probe. This is expected unless the key has "
                        f"been upgraded to that tier."
                    )
                    if error_message:
                        alert += f" Provider response: {error_message}"
                elif "timeout" in error:
                    status = "timeout"
                else:
                    status = ServerStatusType.UNHEALTHY.value

                self.logger.warning("Connection test failed", context={
                    "model_id": model.id,
                    "error": error_message,
                    "status": status,
                    "restricted_tier": is_restricted,
                    "required_tier": required_tier if is_restricted else None,
                })

            # Build response matching HealthCheckResponse
            result = {
                "model_id": model.id,
                "model_name": model.model_name,
                "status": status,
                "response_time_ms": adapter_result["response_time_ms"],
                "error_message": error_message,
                "timestamp": timestamp,
                # Which probe produced this verdict — "catalog" when a
                # background sweep used the free /models listing.
                "probe_type": adapter_result.get("probe_type"),
            }
            if is_restricted:
                result["required_tier"] = required_tier
            if alert:
                result["alert"] = alert

            # Update model health status in database. ``requires_tier``
            # is a first-class state alongside healthy/unhealthy so the
            # runtime router can avoid forwarding to it (it's not
            # reachable with the configured key) without painting the
            # UI red.
            if adapter_result["success"]:
                db_status = ServerStatusType.HEALTHY.value
            elif is_restricted:
                db_status = ServerStatusType.REQUIRES_TIER.value
            else:
                db_status = ServerStatusType.UNHEALTHY.value
            model.update_health_status(
                status=db_status,
                response_time_ms=adapter_result["response_time_ms"],
                error_message=(
                    f"Requires {required_tier} tier"
                    if db_status == ServerStatusType.REQUIRES_TIER.value
                    else error_message
                ),
                # Persist the probe taxonomy that produced this
                # outcome so the admin UI can show the secondary
                # badge ("Catalog Probe" / "Endpoint Probe" /
                # "Synthetic Probe") next to Healthy/Unhealthy.
                probe_type=adapter_result.get("probe_type"),
            )
            # COMMIT before the proxy round-trip below — not just flush.
            #
            # Why: this UPDATE takes a row-level lock on `llm_model.id`.
            # The proxy's `/admin/{id}/reset-resilience` endpoint runs
            # its own UPDATE on the same row. If we hold the lock here
            # while making the HTTP call, the proxy's UPDATE blocks
            # waiting for our transaction to commit, but our
            # transaction can't commit until the HTTP call returns —
            # classic cross-process deadlock that resolves only when
            # the 8 s read-timeout fires. Symptom: every test_connection
            # appears to "succeed" then logs `Proxy resilience reset
            # failed (non-fatal)` 8 s later, slowing every bulk
            # health-check loop and the live e2e suite to a crawl.
            #
            # Committing here releases the row lock immediately. If
            # the proxy reset fails afterwards it's still logged
            # non-fatal — but in the happy path the proxy's UPDATE
            # completes in milliseconds instead of timing out at 8 s.
            db.commit()

            # On success, reset the proxy's in-memory resilience state
            # (circuit breaker, probe throttle, router cooldown) for
            # this model. Without this, the proxy's gate_dispatch would
            # keep rejecting requests until the natural probe cadence
            # elapses — the "stuck unhealthy" symptom operators see
            # when they try to manually rehabilitate a model.
            #
            # Gate the reset on `upstream_called=True`.
            # Synthetic-probe modes (audio_transcription via
            # AssemblyAI's async pipeline, video_generation with
            # `duration_seconds=-1` sentinel, image_generation with
            # `n=0` sentinel, modes in `_PROBE_SKIP_MODES`) never
            # actually hit the upstream — there's no resilience
            # state to reset, and the round trip just adds latency
            # to bulk health checks. ``upstream_called`` defaults
            # to True for back-compat with any path that doesn't
            # set it explicitly (every successful real probe sets
            # it).
            #
            # A BACKGROUND catalog success does not reset either: a
            # /models listing proves key + reachability, not that
            # generation works, so it must not clear a breaker the
            # proxy opened on real traffic failures. Only an operator's
            # check (or a background probe that fell back to the real
            # endpoint) earns the reset.
            upstream_called = adapter_result.get("upstream_called", True)
            background_catalog = (
                probe == PROBE_CATALOG
                and adapter_result.get("probe_type") == PROBE_CATALOG
            )
            if adapter_result["success"] and upstream_called and not background_catalog:
                try:
                    await self._reset_proxy_resilience(model)
                except Exception as reset_err:  # noqa: BLE001 — best-effort
                    self.logger.warning(
                        "Proxy resilience reset failed (non-fatal)",
                        context={
                            "model_id": model.id,
                            "error": str(reset_err),
                        },
                    )

            # Centralized probe accounting — write proxy_metrics +
            # usage_logs + model_metrics atomically via the proxy's
            # record-probe-result endpoint. ALL probe paths above
            # (catalog, endpoint, synthetic) flow through here, so
            # operators see exactly +1 in every view per probe.
            #
            # Best-effort fire-and-forget: a failure to record
            # metrics must not affect the probe response or the
            # health-status update we just committed. Any error is
            # logged at WARN and we move on.
            try:
                await self._record_probe_result(
                    model=model,
                    success=adapter_result["success"],
                    status_code=200 if adapter_result["success"] else 502,
                    latency_ms=adapter_result.get("response_time_ms") or 0.0,
                    probe_type=adapter_result.get("probe_type") or "synthetic",
                )
            except Exception as record_err:  # noqa: BLE001 — best-effort
                self.logger.warning(
                    "Probe result recording failed (non-fatal)",
                    context={
                        "model_id": model.id,
                        "error": str(record_err),
                    },
                )

            return result

        except Exception as e:
            # Handle unexpected errors
            self.logger.warning("Connection test failed with exception", context={
                "model_id": model.id,
                "error": str(e),
                "error_type": type(e).__name__
            })

            error_message = f"Unexpected error: {str(e)}"
            result = {
                "model_id": model.id,
                "model_name": model.model_name,
                "status": ServerStatusType.UNHEALTHY.value,
                "response_time_ms": None,
                "error_message": error_message,
                "timestamp": timestamp,
                "probe_type": None,
            }

            # Update database
            model.update_health_status(
                status=ServerStatusType.UNHEALTHY.value,
                response_time_ms=None,
                error_message=error_message,
                # Unexpected error path — we don't know which probe
                # taxonomy was in flight. Leave None so the badge
                # falls back to "no probe info".
                probe_type=None,
            )
            db.flush()

            return result

    def to_response(
        self,
        model: LLModel,
        include_groups: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Convert LLModel model to API response format

        Args:
            model: LLModel database model
            include_groups: Whether to include group information (requires eager loading)
            **kwargs: Additional fields to include in response (use sparingly)

        Returns:
            Dictionary matching LLModelResponse Pydantic schema exactly
        """
        # Calculate success rate for performance metrics
        success_rate = None
        if model.total_requests and model.total_requests > 0:
            success_rate = ((model.total_requests - model.failed_requests) / model.total_requests) * 100

        # Build response matching LLModelResponse Pydantic model
        response = {
            # Basic Information
            "id": model.id,
            "name": model.name,
            "description": model.description,
            "usage": model.usage,
            "tags": model.tags,

            # Provider Information.
            # `model.provider` is a relationship to
            # the `LlmProvider` row; the legacy slug is on
            # `provider.key`. We surface all three forms so older
            # admin UI code keeps working:
            #   * `provider`      — the slug string (legacy seam)
            #   * `provider_id`   — FK into llm_providers (preferred)
            #   * `provider_info` — nested provider row for rich UI
            "provider": model.provider.key if model.provider else None,
            "provider_id": model.provider_id,
            "provider_info": (
                model.provider.to_dict() if model.provider else None
            ),
            # Catalog provenance — the global llm_catalog entry this model
            # was imported from (NULL for hand-rolled models).
            "catalog_id": model.catalog_id,
            "model_name": model.model_name,
            "mode": model.mode,

            # Connection Details
            "api_url_auto_discovered": model.api_url_auto_discovered,
            "api_url": model.api_url,

            # Authentication
            "auth_type": model.auth_type,
            # Note: api_key is deliberately excluded for security
            # The cloud credential: that one is stored, the region, and
            # which secret keys are stored. Never a secret.
            "has_provider_config": bool(model.provider_config),
            **provider_config_rules.public_view(
                provider_config_rules.read(model.provider_config, getattr(self, "encryption_service", None))
            ),

            # Model Configuration
            "default_temperature": model.default_temperature,
            "default_max_tokens": model.default_max_tokens,
            "system_prompt": model.system_prompt,

            # Completion parameters
            "top_p": model.top_p,
            "n": model.n,
            "stop_sequences": model.stop_sequences,
            "seed": model.seed,
            "presence_penalty": model.presence_penalty,
            "frequency_penalty": model.frequency_penalty,
            "logit_bias": model.logit_bias,
            "response_format": model.response_format,
            "logprobs": model.logprobs,
            "top_logprobs": model.top_logprobs,
            "stream_options": model.stream_options,

            # Capabilities
            "supports_streaming": model.supports_streaming,
            "supports_function_calling": model.supports_function_calling,
            "supports_vision": model.supports_vision,
            "supports_batch": model.supports_batch,

            # Tools/Functions Configuration
            "tools": model.tools,
            "tool_choice": model.tool_choice,
            "parallel_tool_calls": model.parallel_tool_calls,

            # Safety & Monitoring
            "user_identifier": model.user_identifier,

            # Provider-Specific Configuration
            "api_version": model.api_version,
            "extra_headers": model.extra_headers,

            # Advanced configuration (retry/fallback = routing-group)
            "model_metadata": model.model_metadata,
            "drop_params": model.drop_params,
            "additional_drop_params": model.additional_drop_params,

            # Routing configuration
            "routing_group_id": model.routing_group_id,
            "routing_weight": model.routing_weight,
            "fallback_models": model.fallback_models,
            "rate_limit_rpm": model.rate_limit_rpm,
            "rate_limit_tpm": model.rate_limit_tpm,

            # Model Size
            "parameters": getattr(model, 'parameters', None),

            # Cost Management
            "cost_per_input_token": model.cost_per_input_token,
            "cost_per_output_token": model.cost_per_output_token,

            # Multimodal: image generation
            "cost_per_image": model.cost_per_image,
            "supported_image_sizes": model.supported_image_sizes,
            "supported_image_qualities": model.supported_image_qualities,
            "max_images_per_request": model.max_images_per_request,

            # Multimodal: audio TTS
            "cost_per_audio_char": model.cost_per_audio_char,
            "supported_voices": model.supported_voices,
            "supported_audio_formats": model.supported_audio_formats,

            # Multimodal: audio STT
            "cost_per_audio_minute": model.cost_per_audio_minute,
            "max_audio_duration_seconds": model.max_audio_duration_seconds,
            "supported_input_formats": model.supported_input_formats,
            "supported_response_formats": model.supported_response_formats,

            # Multimodal: video generation
            "cost_per_video_second": model.cost_per_video_second,
            "supported_video_resolutions": model.supported_video_resolutions,
            "supported_video_durations": model.supported_video_durations,
            "max_video_duration_seconds": model.max_video_duration_seconds,

            # Reasoning models (thinking_enabled)
            "thinking_enabled": getattr(model, "thinking_enabled", None),

            # Configuration
            "timeout": model.timeout,
            "max_concurrent_requests": model.max_concurrent_requests,
            "allowed_fails": model.allowed_fails,
            "cooldown_time": model.cooldown_time,
            "enabled": model.enabled,
            # Provenance — 'manual' (operator-added) vs 'import' (discovered via
            # Traffic Data Import; observability-only until promoted).
            "source": getattr(model, "source", "manual"),

            # Retirement metadata — Date columns serialise to ISO
            # `YYYY-MM-DD` strings so the admin UI's <input type="date">
            # round-trips without timezone gymnastics. `to_dict()` on
            # the SQLAlchemy model does the same conversion; this
            # builder is independent, so it has to do it explicitly
            # (Pydantic's from_attributes path is NOT used here —
            # see the response = {} pattern above).
            "retire_date": (
                model.retire_date.isoformat() if model.retire_date else None
            ),
            "retirement_announced_at": (
                model.retirement_announced_at.isoformat()
                if model.retirement_announced_at else None
            ),
            "retirement_reminder_date": (
                model.retirement_reminder_date.isoformat()
                if model.retirement_reminder_date else None
            ),
            "retirement_replacement_model_name": model.retirement_replacement_model_name,
            "retirement_replacement_provider_key": model.retirement_replacement_provider_key,
            "retirement_notes": model.retirement_notes,

            # Model Capabilities
            "capabilities": model.capabilities,

            # ===================================================================
            # GROUP-BASED ACCESS CONTROL
            # ===================================================================

            "is_global": len(list(model.resource_groups)) == 0,  # True if no group assignments
            "group_ids": [],  # Will be populated below
            "groups": None,  # Will be populated below if include_groups=True

            # ===================================================================

            # Health Status (NESTED OBJECT - matches LLMModelHealthStatusResponse)
            "health_status": {
                "status": model.health_status or "unknown",
                "last_check": model.last_health_check.isoformat() if model.last_health_check else None,
                "last_success": model.last_health_success.isoformat() if model.last_health_success else None,
                "consecutive_failures": model.consecutive_health_failures or 0,
                "error_message": model.health_check_error,
                "avg_latency_ms": model.avg_latency_ms,
                # `probe_type` drives the admin UI's secondary
                # badge (Catalog / Endpoint / Synthetic). Without
                # this key the field is dropped from the response
                # — Pydantic only forwards keys present in this
                # hand-built dict — and the badge renders as null
                # everywhere. This is the bug that hid the badges
                # for the entire previous test cycle.
                "probe_type": model.last_probe_type,
            },

            # Performance Metrics (NESTED OBJECT - matches PerformanceMetricsResponse)
            "performance_metrics": {
                "total_requests": model.total_requests or 0,
                "failed_requests": model.failed_requests or 0,
                "success_rate": round(success_rate, 2) if success_rate is not None else 0.0,
                "avg_latency_ms": round(model.avg_latency_ms, 2) if model.avg_latency_ms else None,
                "total_tokens_consumed": model.total_tokens_consumed or 0
            },

            # Multi-tenancy
            "tenant_id": model.tenant_id,

            # AI Resource version + authorship. This builder is
            # field-by-field, so a value present on the model and in the
            # response schema still never reaches the client unless it is
            # named here — which is how the Audit Info tab rendered
            # "unknown" over correctly-stamped rows.
            "version": model.version,
            "created_by": model.created_by,
            "updated_by": model.updated_by,
            # Timestamps
            "created_at": model.created_at.isoformat() if model.created_at else None,
            "updated_at": model.updated_at.isoformat() if model.updated_at else None
        }

        # Always populate group_ids (flat list for forms/selectors)
        try:
            response["group_ids"] = [g.id for g in model.resource_groups]
        except Exception:
            response["group_ids"] = []

        # Add detailed group information if requested
        if include_groups:
            try:
                # Get groups from relationship (requires eager loading or separate query)
                groups_list = []
                for group in model.resource_groups:
                    groups_list.append({
                        "id": group.id,
                        "name": group.name,
                        "display_name": group.display_name,
                        "group_type": group.group_type,
                        "is_active": group.is_active,
                        "parent_group_id": group.parent_group_id
                    })
                response["groups"] = groups_list
            except Exception as e:
                # If groups not loaded, leave as None
                self.logger.debug("Could not load groups for model", context={
                    "model_id": model.id,
                    "error": str(e)
                })
                response["groups"] = None

        # Add any additional fields passed as kwargs
        # (Use sparingly - should match Pydantic schema)
        response.update(kwargs)

        # Security: NEVER return api_key
        response.pop('api_key', None)

        return response