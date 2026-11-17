#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""The licence header of every source file: checked, or swapped to an edition's header.

    licence_headers.py check <tree> --edition community|enterprise
    licence_headers.py apply <tree> --edition community|enterprise [--dry-run]

The header wording, the file types, the comment style per type and the exclusion rules are
data: `headers.json` beside this file.

`check` exits 1 when a source file lacks its edition's header, carries the other edition's,
names another copyright holder, or holds wording this tool does not recognise.

`apply` replaces every known proprietary header (or the other edition's header) with the
edition's header, adds one where there is none, and changes nothing else. It never writes an
excluded file, a file naming another copyright holder, or a file it does not fully
recognise: those are reported as refused. A second run changes nothing.

Standard library only. Works on a plain directory tree; it never calls git.
"""
import argparse
import fnmatch
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "headers.json")
SCAN_LINES = 60          # a header is looked for in the first lines only
MAX_HEADER_LINES = 16
LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+\Z")
RULE_RE = re.compile(r"^[=\-*#_~]{4,}$")
COPY_RE = re.compile(r"^Copyright\b\s*(?:\(c\)|©)?\s*((?:19|20)\d{2}(?:\s*-\s*(?:19|20)\d{2})?),?\s+(.+)$", re.I)
ENCODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
DOCKER_DIRECTIVE_RE = re.compile(r"^#\s*(syntax|escape|check)\s*=", re.I)
FUTURE_IMPORT_RE = re.compile(r"^from\s+__future__\s+import\s+[\w\s,]+$")
DOCSTRING_OPEN_RE = re.compile(r"^([rRuUbB]{0,2})(\"\"\"|''')")
LINE_COMMENT = {
    "rust": re.compile(r"^(\s*)(//!|///|//)(.*)$"),
    "js": re.compile(r"^(\s*)(//)(.*)$"),
    "python": re.compile(r"^(\s*)(#)(.*)$"),
    "shell": re.compile(r"^(\s*)(#)(.*)$"),
    "powershell": re.compile(r"^(\s*)(#)(.*)$"),
    "yaml": re.compile(r"^(\s*)(#)(.*)$"),
    "docker": re.compile(r"^(\s*)(#)(.*)$"),
    "sql": re.compile(r"^(\s*)(--)(.*)$"),
}
BLOCK_SYNTAXES = {"rust", "js", "css", "sql"}


# --------------------------------------------------------------------------- configuration

class Config:
    def __init__(self, path=DEFAULT_CONFIG):
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.raw = raw
        self.path = path
        self.editions = raw["editions"]
        # holders the owner has assigned to the company are rewritten like the company's own
        self.assigned = {h.rstrip(".").lower() for h in raw.get("assigned_holders", [])}
        names = raw["company_holders"] + raw.get("assigned_holders", [])
        self.holders = {h.rstrip(".").lower() for h in names}
        self.sentences = raw["legacy_sentences"]
        holder = "(?:" + "|".join(re.escape(h.rstrip(".")) for h in names) + r")\.?"
        parts = [r"Copyright (?:\(c\) |© )?(?:19|20)\d{2}(?:\s?-\s?(?:19|20)\d{2})?,? " + holder,
                 r"(?P<rights> All rights reserved\.?)?"]
        for i, s in enumerate(self.sentences):
            parts.append("(?P<s%d> %s)?" % (i, re.escape(s)))
        parts.append(r"(?: SPDX-License-Identifier: (?P<spdx>\S+))?")
        self.grammar = re.compile("".join(parts))

    def header_lines(self, edition, syntax, eol="\n"):
        """The edition's header as the lines of a file of this syntax."""
        style = self.raw["styles"][syntax]
        text = self.editions[edition]["lines"]
        out = []
        if "open" in style:
            out.append(style["open"])
        for t in text:
            out.append(style["line"] + t + style.get("line_end", ""))
        if "close" in style:
            out.append(style["close"])
        return [l + eol for l in out]

    def spdx_of(self, edition):
        for t in self.editions[edition]["lines"]:
            m = re.match(r"SPDX-License-Identifier:\s*(\S+)", t)
            if m:
                return m.group(1)
        return None


def _glob(rel, pattern):
    return fnmatch.fnmatchcase("/" + rel, pattern)


def classify(rel, cfg, read_head=None):
    """('source', syntax) | ('excluded', rule id) | ('non-source', ext) | ('unclassified', ext).

    `read_head` returns the first bytes of the file; it is called only for a source type, to
    apply the content rules (generated marker, empty)."""
    name = os.path.basename(rel)
    ext = os.path.splitext(name)[1].lower()
    forced = any(_glob(rel, g) for g in cfg.raw.get("force_include", []))
    if not forced:
        for rule in cfg.raw["exclusions"]:
            if any(_glob(rel, g) for g in rule.get("path_globs", [])):
                return ("excluded", rule["id"])
    syntax = None
    if name not in cfg.raw["non_source_names"]:
        for g, s in cfg.raw["syntax_by_path_glob"].items():
            if _glob(rel, "*" + g if not g.startswith("*") else g):
                syntax = s
        if syntax is None:
            for g, s in cfg.raw["syntax_by_name_glob"].items():
                if fnmatch.fnmatchcase(name, g):
                    syntax = s
        if syntax is None:
            syntax = cfg.raw["syntax_by_extension"].get(ext)
    if syntax is None:
        if name in cfg.raw["non_source_names"] or ext in cfg.raw["non_source_extensions"]:
            return ("non-source", ext or name)
        return ("unclassified", ext or name)
    if read_head is not None:
        head = read_head()
        for rule in cfg.raw["exclusions"]:
            if rule.get("empty") and not head.strip():
                return ("excluded", rule["id"])
            if "content_markers" in rule:
                first = b"\n".join(head.splitlines()[: rule["content_markers_first_lines"]])
                if any(m.encode() in first for m in rule["content_markers"]):
                    return ("excluded", rule["id"])
    return ("source", syntax)


# --------------------------------------------------------------------------- reading a file's first lines

class Line:
    __slots__ = ("kind", "cid", "content", "pre", "construct")

    def __init__(self, kind, cid=None, content=None, pre=None, construct=None):
        self.kind = kind            # blank | pre | lc | block | doc | html | code
        self.cid = cid              # comment construct this line belongs to
        self.content = content      # the text of a comment line without its decoration
        self.pre = pre              # which "must come first" line this is
        self.construct = construct  # for block / doc / html: dict(start, end, tainted, label)


def _strip_content(text):
    text = text.strip()
    return "" if RULE_RE.match(text) else text


def scan(lines, syntax):
    """Classify the first SCAN_LINES lines: comment constructs, their text, and what must stay first."""
    out = []
    cid = 0
    code_seen = False
    n = min(len(lines), SCAN_LINES)
    i = 0
    prev_lc = None
    only_pre_so_far = True
    after_future = False     # python: only `from __future__` imports stand before (no other code)
    while i < n:
        s = lines[i].rstrip("\r\n")
        if i == 0 and s.startswith("﻿"):
            s = s[1:]
        st = s.strip()
        if not st:
            out.append(Line("blank"))
            prev_lc = None
            i += 1
            continue
        pre = None
        if i == 0 and s.startswith("#!") and not s.startswith("#!["):
            pre = "shebang"
        elif i == 0 and s.startswith("<?xml"):
            pre = "xml declaration"
        elif syntax == "python" and i <= 1 and ENCODING_RE.match(s) and all(l.kind == "pre" for l in out):
            pre = "encoding line"
        elif syntax == "html" and only_pre_so_far and st.lower().startswith("<!doctype"):
            pre = "doctype"
        elif syntax == "css" and i == 0 and st.lower().startswith("@charset"):
            pre = "@charset"
        elif syntax == "docker" and only_pre_so_far and DOCKER_DIRECTIVE_RE.match(s):
            pre = "parser directive"
        if pre:
            out.append(Line("pre", pre=pre))
            prev_lc = None
            i += 1
            continue
        only_pre_so_far = False
        # block comment
        if syntax in BLOCK_SYNTAXES and st.startswith("/*"):
            cid += 1
            start = i
            label = "/** */" if st.startswith("/**") and not st.startswith("/**/") else "/* */"
            first = st[2:].lstrip("*") if not st.startswith("/**/") else st[2:]
            j = i
            body = first
            contents = []
            tainted = bool(s[: len(s) - len(s.lstrip())].strip())
            while True:
                if "*/" in body:
                    before, after = body.split("*/", 1)
                    contents.append(_strip_content(re.sub(r"^\s*\*?\s?", "", before) if j > start else before))
                    if after.strip():
                        tainted = True
                    break
                contents.append(_strip_content(re.sub(r"^\s*\*?\s?", "", body) if j > start else body))
                j += 1
                if j >= len(lines) or j - start > 400:
                    tainted = True
                    j = min(j, len(lines) - 1)
                    break
                body = lines[j].rstrip("\r\n")
            con = {"start": start, "end": j, "tainted": tainted, "label": label if j > start else label + " one line"}
            for k, c in enumerate(contents[: j - start + 1]):
                out.append(Line("block", cid, c, construct=con))
            prev_lc = None
            i = j + 1
            continue
        # html comment
        if syntax == "html" and st.startswith("<!--"):
            cid += 1
            start = i
            j = i
            body = st[4:]
            contents = []
            tainted = False
            while True:
                if "-->" in body:
                    before, after = body.split("-->", 1)
                    contents.append(_strip_content(before))
                    if after.strip():
                        tainted = True
                    break
                contents.append(_strip_content(body))
                j += 1
                if j >= len(lines) or j - start > 400:
                    tainted = True
                    j = min(j, len(lines) - 1)
                    break
                body = lines[j].rstrip("\r\n")
            con = {"start": start, "end": j, "tainted": tainted, "label": "<!-- -->"}
            for c in contents[: j - start + 1]:
                out.append(Line("html", cid, c, construct=con))
            prev_lc = None
            i = j + 1
            continue
        # python module docstring (only where it can be one: before any code), or a string that
        # stands right after the `from __future__` imports: a statement with no effect
        if syntax == "python" and not code_seen:
            m = DOCSTRING_OPEN_RE.match(s)
            if m:
                cid += 1
                quote = m.group(2)
                start = i
                j = i
                body = s[m.end():]
                contents = []
                tainted = "b" in m.group(1).lower()
                while True:
                    if quote in body:
                        before, after = body.split(quote, 1)
                        contents.append(_strip_content(before))
                        if after.strip() and not after.strip().startswith("#"):
                            tainted = True
                        break
                    contents.append(_strip_content(body))
                    j += 1
                    if j >= len(lines) or j - start > 2000:
                        tainted = True
                        j = min(j, len(lines) - 1)
                        break
                    body = lines[j].rstrip("\r\n")
                con = {"start": start, "end": j, "tainted": tainted,
                       "label": "string after from __future__" if after_future else "docstring"}
                for c in contents[: j - start + 1]:
                    out.append(Line("doc", cid, c, construct=con))
                code_seen = True      # whatever follows is no longer a module docstring
                prev_lc = None
                i = j + 1
                continue
        # line comment
        lc = LINE_COMMENT.get(syntax)
        m = lc.match(s) if lc else None
        if m:
            key = (m.group(1), m.group(2))
            if key != prev_lc:
                cid += 1
            prev_lc = key
            out.append(Line("lc", cid, _strip_content(m.group(3)), construct={"label": m.group(2)}))
            i += 1
            continue
        out.append(Line("code"))
        if syntax == "python" and not code_seen and FUTURE_IMPORT_RE.match(st):
            after_future = True
        else:
            code_seen = True
        prev_lc = None
        i += 1
    return out


def insertion_point(info):
    """Index of the first line that is not something the language wants first."""
    p = 0
    while p < len(info) and info[p].kind == "pre":
        p += 1
    return p


# --------------------------------------------------------------------------- analysis of one file

class Result:
    def __init__(self):
        self.status = None        # ok | add | replace | refused
        self.variant = None       # the header shape found (for counts)
        self.reason = None        # why refused / what is wrong
        self.new_text = None
        self.preamble = None      # what stands before the header by necessity
        self.holder = None
        self.docstring = None     # for python: 'removed' | 'changed'
        self.first_code = None    # crate attribute / 'use client' standing right after the header


def _holder(content):
    m = COPY_RE.match(content)
    if not m:
        return None
    rest = m.group(2)
    # A header quoted in a string literal ends at its escape sequence or the closing quote.
    rest = re.split(r"\\[nrt]|[\"'`]", rest)[0]
    rest = re.split(r"\.?\s+All rights reserved", rest, flags=re.I)[0]
    return rest.strip().rstrip(".").strip()


def _text_form(m, cfg):
    """C = copyright line; then one letter per legacy sentence present (P, I, D, W for the four known)."""
    if m.group("spdx"):
        return "spdx:" + m.group("spdx")
    tags = "PIDW" + "".join(chr(ord("a") + k) for k in range(len(cfg.sentences)))
    return "C" + "".join(tags[i] for i in range(len(cfg.sentences)) if m.group("s%d" % i))


def analyse(text, syntax, edition, cfg):
    """Decide what `apply` would do to one source file. Never raises on odd input: it refuses."""
    r = Result()
    lines = LINE_RE.findall(text)
    eol = "\n"
    for l in lines:
        m = re.search(r"(\r\n|\n|\r)\Z", l)
        if m:
            eol = m.group(1)
            break
    info = scan(lines, syntax)
    pos = insertion_point(info)
    if pos:
        r.preamble = info[pos - 1].pre if pos == 1 else ", ".join(l.pre for l in info[:pos])
    canonical = cfg.header_lines(edition, syntax, eol)

    def is_canonical_at(ls, p):
        got = ls[p: p + len(canonical)]
        if len(got) == len(canonical) and got[-1] and not got[-1].endswith(("\n", "\r")):
            got = got[:-1] + [got[-1] + eol]      # a header that ends the file without a newline
        return got == canonical

    # every copyright line in the leading region
    regions = []          # (start, end, match, construct id, orphans)
    i = 0
    while i < len(info):
        L = info[i]
        if L.content and COPY_RE.match(L.content):
            holder = _holder(L.content)
            if holder is None or holder.lower() not in cfg.holders:
                r.status, r.reason, r.holder = "refused", "names another copyright holder", holder
                return r
            words = []
            best = None
            j = i
            while j < len(info) and j < i + MAX_HEADER_LINES and info[j].cid == L.cid and info[j].content is not None:
                if info[j].content:
                    words.append(" ".join(info[j].content.split()))
                    m = cfg.grammar.fullmatch(" ".join(words))
                    if m:
                        best = (j, m, list(words))
                j += 1
            if not best:
                r.status, r.reason = "refused", "copyright line followed by wording this tool does not know (line %d)" % (i + 1)
                return r
            end, m, words = best
            orphans = []
            for k in range(end + 1, min(len(info), end + 11)):
                c = info[k].content
                if c and info[k].kind == "lc":
                    m2 = cfg.grammar.fullmatch(" ".join(words + [" ".join(c.split())]))
                    if m2:
                        orphans.append(k)
                        words.append(" ".join(c.split()))
                        m = m2
            regions.append((i, end, m, L.cid, orphans))
            i = end + 1
            continue
        i += 1

    # a copyright line that is not in a leading comment (after code, or in a string that is not
    # the module docstring): never guessed at
    for k, L in enumerate(info):
        if L.kind == "code" and k < len(lines):
            bare = re.sub(r"^[\s/*#\"'<!-]*", "", lines[k].rstrip("\r\n"))
            if COPY_RE.match(bare):
                holder = _holder(bare)
                if holder is not None and holder.lower() in cfg.holders and re.search(r"[\"'`]\s*Copyright\b", lines[k]):
                    continue           # the company's header quoted in a string literal: data (a guard pinning the wording)
                if holder is None or holder.lower() not in cfg.holders:
                    r.status, r.reason, r.holder = "refused", "names another copyright holder", holder
                else:
                    r.status, r.reason = "refused", "copyright line outside a leading comment (line %d)" % (k + 1)
                return r

    # already right?
    if len(regions) == 1 and regions[0][2].group("spdx") == cfg.spdx_of(edition) and is_canonical_at(lines, pos):
        s, e = regions[0][0], regions[0][1]
        style = cfg.raw["styles"][syntax]
        first = pos + (1 if "open" in style else 0)
        if s == first and e == first + len(cfg.editions[edition]["lines"]) - 1:
            r.status = "ok"
            r.first_code = _first_code(lines, pos + len(canonical))
            return r

    remove = set()
    variants = []
    for (s, e, m, cid, orphans) in regions:
        L = info[s]
        label = L.construct["label"]
        if L.kind == "lc":
            a = s
            while a > 0 and info[a - 1].cid == cid:
                a -= 1
            b = e
            while b + 1 < len(info) and info[b + 1].cid == cid:
                b += 1
            rm, kept = _strip_region(info, a, b, s, e)
            remove |= rm
            for k in orphans:
                remove.add(k)
                if k - 1 > e and info[k - 1].kind == "lc" and info[k - 1].content == "" and info[k - 1].cid == info[k].cid:
                    remove.add(k - 1)
        else:
            con = L.construct
            a, b = con["start"], con["end"]
            if b >= len(info):
                r.status, r.reason = "refused", "header inside a comment that runs past the first %d lines" % SCAN_LINES
                return r
            others = [k for k in range(a, b + 1) if info[k].content and not (s <= k <= e)]
            if not others:
                if con["tainted"]:
                    r.status, r.reason = "refused", "header comment shares a line with code (line %d)" % (a + 1)
                    return r
                remove |= set(range(a, b + 1))
                kept = False
                if L.kind == "doc" and con["label"] == "docstring":
                    if re.search(r"\b__doc__\b", text):
                        r.status, r.reason = "refused", "the module docstring is only the header and the module reads __doc__"
                        return r
                    r.docstring = "removed"
            else:
                if s == a or e == b:
                    r.status, r.reason = "refused", "header shares the opening or closing line of a comment that also holds documentation (line %d)" % (s + 1)
                    return r
                rm, kept = _strip_region(info, a + 1, b - 1, s, e, opener_has_text=bool(info[a].content))
                remove |= rm
                if L.kind == "doc" and con["label"] == "docstring":
                    r.docstring = "changed"
            if orphans:
                r.status, r.reason = "refused", "header continues outside its comment (line %d)" % (orphans[0] + 1)
                return r
        assigned = (_holder(L.content) or "").lower() in cfg.assigned
        variants.append("%s %s%s%s%s" % (label, _text_form(m, cfg), " +documentation kept" if kept else "",
                                         " +split" if orphans else "", " +assigned holder" if assigned else ""))

    kept_lines = [(k, l) for k, l in enumerate(lines) if k not in remove]
    # one blank line, not two, where a removed header stood between blank lines (or at the top)
    out = []
    last_index = -1
    for n_kept, (k, l) in enumerate(kept_lines):
        gap = k != last_index + 1
        last_index = k
        if gap and syntax == "rust" and len(out) > pos:
            # rustfmt keeps no blank line between a crate attribute and a plain comment that
            # follows it, so a header that stood between the two takes its blank lines along
            above = next((x.strip() for x in reversed(out) if x.strip()), "")
            below = next((x.strip() for _, x in kept_lines[n_kept:] if x.strip()), "")
            if above.startswith("#![") and below.startswith("//") and not below.startswith(("//!", "///")):
                while out and not out[-1].strip():
                    out.pop()
                if not l.strip():
                    continue
        if gap and not l.strip() and (len(out) <= pos or not out[-1].strip()):
            continue
        out.append(l)
    first = out[:pos]
    rest = out[pos:]
    if first and not first[-1].endswith(("\n", "\r")):
        first[-1] += eol                      # e.g. a file that is only a shebang with no newline
    head = list(canonical)
    if rest and rest[0].strip():
        head.append(eol)
    if not rest and text and not text.endswith(("\n", "\r")):
        head[-1] = head[-1][: -len(eol)]      # the file had no final newline: it still has none
    new_text = "".join(first + head + rest)
    if text.startswith("\ufeff") and not new_text.startswith("\ufeff"):
        new_text = "\ufeff" + new_text

    # nothing of the old wording may be left where a reader looks for a header
    after = LINE_RE.findall(new_text)[:SCAN_LINES + len(canonical) + 1]
    body = "".join(after[:pos] + after[pos + len(canonical):])
    for phrase in cfg.editions[edition]["forbidden_in_leading_region"]:
        if phrase in body:
            r.status, r.reason = "refused", "wording left after the swap: %r" % phrase
            return r
    # (an identifier inside a string literal, after an escape sequence or a quote, is data)
    if re.search(r"(?<!\\n)(?<![\"'`])SPDX-License-Identifier:", body):
        r.status, r.reason = "refused", "a second SPDX line in the first %d lines" % SCAN_LINES
        return r

    r.new_text = new_text
    r.first_code = _first_code(after, pos + len(canonical))
    if regions:
        r.status = "replace"
        r.variant = " & ".join(variants)
        if any(m.group("spdx") and not m.group("spdx") == cfg.spdx_of(edition) for (_, _, m, _, _) in regions):
            r.reason = "carries the other edition's header"
        elif all(m.group("spdx") == cfg.spdx_of(edition) for (_, _, m, _, _) in regions):
            r.reason = "the edition's header is there but not where or how it is written for this file type"
        else:
            r.reason = "carries a proprietary header of the old wording"
    else:
        r.status = "add"
        r.variant = "(no header)"
        r.reason = "no header"
    return r


def _first_code(lines, start):
    """What stands right after the header, if it is a line some tools want near the top."""
    for l in lines[start: start + 12]:
        st = l.strip()
        if st.startswith("#!["):
            return "crate attribute"
        if re.match(r"""^['"]use (client|server|strict)['"]""", st):
            return "'use …' directive"
        if not st or st.startswith(("//", "/*", "*", "#")):
            continue
        return None
    return None


def _strip_region(info, a, b, s, e, opener_has_text=False):
    """Lines to drop from comment lines a..b so that header lines s..e go and the documentation
    that shares the comment stays, separated as before. Returns (indices, documentation kept?)."""
    before = [k for k in range(a, s) if info[k].content]
    after = [k for k in range(e + 1, b + 1) if info[k].content]
    rm = set(range(s, e + 1))
    has_before = bool(before) or opener_has_text
    lo = (before[-1] + 1) if before else a
    hi = (after[0] - 1) if after else b
    blanks_before = list(range(lo, s))
    blanks_after = list(range(e + 1, hi + 1))
    if has_before and after:
        # keep exactly one separator between the two pieces of documentation
        keep = blanks_before[:1] or blanks_after[:1]
        rm |= set(blanks_before) | set(blanks_after)
        rm -= set(keep)
    else:
        rm |= set(blanks_before) | set(blanks_after)
    return rm, bool(has_before or after)


# --------------------------------------------------------------------------- walking a tree

def walk(tree):
    for d, dirs, files in os.walk(tree):
        for x in sorted(dirs):
            if os.path.islink(os.path.join(d, x)):
                yield os.path.relpath(os.path.join(d, x), tree).replace(os.sep, "/")
        dirs[:] = sorted(x for x in dirs if x != ".git" and not os.path.islink(os.path.join(d, x)))
        for f in sorted(files):
            yield os.path.relpath(os.path.join(d, f), tree).replace(os.sep, "/")


def run(tree, edition, cfg, write=False):
    """Analyse every file. Returns a report dict; with write=True also rewrites the files."""
    rep = {"edition": edition, "tree": os.path.abspath(tree), "source": 0, "ok": [], "add": [], "replace": [],
           "refused": [], "excluded": {}, "non_source": {}, "unclassified": [], "symlinks": [], "variants": {},
           "preamble": [], "first_code": [], "docstrings": [], "excluded_with_wording": [], "written": 0}
    for rel in walk(tree):
        path = os.path.join(tree, rel)
        if os.path.islink(path):
            rep["symlinks"].append(rel)
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        kind, what = classify(rel, cfg, read_head=lambda: data[:4096])
        if kind == "excluded":
            rep["excluded"].setdefault(what, []).append(rel)
            if cfg.raw["syntax_by_extension"].get(os.path.splitext(rel)[1].lower()):
                head = b"\n".join(data.splitlines()[:SCAN_LINES]).decode("utf-8", "replace")
                hit = [p for p in cfg.editions[edition]["forbidden_in_leading_region"] if p in head]
                if hit:
                    rep["excluded_with_wording"].append((rel, what, hit[0]))
            continue
        if kind == "non-source":
            rep["non_source"][what] = rep["non_source"].get(what, 0) + 1
            continue
        if kind == "unclassified":
            rep["unclassified"].append(rel)
            continue
        rep["source"] += 1
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            rep["refused"].append((rel, "not UTF-8", None))
            continue
        r = analyse(text, what, edition, cfg)
        if r.preamble and r.status != "refused":
            rep["preamble"].append((rel, r.preamble))
        if r.first_code and r.status != "refused":
            rep["first_code"].append((rel, r.first_code))
        if r.status == "ok":
            rep["ok"].append(rel)
        elif r.status == "refused":
            rep["refused"].append((rel, r.reason, r.holder))
        else:
            rep[r.status].append((rel, r.variant, r.reason))
            rep["variants"][r.variant] = rep["variants"].get(r.variant, 0) + 1
            if r.docstring:
                rep["docstrings"].append((rel, r.docstring))
            if write:
                with open(path, "wb") as fh:
                    fh.write(r.new_text.encode("utf-8"))
                rep["written"] += 1
    return rep


def _print_list(title, rows, limit):
    if not rows:
        return
    print("\n%s: %d" % (title, len(rows)))
    for row in rows[:limit]:
        print("  " + (row if isinstance(row, str) else "  |  ".join(str(x) for x in row if x is not None)))
    if len(rows) > limit:
        print("  … %d more (use --list-all or --json)" % (len(rows) - limit))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["check", "apply"])
    ap.add_argument("tree")
    ap.add_argument("--edition", required=True, choices=["community", "enterprise"])
    ap.add_argument("--dry-run", action="store_true", help="apply: report what would change, write nothing")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--json", help="also write the full report to this file")
    ap.add_argument("--list-all", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.tree):
        print("not a directory: %s" % a.tree, file=sys.stderr)
        return 2
    cfg = Config(a.config)
    limit = 10 ** 9 if a.list_all else 25
    rep = run(a.tree, a.edition, cfg, write=(a.mode == "apply" and not a.dry_run))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1, sort_keys=True)
    print("%s %s  edition=%s%s" % (a.mode, rep["tree"], a.edition, "  (dry run)" if a.dry_run else ""))
    print("header wording (%s): %s" % (os.path.basename(cfg.path), " / ".join(cfg.editions[a.edition]["lines"])))
    print("source files: %d   excluded: %d   not a source type: %d   unclassified: %d   symlinks: %d" % (
        rep["source"], sum(len(v) for v in rep["excluded"].values()), sum(rep["non_source"].values()),
        len(rep["unclassified"]), len(rep["symlinks"])))
    for rule, files in sorted(rep["excluded"].items()):
        print("  excluded by %-20s %d" % (rule, len(files)))
    if a.mode == "check":
        no_header = [x for x in rep["add"]]
        other = [x for x in rep["replace"] if x[2].startswith(("carries the other", "carries a proprietary"))]
        misplaced = [x for x in rep["replace"] if x not in other]
        individual = [x for x in rep["refused"] if x[1] == "names another copyright holder"]
        unknown = [x for x in rep["refused"] if x not in individual]
        print("carry the %s header, at the top: %d" % (a.edition, len(rep["ok"])))
        _print_list("FAIL  no header at all", no_header, limit)
        _print_list("FAIL  carries the other edition's or the old proprietary header", other, limit)
        _print_list("FAIL  the header is not where it belongs", misplaced, limit)
        _print_list("FAIL  names another copyright holder (never rewritten)", individual, limit)
        _print_list("FAIL  not recognised", unknown, limit)
        _print_list("FAIL  not classified (neither a source type nor a listed non-source type)", rep["unclassified"], limit)
        _print_list("note  excluded files that carry the other edition's wording (their generator or owner must change them)",
                    rep["excluded_with_wording"], limit)
        _print_list("note  header stands after something the language wants first", rep["preamble"], limit)
        _print_list("note  header stands above a crate attribute or a 'use …' directive (comments may precede both)",
                    rep["first_code"], limit)
        failed = bool(no_header or other or misplaced or individual or unknown or rep["unclassified"])
        print("\nRESULT: %s" % ("FAIL" if failed else "PASS"))
        return 1 if failed else 0
    verb = "would change" if a.dry_run else "changed"
    print("%s: %d  (header added: %d, header replaced: %d)   already right: %d   refused: %d" % (
        verb, len(rep["add"]) + len(rep["replace"]), len(rep["add"]), len(rep["replace"]), len(rep["ok"]), len(rep["refused"])))
    print("\nby header variant found:")
    for v, n in sorted(rep["variants"].items(), key=lambda kv: (-kv[1], kv[0])):
        print("  %6d  %s" % (n, v))
    _print_list("refused (not written)", rep["refused"], limit)
    _print_list("not classified (not written)", rep["unclassified"], limit)
    _print_list("module docstrings removed or shortened", rep["docstrings"], limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
