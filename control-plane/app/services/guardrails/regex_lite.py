# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Whether the gateway can compile a banned pattern.

The gateway compiles banned patterns with the Rust crate `regex-lite`
(`brutor-gateway-core/src/services/guardrails/banned.rs`); the control plane
validated them with Python's `re`. The two languages differ: `regex-lite`
has no look-around, no backreferences, no Unicode classes (`\\p{..}`), no
atomic groups or possessive quantifiers, no conditional groups, no `\\Z`,
`\\N{..}` or octal escapes, no nested classes or class set operations. A
pattern Python accepts and the gateway cannot compile is a check that cannot
be built: the gateway then follows the policy's `fail_open`, which on a
fail-closed policy blocks every call.

`problem(pattern)` says why `regex-lite` would refuse a pattern, or `None`.
It is a port of that crate's parser (`regex-lite` 0.1.9, `src/hir/parse.rs`),
routine by routine, with the same names, and it is conservative where an
exact port would need Rust's own tables: what it accepts `regex-lite`
compiles, and it refuses a little that `regex-lite` would compile:

* a possessive quantifier (`a++`, `a*+`) is refused: `regex-lite` compiles
  it as a repetition of a repetition, which is not what Python and PCRE
  mean by it;
* a capture group name must be ASCII (`regex-lite` asks Unicode's
  `Alphabetic`, whose table differs between Rust and Python releases);
* nesting is counted as the crate builds its tree, less any further
  simplification the crate makes, against the same limit (50);
* the compiled size is estimated, state by state as the crate's compiler
  emits them and rounded up, against a third of the crate's limit (10 MiB).

`tests/test_guardrail_banned_patterns.py` holds the port against the
crate's own test table where the crate is in the local cargo registry, and
reads the gateway's source for the crate it compiles with.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

NEST_LIMIT = 50
# `regex-lite`'s default size limit is 10 MiB of NFA; a state is at most 32
# bytes on a 64-bit target. A third of that, in states.
MAX_STATES = 100_000

# Unicode `White_Space`, as Rust's `char::is_whitespace`.
_WHITE_SPACE = frozenset(
    [*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
)
_META = frozenset("\\.+*?()|[]{}^$#&-~")
_POSIX = frozenset(
    ["alnum", "alpha", "ascii", "blank", "cntrl", "digit", "graph", "lower", "print", "punct", "space", "upper",
     "word", "xdigit"]
)
_HEX = frozenset("0123456789abcdefABCDEF")
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DIGITS = frozenset("0123456789")
_SIMPLE_ESCAPES = {"a": 0x07, "f": 0x0C, "t": 0x09, "n": 0x0A, "r": 0x0D, "v": 0x0B}

# What a parsed item is, for a character class: one character, a class of
# them (`\d`), or an assertion (`\b`).
_CHAR, _CLASS, _LOOK = "char", "class", "look"


class _Refused(Exception):
    pass


def _is_whitespace(ch: str) -> bool:
    return ord(ch) in _WHITE_SPACE


def _is_escapable(ch: str) -> bool:
    if ch in _META:
        return True
    if ord(ch) > 0x7F:
        return False
    return ch not in _ASCII_LETTERS and ch not in _DIGITS and ch not in "<>"


class _Node:
    """A parsed sub-expression: how deep it nests and how many NFA states
    it compiles to, both rounded up."""

    __slots__ = ("depth", "states", "repetition")

    def __init__(self, depth: int, states: int, repetition: bool = False) -> None:
        self.depth = depth
        self.states = states
        # True for what a repetition operator has just made.
        self.repetition = repetition


class _Parser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.pos = 0
        self.depth = 0
        self.ignore_whitespace = False
        self.names: set = set()
        # The character the last escape parsed stands for.
        self.scalar = 0

    # -- helpers -----------------------------------------------------------
    def is_done(self) -> bool:
        return self.pos >= len(self.pattern)

    def char(self) -> str:
        if self.is_done():
            # `regex-lite` panics here; no pattern reaches it there, and
            # none is accepted that reaches it here.
            raise _Refused("unexpected end of pattern")
        return self.pattern[self.pos]

    def bump(self) -> bool:
        if self.is_done():
            return False
        self.pos += 1
        return not self.is_done()

    def bump_if(self, prefix: str) -> bool:
        if self.pattern.startswith(prefix, self.pos):
            self.pos += len(prefix)
            return True
        return False

    def bump_space(self) -> None:
        if not self.ignore_whitespace:
            return
        while not self.is_done():
            if _is_whitespace(self.char()):
                self.bump()
            elif self.char() == "#":
                self.bump()
                while not self.is_done():
                    c = self.char()
                    self.bump()
                    if c == "\n":
                        break
            else:
                break

    def bump_and_bump_space(self) -> bool:
        if not self.bump():
            return False
        self.bump_space()
        return not self.is_done()

    def peek(self) -> Optional[str]:
        return self.pattern[self.pos + 1] if self.pos + 1 < len(self.pattern) else None

    def peek_space(self) -> Optional[str]:
        if not self.ignore_whitespace:
            return self.peek()
        if self.is_done():
            return None
        start = self.pos + 1
        in_comment = False
        for i in range(start, len(self.pattern)):
            ch = self.pattern[i]
            if _is_whitespace(ch):
                continue
            if not in_comment and ch == "#":
                in_comment = True
            elif in_comment and ch == "\n":
                in_comment = False
            else:
                start = i
                break
        return self.pattern[start] if start < len(self.pattern) else None

    # -- the grammar -------------------------------------------------------
    def parse(self) -> _Node:
        node = self.parse_inner()
        if node.depth > NEST_LIMIT:
            raise _Refused("pattern has too much nesting")
        if node.states > MAX_STATES:
            raise _Refused("the compiled pattern would be too large")
        return node

    def parse_inner(self) -> _Node:
        depth = self.depth
        if depth > NEST_LIMIT:
            raise _Refused("pattern has too much nesting")
        self.depth += 1
        alternates: List[List[_Node]] = []
        concat: List[_Node] = []
        while True:
            self.bump_space()
            if self.is_done():
                break
            ch = self.char()
            if ch == "(":
                old = self.ignore_whitespace
                sub = self.parse_group()
                if sub is not None:
                    concat.append(sub)
                    self.ignore_whitespace = old
                if self.is_done() or self.char() != ")":
                    raise _Refused("found open group without closing ')'")
                self.bump()
            elif ch == ")":
                if depth == 0:
                    raise _Refused("found closing ')' without matching '('")
                break
            elif ch == "|":
                alternates.append(concat)
                concat = []
                self.bump()
            elif ch == "[":
                concat.append(self.parse_class())
            elif ch in "?*+":
                self.parse_uncounted_repetition(concat)
            elif ch == "{":
                self.parse_counted_repetition(concat)
            else:
                concat.append(self.parse_primitive()[1])
        self.depth -= 1
        alternates.append(concat)
        # `Hir::concat` and `Hir::alternation`: one element is that element,
        # more are one level above the deepest. Two more states than the
        # parts, and a split target for each branch.
        branches = [
            branch[0] if len(branch) == 1
            else _Node(1 + max((n.depth for n in branch), default=0), 2 + sum(n.states for n in branch))
            for branch in alternates
        ]
        if len(branches) == 1:
            return branches[0]
        return _Node(1 + max(b.depth for b in branches), 2 + len(branches) + sum(b.states for b in branches))

    def parse_primitive(self) -> Tuple[str, _Node]:
        ch = self.char()
        self.bump()
        if ch == "\\":
            return self.parse_escape()
        if ch in ".":
            return _CLASS, _Node(0, 2)
        if ch in "^$":
            return _LOOK, _Node(0, 1)
        return _CHAR, _Node(0, 2)

    def parse_escape(self) -> Tuple[str, _Node]:
        if self.is_done():
            raise _Refused("the pattern ends in the middle of an escape sequence")
        ch = self.char()
        if ch in _DIGITS:
            raise _Refused("backreferences (and octal escapes) are not supported")
        if ch in "pP":
            raise _Refused("Unicode character classes (\\p{..}) are not supported")
        if ch in "xuU":
            return self.parse_hex()
        if ch in "dswDSW":
            self.bump()
            return _CLASS, _Node(0, 3)
        self.bump()
        if _is_escapable(ch):
            self.scalar = ord(ch)
            return _CHAR, _Node(0, 2)
        if ch in _SIMPLE_ESCAPES:
            self.scalar = _SIMPLE_ESCAPES[ch]
            return _CHAR, _Node(0, 2)
        if ch in "AzB<>":
            return _LOOK, _Node(0, 1)
        if ch == "b":
            if not self.is_done() and self.char() == "{":
                self.maybe_parse_special_word_boundary()
            return _LOOK, _Node(0, 1)
        raise _Refused(f"unrecognized escape sequence \\{ch}")

    def maybe_parse_special_word_boundary(self) -> None:
        valid = lambda c: c in _ASCII_LETTERS or c == "-"  # noqa: E731
        start = self.pos
        if not self.bump_and_bump_space():
            raise _Refused("found start of special word boundary or repetition without an end")
        if not valid(self.char()):
            self.pos = start
            return
        scratch = ""
        while not self.is_done() and valid(self.char()):
            scratch += self.char()
            self.bump_and_bump_space()
        if self.is_done() or self.char() != "}":
            raise _Refused("special word boundary assertion is unclosed or has an invalid character")
        self.bump()
        if scratch not in ("start", "end", "start-half", "end-half"):
            raise _Refused("special word boundary assertion is unrecognized")

    def parse_hex(self) -> Tuple[str, _Node]:
        digit_len = {"x": 2, "u": 4, "U": 8}[self.char()]
        if not self.bump_and_bump_space():
            raise _Refused("expected hexadecimal number, but saw end of pattern first")
        scratch = ""
        if self.char() == "{":
            while self.bump_and_bump_space() and self.char() != "}":
                if self.char() not in _HEX:
                    raise _Refused("expected hexadecimal number in braces, but got non-hex digit")
                scratch += self.char()
            if self.is_done():
                raise _Refused("expected hexadecimal number, but saw end of pattern before closing brace")
            self.bump_and_bump_space()
            if not scratch:
                raise _Refused("expected hexadecimal number in braces, but got no digits")
        else:
            for i in range(digit_len):
                if i > 0 and not self.bump_and_bump_space():
                    raise _Refused("expected fixed length hexadecimal number, but saw end of pattern first")
                if self.char() not in _HEX:
                    raise _Refused("expected fixed length hexadecimal number, but got non-hex digit")
                scratch += self.char()
            self.bump_and_bump_space()
        # `u32::from_str_radix` then `char::from_u32`.
        if len(scratch.lstrip("0")) > 8:
            raise _Refused("got invalid hexadecimal number")
        value = int(scratch, 16)
        if value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            raise _Refused("got invalid hexadecimal number")
        self.scalar = value
        return _CHAR, _Node(0, 2)

    def parse_decimal(self) -> int:
        scratch = ""
        while not self.is_done() and _is_whitespace(self.char()):
            self.bump()
        while not self.is_done() and self.char() in _DIGITS:
            scratch += self.char()
            self.bump_and_bump_space()
        while not self.is_done() and _is_whitespace(self.char()):
            self.bump_and_bump_space()
        if not scratch:
            raise _Refused("expected decimal number, but found no digits")
        if int(scratch) > 0xFFFFFFFF:
            raise _Refused("got invalid decimal number")
        return int(scratch)

    def parse_uncounted_repetition(self, concat: List[_Node]) -> None:
        if not concat:
            raise _Refused("a repetition operator (? * +) must follow something to repeat")
        sub = concat.pop()
        if self.char() == "+" and sub.repetition:
            # `a++`, `a*+`, `a?+`, `a{2}+`: `regex-lite` repeats the
            # repetition; Python (and PCRE) read a possessive quantifier,
            # which `regex-lite` does not have. The two would not match the
            # same text, so it is not saved.
            raise _Refused(
                "possessive quantifiers (a++, a*+, a?+, a{m,n}+) are not supported: write (?:a+)+ "
                "if a repeated repetition is meant"
            )
        if self.bump() and self.char() == "?":
            self.bump()
        concat.append(_Node(sub.depth + 1, sub.states + 4, True))

    def parse_counted_repetition(self, concat: List[_Node]) -> None:
        if not concat:
            raise _Refused("a counted repetition {m,n} must follow something to repeat")
        sub = concat.pop()
        if not self.bump_and_bump_space():
            raise _Refused("found unclosed counted repetition operator")
        low = self.parse_decimal()
        high: Optional[int] = low
        if self.is_done():
            raise _Refused("found incomplete and unclosed counted repetition operator")
        if self.char() == ",":
            if not self.bump_and_bump_space():
                raise _Refused("found counted repetition operator with a comma that is unclosed")
            high = self.parse_decimal() if self.char() != "}" else None
            if self.is_done():
                raise _Refused("found counted repetition with min and max that is unclosed")
        if self.char() != "}":
            raise _Refused("expected closing brace for counted repetition, but got something else")
        if self.bump_and_bump_space() and self.char() == "?":
            self.bump()
        if high is not None and low > high:
            raise _Refused("found counted repetition with a min bigger than its max")
        copies = (high if high is not None else low + 1) + 1
        states = copies * (sub.states + 4) + 4
        if states > MAX_STATES:
            raise _Refused("the compiled pattern would be too large (a counted repetition of this size)")
        concat.append(_Node(sub.depth + 1, states, True))

    def parse_group(self) -> Optional[_Node]:
        self.bump_and_bump_space()
        for look in ("?=", "?!", "?<=", "?<!"):
            if self.bump_if(look):
                raise _Refused("look-around ((?=..), (?!..), (?<=..), (?<!..)) is not supported")
        if self.bump_if("?P<") or self.bump_if("?<"):
            self.parse_capture_name()
            sub = self.parse_inner()
            return _Node(sub.depth + 1, sub.states + 2)
        if self.bump_if("?"):
            if self.is_done():
                raise _Refused("expected closing ')', but got end of pattern")
            start = self.pos
            self.parse_flags()
            if self.char() == ")":
                if self.pos == start:
                    raise _Refused("empty flag directive '(?)' is not allowed")
                return None
            self.bump()
            sub = self.parse_inner()
            return _Node(sub.depth, sub.states)
        sub = self.parse_inner()
        return _Node(sub.depth + 1, sub.states + 2)

    def parse_capture_name(self) -> None:
        if self.is_done():
            raise _Refused("expected capture group name, but got end of pattern")
        start = self.pos
        while True:
            ch = self.char()
            if ch == ">":
                break
            first = self.pos == start
            ok = ch == "_" or ch in _ASCII_LETTERS or (not first and (ch in ".[]" or ch in _DIGITS))
            if not ok:
                raise _Refused("invalid capture group name (ASCII letters, digits, '_', '.', '[' and ']')")
            if not self.bump():
                break
        if self.is_done():
            raise _Refused("expected end of capture group name, but got end of pattern")
        name = self.pattern[start:self.pos]
        self.bump()
        if not name:
            raise _Refused("empty capture group names are not allowed")
        if name in self.names:
            raise _Refused("duplicate capture group name")
        self.names.add(name)

    def parse_flags(self) -> None:
        negate = False
        last_was_negation = False
        seen = set()
        while self.char() not in ":)":
            ch = self.char()
            if ch == "-":
                last_was_negation = True
                if negate:
                    raise _Refused("inline flag negation cannot be repeated")
                negate = True
            else:
                last_was_negation = False
                if ch not in "imsURxu":
                    raise _Refused(
                        "unrecognized inline flag (the flags are i, m, s, U, R, x and u; atomic groups, "
                        "conditionals and comments are not supported)"
                    )
                if ch == "x":
                    self.ignore_whitespace = not negate
                if ch in seen:
                    raise _Refused("duplicate inline flag is not allowed")
                seen.add(ch)
            if not self.bump():
                raise _Refused("expected ':' or ')' to end inline flags, but got end of pattern")
        if last_was_negation:
            raise _Refused("inline flags cannot end with negation directive")

    def parse_class(self) -> _Node:
        ranges = 0
        if not self.bump_and_bump_space():
            raise _Refused("found unclosed character class")
        if self.char() == "^":
            if not self.bump_and_bump_space():
                raise _Refused("negated character class has no closing bracket")
        while self.char() == "-":
            ranges += 1
            if not self.bump_and_bump_space():
                raise _Refused("non-empty character class has no closing bracket after dash")
        if ranges == 0 and self.char() == "]":
            ranges += 1
            if not self.bump_and_bump_space():
                raise _Refused("character class begins with literal ']' but has no closing bracket")
        while True:
            self.bump_space()
            if self.is_done():
                raise _Refused("found unclosed character class")
            ch = self.char()
            if ch == "[":
                if self.maybe_parse_posix_class():
                    ranges += 12
                    continue
                raise _Refused("nested character classes are not supported")
            if ch == "]":
                self.bump()
                # The ranges are held beside the state: up to two per item
                # once case is folded, one more once negated, 8 bytes each.
                return _Node(0, 2 + ranges)
            if ch == "&" and self.peek() == "&":
                raise _Refused("character class intersection (&&) is not supported")
            if ch == "-" and self.peek() == "-":
                raise _Refused("character class difference (--) is not supported")
            if ch == "~" and self.peek() == "~":
                raise _Refused("character class symmetric difference (~~) is not supported")
            ranges += self.parse_class_range()

    def parse_class_item(self) -> Tuple[str, int]:
        ch = self.char()
        self.bump()
        if ch == "\\":
            kind, _ = self.parse_escape()
            return kind, self.scalar
        return _CHAR, ord(ch)

    def parse_class_range(self) -> int:
        kind1, text1 = self.parse_class_item()
        self.bump_space()
        if self.is_done():
            raise _Refused("non-empty character class has no closing bracket")
        if self.char() != "-" or self.peek_space() in ("]", "-"):
            if kind1 == _LOOK:
                raise _Refused("invalid escape sequence in character class")
            return 12 if kind1 == _CLASS else 3
        if not self.bump_and_bump_space():
            raise _Refused("non-empty character class has no closing bracket after dash")
        kind2, text2 = self.parse_class_item()
        if kind1 != _CHAR or kind2 != _CHAR:
            raise _Refused("character class ranges must start and end with a single character")
        if text1 > text2:
            raise _Refused("invalid range in character class")
        return 3

    def maybe_parse_posix_class(self) -> bool:
        start = self.pos
        if not self.bump() or self.char() != ":":
            self.pos = start
            return False
        if not self.bump():
            self.pos = start
            return False
        if self.char() == "^":
            if not self.bump():
                self.pos = start
                return False
        name_start = self.pos
        while self.char() != ":" and self.bump():
            pass
        if self.is_done():
            self.pos = start
            return False
        name = self.pattern[name_start:self.pos]
        if not self.bump_if(":]") or name not in _POSIX:
            self.pos = start
            return False
        return True


def problem(pattern: object) -> Optional[str]:
    """Why the gateway's `regex-lite` would not compile `pattern`; `None`
    for a pattern it compiles."""
    if not isinstance(pattern, str):
        return "a pattern must be a string"
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in pattern):
        return "the pattern is not valid Unicode text"
    try:
        _Parser(pattern).parse()
    except _Refused as refused:
        return str(refused)
    except RecursionError:
        return "pattern has too much nesting"
    return None
