# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Skill source sync scheduler — background asyncio loop that wakes
every minute, finds due SkillSource rows, walks each one's GitHub
repo, and feeds discovered skills + sibling files into the existing
`_ingest_discovered_skills` reconciler.

Mirrors the pattern used by `audit_export_scheduler` and
`llm_retirement_warner`:

  * One asyncio task, NOT one per source.
  * `asyncio.to_thread(...)` for the DB-bound work so SQLAlchemy's
    sync session doesn't fight the event loop.
  * The async GitHub walks happen on the loop directly (httpx
    AsyncClient).
  * Per-source failures are caught and recorded on the row via
    `record_error()`; the loop never dies on one bad repo.
  * Per-tick beacon written FIRST so the badge doesn't flap.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, Sequence

import httpx

from app.core.logging_system import BrutorLogger
from app.database import SessionLocal
from app.database.models.scheduler_runtime_state import (
    SCHEDULER_SKILL_SOURCE_SYNC,
)
from app.services.scheduler_leader import run_as_leader
from app.services.skill_source_sync.frontmatter import (
    FrontmatterError,
    parse_skill_md,
)
from app.services.skill_source_sync.github_walker import (
    GithubError,
    GithubRateLimitedError,
    SiblingFile,
    SkillTooLargeError,
    check_size_caps,
    check_skill_total,
    classify_sibling,
    collect_sibling_files,
    decode_blob_for_storage,
    fetch_blob,
    fetch_tree,
    find_skill_md_paths,
    parse_github_url,
    resolve_token_from_env,
    skill_dir_of,
    tree_size_of_skill,
)


log = logging.getLogger("brutor-skill-source-sync")
_brutor_log = BrutorLogger("brutor-skill-source-sync")


# Wake every minute. SkillSource intervals are minutes-to-hours, so
# 60s granularity is more than enough; tighter ticks would just burn
# DB queries finding "nothing's due yet".
TICK_INTERVAL_SECONDS = 60


# Cap concurrent sources per tick. Most installs have <10 sources
# total, so this rarely kicks in — but it keeps a 100-source tenant
# from saturating the GitHub rate limit in a single burst.
MAX_CONCURRENT_SOURCES = 4


async def skill_source_sync_loop(
    tick_interval_seconds: int = TICK_INTERVAL_SECONDS,
) -> None:
    """The actual background loop. Long-running, never returns under
    normal operation.

    Args:
        tick_interval_seconds: Override for tests. Production should
            leave at the default.
    """
    log.info(
        "Skill source sync scheduler started — tick every %ds",
        tick_interval_seconds,
    )
    while True:
        try:
            # Per-tick beacon. Best-effort: a
            # beacon write failure logs but never blocks the tick.
            await asyncio.to_thread(_write_beacon_safe)
            await _run_due_sources()
        except Exception:
            # Defensive — per-source failures are already caught
            # inside `_sync_one_source`. This catches catastrophic
            # failures (DB unreachable, etc.) so the loop doesn't
            # silently die.
            log.exception(
                "Skill source sync scheduler tick failed at the top level"
            )
        await asyncio.sleep(tick_interval_seconds)


def _write_beacon_safe() -> None:
    """Write the per-tick beacon. Runs inside `asyncio.to_thread` so
    the sync SQLAlchemy session doesn't block the event loop. Failure
    is non-fatal."""
    from app.services.system_ops.schedulers import write_beacon

    db = SessionLocal()
    try:
        write_beacon(db, "skill_source_sync")
    except Exception:
        log.exception("Skill source sync beacon write failed (non-fatal)")
    finally:
        db.close()


async def _run_due_sources() -> None:
    """One scheduler tick:
      1. Fetch the list of due source IDs (sync, short DB session).
      2. Open ONE httpx AsyncClient for the whole tick (connection
         pooling).
      3. Process each due source. We bound concurrency with a
         semaphore — see MAX_CONCURRENT_SOURCES rationale at module
         top.
    """
    # Leader election (app/services/scheduler_leader.py). Gunicorn's
    # default 2 workers each run this loop, and `_claim_source`'s CAS is
    # a read-then-write with no row lock — two workers can both see the
    # row `idle` and both walk the same GitHub repo, burning double the
    # rate limit for one sync. Gating the *selection* means only the
    # winning worker even attempts a claim; `run_as_leader` returns None
    # on a skipped tick, which falls through the empty check below.
    due_ids = await asyncio.to_thread(
        run_as_leader, SCHEDULER_SKILL_SOURCE_SYNC, _fetch_due_source_ids
    )
    if not due_ids:
        log.debug("Skill source sync tick: no sources due")
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_SOURCES)
    # httpx.AsyncClient + connection-pool. One client per tick gives
    # GitHub's TCP/TLS overhead a chance to amortise across sibling
    # blob fetches; reopening per-source would be measurably slower.
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
        # GitHub asks API clients to set a UA. Some endpoints
        # 403 without it.
        headers={"User-Agent": "brutor-skill-source-sync/1.0"},
    ) as client:
        async def _bounded(source_id: str) -> None:
            async with sem:
                await _sync_one_source(client, source_id)

        await asyncio.gather(
            *(_bounded(sid) for sid in due_ids),
            return_exceptions=False,
        )


def _fetch_due_source_ids() -> List[str]:
    """Return IDs of all SkillSource rows that are due for a sync.

    Rule (delegated to `SkillSource.is_due()` so the model owns the
    state machine): enabled AND not currently syncing AND elapsed-
    time check passes.

    We pre-filter in SQL on the easy predicates (enabled, status) to
    keep the candidate set small, then evaluate `is_due()` in Python
    because it does timezone-aware arithmetic that's awkward in SQL.
    """
    from app.database.models.skill_source import (
        SYNC_STATUS_SYNCING,
        SkillSource,
    )

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        rows = (
            db.query(SkillSource)
            .filter(
                SkillSource.enabled.is_(True),
                SkillSource.status != SYNC_STATUS_SYNCING,
            )
            .all()
        )
        return [r.id for r in rows if r.is_due(now)]
    finally:
        db.close()


async def _sync_one_source(
    client: httpx.AsyncClient, source_id: str
) -> None:
    """End-to-end sync of a single source.

      1. CAS the row to `syncing` (short DB session).
      2. Walk GitHub (async, no DB).
      3. Fetch SKILL.md + sibling blobs (async, no DB).
      4. Hand the discovered set to `_ingest_discovered_skills`
         (short DB session — the existing reconciler).
      5. Finish or record the error (short DB session).

    All per-source exceptions land in `record_error()`; we don't
    re-raise.
    """
    # Step 1 — claim the row. If is_due() raced and another scheduler
    # already started this one, the CAS short-circuits.
    claimed = await asyncio.to_thread(_claim_source, source_id)
    if not claimed:
        return
    repo_url, git_ref, subpath, token = claimed

    try:
        # Step 2 + 3 — walk GitHub.
        owner, repo = parse_github_url(repo_url)
        commit_sha, entries = await fetch_tree(
            client, owner, repo, git_ref, token
        )
        skill_md_entries = find_skill_md_paths(entries, subpath)
        if not skill_md_entries:
            # Empty repo / wrong subpath / etc. is a real operator
            # error — record it on the row so the Sources tab shows
            # something useful, but don't crash.
            await asyncio.to_thread(
                _finish_or_error,
                source_id,
                commit_sha=commit_sha,
                error_message=(
                    "no SKILL.md files found"
                    + (f" under subpath {subpath!r}" if subpath else "")
                ),
            )
            return

        discovered_list = []
        refused: list = []
        for skill_md_entry in skill_md_entries:
            try:
                discovered = await _build_discovered_skill(
                    client, owner, repo, token, entries, skill_md_entry
                )
            except SkillTooLargeError as e:
                # Past the per-skill limit: reported on the source with the
                # other refusals of this sync, the others go on.
                from app.api.v1.endpoints.admin.skill_sources import refused_line

                refused.append(refused_line(skill_md_entry.path, str(e)))
                _brutor_log.warning(
                    "skill_source_sync: refused one skill (too large)",
                    context={"source_id": source_id, "path": skill_md_entry.path, "reason": e.reason},
                )
                continue
            except GithubRateLimitedError:
                # Rate limit is a hard stop — don't try the next
                # skill, abort the whole source. The outer `except
                # GithubRateLimitedError` records the error on the
                # row and the next tick (after the limit resets)
                # picks up where we left off.
                raise
            except (FrontmatterError, GithubError) as e:
                # One bad skill shouldn't tank the whole sync. Log
                # and move on; the operator can fix the SKILL.md and
                # the next tick will pick it up.
                _brutor_log.warning(
                    "skill_source_sync: skipped one skill",
                    context={
                        "source_id": source_id,
                        "path": skill_md_entry.path,
                        "error": str(e)[:500],
                    },
                )
                continue
            discovered_list.append(discovered)

        # Step 4 + 5 — call the existing reconciler in a thread.
        await asyncio.to_thread(
            _ingest_and_finish,
            source_id,
            commit_sha,
            discovered_list,
            refused,
        )

    except GithubRateLimitedError as e:
        # Special-case rate-limit so it shows up clearly in the
        # Sources tab. Logging at WARN (not ERROR) because the
        # operator can fix this by setting BRUTOR_GITHUB_TOKEN —
        # paging on-call isn't appropriate.
        log.warning(
            "skill_source_sync: %s hit GitHub rate limit — "
            "set BRUTOR_GITHUB_TOKEN to raise the limit",
            source_id,
        )
        await asyncio.to_thread(
            _finish_or_error,
            source_id,
            commit_sha=None,
            error_message=str(e),
        )
    except GithubError as e:
        # Hard GitHub-level failure (404 repo, malformed JSON, etc.) —
        # record it on the row, no commit_sha to stamp because we
        # never got one.
        await asyncio.to_thread(
            _finish_or_error,
            source_id,
            commit_sha=None,
            error_message=str(e),
        )
    except Exception as e:  # noqa: BLE001
        # Anything else (bug, OOM, etc.) — same handling, but at
        # ERROR level so it shows up in alerting.
        log.exception(
            "skill_source_sync: unexpected error syncing %s", source_id
        )
        await asyncio.to_thread(
            _finish_or_error,
            source_id,
            commit_sha=None,
            error_message=f"unexpected: {type(e).__name__}: {str(e)[:500]}",
        )


def _claim_source(source_id: str) -> Optional[tuple]:
    """CAS the row to `syncing` and return (repo_url, git_ref,
    subpath, token) for the walker. Returns None if the row vanished
    or was already claimed (race against another scheduler — should
    not happen in single-replica mode but we handle it cheaply)."""
    from app.database.models.skill_source import (
        SYNC_STATUS_IDLE,
        SkillSource,
    )

    db = SessionLocal()
    try:
        row = (
            db.query(SkillSource).filter(SkillSource.id == source_id).first()
        )
        if row is None:
            return None
        if row.status != SYNC_STATUS_IDLE and row.status != "failed":
            # Either someone else has it OR is_due() lied to us.
            # Bail without touching the row.
            return None
        row.begin_sync()
        db.commit()
        # Resolve the token here so we don't have to re-open a
        # session in the async path. Returns None today (no secrets
        # manager integration); the token field on the row is a
        # `secret_ref` string, not the raw token.
        token = _resolve_github_token(row)
        return (row.repo_url, row.git_ref or "main", row.subpath, token)
    finally:
        db.close()


def _resolve_github_token(row) -> Optional[str]:
    """Resolve the GitHub token for this source.

    Priority order:
      1. `row.github_token_secret_ref` via Secrets Manager (TODO —
         stubbed; tracked separately).
      2. `BRUTOR_GITHUB_TOKEN` env var —
         global fallback that covers the common single-tenant case.

    The unauthenticated 60/h limit isn't sustainable for any
    repo with more than a couple of skills — Anthropic's skills
    repo alone needs 150+ blob fetches per sync. Operators with
    NO token configured will hit the cap on the first run; we
    surface that as a clear `GithubRateLimitedError` in the row's
    sync_error field so it shows up in the Sources tab.
    """
    return resolve_token_from_env()


async def _build_discovered_skill(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    token: Optional[str],
    entries: list,
    skill_md_entry,
):
    """Fetch one SKILL.md + its sibling files and assemble the
    DiscoveredSkill payload that `_ingest_discovered_skills`
    expects.

    Returns the Pydantic model instance directly so the caller can
    just hand the list to the ingestor.
    """
    # Lazy import to keep this module light at FastAPI startup.
    from app.api.v1.models.admin.skill_source import (
        DiscoveredSkill,
        DiscoveredSkillFile,
    )

    # Size guard — refuse comically large SKILL.md before paying for
    # the blob download.
    size_err = check_size_caps(skill_md_entry, "skill_md")
    if size_err:
        raise GithubError(size_err)
    # The whole skill against the per-skill limit, from the tree's sizes,
    # before any of it is downloaded.
    skill_dir = skill_dir_of(skill_md_entry.path)
    label = skill_dir.rsplit("/", 1)[-1] if skill_dir else skill_md_entry.path
    check_skill_total(label, tree_size_of_skill(entries, skill_md_entry))

    raw = await fetch_blob(client, owner, repo, skill_md_entry.sha, token)
    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise GithubError(
            f"SKILL.md at {skill_md_entry.path} is not valid UTF-8: {e}"
        ) from e

    fm, body = parse_skill_md(body_text)
    name = fm.get("name")
    description = fm.get("description")
    if not name or not isinstance(name, str):
        raise FrontmatterError(
            f"SKILL.md frontmatter is missing required `name` field"
        )
    if not description or not isinstance(description, str):
        raise FrontmatterError(
            f"SKILL.md frontmatter is missing required `description` field"
        )

    # Clamp at the spec's published caps so we don't hit the
    # varchar overflow we hit in the Rust implementation. The
    # frontmatter parser doesn't enforce these — the Pydantic
    # model would, but raising here gives a nicer error.
    if len(name) > 64:
        name = name[:64]
    if len(description) > 1024:
        description = description[:1021] + "..."

    # Sibling files. Skill dir is the parent of SKILL.md.
    siblings = collect_sibling_files(entries, skill_dir) if skill_dir else []

    files: list = []
    for s in siblings:
        # classify_sibling always buckets into script/reference/asset —
        # it never drops a file (the spec permits arbitrary sibling
        # dirs, which all bucket to asset). The stored filename keeps the
        # full root-relative path, so the bucket is only a UI grouping.
        kind = classify_sibling(s.path, skill_dir)
        size_err = check_size_caps(s, kind)
        if size_err:
            _brutor_log.warning(
                "skill_source_sync: skipped sibling file (too large)",
                context={"path": s.path, "error": size_err},
            )
            continue
        try:
            raw_blob = await fetch_blob(client, owner, repo, s.sha, token)
        except GithubRateLimitedError:
            # Don't keep hammering — every remaining call will fail
            # the same way until the limit resets. Propagate so
            # `_sync_one_source` can record the error on the row and
            # bail. We preserve any sibling blobs already fetched for
            # this skill in `files` but they'll be discarded along
            # with the skill itself (no partial discoveries).
            raise
        except GithubError as e:
            _brutor_log.warning(
                "skill_source_sync: skipped sibling file (fetch failed)",
                context={"path": s.path, "error": str(e)[:200]},
            )
            continue
        # decode_blob_for_storage returns the EFFECTIVE kind it
        # decided on (may differ from the classifier's guess when a
        # binary tarball / image is committed under scripts/ or
        # references/). The decoder auto-demotes to "asset" rather
        # than dropping the file. Authoring smell on the source side
        # but we preserve the file so the operator can reorganise.
        content, encoding, effective_kind = decode_blob_for_storage(
            raw_blob, kind
        )
        if effective_kind != kind:
            _brutor_log.info(
                "skill_source_sync: demoted non-text sibling to asset",
                context={
                    "path": s.path,
                    "original_kind": kind,
                    "effective_kind": effective_kind,
                },
            )
        files.append(
            DiscoveredSkillFile(
                path=s.path,
                kind=effective_kind,
                content=content,
                encoding=encoding,
                mime_type=None,
            )
        )

    # Again from what was downloaded (GitHub does not always state a size).
    from app.services import agent_skill_size

    check_skill_total(name, agent_skill_size.discovered_bytes(body, files))

    return DiscoveredSkill(
        skill_path=skill_md_entry.path,
        name=name,
        description=description,
        version=fm.get("version") if isinstance(fm.get("version"), str) else None,
        license=fm.get("license") if isinstance(fm.get("license"), str) else None,
        compatibility=fm.get("compatibility"),
        allowed_tools=fm.get("allowed_tools"),
        skill_metadata=fm.get("metadata") if isinstance(fm.get("metadata"), dict) else None,
        skill_md_body=body,
        files=files,
    )


def _ingest_and_finish(
    source_id: str,
    commit_sha: str,
    discovered_list: list,
    refused: Sequence[str] = (),
) -> None:
    """Call the existing `_ingest_discovered_skills` reconciler
    and then flip the row to `idle` with the discovered count."""
    from app.api.v1.endpoints.admin.skill_sources import (
        _ingest_discovered_skills,
    )
    from app.api.v1.models.admin.skill_source import (
        SkillSourceIngestRequest,
    )
    from app.database.models.skill_source import SkillSource

    db = SessionLocal()
    try:
        source = (
            db.query(SkillSource).filter(SkillSource.id == source_id).first()
        )
        if source is None:
            return
        payload = SkillSourceIngestRequest(
            commit_sha=commit_sha,
            discovered=discovered_list,
            removed_paths=[],
        )
        summary = _ingest_discovered_skills(
            db=db,
            source=source,
            payload=payload,
            logger=_brutor_log,
            refused=refused,
        )
        # `_ingest_discovered_skills` commits internally. Now flip
        # the source row to idle.
        source.finish_sync(
            commit_sha=commit_sha,
            discovered_count=len(discovered_list),
        )
        db.commit()
        log.info(
            "skill_source_sync: %s synced — created=%d updated=%d "
            "unchanged=%d skipped=%d",
            source_id,
            summary.created,
            summary.updated,
            summary.unchanged,
            summary.skipped,
        )
    finally:
        db.close()


def _finish_or_error(
    source_id: str,
    *,
    commit_sha: Optional[str],
    error_message: Optional[str],
) -> None:
    """Helper used on the failure paths: record the error if any,
    else mark idle with the resolved commit_sha. Always commits."""
    from app.database.models.skill_source import SkillSource

    db = SessionLocal()
    try:
        source = (
            db.query(SkillSource).filter(SkillSource.id == source_id).first()
        )
        if source is None:
            return
        if error_message:
            source.record_error(error_message)
        elif commit_sha:
            source.finish_sync(commit_sha=commit_sha, discovered_count=0)
        db.commit()
    finally:
        db.close()


def start_skill_source_sync_scheduler() -> asyncio.Task:
    """Spawn the scheduler as a background asyncio task. Call once at
    FastAPI lifespan startup. Returns the task so the lifespan
    handler can cancel it on shutdown."""
    log.info("Starting skill source sync scheduler")
    return asyncio.create_task(
        skill_source_sync_loop(),
        name="skill-source-sync-scheduler",
    )


def trigger_sync_now(source_id: str) -> asyncio.Task:
    """Fire-and-forget an immediate sync of one source on the
    running event loop. Used by the `POST /{source_id}/refresh`
    admin endpoint so the operator sees the row go to `syncing`
    within a second of clicking, instead of waiting up to a minute
    for the periodic scheduler to tick.

    Safe to call repeatedly — the underlying `_claim_source` does
    a status-check CAS so a refresh that races with the periodic
    scheduler quietly bails on the second one.

    Returns the task so callers in tests can `await` it; the
    endpoint just lets it run in the background and returns.
    """
    log.info(
        "skill_source_sync: immediate-sync requested for %s", source_id
    )

    async def _run_one_off() -> None:
        # Local httpx client — we don't share the tick loop's
        # client because the lifecycles are independent.
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "brutor-skill-source-sync/1.0"},
        ) as client:
            await _sync_one_source(client, source_id)

    return asyncio.create_task(
        _run_one_off(),
        name=f"skill-source-sync-refresh-{source_id}",
    )
