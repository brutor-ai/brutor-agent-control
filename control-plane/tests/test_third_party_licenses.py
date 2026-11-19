# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The third-party licences of the Community image. The rule: third-party
packages ship unmodified with their licence texts, in a generated file in each
image and in the repository.

Held here:

* the guard (`scripts/third_party_licenses.py`): an unknown or disallowed
  licence, a package installed but not locked, a package locked but not
  installed and a package without a licence text each fail the generation,
  and so the image build; MPL-2.0 (certifi) and psycopg2's LGPL with its
  OpenSSL exception pass, on the allowed list by name;
* the repository's `THIRD_PARTY_LICENSES` is current: its packages and
  versions are exactly the lock (`constraints.txt`), its generator is the
  pinned one (the full text is regenerated and compared on Linux by CI, job
  `third-party-licences`);
* the lock satisfies `pyproject.toml`;
* the Community `Dockerfile` installs the lock, generates the file in its
  build stage with the pinned pip-licenses outside the shipped environment,
  and ships it at `/usr/share/licenses/brutor/THIRD_PARTY_LICENSES` and
  beside the application, with a label naming it.

The Enterprise image's half is `enterprise/tests/test_third_party_licenses.py`.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

from tests import workflows
from tests.packaging import COMMUNITY_PROJECT, REPO, project

SCRIPT = REPO / "scripts" / "third_party_licenses.py"
CONFIG = REPO / "scripts" / "third_party_licenses.toml"
IMAGE_PATH = "/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"


def _load():
    spec = importlib.util.spec_from_file_location("third_party_licenses", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["third_party_licenses"] = module
    spec.loader.exec_module(module)
    return module


tpl = _load()
CFG = tpl.load_config(CONFIG)


# ---------------------------------------------------------------------------
# Helpers: a site-packages directory and what pip-licenses reports for it
# ---------------------------------------------------------------------------
def _dist(site: Path, name: str, version: str, *, expression=None, licence=None,
          classifiers=(), text="Licence text of {name}.") -> None:
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if expression:
        lines.append(f"License-Expression: {expression}")
    if licence:
        lines.append(f"License: {licence}")
    lines += [f"Classifier: License :: {c}" for c in classifiers]
    if text is not None:
        lines.append("License-File: LICENSE")
        (info / "licenses").mkdir()
        (info / "licenses" / "LICENSE").write_text(text.format(name=name), "utf-8")
    (info / "METADATA").write_text("\n".join(lines) + "\n\n", "utf-8")


def _report(name: str, *, expression="UNKNOWN", licence="UNKNOWN", classifier="UNKNOWN") -> dict:
    return {"Name": name, "License-Expression": expression, "License-Metadata": licence,
            "License-Classifier": classifier}


def _collect(tmp_path, dists, pins=None, cfg=CFG):
    site = tmp_path / "site-packages"
    site.mkdir(exist_ok=True)
    report = {}
    for name, version, kw, rep in dists:
        _dist(site, name, version, **kw)
        report[tpl.normalise(name)] = _report(name, **rep)
    if pins is None:
        pins = {tpl.normalise(n): (n, v) for n, v, _, _ in dists}
    return tpl.collect(site, report, cfg, pins)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("expression, allowed", [
    ("MIT", True),
    ("Apache-2.0 OR BSD-3-Clause", True),
    ("MIT OR GPL-3.0-only", True),            # one allowed alternative suffices
    ("MIT AND GPL-3.0-only", False),          # every term of an AND must be allowed
    ("(MIT OR GPL-3.0-only) AND Zlib", True),
    ("GPL-3.0-only", False),
    ("AGPL-3.0-or-later", False),
    ("LicenseRef-Proprietary", False),
    ("MPL-2.0", True),                        # certifi, allowed by name
    ("LGPL-3.0-or-later WITH AdditionRef-psycopg2-OpenSSL-exception", True),
    ("LGPL-3.0-or-later", False),             # the LGPL is allowed with psycopg2's exception only
    ("LGPL-2.1-or-later", False),
])
def test_the_guard_allows_exactly_the_allowed_list(expression, allowed):
    assert tpl.allowed_by(expression, CFG.allowed) is allowed


@pytest.mark.parametrize("text", ["", "MIT AND", "(MIT", "MIT License", "MIT OR OR BSD-3-Clause", "WITH MIT"])
def test_what_is_not_an_spdx_expression_is_refused(text):
    with pytest.raises(tpl.LicenceError):
        tpl.spdx_terms(text)


def test_the_weak_copyleft_licences_are_allowed_by_name_with_the_reason_written_out():
    text = CONFIG.read_text("utf-8")
    assert {"MPL-2.0", "LGPL-3.0-or-later WITH AdditionRef-psycopg2-OpenSSL-exception"} <= CFG.allowed
    # Each by its exact name, after the comment that says why: the packages
    # ship unmodified with their licence texts.
    for entry in ('"MPL-2.0"', '"LGPL-3.0-or-later WITH AdditionRef-psycopg2-OpenSSL-exception"'):
        before = text[: text.index(entry)]
        reason = before[before.index("# Weak copyleft, allowed by name"):]
        assert "unmodified" in reason and "licence texts" in reason, entry


def test_an_unknown_licence_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="unknown licence"):
        _collect(tmp_path, [("odd", "1.0", {"licence": "Some Licence"}, {"licence": "Some Licence"})])


def test_an_unknown_classifier_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="unknown licence classifier"):
        _collect(tmp_path, [("odd", "1.0", {"classifiers": ["Free To Use"]}, {"classifier": "Free To Use"})])


def test_a_disallowed_licence_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="GPL-3.0-only is not an allowed licence"):
        _collect(tmp_path, [("copyleft", "1.0", {"expression": "GPL-3.0-only"}, {"expression": "GPL-3.0-only"})])


def test_a_package_that_declares_no_licence_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="declares no licence"):
        _collect(tmp_path, [("bare", "1.0", {}, {})])


def test_several_classifiers_need_a_resolution(tmp_path):
    rep = {"classifier": "Apache Software License; MIT License"}
    with pytest.raises(tpl.LicenceError, match="several licence classifiers"):
        _collect(tmp_path, [("dual", "1.0", {}, rep)])


def test_a_resolution_made_for_other_declarations_fails(tmp_path):
    # psycopg2's resolution is for what it declares now; a release
    # that declares something else is reviewed again.
    rep = {"licence": "LGPL", "classifier": "GNU Lesser General Public License v3 (LGPLv3)"}
    with pytest.raises(tpl.LicenceError, match="review the licence"):
        _collect(tmp_path, [("psycopg2", "9.9", {}, rep)])


def test_psycopg2_and_certifi_pass_and_psycopg2_ships_the_lgpl_and_gpl_texts(tmp_path):
    over = CFG.overrides["psycopg2"]
    rep = dict(zip(("licence", "classifier", "expression"), over.declared))
    packages = _collect(tmp_path, [
        ("psycopg2", "2.9.13", {}, rep),
        ("certifi", "2026.10.23", {"licence": "MPL-2.0"}, {"licence": "MPL-2.0",
                                                           "classifier": "Mozilla Public License 2.0 (MPL 2.0)"}),
    ])
    by_name = {p.name: p for p in packages}
    assert by_name["certifi"].spdx == "MPL-2.0"
    assert by_name["psycopg2"].spdx == "LGPL-3.0-or-later WITH AdditionRef-psycopg2-OpenSSL-exception"
    texts = [t for _, t in by_name["psycopg2"].texts]
    assert any(t.lstrip().startswith("GNU LESSER GENERAL PUBLIC LICENSE") for t in texts)
    assert any(t.lstrip().startswith("GNU GENERAL PUBLIC LICENSE") for t in texts)


def test_a_package_installed_but_not_locked_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="installed but not in the lock"):
        _collect(tmp_path, [("loose", "1.0", {"expression": "MIT"}, {"expression": "MIT"})], pins={})


def test_a_package_at_another_version_than_the_lock_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="the lock says 2.0"):
        _collect(tmp_path, [("moved", "1.0", {"expression": "MIT"}, {"expression": "MIT"})],
                 pins={"moved": ("moved", "2.0")})


def test_a_package_locked_but_not_installed_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="in the lock but not installed"):
        _collect(tmp_path, [("here", "1.0", {"expression": "MIT"}, {"expression": "MIT"})],
                 pins={"here": ("here", "1.0"), "gone": ("gone", "1.0")})


def test_a_package_without_a_licence_text_fails(tmp_path):
    with pytest.raises(tpl.LicenceError, match="carries no licence text"):
        _collect(tmp_path, [("textless", "1.0", {"expression": "MIT", "text": None}, {"expression": "MIT"})])


def test_our_own_packages_are_not_listed(tmp_path):
    packages = _collect(tmp_path, [
        ("brutor-agent-control-plane", "0.11.32", {}, {}),
        ("third", "1.0", {"expression": "MIT"}, {"expression": "MIT"}),
    ], pins={"third": ("third", "1.0")})
    assert [p.name for p in packages] == ["third"]


def test_the_file_lists_every_package_and_each_distinct_text_once(tmp_path):
    same = {"text": "The same MIT text."}
    packages = _collect(tmp_path, [
        ("alpha", "1.0", {"expression": "MIT", **same}, {"expression": "MIT"}),
        ("beta", "2.0", {"expression": "MIT", **same}, {"expression": "MIT"}),
        ("gamma", "3.0", {"licence": "Apache-2.0"}, {"licence": "Apache-2.0", "classifier": "Apache Software License"}),
    ])
    text = tpl.render(packages, CFG, "community", "5.5.5")
    assert "Dependencies (3)\n\nalpha 1.0 MIT\nbeta 2.0 MIT\ngamma 3.0 Apache-2.0\n" in text
    assert text.count("The same MIT text.") == 1
    assert "Used by: alpha 1.0, beta 2.0\n" in text
    assert "Used by: gamma 3.0\n" in text


# ---------------------------------------------------------------------------
# The repository's file is current against the lock
# ---------------------------------------------------------------------------
def committed_packages(path: Path) -> dict[str, tuple[str, str]]:
    text = path.read_text("utf-8")
    m = re.search(r"^Dependencies \((\d+)\)\n\n(.*?)\n\n", text, re.M | re.S)
    assert m, f"{path.name} has no dependency list"
    rows = [line.split(" ", 2) for line in m.group(2).splitlines()]
    assert len(rows) == int(m.group(1))
    return {tpl.normalise(name): (version, spdx) for name, version, spdx in rows}


def pinned_tool_version() -> str:
    m = re.search(r"^pip-licenses==(\S+)", (REPO / "scripts" / "third_party_licenses_tool.txt").read_text("utf-8"), re.M)
    assert m
    return m.group(1)


def test_the_committed_file_lists_exactly_the_community_lock():
    path = REPO / CFG.editions["community"]["output"]
    pins = tpl.read_constraints([REPO / c for c in CFG.editions["community"]["constraints"]])
    listed = committed_packages(path)
    assert {k: v for k, (v, _) in listed.items()} == {k: v for k, (_, v) in pins.items()}
    for spdx in {s for _, s in listed.values()}:
        assert tpl.allowed_by(spdx, CFG.allowed), spdx
    head = path.read_text("utf-8").splitlines()[:4]
    assert head[0] == "Third-party licences: Brutor Agent Control Control Plane (Community Edition)"
    assert head[2] == f"Generated by pip-licenses {pinned_tool_version()} from constraints.txt; do not edit."
    assert head[3] == "Regenerate: python scripts/third_party_licenses.py regenerate community"


def test_the_lock_satisfies_the_package_definition():
    pins = tpl.read_constraints([REPO / "constraints.txt"])
    for dep in project(COMMUNITY_PROJECT)["dependencies"]:
        req = Requirement(dep)
        key = tpl.normalise(req.name)
        assert key in pins, f"{req.name} is not in constraints.txt"
        assert req.specifier.contains(pins[key][1], prereleases=True), f"{req.name}=={pins[key][1]} is outside {req.specifier}"
    # The build tools the image copies in are pinned too.
    assert {"pip", "setuptools", "wheel"} <= set(pins)


# ---------------------------------------------------------------------------
# The lock is what the images install on Linux, on each architecture they are
# built for. A lock frozen on macOS arm64 missed greenlet, which SQLAlchemy
# needs on x86_64 and aarch64 only, and the image's licence step refused the
# build ("greenlet ... is installed but not in the lock"). The resolutions are
# recorded in python:3.13-slim by `scripts/record_linux_resolution.sh`
# (Docker, the network), and the lock is held equal to each of them here.
# ---------------------------------------------------------------------------
IMAGE_ARCHITECTURES = ("amd64", "arm64")


def linux_resolution(path: Path) -> dict[str, str]:
    """{normalised name: version} of a recorded `pip freeze --all`."""
    out: dict[str, str] = {}
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line)
        assert m, f"{path}: not `name==version`: {raw!r}"
        out[tpl.normalise(m.group(1))] = m.group(2)
    return out


def lock_faults(pins: dict[str, tuple[str, str]], resolved: dict[str, str]) -> list[str]:
    """How a lock differs from what a Linux install resolved to (our own
    packages aside)."""
    ours = tuple(tpl.normalise(p) for p in CFG.exclude_prefixes)
    resolved = {k: v for k, v in resolved.items() if not k.startswith(ours)}
    faults = [f"{k} {v} is installed on Linux but not in the lock" for k, v in sorted(resolved.items()) if k not in pins]
    faults += [f"{k} is {v} on Linux, the lock says {pins[k][1]}" for k, v in sorted(resolved.items()) if k in pins and pins[k][1] != v]
    faults += [f"{name}=={version} is in the lock but not installed on Linux" for k, (name, version) in sorted(pins.items()) if k not in resolved]
    return faults


def hold_the_lock_to_linux(directory: Path, edition: str) -> None:
    pins = tpl.read_constraints([REPO / c for c in CFG.editions[edition]["constraints"]])
    assert sorted(p.stem for p in directory.glob("*.txt")) == sorted(IMAGE_ARCHITECTURES)
    for arch in IMAGE_ARCHITECTURES:
        path = directory / f"{arch}.txt"
        head = path.read_text("utf-8").splitlines()[0]
        assert f"the {edition} image's install resolves to on linux/{arch} (python:{CFG.python}-slim, Python {CFG.python}." in head, head
        assert lock_faults(pins, linux_resolution(path)) == [], arch


def test_the_community_lock_is_what_the_image_installs_on_each_linux_architecture():
    hold_the_lock_to_linux(REPO / "tests" / "fixtures" / "linux_resolution", "community")


def test_the_lock_check_catches_a_package_only_linux_installs_and_any_other_difference():
    pins = {"sqlalchemy": ("SQLAlchemy", "2.0.57"), "six": ("six", "1.0")}
    resolved = {"sqlalchemy": "2.0.57", "greenlet": "3.5.6", "six": "1.1", "brutor-agent-control-plane": "1.0.0"}
    assert lock_faults(pins, resolved) == [
        "greenlet 3.5.6 is installed on Linux but not in the lock",
        "six is 1.1 on Linux, the lock says 1.0",
    ]
    assert lock_faults(pins, {"sqlalchemy": "2.0.57"}) == ["six==1.0 is in the lock but not installed on Linux"]


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------
def instructions(dockerfile: str) -> list[str]:
    lines, current = [], ""
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current += line.rstrip("\\") + " "
        if not line.endswith("\\"):
            lines.append(re.sub(r"\s+", " ", current.strip()))
            current = ""
    return lines


def stages(dockerfile: str) -> list[list[str]]:
    out: list[list[str]] = []
    for ins in instructions(dockerfile):
        if ins.startswith("FROM "):
            out.append([])
        out[-1].append(ins)
    return out


def hold_the_image(dockerfile: str, edition: str, constraints: list[str]) -> None:
    builder, final = stages(dockerfile)[0], stages(dockerfile)[-1]
    build = "\n".join(builder)
    # The lock is installed: every pip install of the builder names it.
    flags = " ".join(f"-c {c}" for c in constraints)
    installs = [i for i in builder if i.startswith("RUN ") and "pip install" in i]
    for ins in installs:
        for part in ins.split("&&"):
            if "pip install" in part and "/opt/pip-licenses/" not in part:
                assert flags in part, part
    # The generator: pinned, with hashes, in a virtual environment of its own;
    # it runs on the environment the final stage copies.
    assert "python -m venv /opt/pip-licenses" in build
    assert ("/opt/pip-licenses/bin/pip install --no-cache-dir --no-deps --require-hashes "
            "-r scripts/third_party_licenses_tool.txt") in build
    assert (f"python scripts/third_party_licenses.py generate {edition} "
            "--site-packages /usr/local/lib/python3.13/site-packages "
            "--pip-licenses /opt/pip-licenses/bin/pip-licenses --out /build/THIRD_PARTY_LICENSES") in build
    assert build.index("pip-licenses --out") > max(build.index(i) for i in installs if "-e ." in i)
    shipped = "\n".join(final)
    assert "/opt/pip-licenses" not in shipped
    assert "COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages" in shipped
    assert f"COPY --from=builder /build/THIRD_PARTY_LICENSES {IMAGE_PATH}" in shipped
    copy_app = next(i for i, ins in enumerate(final) if ins.startswith("COPY --chown=brutor:brutor . ."))
    beside = next(i for i, ins in enumerate(final) if ins == "COPY --from=builder /build/THIRD_PARTY_LICENSES /app/THIRD_PARTY_LICENSES")
    assert beside > copy_app
    assert f'LABEL ai.brutor.third-party-licenses="{IMAGE_PATH}"' in shipped


def test_the_community_image_generates_and_ships_its_licences():
    hold_the_image((REPO / "Dockerfile").read_text("utf-8"), "community", ["constraints.txt"])


def test_the_build_context_holds_the_generator_but_not_the_committed_file():
    ignored = [line.strip() for line in (REPO / ".dockerignore").read_text("utf-8").splitlines()]
    # The image's file is the one its build generates.
    assert "**/THIRD_PARTY_LICENSES" in ignored
    assert not any(p in ignored for p in ("scripts/", "scripts", "constraints.txt"))


def test_ci_regenerates_both_files_on_linux_and_fails_on_a_difference():
    """This repository's CI regenerates both editions' files; the public
    repository's (the root's control plane workflow, `tests/workflows.py`)
    the Community one, the only one it has."""
    workflow = yaml.safe_load(workflows.workflow_text("ci.yml"))
    job = workflow["jobs"]["third-party-licences"]
    assert job["runs-on"] == "ubuntu-latest"
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "python scripts/third_party_licenses.py regenerate community --check" in runs
    if workflows.layout() == workflows.REPOSITORY:
        assert "python scripts/third_party_licenses.py regenerate enterprise --check" in runs
    setup = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert str(setup["with"]["python-version"]) == CFG.python


# ---------------------------------------------------------------------------
# The PostgreSQL driver is built from source against Debian's libpq, because
# third-party packages ship unmodified with their licence texts, in each image
# and in the repository: the psycopg2-binary wheel bundles libpq, OpenSSL,
# Kerberos and others without their licence texts. Built from source, the
# native libraries are the Debian packages' (`libpq5` and what it depends
# on), whose copyright files the image ships under /usr/share/doc.
# ---------------------------------------------------------------------------
NATIVE_NOTE = "/usr/share/doc/libpq5/copyright"


def test_the_driver_is_psycopg2_from_source_not_the_binary_wheel():
    names = {tpl.normalise(Requirement(d).name) for d in project(COMMUNITY_PROJECT)["dependencies"]}
    assert "psycopg2" in names and "psycopg2-binary" not in names
    pins = tpl.read_constraints([REPO / "constraints.txt"])
    assert "psycopg2" in pins and "psycopg2-binary" not in pins
    assert CFG.built_from_source == ("psycopg2",)
    assert "psycopg2" in CFG.overrides and "psycopg2-binary" not in CFG.overrides
    assert CFG.overrides["psycopg2"].spdx == "LGPL-3.0-or-later WITH AdditionRef-psycopg2-OpenSSL-exception"


def test_the_file_says_where_the_native_libraries_licences_are(tmp_path):
    packages = _collect(tmp_path, [("alpha", "1.0", {"expression": "MIT"}, {"expression": "MIT"})])
    text = tpl.render(packages, CFG, "community", "5.5.5")
    assert "psycopg2" in text.split("Dependencies (")[0] and NATIVE_NOTE in text


def test_regenerate_builds_the_source_packages_and_installs_the_rest_as_linux_wheels(monkeypatch):
    calls: list[list[str]] = []

    def run(command, *args, **kwargs):
        calls.append([str(part) for part in command])
        if "-r" in command:
            requirements = Path(command[command.index("-r") + 1]).read_text("utf-8")
            calls[-1].append("REQUIREMENTS:" + requirements)
        if command[1:4] == ["-m", "pip", "wheel"]:
            out = Path(command[command.index("-w") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "psycopg2-2.9.13-cp313-cp313-built.whl").write_text("")

        class Done:
            returncode = 0
            stdout = ""
        return Done()

    monkeypatch.setattr(tpl.subprocess, "run", run)
    monkeypatch.setattr(tpl, "generate", lambda *a, **k: "generated")
    assert tpl.regenerate(CFG, "community", "x86_64") == "generated"
    binary = next(c for c in calls if "--only-binary=:all:" in c)
    listed = next(part for part in binary if part.startswith("REQUIREMENTS:"))
    assert "psycopg2==" not in listed and "fastapi==" in listed
    pin = tpl.read_constraints([REPO / "constraints.txt"])["psycopg2"][1]
    built = next(c for c in calls if c[1:4] == ["-m", "pip", "wheel"])
    assert "--no-binary" in built and f"psycopg2=={pin}" in built
    unpacked = next(c for c in calls if any(part.endswith("-built.whl") for part in c))
    assert "--platform" not in unpacked and "--only-binary=:all:" not in unpacked and "--target" in unpacked


def hold_the_driver_build(dockerfile: str) -> None:
    """The builder compiles psycopg2 against Debian's libpq (`libpq-dev`,
    `gcc`), never from a wheel, and checks the result links the system
    library; the final stage has the run-time library only."""
    builder, final = stages(dockerfile)[0], stages(dockerfile)[-1]
    build, shipped = "\n".join(builder), "\n".join(final)
    apt = next(i for i in builder if "apt-get install" in i)
    assert "libpq-dev" in apt and "gcc" in apt
    package_install = next(i for i in builder if "pip install --no-cache-dir -e . " in i)
    assert "--no-binary psycopg2" in package_install
    assert "libpq.so.5" in build and "psycopg2" in build.split("libpq.so.5")[0].rsplit("RUN ", 1)[-1]
    runtime_apt = next(i for i in final if "apt-get install" in i)
    assert "libpq5" in runtime_apt
    for build_only in ("libpq-dev", "gcc", "g++"):
        assert build_only not in shipped, build_only


def test_the_community_image_builds_the_driver_from_source():
    hold_the_driver_build((REPO / "Dockerfile").read_text("utf-8"))
