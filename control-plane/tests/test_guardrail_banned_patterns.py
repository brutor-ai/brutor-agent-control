# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Banned patterns are what the gateway can compile.

The gateway compiles a banned pattern with the Rust crate `regex-lite`
(`src/services/guardrails/banned.rs`), and one it cannot compile is a check
that cannot be built: the policy's `fail_open` then decides every call.
The control plane validated with Python's `re`, which accepts
look-around, backreferences, atomic groups, possessive quantifiers,
conditionals, `\\Z`, `\\N{..}` and octal escapes, none of which `regex-lite`
has. `app/services/guardrails/regex_lite.py` is a port of the crate's
parser; a pattern is saved when both it and Python's `re` (which the control
plane's own guardrail test runs patterns with) accept it.

The entries of a per-surface list, which were not looked at, have the shape
the gateway reads (`orchestrator.rs`: `parse_banned_words`,
`parse_banned_patterns`): a string, or an object with `word` / `pattern`.

`CRATE_OK` and `CRATE_ERR` are the patterns of the crate's own parser tests
(`regex-lite` 0.1.9, `src/hir/parse.rs`: every `p("..")` and `perr("..")`),
copied; the port was also run against the compiled crate on 400,000
generated patterns (none accepted that the crate refuses).
"""

from __future__ import annotations

import os
import re

import pytest
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.models.admin.guardrail import GuardrailBuiltinConfig
from app.services.guardrails import regex_lite
from tests.core_repo import core_revision, core_source

CRATE_OK = ['$',
 '(?:ab)?',
 '(?:a|b)',
 '(?<a.1>z)',
 '(?<a>z)',
 '(?<a[1]>z)',
 '(?<a_1>z)',
 '(?<a¾>z)',
 '(?<foo>a)',
 '(?<foo>ab)',
 '(?<foo>a|b)',
 '(?<名字>z)',
 '(?P<a.1>z)',
 '(?P<a>z)',
 '(?P<a[1]>z)',
 '(?P<a_1>z)',
 '(?P<a¾>z)',
 '(?P<foo>a)',
 '(?P<foo>ab)',
 '(?P<名字>z)',
 '(?R:.)',
 '(?i)[A]',
 '(?i)[a]',
 '(?i)[k]',
 '(?i)[s]',
 '(?i)[β]',
 '(?i)a',
 '(?i:a)',
 '(?m:$)',
 '(?m:^)',
 '(?mR:$)',
 '(?mR:^)',
 '(?s:.)',
 '(?sR:.)',
 '(?x)(   ?:  a )',
 '(?x)(  a )',
 '(?x)( ?P<foo> a )',
 '(?x)[\n            a\n            - ]\n        ',
 '(?x)[\n            a # wat\n            - ]\n        ',
 '(?x)[   a]',
 '(?x)[ - a]',
 '(?x)[ / - ]',
 '(?x)[ ] a]',
 '(?x)[ ^  a]',
 '(?x)[ a - ]',
 '(?x)\\ ',
 '(?x)\\x { 53 }',
 '(?x)a b',
 '(?x)a b(?-x)a b',
 '(?x)a{5,9} ?',
 '(a)',
 '(ab)',
 '(ab)?',
 '(ax|(by|(cz)))',
 '(a|)',
 '(a|ab|c|bcd){4,10}(d*)',
 '(a|b)',
 '(|)',
 '(|a)',
 '.',
 '[---a]',
 '[--a]',
 '[-]a]',
 '[-a]',
 '[[:alnum:]]',
 '[\\[]',
 '[\\s\\S]',
 '[\\w]',
 '[]\\[]',
 '[]]',
 '[]a]',
 '[^-a]',
 '[^\\s\\S]',
 '[^^]',
 '[a-]',
 '[a-cx-z]',
 '[a\\-z]',
 '[a\\]]',
 '[a\\wz]',
 '[a]',
 '[ab]',
 '[☃-⛄]',
 '\\$',
 '\\(',
 '\\)',
 '\\*',
 '\\+',
 '\\?',
 '\\A',
 '\\B',
 '\\D',
 '\\S',
 '\\U0001F4A9',
 '\\U{1F4A9}',
 '\\W',
 '\\[',
 '\\]',
 '\\^',
 '\\a',
 '\\b',
 '\\d',
 '\\f',
 '\\n',
 '\\r',
 '\\s',
 '\\t',
 '\\u2603',
 '\\u{1F4A9}',
 '\\v',
 '\\w',
 '\\x41',
 '\\x{1F4A9}',
 '\\z',
 '\\|',
 '^',
 'a',
 'a (?x:a )a ',
 'a((?i)a)a',
 'a(?:(?i)a)a',
 'a(?i)',
 'a(?i)a(?-i)a',
 'a(?i)b',
 'a(?i:b)',
 'a*',
 'a*?',
 'a+',
 'a+?',
 'a?',
 'a??',
 'a?b',
 'ab',
 'ab?',
 'ab{5}c',
 'ax|by|cz',
 'a{ 5 , 9 }',
 'a{ 5 }',
 'a{5,9}',
 'a{5,}',
 'a{5}',
 'a{5}?',
 'a|',
 'a|b',
 'a|b|c',
 '|',
 '|a',
 '|a?',
 '||',
 '💩']

CRATE_ERR = ['(',
 '(((((((((((((((((((((((((((((((((((((((((((((((((((a)))))))))))))))))))))))))))))))))))))))))))))))))))',
 '(+)',
 '(?',
 '(?!a)',
 '(?)',
 '(?<',
 '(?<!a)',
 '(?<1abc>z)',
 '(?<=a)',
 '(?<>z)',
 '(?<a☃>z)',
 '(?<foo',
 '(?<¾>z)',
 '(?<¾a>z)',
 '(?<☃>z)',
 '(?=a)',
 '(?P<',
 '(?P<1abc>z)',
 '(?P<>z)',
 '(?P<a>y)(?P<a>z)',
 '(?P<foo',
 '(?i)?',
 '(?i){5}',
 '(?is',
 '(?is-:foo)',
 '(?is-i)',
 '(?isi)',
 '(?s-i-R)',
 '(?x)[         ',
 '(?x)[ / -',
 '(?x)[ / - ',
 '(?x)[-#]',
 '(?x)[a ',
 '(?x)[a- ',
 '(?x)\\b{ ',
 '(?z:foo)',
 '({5})',
 ')',
 '*',
 '+',
 '?',
 '[',
 '[[:alnum]]',
 '[\\b]',
 '[\\w-a]',
 '[]',
 '[^',
 '[a',
 '[a&&b]',
 '[a-',
 '[a--b]',
 '[a-\\w]',
 '[a-z',
 '[a-z[A-Z]]',
 '[a~~b]',
 '[z-a]',
 '\\',
 '\\0',
 '\\1',
 '\\8',
 '\\UFFFFFFFF',
 '\\b{',
 '\\b{foo',
 '\\b{foo!}',
 '\\b{foo}',
 '\\i',
 '\\pL',
 '\\p{L}',
 '\\uD800',
 '\\x',
 '\\xA',
 '\\xAZ',
 '\\xZ',
 '\\xZA',
 '\\x{',
 '\\x{A',
 '\\x{FFFFFFFFFFFFFFFFF}',
 '\\x{Z}',
 '\\x{}',
 'a{',
 'a{5',
 'a{5,',
 'a{5,6',
 'a{5,6Z',
 'a{6,5}',
 'a{999999999999999}',
 'a{]}',
 'a{}',
 '{5}',
 '|?']

# What the port refuses that the crate compiles, on purpose (see the module).
CONSERVATIVE = {"(?<a¾>z)", "(?P<a¾>z)", "(?<名字>z)", "(?P<名字>z)"}


@pytest.mark.parametrize("pattern", CRATE_OK)
def test_what_the_crate_parses_is_accepted(pattern):
    assert (regex_lite.problem(pattern) is None) == (pattern not in CONSERVATIVE), regex_lite.problem(pattern)


@pytest.mark.parametrize("pattern", CRATE_ERR)
def test_what_the_crate_refuses_is_refused(pattern):
    assert regex_lite.problem(pattern)


# Python's `re` compiles every one of these; `regex-lite` none.
PYTHON_ONLY = [
    (r"foo(?=bar)", "look-around"),
    (r"foo(?!bar)", "look-around"),
    (r"(?<=\$)\d+", "look-around"),
    (r"(?<!un)happy", "look-around"),
    (r"(a)\1", "backreferences"),
    (r"(?P<q>['\"]).*?(?P=q)", "inline flag"),
    (r"(?>a+)b", "inline flag"),
    (r"a++b", "possessive"),
    (r"a*+b", "possessive"),
    (r"a?+b", "possessive"),
    (r"a{2,3}+b", "possessive"),
    (r"(a)(?(1)b|c)", "inline flag"),
    (r"(?#a comment)a", "inline flag"),
    (r"end\Z", "unrecognized escape"),
    (r"\N{EM DASH}", "unrecognized escape"),
    (r"\101", "backreferences"),
    (r"\0", "backreferences"),
    (r"(?a)\w+", "inline flag"),
    (r"(?L)a", "inline flag"),
]


@pytest.mark.parametrize("pattern, why", PYTHON_ONLY)
def test_a_pattern_only_python_compiles_is_refused(pattern, why):
    if pattern != r"(?L)a":
        re.compile(pattern)
    problem = regex_lite.problem(pattern)
    assert problem and why in problem, problem
    with pytest.raises(ValidationError) as refused:
        GuardrailBuiltinConfig(banned_patterns=["ok", pattern])
    message = str(refused.value)
    assert "index 1" in message and "gateway" in message


@pytest.mark.parametrize(
    "pattern",
    [r"(?i)secret\s+project", r"\b\d{3}-\d{2}-\d{4}\b", r"[\w.+-]+@[\w-]+\.[\w.]+", r"(?:DROP|TRUNCATE)\s+TABLE",
     r"^sk-[A-Za-z0-9]{48}$", r"(?P<key>AKIA[0-9A-Z]{16})", r"a{2,}?", r"[^\x00-\x7F]", r"\$\d+(?:\.\d\d)?", ""],
)
def test_an_ordinary_pattern_is_saved(pattern):
    assert regex_lite.problem(pattern) is None
    assert GuardrailBuiltinConfig(banned_patterns=[pattern]).banned_patterns == [pattern]


@pytest.mark.parametrize(
    "pattern",
    [r"(?<key>a)", r"end\z", r"\x{41}", r"a(?i)b"],
)
def test_a_pattern_only_the_gateway_compiles_is_refused_for_the_control_planes_own_test(pattern):
    # The control plane's guardrail test runs patterns with Python's `re`.
    assert regex_lite.problem(pattern) is None
    with pytest.raises(ValidationError) as refused:
        GuardrailBuiltinConfig(banned_patterns=[pattern])
    assert "control plane" in str(refused.value)


def test_a_repetition_of_a_repetition_is_written_with_a_group():
    assert regex_lite.problem(r"(?:a+)+b") is None and regex_lite.problem(r"(a*)*") is None
    assert regex_lite.problem(r"a**") is None and regex_lite.problem(r"a+?") is None


def test_size_and_nesting():
    assert regex_lite.problem("(" * 50 + "a" + ")" * 50) is None
    assert regex_lite.problem("(" * 60 + "a" + ")" * 60) == "pattern has too much nesting"
    assert regex_lite.problem(r"\w{50}") is None
    assert "too large" in regex_lite.problem(r"(\w{1000}){1000}")
    assert "too large" in regex_lite.problem(r"a{4000000000}")
    assert regex_lite.problem("a{4294967296}") == "got invalid decimal number"
    assert regex_lite.problem("(" * 5000) is not None
    assert regex_lite.problem(7) == "a pattern must be a string"
    assert regex_lite.problem("a\ud800") == "the pattern is not valid Unicode text"


def test_the_x_flag_is_followed():
    assert regex_lite.problem("(?x) a b  # a (comment\n c") is None
    assert regex_lite.problem("(?x)[a - z # c\n]") is None
    assert regex_lite.problem("(?x:a{ 2 , 3 }) {") is not None
    assert regex_lite.problem("a # (") is not None


# --- the entries of a per-surface list -----------------------------------------


def test_a_per_surface_list_has_the_gateways_shape():
    ok = GuardrailBuiltinConfig(
        banned_words_per_surface={"chat_input": {"words": ["a", {"word": "b", "action": "warn"}]}},
        banned_patterns_per_surface={"mcp_input": {"patterns": [r"DROP\s+TABLE", {"pattern": "x+", "action": "warn"}]}},
    )
    assert ok.banned_words_per_surface["chat_input"]["words"][1] == {"word": "b", "action": "warn"}


@pytest.mark.parametrize(
    "field, override, why",
    [
        ("banned_words_per_surface", {"words": "a"}, "must be a list"),
        ("banned_words_per_surface", {"words": [7]}, "index 0"),
        ("banned_words_per_surface", {"words": ["a", {"action": "warn"}]}, "index 1"),
        ("banned_words_per_surface", {"words": [{"word": 7}]}, "index 0"),
        ("banned_words_per_surface", {"words": [{"word": "a", "action": "nuke"}]}, "action"),
        ("banned_patterns_per_surface", {"patterns": {"pattern": "a"}}, "must be a list"),
        ("banned_patterns_per_surface", {"patterns": [None]}, "index 0"),
        ("banned_patterns_per_surface", {"patterns": [{"word": "a"}]}, "index 0"),
        ("banned_patterns_per_surface", {"patterns": ["ok", "(?=a)"]}, "look-around"),
        ("banned_patterns_per_surface", {"patterns": [{"pattern": r"(a)\1"}]}, "backreferences"),
        ("banned_patterns_per_surface", {"patterns": ["("]}, "index 0"),
    ],
)
def test_a_per_surface_entry_the_gateway_cannot_read_is_refused(field, override, why):
    with pytest.raises(ValidationError) as refused:
        GuardrailBuiltinConfig(**{field: {"chat_input": override}})
    assert why in str(refused.value), str(refused.value)


# --- the gateway's side -----------------------------------------------------------


def test_the_gateway_compiles_with_regex_lite_and_reads_these_shapes():
    read = f" [the gateway's source read: {core_revision()}]"
    banned = core_source("services", "guardrails", "banned.rs").read_text(encoding="utf-8")
    assert "use regex_lite::Regex;" in banned, "the gateway compiles banned patterns with another crate" + read
    assert "let regex = Regex::new(&source).map_err(" in banned, read
    assert "RegexBuilder" not in banned, "the gateway sets limits of its own on a banned pattern" + read
    orchestrator = re.sub(r"\s+", " ", core_source("services", "guardrails", "orchestrator.rs").read_text(encoding="utf-8"))
    for piece in (
        "if let Some(s) = v.as_str() { out.push(BannedWord {",
        '} else if let Some(word) = v.get("word").and_then(|w| w.as_str()) {',
        "let (source, action) = if let Some(s) = v.as_str() { (s, None) } else if let Some(p) = "
        'v.get("pattern").and_then(|x| x.as_str()) { (p, v.get("action").and_then(|a| a.as_str())) } else {',
    ):
        assert piece in orchestrator, f"the gateway reads a banned entry differently ({piece!r})" + read
