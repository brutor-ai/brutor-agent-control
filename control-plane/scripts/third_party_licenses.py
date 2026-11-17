#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The third-party licences file of a Python image: `THIRD_PARTY_LICENSES`.

Every package the image ships, with its version, its licence as an SPDX
expression and the full text of every licence file it carries (one section
per distinct text). The rule it serves: third-party packages ship
unmodified with their licence texts, in a generated file in each image and in
the repository, listing every dependency with its licence.

Two commands, both reading one configuration file (`--config`, a TOML file
beside this script: the allowed licences, the editions, the per-package
resolutions):

    generate    Run in the image's build stage against the environment the
                image ships (`--site-packages`). `pip-licenses` (pinned, in a
                virtual environment of its own that the image never ships)
                reports each package's declared licence; this script resolves
                it to SPDX, reads every licence file, checks the environment
                is exactly the lock (`constraints`) and that every licence is
                allowed, and writes the file. Any unknown or disallowed
                licence, and any package installed but not locked (or locked
                but not installed), fails with a non-zero exit: the build
                fails.

    regenerate  The repository's copy, from the lock alone: the locked
                packages' Linux wheels (the image's platform) are unpacked into
                a scratch directory and `generate` runs against it. Needs the
                network (PyPI). A package the image builds from source
                (`built_from_source`: no wheel is used for it) is built here
                from its sdist too, and its metadata and licence files, which do
                not depend on the platform, are unpacked beside the wheels.

The script is the same in every repository that uses it (the Control Plane,
the witness), its header apart; what differs is in the configuration. Standard library
only: it runs on the image's interpreter.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path

# ---------------------------------------------------------------------------
# Declared licence strings (Trove classifiers and free-text `License:` fields
# as pip-licenses reports them) and the SPDX identifier each means. The one
# place a name is mapped. A string that is neither here nor an SPDX expression
# is unknown: the build fails until it is resolved here or, when it is
# ambiguous ("BSD License"), per package in the configuration.
# ---------------------------------------------------------------------------
DECLARED_TO_SPDX: dict[str, str] = {
    "Apache 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    "Apache Software License": "Apache-2.0",
    "Apache Software License 2.0": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT-0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "3-Clause BSD License": "BSD-3-Clause",
    "zlib/libpng License": "Zlib",
}

UNKNOWN = "UNKNOWN"
LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)
SPDX_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+:-]+)")
RULE = "=" * 79
THIN = "-" * 79


class LicenceError(Exception):
    """A fault that must stop the build."""


# ---------------------------------------------------------------------------
# SPDX expressions
# ---------------------------------------------------------------------------
def spdx_terms(expression: str) -> list[list[str]]:
    """The expression in disjunctive form: a list of alternatives, each the
    list of terms (`MIT`, `LGPL-3.0-or-later WITH ...`) that must all be met.
    Raises LicenceError if it is not an SPDX expression."""
    tokens: list[str] = []
    pos = 0
    text = expression.strip()
    while pos < len(text):
        m = SPDX_TOKEN.match(text, pos)
        if not m:
            raise LicenceError(f"not an SPDX expression: {expression!r}")
        tokens.append(m.group(1))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if not tokens:
        raise LicenceError(f"not an SPDX expression: {expression!r}")
    at = 0

    def peek() -> str | None:
        return tokens[at] if at < len(tokens) else None

    def take() -> str:
        nonlocal at
        if at >= len(tokens):
            raise LicenceError(f"not an SPDX expression: {expression!r}")
        at += 1
        return tokens[at - 1]

    def is_op(tok: str | None, op: str) -> bool:
        return tok is not None and tok.upper() == op

    def atom() -> list[list[str]]:
        tok = take()
        if tok == "(":
            inner = disjunction()
            if take() != ")":
                raise LicenceError(f"not an SPDX expression: {expression!r}")
            return inner
        if tok == ")" or tok.upper() in ("AND", "OR", "WITH"):
            raise LicenceError(f"not an SPDX expression: {expression!r}")
        if is_op(peek(), "WITH"):
            take()
            exc = take()
            if exc in ("(", ")") or exc.upper() in ("AND", "OR", "WITH"):
                raise LicenceError(f"not an SPDX expression: {expression!r}")
            return [[f"{tok} WITH {exc}"]]
        return [[tok]]

    def conjunction() -> list[list[str]]:
        result = atom()
        while is_op(peek(), "AND"):
            take()
            right = atom()
            result = [a + b for a in result for b in right]
        return result

    def disjunction() -> list[list[str]]:
        result = conjunction()
        while is_op(peek(), "OR"):
            take()
            result = result + conjunction()
        return result

    result = disjunction()
    if at != len(tokens):
        raise LicenceError(f"not an SPDX expression: {expression!r}")
    return result


def is_spdx_expression(text: str) -> bool:
    try:
        spdx_terms(text)
    except LicenceError:
        return False
    return True


def allowed_by(expression: str, allowed: set[str]) -> bool:
    """An expression is allowed when one of its alternatives has every term
    on the allowed list (AND: all of them; OR: one alternative suffices)."""
    return any(all(term in allowed for term in alt) for alt in spdx_terms(expression))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class Override:
    declared: tuple[str, str, str]
    spdx: str
    texts: list[str] = field(default_factory=list)


@dataclass
class Config:
    path: Path
    root: Path
    component: str
    python: str
    allowed: set[str]
    exclude_prefixes: tuple[str, ...]
    pinned_elsewhere: dict[str, str]
    overrides: dict[str, Override]
    editions: dict[str, dict]
    tool_requirements: Path
    wheel_tags: tuple[str, ...]
    # Packages the image compiles from source against the system's libraries.
    built_from_source: tuple[str, ...] = ()


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    overrides = {}
    for name, o in data.get("overrides", {}).items():
        declared = o["declared"]
        if len(declared) != 3:
            raise LicenceError(f"{path}: overrides.{name}.declared is [metadata, classifier, expression]")
        overrides[normalise(name)] = Override(tuple(declared), o["spdx"], list(o.get("texts", [])))
    return Config(
        path=path,
        root=(base / data.get("root", ".")).resolve(),
        component=data["component"],
        python=data["python"],
        allowed=set(data["allowed"]),
        exclude_prefixes=tuple(data.get("exclude_prefixes", [])),
        pinned_elsewhere={normalise(k): v for k, v in data.get("pinned_elsewhere", {}).items()},
        overrides=overrides,
        editions=data["editions"],
        tool_requirements=(base / data["tool_requirements"]).resolve(),
        wheel_tags=tuple(data["wheel_tags"]),
        built_from_source=tuple(normalise(n) for n in data.get("built_from_source", [])),
    )


def read_constraints(paths: list[Path]) -> dict[str, tuple[str, str]]:
    """{normalised name: (name as written, version)} of `name==version` lines."""
    pins: dict[str, tuple[str, str]] = {}
    for p in paths:
        for n, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
            if not m:
                raise LicenceError(f"{p}:{n}: not `name==version`: {raw!r}")
            key = normalise(m.group(1))
            if key in pins:
                raise LicenceError(f"{p}:{n}: {m.group(1)} is pinned twice")
            pins[key] = (m.group(1), m.group(2))
    return pins


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------
@dataclass
class Package:
    name: str
    version: str
    spdx: str
    texts: list[tuple[str, str]]  # (where the text came from, the text)


def clean_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


def licence_files(dist_info: Path, metadata) -> list[Path]:
    """Every licence file a distribution carries: the `License-File` entries
    (in `licenses/` under metadata 2.4, beside METADATA before it), the whole
    `licenses/` directory, and the licence, notice and authors files beside
    METADATA."""
    found: list[Path] = []
    for entry in metadata.get_all("License-File") or []:
        for candidate in (dist_info / "licenses" / entry, dist_info / entry):
            if candidate.is_file():
                found.append(candidate)
                break
    lic_dir = dist_info / "licenses"
    if lic_dir.is_dir():
        found.extend(p for p in sorted(lic_dir.rglob("*")) if p.is_file())
    found.extend(p for p in sorted(dist_info.iterdir()) if p.is_file() and LICENCE_FILE.match(p.name))
    unique: list[Path] = []
    for p in found:
        if p.resolve() not in [u.resolve() for u in unique]:
            unique.append(p)
    return unique


def scan(site_packages: Path) -> dict[str, tuple[str, str, Path, object]]:
    """{normalised name: (name, version, dist-info directory, metadata)}."""
    out = {}
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        meta_file = dist_info / "METADATA"
        if not meta_file.is_file():
            continue
        md = Parser().parsestr(meta_file.read_text(encoding="utf-8", errors="replace"), headersonly=True)
        key = normalise(md["Name"])
        if key in out:
            raise LicenceError(f"{md['Name']} is installed twice in {site_packages}")
        out[key] = (md["Name"], md["Version"], dist_info, md)
    return out


def run_pip_licenses(pip_licenses: str, python: str) -> dict[str, dict]:
    proc = subprocess.run(
        [pip_licenses, "--python", python, "--with-system", "--format=json", "--from=all",
         "--with-license-file", "--no-license-path", "--with-notice-file"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise LicenceError(f"pip-licenses failed: {proc.stderr.strip()}")
    return {normalise(p["Name"]): p for p in json.loads(proc.stdout)}


def pip_licenses_version(pip_licenses: str) -> str:
    out = subprocess.run([pip_licenses, "--version"], capture_output=True, text=True, check=True).stdout
    return out.strip().split()[-1]


def resolve_spdx(name: str, report: dict, cfg: Config) -> tuple[str, list[str]]:
    """The package's SPDX expression and the extra texts its resolution names."""
    declared = (report.get("License-Metadata", UNKNOWN), report.get("License-Classifier", UNKNOWN),
                report.get("License-Expression", UNKNOWN))
    override = cfg.overrides.get(normalise(name))
    if override is not None:
        if tuple(override.declared) != declared:
            raise LicenceError(
                f"{name}: declares {list(declared)}, but its resolution in {cfg.path.name} was made "
                f"for {list(override.declared)}: review the licence and update the resolution")
        spdx_terms(override.spdx)
        return override.spdx, override.texts
    metadata, classifier, expression = declared
    if expression != UNKNOWN:
        if not is_spdx_expression(expression):
            raise LicenceError(f"{name}: License-Expression {expression!r} is not an SPDX expression")
        return expression, []
    candidates = []
    meta = metadata.strip()
    if meta != UNKNOWN and "\n" not in meta:
        if meta in DECLARED_TO_SPDX:
            candidates.append(DECLARED_TO_SPDX[meta])
        elif is_spdx_expression(meta):
            candidates.append(meta)
        else:
            raise LicenceError(f"{name}: unknown licence {meta!r} (map it in DECLARED_TO_SPDX or resolve the package)")
    if classifier != UNKNOWN:
        parts = [c.strip() for c in classifier.split(";") if c.strip()]
        if len(parts) > 1:
            raise LicenceError(f"{name}: several licence classifiers {parts}: resolve the package in {cfg.path.name}")
        if parts[0] not in DECLARED_TO_SPDX:
            raise LicenceError(f"{name}: unknown licence classifier {parts[0]!r}")
        candidates.append(DECLARED_TO_SPDX[parts[0]])
    if not candidates:
        raise LicenceError(f"{name}: declares no licence")
    if len(set(candidates)) > 1:
        raise LicenceError(f"{name}: its declarations disagree ({candidates}): resolve the package in {cfg.path.name}")
    return candidates[0], []


def collect(site_packages: Path, report: dict[str, dict], cfg: Config,
            pins: dict[str, tuple[str, str]]) -> list[Package]:
    installed = scan(site_packages)
    faults: list[str] = []
    if set(installed) != set(report):
        faults.append(f"pip-licenses and the scan of {site_packages} see different packages: "
                      f"{sorted(set(installed) ^ set(report))}")
    packages: list[Package] = []
    for key in sorted(installed):
        name, version, dist_info, md = installed[key]
        if any(key.startswith(normalise(p)) for p in cfg.exclude_prefixes):
            continue
        if key in cfg.pinned_elsewhere:
            pass
        elif key not in pins:
            faults.append(f"{name} {version} is installed but not in the lock")
        elif pins[key][1] != version:
            faults.append(f"{name} is {version}, the lock says {pins[key][1]}")
        try:
            spdx, extra = resolve_spdx(name, report.get(key, {}), cfg)
            if not allowed_by(spdx, cfg.allowed):
                faults.append(f"{name} {version}: {spdx} is not an allowed licence")
        except LicenceError as e:
            faults.append(str(e))
            continue
        texts = [(str(p.relative_to(dist_info)), clean_text(p.read_text(encoding="utf-8", errors="replace")))
                 for p in licence_files(dist_info, md)]
        texts += [(t, clean_text((cfg.path.parent / t).read_text(encoding="utf-8"))) for t in extra]
        texts = [(w, t) for w, t in texts if t]
        if not texts:
            faults.append(f"{name} {version} carries no licence text")
        packages.append(Package(name, version, spdx, texts))
    for key, (name, version) in sorted(pins.items()):
        if key not in installed:
            faults.append(f"{name}=={version} is in the lock but not installed")
    if faults:
        raise LicenceError("\n".join(faults))
    return packages


def render(packages: list[Package], cfg: Config, edition: str, tool: str) -> str:
    ed = cfg.editions[edition]
    lines = [
        f"Third-party licences: {cfg.component} ({ed['title']})",
        "",
        f"Generated by pip-licenses {tool} from {', '.join(ed['constraints'])}; do not edit.",
        f"Regenerate: {ed['regenerate']}",
        "",
        "Every third-party package this image ships, unmodified, with its version,",
        "its licence (SPDX) and below the full text of each licence file it carries",
        "(each distinct text once).",
        "",
    ]
    if cfg.built_from_source:
        lines += [
            f"Built from source in the image, not from a wheel: {', '.join(cfg.built_from_source)}.",
            "They link the system's libraries, which are Debian packages (libpq5 and",
            "what it depends on): the licence of each is its package's copyright file",
            "in the image, /usr/share/doc/<package>/copyright (/usr/share/doc/libpq5/copyright).",
            "",
        ]
    lines += [
        f"Dependencies ({len(packages)})",
        "",
    ]
    lines += [f"{p.name} {p.version} {p.spdx}" for p in packages]
    sections: dict[str, list[Package]] = {}
    for p in packages:
        for _, text in p.texts:
            users = sections.setdefault(text, [])
            if p not in users:
                users.append(p)
    for text, users in sections.items():
        ids: list[str] = []
        for u in users:
            if u.spdx not in ids:
                ids.append(u.spdx)
        lines += ["", RULE, " / ".join(ids),
                  "Used by: " + ", ".join(f"{u.name} {u.version}" for u in users), THIN, text]
    return "\n".join(lines) + "\n"


def generate(cfg: Config, edition: str, site_packages: Path, pip_licenses: str, python: str) -> str:
    if edition not in cfg.editions:
        raise LicenceError(f"no edition {edition!r} in {cfg.path}")
    pins = read_constraints([cfg.root / c for c in cfg.editions[edition]["constraints"]])
    report = run_pip_licenses(pip_licenses, python)
    packages = collect(site_packages, report, cfg, pins)
    return render(packages, cfg, edition, pip_licenses_version(pip_licenses))


# ---------------------------------------------------------------------------
# The repository's copy, from the lock
# ---------------------------------------------------------------------------


def regenerate(cfg: Config, edition: str, arch: str) -> str:
    ed = cfg.editions[edition]
    work = Path(tempfile.mkdtemp(prefix="third-party-licences-"))
    try:
        site = work / "site-packages"
        platforms = [a for t in cfg.wheel_tags for a in ("--platform", f"{t}_{arch}")]
        common = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                  "--no-deps", "--no-compile", "--only-binary=:all:", "--target", str(site),
                  "--python-version", cfg.python, "--implementation", "cp", *platforms]
        pins = read_constraints([cfg.root / c for c in ed["constraints"]])
        # The lock without the packages built from source: those have no
        # Linux wheel to unpack.
        binary = work / "binary-requirements.txt"
        binary.write_text("".join(f"{name}=={version}\n" for key, (name, version) in pins.items()
                                  if key not in cfg.built_from_source), encoding="utf-8")
        reqs = ["-r", str(binary)]
        sources = ed.get("sources", [])
        built: list[str] = []
        if sources:
            wheels = work / "wheels"
            subprocess.run([sys.executable, "-m", "pip", "wheel", "--quiet", "--disable-pip-version-check",
                            "--no-deps", "-w", str(wheels), *sources], check=True)
            built = [str(w) for w in sorted(wheels.glob("*.whl"))]
        subprocess.run([*common, *reqs, *built], check=True)
        compiled = [f"{pins[key][0]}=={pins[key][1]}" for key in cfg.built_from_source if key in pins]
        if compiled:
            # Built here as the image builds them (from the sdist, never a
            # wheel), and unpacked without a platform: what is read of them
            # (metadata, licence files) is the same on every platform.
            native = work / "native-wheels"
            subprocess.run([sys.executable, "-m", "pip", "wheel", "--quiet", "--disable-pip-version-check",
                            "--no-deps", "--no-binary", ",".join(cfg.built_from_source), "-w", str(native),
                            *compiled], check=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "--no-deps", "--no-compile", "--target", str(site),
                            *[str(w) for w in sorted(native.glob("*.whl"))]], check=True)
        tool = work / "tool"
        subprocess.run([sys.executable, "-m", "venv", str(tool)], check=True)
        subprocess.run([str(tool / "bin" / "pip"), "install", "--quiet", "--disable-pip-version-check",
                        "--no-deps", "--require-hashes", "-r", str(cfg.tool_requirements)], check=True)
        # pip-licenses asks the interpreter it is given for its search path;
        # this one answers with the unpacked wheels alone.
        shim = work / "python"
        shim.write_text(f"#!/bin/sh\necho '{site}'\n")
        shim.chmod(0o755)
        return generate(cfg, edition, site, str(tool / "bin" / "pip-licenses"), str(shim))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=here / "third_party_licenses.toml")
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate", help="in the image build: the shipped environment")
    g.add_argument("edition")
    g.add_argument("--site-packages", type=Path, required=True)
    g.add_argument("--pip-licenses", required=True)
    g.add_argument("--python", default=sys.executable)
    g.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("regenerate", help="the repository's copy, from the lock")
    r.add_argument("edition")
    r.add_argument("--arch", default="x86_64", choices=("x86_64", "aarch64"))
    r.add_argument("--check", action="store_true", help="fail if the committed file differs")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "generate":
            args.out.write_text(generate(cfg, args.edition, args.site_packages, args.pip_licenses, args.python),
                                encoding="utf-8")
            return 0
        text = regenerate(cfg, args.edition, args.arch)
        out = cfg.root / cfg.editions[args.edition]["output"]
        if args.check:
            if not out.is_file() or out.read_text(encoding="utf-8") != text:
                print(f"{out} is not current: run `{cfg.editions[args.edition]['regenerate']}`", file=sys.stderr)
                return 1
            return 0
        out.write_text(text, encoding="utf-8")
        return 0
    except LicenceError as e:
        print(f"third-party licences: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
