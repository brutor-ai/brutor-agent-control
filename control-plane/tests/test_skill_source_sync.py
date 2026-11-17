# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the Python skill source sync replacement.

Covers the pure-function pieces that don't need a live DB:
  * SKILL.md frontmatter parsing (happy path + every error path)
  * GitHub URL parsing
  * Tree walker classifiers (find_skill_md_paths, collect_sibling_files,
    classify_sibling)
  * Blob decoding (text vs binary)
  * Size cap enforcement

The HTTP + ORM integration paths are covered by the existing
`test_skill_sources.py` (the ingest endpoint) plus future end-to-end
tests; here we focus on the new modules we just added so a refactor
can't silently break them.
"""

import pytest

from app.services.skill_source_sync.frontmatter import (
    FrontmatterError,
    parse_skill_md,
)
from app.services.skill_source_sync.github_walker import (
    GithubError,
    GithubRateLimitedError,
    TreeEntry,
    _raise_for_rate_limit,
    check_size_caps,
    classify_sibling,
    collect_sibling_files,
    decode_blob_for_storage,
    find_skill_md_paths,
    parse_github_url,
    resolve_token_from_env,
)


# ============================================================================
# Frontmatter parser
# ============================================================================


class TestParseSkillMd:
    def test_happy_path(self):
        content = (
            "---\n"
            "name: my-skill\n"
            "description: Does the thing\n"
            "version: 1.2.3\n"
            "license: MIT\n"
            "---\n"
            "# My Skill\n\nBody text."
        )
        fm, body = parse_skill_md(content)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "Does the thing"
        assert fm["version"] == "1.2.3"
        assert fm["license"] == "MIT"
        assert body == "# My Skill\n\nBody text."

    def test_allowed_tools_kebab_case_normalised(self):
        # The spec uses `allowed-tools`; we expose `allowed_tools`.
        content = (
            "---\n"
            "name: foo\n"
            "description: x\n"
            "allowed-tools: brutor:list, brutor:read\n"
            "---\n"
            "body"
        )
        fm, _ = parse_skill_md(content)
        assert fm["allowed_tools"] == ["brutor:list", "brutor:read"]

    def test_allowed_tools_list_form(self):
        content = (
            "---\n"
            "name: foo\n"
            "description: x\n"
            "allowed-tools:\n"
            "  - brutor:list\n"
            "  - brutor:read\n"
            "---\n"
            "body"
        )
        fm, _ = parse_skill_md(content)
        assert fm["allowed_tools"] == ["brutor:list", "brutor:read"]

    def test_compatibility_csv(self):
        content = (
            "---\n"
            "name: foo\n"
            "description: x\n"
            "compatibility: claude-3-opus, claude-3-sonnet\n"
            "---\n"
            "body"
        )
        fm, _ = parse_skill_md(content)
        assert fm["compatibility"] == ["claude-3-opus", "claude-3-sonnet"]

    def test_empty_string_collapses_to_none(self):
        content = (
            "---\n"
            "name: foo\n"
            "description: x\n"
            "compatibility: ''\n"
            "---\n"
            "body"
        )
        fm, _ = parse_skill_md(content)
        assert fm["compatibility"] is None

    def test_missing_frontmatter_raises(self):
        with pytest.raises(FrontmatterError, match="missing the YAML frontmatter"):
            parse_skill_md("# Just a markdown file\n\nNo frontmatter here.")

    def test_empty_file_raises(self):
        with pytest.raises(FrontmatterError, match="empty"):
            parse_skill_md("")

    def test_malformed_yaml_recovers_what_it_can(self):
        # Strict YAML rejects the bad indentation, but the lenient
        # fallback still salvages the well-formed `name` line. Per the
        # client-implementation guide we prefer a partially-loaded skill
        # over dropping it entirely.
        content = "---\nname: foo\n  bad: indent: here\n---\nbody"
        fm, _ = parse_skill_md(content)
        assert fm["name"] == "foo"

    def test_frontmatter_must_be_mapping(self):
        # A YAML scalar at the top level isn't a dict.
        content = "---\njust-a-string\n---\nbody"
        with pytest.raises(FrontmatterError, match="must be a mapping"):
            parse_skill_md(content)

    def test_unquoted_colon_in_description_recovers(self):
        # The canonical cross-client slip the spec tells us to tolerate:
        # an unquoted value that itself contains a colon. Strict YAML
        # reads `Use when: drafting` as a nested map and rejects it; the
        # lenient fallback keeps the whole string.
        content = (
            "---\n"
            "name: docx\n"
            "description: Use when: drafting Word documents\n"
            "version: 1.0.0\n"
            "---\n"
            "# Body"
        )
        fm, body = parse_skill_md(content)
        assert fm["name"] == "docx"
        assert fm["description"] == "Use when: drafting Word documents"
        assert fm["version"] == "1.0.0"
        assert body == "# Body"

    def test_lenient_fallback_recovers_nested_metadata(self):
        # A nested metadata: block survives even when a sibling line
        # forces the strict parser to bail.
        content = (
            "---\n"
            "name: docx\n"
            "description: Bad: value here\n"
            "metadata:\n"
            "  author: Anthropic\n"
            "  category: office\n"
            "---\n"
            "body"
        )
        fm, _ = parse_skill_md(content)
        assert fm["description"] == "Bad: value here"
        assert fm["metadata"] == {"author": "Anthropic", "category": "office"}

    def test_lenient_fallback_recovers_list(self):
        content = (
            "---\n"
            "name: foo\n"
            "description: Use when: doing things\n"
            "allowed-tools:\n"
            "  - brutor:list\n"
            "  - brutor:read\n"
            "---\n"
            "body"
        )
        fm, _ = parse_skill_md(content)
        assert fm["allowed_tools"] == ["brutor:list", "brutor:read"]

    def test_truly_unrecoverable_yaml_still_raises(self):
        # If the fallback can't extract a single key either, surface the
        # original malformed-YAML error rather than silently passing an
        # empty frontmatter.
        content = "---\n\t\t: : :\n  @#$%\n---\nbody"
        with pytest.raises(FrontmatterError, match="malformed"):
            parse_skill_md(content)


# ============================================================================
# GitHub URL parsing
# ============================================================================


class TestParseGithubUrl:
    @pytest.mark.parametrize(
        "url,owner,repo",
        [
            ("https://github.com/anthropic/skills", "anthropic", "skills"),
            ("https://github.com/anthropic/skills/", "anthropic", "skills"),
            ("https://github.com/anthropic/skills.git", "anthropic", "skills"),
            ("https://github.com/anthropic/skills.git/", "anthropic", "skills"),
            # Names with dots / hyphens / underscores
            ("https://github.com/my-org/some_repo.v2", "my-org", "some_repo.v2"),
        ],
    )
    def test_valid(self, url, owner, repo):
        assert parse_github_url(url) == (owner, repo)

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:anthropic/skills.git",  # SSH form
            "https://gitlab.com/foo/bar",  # not github
            "https://example.com/foo/bar",
            "not-a-url",
            "",
        ],
    )
    def test_invalid_raises(self, url):
        with pytest.raises(GithubError, match="not a recognisable github.com"):
            parse_github_url(url)


# ============================================================================
# Tree walker classifiers
# ============================================================================


def _entry(path: str, size: int = 100) -> TreeEntry:
    return TreeEntry(path=path, sha=f"sha-of-{path}", size=size)


class TestFindSkillMdPaths:
    def test_finds_nested(self):
        entries = [
            _entry("skills/foo/SKILL.md"),
            _entry("skills/bar/SKILL.md"),
            _entry("skills/foo/scripts/run.py"),
            _entry("README.md"),
        ]
        found = find_skill_md_paths(entries, subpath=None)
        assert sorted(e.path for e in found) == [
            "skills/bar/SKILL.md",
            "skills/foo/SKILL.md",
        ]

    def test_subpath_filters(self):
        entries = [
            _entry("skills/foo/SKILL.md"),
            _entry("other/baz/SKILL.md"),
        ]
        found = find_skill_md_paths(entries, subpath="skills")
        assert [e.path for e in found] == ["skills/foo/SKILL.md"]

    def test_root_skill_md(self):
        entries = [_entry("SKILL.md")]
        assert [e.path for e in find_skill_md_paths(entries, None)] == ["SKILL.md"]

    def test_case_sensitive(self):
        # Spec is case-sensitive — we match it literally.
        entries = [_entry("skills/foo/Skill.md"), _entry("skills/bar/SKILL.MD")]
        assert find_skill_md_paths(entries, None) == []

    def test_ignores_skill_md_substring(self):
        # `MY_SKILL.md` is not `SKILL.md` even though one contains the other.
        entries = [_entry("skills/foo/MY_SKILL.md")]
        assert find_skill_md_paths(entries, None) == []


class TestCollectSiblingFiles:
    def test_captures_all_files_under_skill_dir(self):
        # The spec allows "any additional files or directories" beside
        # SKILL.md, so the walker must capture EVERYTHING under the skill
        # dir — including non-canonical dirs (agents/, evals/) and bare
        # root-level files — not just scripts/references/assets.
        skill_dir = "skills/foo"
        entries = [
            _entry("skills/foo/SKILL.md"),
            _entry("skills/foo/scripts/run.py"),
            _entry("skills/foo/scripts/helper/utils.py"),  # nested
            _entry("skills/foo/references/api.md"),
            _entry("skills/foo/assets/template.txt"),
            _entry("skills/foo/agents/grader.md"),  # non-canonical dir
            _entry("skills/foo/random-file.txt"),  # bare root file
            _entry("skills/bar/scripts/other.py"),  # ignored — different skill
        ]
        sibs = collect_sibling_files(entries, skill_dir)
        paths = sorted(s.path for s in sibs)
        assert paths == [
            "skills/foo/agents/grader.md",
            "skills/foo/assets/template.txt",
            "skills/foo/random-file.txt",
            "skills/foo/references/api.md",
            "skills/foo/scripts/helper/utils.py",
            "skills/foo/scripts/run.py",
        ]

    def test_excludes_the_skill_md(self):
        entries = [_entry("skills/foo/SKILL.md")]
        assert collect_sibling_files(entries, "skills/foo") == []

    def test_excludes_nested_skill_md(self):
        # A nested SKILL.md belongs to a different (sub-)skill and must
        # not be vacuumed into this skill's sibling list.
        entries = [
            _entry("skills/foo/SKILL.md"),
            _entry("skills/foo/scripts/run.py"),
            _entry("skills/foo/sub/SKILL.md"),
        ]
        sibs = collect_sibling_files(entries, "skills/foo")
        assert sorted(s.path for s in sibs) == ["skills/foo/scripts/run.py"]


class TestClassifySibling:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("skills/foo/scripts/run.py", "script"),
            ("skills/foo/scripts/nested/helper.py", "script"),
            ("skills/foo/references/api.md", "reference"),
            ("skills/foo/assets/template.txt", "asset"),
            # Anything not under scripts/ or references/ buckets as an
            # asset (the bucket only drives UI grouping — the stored
            # root-relative filename preserves the true layout). The spec
            # permits arbitrary sibling dirs, so they must never be
            # dropped.
            ("skills/foo/templates/letter.docx", "asset"),
            ("skills/foo/templates/nested/sheet.xlsx", "asset"),
            ("skills/foo/agents/grader.md", "asset"),
            ("skills/foo/other-folder/file.md", "asset"),
            ("skills/foo/README.md", "asset"),
        ],
    )
    def test_classifies(self, path, expected):
        assert classify_sibling(path, "skills/foo") == expected

    def test_never_returns_none(self):
        # Regression: classify_sibling used to return None for
        # unrecognised dirs, which made the walker silently drop those
        # files. It must always bucket into one of the three kinds.
        for p in (
            "skills/foo/scripts/x.py",
            "skills/foo/references/y.md",
            "skills/foo/weird/z.bin",
            "skills/foo/top.txt",
        ):
            assert classify_sibling(p, "skills/foo") in ("script", "reference", "asset")


class TestCollectSiblingFilesTemplates:
    def test_templates_dir_collected(self):
        # The bug report: a top-level templates/ directory was being
        # dropped entirely (pptx/docx/xlsx skills keep doc templates
        # there).
        entries = [
            _entry("skills/pptx/SKILL.md"),
            _entry("skills/pptx/templates/default.pptx"),
            _entry("skills/pptx/templates/wide/widescreen.pptx"),
            _entry("skills/pptx/scripts/build.py"),
        ]
        sibs = collect_sibling_files(entries, "skills/pptx")
        paths = sorted(s.path for s in sibs)
        assert paths == [
            "skills/pptx/scripts/build.py",
            "skills/pptx/templates/default.pptx",
            "skills/pptx/templates/wide/widescreen.pptx",
        ]


# ============================================================================
# Blob decoding
# ============================================================================


class TestDecodeBlobForStorage:
    def test_script_text(self):
        raw = b"print('hello')\n"
        content, encoding, kind = decode_blob_for_storage(raw, "script")
        assert content == "print('hello')\n"
        assert encoding == "utf-8"
        assert kind == "script"

    def test_script_binary_demotes_to_asset(self):
        # Real-world repos drop binaries under scripts/ (e.g. a
        # bundled tarball). We auto-demote to asset rather than
        # silently dropping the file.
        raw = b"\x1f\x8b\x08\x00binary tarball contents"
        content, encoding, kind = decode_blob_for_storage(raw, "script")
        assert kind == "asset"
        assert encoding == "base64"
        import base64
        assert base64.b64decode(content) == raw

    def test_reference_text(self):
        raw = b"# REFERENCE\n"
        content, encoding, kind = decode_blob_for_storage(raw, "reference")
        assert content == "# REFERENCE\n"
        assert encoding == "utf-8"
        assert kind == "reference"

    def test_reference_binary_demotes_to_asset(self):
        raw = bytes([0xFF, 0xFE, 0x00, 0x42])
        content, encoding, kind = decode_blob_for_storage(raw, "reference")
        assert kind == "asset"
        assert encoding == "base64"

    def test_asset_text(self):
        raw = b"plain text asset"
        content, encoding, kind = decode_blob_for_storage(raw, "asset")
        assert encoding == "utf-8"
        assert content == "plain text asset"
        assert kind == "asset"

    def test_asset_binary_falls_back_to_base64(self):
        raw = bytes([0xFF, 0xFE, 0x00, 0x42])
        content, encoding, kind = decode_blob_for_storage(raw, "asset")
        assert encoding == "base64"
        assert kind == "asset"
        import base64
        assert base64.b64decode(content) == raw


# ============================================================================
# Size cap enforcement
# ============================================================================


class TestCheckSizeCaps:
    def test_skill_md_under_cap(self):
        assert check_size_caps(_entry("skills/foo/SKILL.md", size=1000), "skill_md") is None

    def test_skill_md_over_cap(self):
        # 5 MB cap; try 5 MB + 1
        big = 5 * 1024 * 1024 + 1
        msg = check_size_caps(_entry("x/SKILL.md", size=big), "skill_md")
        assert msg is not None
        assert "SKILL.md too large" in msg

    def test_sibling_under_cap(self):
        assert check_size_caps(_entry("x/assets/img.png", size=1024 * 1024), "asset") is None

    def test_sibling_over_cap(self):
        big = 10 * 1024 * 1024 + 1
        msg = check_size_caps(_entry("x/assets/img.png", size=big), "asset")
        assert msg is not None
        assert "sibling file too large" in msg

    def test_unknown_size_passes(self):
        # GitHub returns null size for some entries; we let the
        # download decide rather than guessing.
        assert check_size_caps(TreeEntry(path="x", sha="y", size=None), "skill_md") is None


# ============================================================================
# Rate-limit detection
# ============================================================================


class _FakeResponse:
    """Minimal stub of httpx.Response — just the fields the
    _raise_for_rate_limit() helper reads."""

    def __init__(self, *, status_code: int, headers=None, text: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class TestRaiseForRateLimit:
    def test_403_with_zero_remaining_raises(self):
        resp = _FakeResponse(
            status_code=403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1234567890"},
            text="API rate limit exceeded",
        )
        with pytest.raises(GithubRateLimitedError, match="rate limit exceeded"):
            _raise_for_rate_limit(resp, "https://api.github.com/test")

    def test_403_body_says_rate_limit(self):
        # Some endpoints don't populate X-RateLimit-Remaining but
        # do mention rate limit in the body.
        resp = _FakeResponse(
            status_code=403,
            headers={},
            text='{"message":"API rate limit exceeded for 1.2.3.4"}',
        )
        with pytest.raises(GithubRateLimitedError, match="rate limit"):
            _raise_for_rate_limit(resp, "https://api.github.com/test")

    def test_429_raises(self):
        resp = _FakeResponse(
            status_code=429,
            headers={"Retry-After": "120"},
            text="Too Many Requests",
        )
        with pytest.raises(GithubRateLimitedError, match="429"):
            _raise_for_rate_limit(resp, "https://api.github.com/test")

    def test_403_unrelated_does_not_raise(self):
        # 403 with X-RateLimit-Remaining > 0 is e.g. a permissions
        # error, not a rate limit — caller handles it via the normal
        # GithubError path.
        resp = _FakeResponse(
            status_code=403,
            headers={"X-RateLimit-Remaining": "4992"},
            text='{"message":"Resource not accessible"}',
        )
        # Should NOT raise.
        _raise_for_rate_limit(resp, "https://api.github.com/test")

    def test_200_does_not_raise(self):
        resp = _FakeResponse(status_code=200)
        _raise_for_rate_limit(resp, "https://api.github.com/test")

    def test_rate_limit_error_is_a_github_error(self):
        # Subclass relationship matters for callers that catch the
        # superclass to "handle anything from GitHub".
        assert issubclass(GithubRateLimitedError, GithubError)


# ============================================================================
# Environment token resolver
# ============================================================================


class TestResolveTokenFromEnv:
    def test_brutor_var_wins(self, monkeypatch):
        monkeypatch.setenv("BRUTOR_GITHUB_TOKEN", "brutor-tok")
        monkeypatch.setenv("GITHUB_TOKEN", "somebody-elses-tok")
        assert resolve_token_from_env() == "brutor-tok"

    def test_no_other_name_is_read(self, monkeypatch):
        """`GITHUB_TOKEN` is whatever set it: a CI runner's, a developer's
        shell's. The control plane's token has one name."""
        monkeypatch.delenv("BRUTOR_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "somebody-elses-tok")
        assert resolve_token_from_env() is None

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("BRUTOR_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert resolve_token_from_env() is None


# ============================================================================
# Filename collision protection (relative-filename helper)
# ============================================================================


class TestRelativeFilename:
    """The ingestor's _relative_filename() turns a full git path into
    the stored `filename` = the path relative to the skill ROOT, KEEPING
    the bucket directory (scripts/, references/, agents/, ...). This
    matches the ZIP-importer convention (Design 2) so the export
    endpoint reconstructs the original layout faithfully, and it keeps
    same-basename siblings (e.g. multiple __init__.py) distinct on the
    UNIQUE(skill_id, filename) constraint."""

    def test_keeps_bucket_and_nesting(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        assert _relative_filename(
            "skills/docx/scripts/office/validators/pptx.py",
            "skills/docx",
        ) == "scripts/office/validators/pptx.py"

    def test_top_level_in_scripts_keeps_bucket(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        assert _relative_filename(
            "skills/docx/scripts/foo.py",
            "skills/docx",
        ) == "scripts/foo.py"

    def test_two_init_py_files_get_distinct_names(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        a = _relative_filename(
            "skills/docx/scripts/office/__init__.py",
            "skills/docx",
        )
        b = _relative_filename(
            "skills/docx/scripts/office/validators/__init__.py",
            "skills/docx",
        )
        # The exact bug from the production log — they MUST differ.
        assert a == "scripts/office/__init__.py"
        assert b == "scripts/office/validators/__init__.py"
        assert a != b

    def test_non_canonical_dir_preserved(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        # agents/, evals/, eval-viewer/ etc. are permitted by the spec
        # and must survive verbatim in the stored filename.
        assert _relative_filename(
            "skills/skill-creator/agents/grader.md", "skills/skill-creator"
        ) == "agents/grader.md"

    def test_root_level_file_preserved(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        assert _relative_filename(
            "skills/foo/LICENSE.txt", "skills/foo"
        ) == "LICENSE.txt"

    def test_templates_dir_preserved(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        assert _relative_filename(
            "skills/pptx/templates/wide/widescreen.pptx", "skills/pptx"
        ) == "templates/wide/widescreen.pptx"

    def test_no_skill_dir_falls_back_to_basename(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        # Defensive: with no skill_dir prefix we can't compute a
        # root-relative path, so fall back to the bare basename.
        assert _relative_filename("scripts/foo.py", "") == "foo.py"

    def test_path_not_under_skill_dir_falls_back_to_basename(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        assert _relative_filename(
            "weird/path/foo.py", "skills/x"
        ) == "foo.py"

    def test_cap_at_255_chars(self):
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        deep = "skills/x/scripts/" + "/".join(["a" * 30] * 20) + "/foo.py"
        result = _relative_filename(deep, "skills/x")
        assert len(result) <= 255


# ============================================================================
# Export path reconstruction (round-trip fidelity)
# ============================================================================


class TestSafeRelativePath:
    def test_preserves_nesting(self):
        from app.api.v1.endpoints.admin.agent_skills_export import safe_relative_path
        assert safe_relative_path("scripts/office/foo.py") == "scripts/office/foo.py"

    def test_strips_traversal(self):
        from app.api.v1.endpoints.admin.agent_skills_export import safe_relative_path
        assert safe_relative_path("../etc/passwd") == "etc/passwd"
        assert safe_relative_path("a/../../b/c") == "a/b/c"

    def test_strips_leading_slash(self):
        from app.api.v1.endpoints.admin.agent_skills_export import safe_relative_path
        assert safe_relative_path("/abs/path/x.py") == "abs/path/x.py"

    def test_normalises_backslashes(self):
        from app.api.v1.endpoints.admin.agent_skills_export import safe_relative_path
        assert safe_relative_path("scripts\\win\\x.py") == "scripts/win/x.py"

    def test_munges_unsafe_segment_chars(self):
        from app.api.v1.endpoints.admin.agent_skills_export import safe_relative_path
        assert safe_relative_path("scripts/we ird$.py") == "scripts/we_ird_.py"

    def test_empty_collapses_to_placeholder(self):
        from app.api.v1.endpoints.admin.agent_skills_export import safe_relative_path
        assert safe_relative_path("") == "file"
        assert safe_relative_path("///") == "file"


class TestExportPathFor:
    def test_root_relative_filename_kept_verbatim(self):
        from app.api.v1.endpoints.admin.agent_skills_export import export_path_for
        # Source-sync / ZIP-import convention: filename already carries
        # its bucket dir, so the bucket arg is ignored.
        assert export_path_for("my-skill", "scripts", "scripts/office/foo.py") == (
            "my-skill/scripts/office/foo.py"
        )
        assert export_path_for("my-skill", "assets", "agents/grader.md") == (
            "my-skill/agents/grader.md"
        )

    def test_bare_basename_gets_bucket_prefix(self):
        from app.api.v1.endpoints.admin.agent_skills_export import export_path_for
        # Manual create/edit convention: bare filename → prefix the bucket.
        assert export_path_for("my-skill", "scripts", "foo.py") == (
            "my-skill/scripts/foo.py"
        )
        assert export_path_for("my-skill", "references", "api.md") == (
            "my-skill/references/api.md"
        )

    def test_traversal_blocked_in_export(self):
        from app.api.v1.endpoints.admin.agent_skills_export import export_path_for
        out = export_path_for("my-skill", "scripts", "../../etc/passwd")
        assert ".." not in out
        assert out.startswith("my-skill/")

    def test_round_trip_with_relative_filename(self):
        # The end-to-end invariant: a path the source-sync ingestor
        # stored via _relative_filename() reconstructs to the SAME
        # in-skill layout on export (minus the <slug>/ prefix), as long
        # as the file lives under a directory (the common case).
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        from app.api.v1.endpoints.admin.agent_skills_export import export_path_for

        for git_path, bucket in [
            ("skills/docx/scripts/office/validators/pptx.py", "scripts"),
            ("skills/docx/references/api.md", "references"),
            ("skills/docx/agents/grader.md", "assets"),
            ("skills/docx/templates/wide/widescreen.pptx", "assets"),
        ]:
            stored = _relative_filename(git_path, "skills/docx")
            export_path = export_path_for("docx", bucket, stored)
            # Export path is the original git path with the skill_dir
            # prefix swapped for the slug.
            assert export_path == "docx/" + git_path[len("skills/docx/"):]

    def test_root_level_source_file_buckets_on_export(self):
        # Known, documented limitation: a skill-root file (e.g. a bare
        # LICENSE.txt) stores as a bare basename, which is
        # indistinguishable from a manually-added file. On export it
        # therefore lands under its classified bucket rather than at the
        # skill root. The file is preserved — only its directory shifts.
        from app.api.v1.endpoints.admin.skill_sources import _relative_filename
        from app.api.v1.endpoints.admin.agent_skills_export import export_path_for

        stored = _relative_filename("skills/docx/LICENSE.txt", "skills/docx")
        assert stored == "LICENSE.txt"
        assert export_path_for("docx", "assets", stored) == "docx/assets/LICENSE.txt"
