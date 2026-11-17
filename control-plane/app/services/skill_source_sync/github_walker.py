# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
GitHub tree walker for skill source sync.

Given a (owner, repo, git_ref) and an optional path prefix, fetches
the full git tree once via the GitHub REST API, classifies blobs
into (SKILL.md / scripts / references / assets), and returns the
SKILL.md set plus a per-skill_dir sibling index.

# Why httpx and not the github SDK

We need at most two REST endpoints (`/git/trees` and `/git/blobs`)
and we're already using httpx everywhere in the control plane.
PyGithub would pull in 600 KB of dependencies for the equivalent
of 30 lines of HTTP plumbing.

# Rate limit awareness

GitHub allows 5,000 req/hour authenticated, 60 unauthenticated. A
typical skills repo (Anthropic's `claude-skills`) has ~12 skills with
~3 sibling files each ≈ 36 blob fetches + 1 tree fetch per sync. At
the default 1h interval this is well below the limit even
unauthenticated. We only hit the auth path if `github_token` is set
on the source row.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx
from app.env_vars import BRUTOR_GITHUB_TOKEN


log = logging.getLogger("brutor-skill-source-sync")


# GitHub REST API base — pinned to api.github.com. GitHub Enterprise
# hosts have a different base URL pattern; supporting GHE is a
# follow-up (we'd need to parse the GHE host from repo_url and route
# accordingly).
_GITHUB_API_BASE = "https://api.github.com"

# Hard caps to keep a runaway repo from eating all our memory. A
# 5 MB SKILL.md is already absurd (the spec says 1 KB description,
# the body is usually a few paragraphs); 10 MB on a sibling covers
# data files but rejects accidental binary blobs.
_MAX_SKILL_MD_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_SIBLING_BYTES = 10 * 1024 * 1024  # 10 MB


@dataclass(frozen=True)
class TreeEntry:
    """One blob in the git tree, as returned by GitHub."""

    path: str
    sha: str
    size: Optional[int]  # GitHub returns null for some entry types


@dataclass(frozen=True)
class SiblingFile:
    """One sibling file under a skill directory.

    `kind` is one of: 'script' | 'reference' | 'asset'.
    `content` is the decoded body — text where possible, base64 for
    binary blobs (matched to the DiscoveredSkillFile encoding field).
    """

    path: str
    kind: str
    content: str
    encoding: str  # "utf-8" | "base64"
    mime_type: Optional[str] = None


class GithubError(RuntimeError):
    """Raised for GitHub REST API failures (4xx/5xx, timeouts, bad
    JSON). Caller is expected to record the message on the
    SkillSource row via `record_error()`."""


class SkillTooLargeError(GithubError):
    """A skill whose files are past the per-skill limit (`SKILL_MAX_BYTES`,
    30 MB): refused with the reason `too_large` and the words every other
    write path uses. Raised from the tree's sizes before anything of the
    skill is downloaded, and again from what was downloaded when GitHub did
    not state a size."""

    reason = "too_large"


def skill_dir_of(skill_md_path: str) -> str:
    """The directory of a SKILL.md ("" at the repository root)."""
    return skill_md_path.rsplit("/", 1)[0] if "/" in skill_md_path else ""


def check_skill_total(label: str, size: int) -> None:
    """Raise `SkillTooLargeError` when a skill of `size` bytes is past the
    per-skill limit."""
    from app.services import agent_skill_size

    if agent_skill_size.too_large(size):
        raise SkillTooLargeError(agent_skill_size.message(label, size))


def tree_size_of_skill(entries: List[TreeEntry], skill_md_entry: TreeEntry) -> int:
    """The bytes the tree says a skill holds: its SKILL.md and every sibling
    file the per-file caps let through (an entry without a size counts 0;
    the downloaded bytes are checked again)."""
    skill_dir = skill_dir_of(skill_md_entry.path)
    siblings = collect_sibling_files(entries, skill_dir) if skill_dir else []
    total = skill_md_entry.size or 0
    for sibling in siblings:
        if check_size_caps(sibling, classify_sibling(sibling.path, skill_dir)) is None:
            total += sibling.size or 0
    return total


class GithubRateLimitedError(GithubError):
    """Raised specifically when GitHub returns a rate-limit error
    (403 with X-RateLimit-Remaining: 0, or 429). Callers should
    abort the whole source — every remaining call will fail the
    same way until the limit resets, so we save the rate-limit
    budget for the next tick instead of wasting it on doomed
    retries."""


def parse_github_url(repo_url: str) -> Tuple[str, str]:
    """Strip trailing `/` or `.git`, extract (owner, repo) from a URL
    like `https://github.com/anthropic/skills`.

    Raises GithubError if the URL isn't a recognisable github.com
    HTTPS URL. We deliberately reject SSH form (`git@github.com:...`)
    because the scheduler uses HTTPS for REST anyway.
    """
    cleaned = repo_url.strip().rstrip("/").removesuffix(".git")
    m = re.match(
        r"^https?://github\.com/([^/]+)/([^/]+)$",
        cleaned,
    )
    if not m:
        raise GithubError(
            f"repo_url is not a recognisable github.com HTTPS URL: "
            f"{repo_url!r} — expected https://github.com/<owner>/<repo>"
        )
    return m.group(1), m.group(2)


async def fetch_tree(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    git_ref: str,
    token: Optional[str],
) -> Tuple[str, List[TreeEntry]]:
    """Fetch the recursive git tree for (owner, repo, git_ref).

    Returns (commit_sha, [TreeEntry,...]). The commit_sha is the
    resolved SHA of the ref — used so re-syncs can detect "nothing
    changed upstream" without fetching every blob.

    GitHub's `trees/{ref}?recursive=1` endpoint returns the entire
    tree in one call up to ~100k entries. A flag on the response
    (`truncated: true`) signals "we cut it off"; we fail loudly in
    that case rather than silently miss skills — operators with
    monorepos should add a `subpath` to scope the source down.
    """
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{git_ref}?recursive=1"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await client.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as e:
        raise GithubError(f"github tree fetch failed: {e}") from e
    _raise_for_rate_limit(resp, url)
    if resp.status_code != 200:
        raise GithubError(
            f"github: GET {url} returned {resp.status_code} "
            f"{resp.reason_phrase}: {resp.text[:500]}"
        )
    data = resp.json()
    if data.get("truncated"):
        raise GithubError(
            "github tree response truncated — repo is too large to walk "
            "in one call. Scope the source with `subpath` so we only "
            "see the skills/ directory."
        )
    commit_sha = data.get("sha")
    if not commit_sha or not isinstance(commit_sha, str):
        raise GithubError("github tree response missing 'sha' field")
    entries: List[TreeEntry] = []
    for raw in data.get("tree", []):
        if raw.get("type") != "blob":
            continue
        path = raw.get("path")
        sha = raw.get("sha")
        if not path or not sha:
            continue
        entries.append(
            TreeEntry(
                path=path,
                sha=sha,
                size=raw.get("size") if isinstance(raw.get("size"), int) else None,
            )
        )
    return commit_sha, entries


async def fetch_blob(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    sha: str,
    token: Optional[str],
) -> bytes:
    """Fetch a blob by SHA. GitHub returns it base64-encoded; we
    decode to raw bytes. Caller decides text vs base64 storage."""
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/git/blobs/{sha}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await client.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as e:
        raise GithubError(f"github blob fetch failed: {e}") from e
    _raise_for_rate_limit(resp, url)
    if resp.status_code != 200:
        raise GithubError(
            f"github: GET {url} returned {resp.status_code} "
            f"{resp.reason_phrase}: {resp.text[:500]}"
        )
    data = resp.json()
    encoded = data.get("content", "")
    encoding = data.get("encoding", "base64")
    if encoding != "base64":
        raise GithubError(
            f"github blob encoding unexpectedly {encoding!r} — only "
            "base64 is supported"
        )
    # Strip whitespace GitHub inserts every 60 chars for readability.
    return base64.b64decode(encoded.replace("\n", "").replace("\r", ""))


def find_skill_md_paths(
    entries: List[TreeEntry], subpath: Optional[str]
) -> List[TreeEntry]:
    """Return every blob whose path ends in `/SKILL.md` (or is
    `SKILL.md` at the repo root). If `subpath` is given, only paths
    under that prefix are considered.

    We intentionally ignore case (`Skill.md` would not match) — the
    spec is case-sensitive and we match it literally."""
    prefix = subpath.strip("/") + "/" if subpath else ""
    out: List[TreeEntry] = []
    for e in entries:
        if prefix and not e.path.startswith(prefix):
            continue
        if e.path == "SKILL.md" or e.path.endswith("/SKILL.md"):
            out.append(e)
    return out


def collect_sibling_files(
    entries: List[TreeEntry], skill_dir: str
) -> List[TreeEntry]:
    """Find every scripts/, references/, assets/ blob inside the same
    skill directory.

    The Anthropic spec layout is:
        skills/<name>/SKILL.md
        skills/<name>/scripts/foo.py
        skills/<name>/references/REFERENCE.md
        skills/<name>/assets/template.txt
        skills/<name>/...        # any additional files or directories

    The spec explicitly allows "any additional files or directories"
    at the skill root, and real skills use them: the Anthropic
    `skill-creator` skill references `agents/grader.md`,
    `evals/evals.json`, and `eval-viewer/generate_review.py`, and the
    pptx/docx/xlsx skills keep document templates in `templates/`.
    Dropping those breaks the skill (its SKILL.md references them) and
    breaks round-trip export, so we capture EVERY file under the skill
    directory (except SKILL.md). The classifier buckets them for the
    UI; the stored filename keeps the full path so export reconstructs
    the original layout faithfully.
    """
    base = f"{skill_dir}/"
    own_skill_md = f"{skill_dir}/SKILL.md"
    out: List[TreeEntry] = []
    for e in entries:
        if not e.path.startswith(base):
            continue
        # Skip the skill's own SKILL.md (already in skill_md_paths) and
        # any nested skill's manifest — a `**/SKILL.md` belongs to a
        # different skill and must not be vacuumed in as this one's
        # bundled file. (Flat layouts never hit the nested case.)
        if e.path == own_skill_md or e.path.endswith("/SKILL.md"):
            continue
        out.append(e)
    return out


def classify_sibling(path: str, skill_dir: str) -> str:
    """Return the bucket ('script' | 'reference' | 'asset') for a
    sibling path. Files under `scripts/` and `references/` map to those
    buckets; EVERYTHING else (assets/, templates/, agents/, evals/, or
    any other directory the spec permits) buckets to 'asset' so it's
    preserved rather than dropped.

    The bucket only drives UI categorisation — the stored filename
    keeps the full path relative to the skill root, so export
    reconstructs the original directory regardless of bucket.

    Always returns a bucket (never None); callers feed the result to
    `DiscoveredSkillFile.kind`."""
    if path.startswith(f"{skill_dir}/scripts/"):
        return "script"
    if path.startswith(f"{skill_dir}/references/"):
        return "reference"
    return "asset"


def decode_blob_for_storage(
    raw: bytes, kind: str
) -> Tuple[str, str, str]:
    """Return (content_str, encoding, effective_kind) suited to the
    AgentSkillScript / Resource / Asset row that will be stored.

    Scripts and references are *expected* to be UTF-8 text — that's
    what the runtime expects to execute / read. But real-world repos
    sometimes drop binary artifacts in those subdirs (bundled
    tarballs, prebuilt fonts, sample images committed under
    `scripts/`). Rather than silently dropping the file, we
    demote the kind to `asset` and base64-encode the bytes. The
    skill author can always reorganise later; we'd rather store
    the file in the wrong table than lose it.

    Returns a 3-tuple so the caller knows whether the demotion
    happened: when `effective_kind != kind`, the caller should
    classify the file as an asset in the discovered-skill payload.
    """
    if kind in ("script", "reference"):
        try:
            return raw.decode("utf-8"), "utf-8", kind
        except UnicodeDecodeError:
            # Demote to asset rather than skip. Authoring smell on the
            # source side (binary in scripts/ or references/), but the
            # file is clearly intentional so we preserve it.
            return (
                base64.b64encode(raw).decode("ascii"),
                "base64",
                "asset",
            )
    # asset
    try:
        return raw.decode("utf-8"), "utf-8", "asset"
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii"), "base64", "asset"


def _raise_for_rate_limit(resp: httpx.Response, url: str) -> None:
    """If the response indicates a GitHub rate-limit hit, raise
    GithubRateLimitedError with a clear message including the reset
    time. The scheduler aborts the whole source on this so we don't
    burn the remaining budget on doomed retries.

    GitHub signals rate-limit in three ways:
      * 403 with `X-RateLimit-Remaining: 0` → primary limit (60/h
        unauthenticated, 5000/h with a token)
      * 403 with body mentioning "rate limit" → some endpoints don't
        populate the header but do say it in the body
      * 429 → secondary / abuse-detection limit (rare for our usage
        pattern but worth handling)
    """
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After") or "unknown"
        raise GithubRateLimitedError(
            f"github: GET {url} returned 429 (secondary rate limit); "
            f"Retry-After={retry_after}s. Configure a GitHub token to "
            f"raise the limit from 60/h to 5000/h."
        )
    if resp.status_code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        body = resp.text or ""
        if (remaining == "0") or ("rate limit" in body.lower()):
            raise GithubRateLimitedError(
                f"github: rate limit exceeded "
                f"(X-RateLimit-Remaining={remaining}, reset={reset}). "
                f"Anthropic-skills walks ~150-200 blobs per sync; the "
                f"unauthenticated 60/h budget can't sustain that. Set "
                f"BRUTOR_GITHUB_TOKEN (a fine-grained PAT with "
                f"contents:read on this repo) to raise the limit to "
                f"5000/h. URL: {url}"
            )


def resolve_token_from_env() -> Optional[str]:
    """Read a GitHub token from environment as a global fallback.

    Source rows can carry a per-source `github_token_secret_ref` for
    Secrets Manager (not yet wired); until that lands, this env-var
    fallback lets operators get the rate-limit fix without DB
    schema changes. The token is `BRUTOR_GITHUB_TOKEN`, and no other
    name: a `GITHUB_TOKEN` in the environment belongs to whatever put it
    there (a CI runner, a developer's shell) and is not read.
    """
    return BRUTOR_GITHUB_TOKEN.exact()


def check_size_caps(entry: TreeEntry, kind: str) -> Optional[str]:
    """Return an error message if the blob exceeds our hard size cap,
    else None. Caller logs the message and skips the file."""
    if entry.size is None:
        return None  # GitHub didn't tell us; let the download decide
    if kind == "skill_md":
        if entry.size > _MAX_SKILL_MD_BYTES:
            return (
                f"SKILL.md too large: {entry.size} bytes > "
                f"{_MAX_SKILL_MD_BYTES} byte cap"
            )
    else:
        if entry.size > _MAX_SIBLING_BYTES:
            return (
                f"sibling file too large: {entry.size} bytes > "
                f"{_MAX_SIBLING_BYTES} byte cap"
            )
    return None
