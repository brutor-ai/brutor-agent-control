# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The witness image's third-party licences. The rule: third-party packages ship
unmodified with their licence texts, in a generated file in each image and in
the repository, listing every dependency with its licence.

Held here (standard library and pytest only; the workflow runs it,
regenerates the file on Linux and compares the built image's copy with the
repository's: `.github/workflows/witness-image.yml` of the repository that
holds the witness at `trial_bundle/witness`, or `.github/workflows/witness.yml`
of the monorepo that holds it at `witness`):

* the guard (`third_party_licenses.py`): an unknown or disallowed licence and
  a package installed but not locked fail; MPL-2.0 (certifi) and
  LGPL-3.0-only (psycopg) pass, on the allowed list by name;
* `THIRD_PARTY_LICENSES` lists exactly `constraints.txt` and capsule-anchor,
  with the pinned generator;
* the Dockerfile generates it in its build stage, outside the shipped
  environment, and ships it where its label says;
* the PostgreSQL driver's C extension (psycopg-c) is compiled in the build
  stage against Debian's libpq and the build checks that it links the
  system's libpq: no wheel that bundles native libraries without their
  licence texts (psycopg-binary) is installed, and the runtime has libpq5;
* the script is the Control Plane's, its header apart.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

WITNESS = Path(__file__).resolve().parents[1]
# The layouts the witness directory ships in: the workflow that runs this file,
# and the witness's directory, both relative to the repository's root (this
# repository: trial_bundle/witness; the public repository: witness, its own
# component).
LAYOUTS = (
    (".github/workflows/witness-image.yml", "trial_bundle/witness"),
    (".github/workflows/witness.yml", "witness"),
)


def repositories_of(witness: Path) -> list[tuple[Path, str, str]]:
    """(repository root, workflow, witness directory) of each layout whose witness directory
    the path `witness` ends with: the root is as many directories up as that directory is deep."""
    out = []
    for workflow, directory in LAYOUTS:
        parts = Path(directory).parts
        if witness.parts[-len(parts):] == parts:
            out.append((witness.parents[len(parts) - 1], workflow, directory))
    return out
IMAGE_PATH = "/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"
# How the file says it is made: from the witness directory, whatever directory holds it.
REGENERATE = "python third_party_licenses.py regenerate community (in the witness directory)"


def _load():
    spec = importlib.util.spec_from_file_location("witness_third_party_licenses", WITNESS / "third_party_licenses.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tpl = _load()
CFG = tpl.load_config(WITNESS / "third_party_licenses.toml")


def _site(tmp_path, name, version, expression):
    info = tmp_path / f"{name}-{version}.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "licenses" / "LICENSE").write_text(f"Licence of {name}.", "utf-8")
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense-Expression: {expression}\n"
        "License-File: LICENSE\n\n", "utf-8")
    report = {tpl.normalise(name): {"Name": name, "License-Expression": expression,
                                    "License-Metadata": "UNKNOWN", "License-Classifier": "UNKNOWN"}}
    return report


@pytest.mark.parametrize("expression, allowed", [
    ("MIT", True),
    ("MPL-2.0", True),
    ("LGPL-3.0-only", True),
    ("PSF-2.0", True),
    ("MIT OR Apache-2.0", True),
    ("GPL-3.0-only", False),
    ("LGPL-3.0-or-later", False),
    ("MIT AND AGPL-3.0-only", False),
])
def test_the_guard_allows_exactly_the_allowed_list(expression, allowed):
    assert tpl.allowed_by(expression, CFG.allowed) is allowed


def test_the_weak_copyleft_licences_are_allowed_by_name_with_the_reason_written_out():
    text = (WITNESS / "third_party_licenses.toml").read_text("utf-8")
    for entry in ('"MPL-2.0"', '"LGPL-3.0-only"'):
        before = text[: text.index(entry)]
        reason = before[before.index("# Weak copyleft, allowed by name"):]
        assert "unmodified" in reason and "licence texts" in reason, entry


def test_a_disallowed_licence_fails(tmp_path):
    report = _site(tmp_path, "copyleft", "1.0", "GPL-3.0-only")
    with pytest.raises(tpl.LicenceError, match="not an allowed licence"):
        tpl.collect(tmp_path, report, CFG, {"copyleft": ("copyleft", "1.0")})


def test_an_unknown_licence_fails(tmp_path):
    report = _site(tmp_path, "odd", "1.0", "MIT")
    report["odd"].update({"License-Expression": "UNKNOWN", "License-Metadata": "Free for all"})
    with pytest.raises(tpl.LicenceError, match="unknown licence"):
        tpl.collect(tmp_path, report, CFG, {"odd": ("odd", "1.0")})


def test_a_package_installed_but_not_locked_fails(tmp_path):
    report = _site(tmp_path, "loose", "1.0", "MIT")
    with pytest.raises(tpl.LicenceError, match="installed but not in the lock"):
        tpl.collect(tmp_path, report, CFG, {})


def test_capsule_anchor_is_pinned_by_the_dockerfiles_commit():
    dockerfile = (WITNESS / "Dockerfile").read_text("utf-8")
    commit = re.search(r"^ARG CAPSULE_ANCHOR_COMMIT=([0-9a-f]{40})$", dockerfile, re.M).group(1)
    assert set(CFG.pinned_elsewhere) == {"capsule-anchor"}
    # Without its [postgres] extra (psycopg[binary]): the image installs the
    # driver itself, built from source.
    assert CFG.editions["community"]["sources"] == [
        f"capsule-anchor @ git+https://github.com/action-state-group/capsule-anchor.git@{commit}"]


def committed_packages() -> dict[str, str]:
    text = (WITNESS / "THIRD_PARTY_LICENSES").read_text("utf-8")
    m = re.search(r"^Dependencies \((\d+)\)\n\n(.*?)\n\n", text, re.M | re.S)
    assert m
    rows = [line.split(" ", 2) for line in m.group(2).splitlines()]
    assert len(rows) == int(m.group(1))
    for _, _, spdx in rows:
        assert tpl.allowed_by(spdx, CFG.allowed), spdx
    return {tpl.normalise(name): version for name, version, _ in rows}


def test_no_shipped_file_of_the_witness_names_the_directory_that_holds_it():
    """The witness directory sits in more than one repository layout; what it says of itself
    is relative to its own directory, never a path from a repository root."""
    for name in ("THIRD_PARTY_LICENSES", "third_party_licenses.toml", "Dockerfile"):
        text = (WITNESS / name).read_text("utf-8")
        assert "witness/third_party_licenses.py" not in text, name


def test_the_committed_file_lists_exactly_the_lock_and_capsule_anchor():
    pins = tpl.read_constraints([WITNESS / "constraints.txt"])
    listed = committed_packages()
    assert "capsule-anchor" in listed and "pip" in pins
    assert {k: v for k, v in listed.items() if k != "capsule-anchor"} == {k: v for k, (_, v) in pins.items()}
    tool = re.search(r"^pip-licenses==(\S+)", (WITNESS / "third_party_licenses_tool.txt").read_text("utf-8"), re.M)
    head = (WITNESS / "THIRD_PARTY_LICENSES").read_text("utf-8").splitlines()[:4]
    assert head[0] == "Third-party licences: Brutor Agent Control Witness (Community Edition)"
    assert head[2] == f"Generated by pip-licenses {tool.group(1)} from constraints.txt; do not edit."
    # Relative to the witness directory: the file ships in more than one layout.
    assert head[3] == f"Regenerate: {REGENERATE}"
    assert CFG.editions["community"]["regenerate"] == REGENERATE


def _instructions(text: str) -> list[str]:
    lines, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current += line.rstrip("\\") + " "
        if not line.endswith("\\"):
            lines.append(re.sub(r"\s+", " ", current.strip()))
            current = ""
    return lines


def test_the_image_generates_and_ships_its_licences():
    instructions = _instructions((WITNESS / "Dockerfile").read_text("utf-8"))
    last = max(i for i, ins in enumerate(instructions) if ins.startswith("FROM "))
    build, final = "\n".join(instructions[:last]), "\n".join(instructions[last:])
    assert "/opt/witness/bin/pip install -c /src/constraints.txt --upgrade pip" in build
    assert "python -m venv /opt/pip-licenses" in build
    assert ("/opt/pip-licenses/bin/pip install --no-deps --require-hashes -r /src/third_party_licenses_tool.txt"
            in build)
    assert ("python /src/third_party_licenses.py generate community "
            "--site-packages /opt/witness/lib/python3.12/site-packages "
            "--pip-licenses /opt/pip-licenses/bin/pip-licenses --python /opt/witness/bin/python "
            "--out /src/THIRD_PARTY_LICENSES") in build
    assert build.index("generate community") > build.index('"/src/capsule-anchor"')
    assert "/opt/pip-licenses" not in final
    assert f"COPY --from=build /src/THIRD_PARTY_LICENSES {IMAGE_PATH}" in final
    assert "COPY --from=build /src/THIRD_PARTY_LICENSES /opt/witness/THIRD_PARTY_LICENSES" in final
    assert f'LABEL ai.brutor.third-party-licenses="{IMAGE_PATH}"' in final
    assert CFG.python in (WITNESS / "Dockerfile").read_text("utf-8").split("ARG PYTHON_IMAGE=python:", 1)[1][:6]


def workflow_of(witness: Path) -> tuple[Path, str]:
    """The workflow that runs the witness's tests and where it says the witness is: the
    layout whose workflow is there and whose witness directory is `witness`. Neither: a
    failure, never a skip (a check of a workflow that is not read passes on nothing)."""
    looked = []
    for repo, workflow, directory in repositories_of(witness):
        path = repo / workflow
        looked.append(f"{path} (with the witness at {directory})")
        if path.is_file():
            return path, directory
    looked += [f"{workflow} (with the witness at {directory})" for workflow, directory in LAYOUTS
               if not any(d == directory for _, _, d in repositories_of(witness))]
    pytest.fail("no workflow runs the witness's tests; looked for " + " and ".join(looked))


def test_the_workflow_is_found_in_either_layout_and_its_absence_fails(tmp_path):
    for workflow, directory in LAYOUTS:
        repo = tmp_path / ("repo-" + directory.replace("/", "-"))
        witness = repo / directory
        witness.mkdir(parents=True)
        assert pytest.raises(pytest.fail.Exception, workflow_of, witness).match("no workflow runs")
        (repo / workflow).parent.mkdir(parents=True)
        (repo / workflow).write_text("on: push\n", "utf-8")
        assert workflow_of(witness) == (repo / workflow, directory)
    # One layout's workflow beside the other layout's directory is neither.
    private, public = LAYOUTS
    repo = tmp_path / "mixed"
    (repo / public[1]).mkdir(parents=True)
    (repo / private[0]).parent.mkdir(parents=True)
    (repo / private[0]).write_text("on: push\n", "utf-8")
    with pytest.raises(pytest.fail.Exception, match="no workflow runs"):
        workflow_of(repo / public[1])


def test_the_workflow_regenerates_the_file_and_compares_the_images_copy():
    path, directory = workflow_of(WITNESS)
    workflow = path.read_text("utf-8")
    assert f"python {directory}/third_party_licenses.py regenerate community --check" in workflow
    assert f"pytest -q {directory}/tests" in workflow
    assert (f"docker run --rm --entrypoint cat brutor-agent-control-witness:ci {IMAGE_PATH}"
            f" | diff - {directory}/THIRD_PARTY_LICENSES") in workflow


def test_the_postgresql_driver_is_built_from_source_against_debians_libpq():
    instructions = _instructions((WITNESS / "Dockerfile").read_text("utf-8"))
    last = max(i for i, ins in enumerate(instructions) if ins.startswith("FROM "))
    build, final = "\n".join(instructions[:last]), "\n".join(instructions[last:])
    # Build dependencies in the build stage only.
    assert re.search(r"apt-get install -y --no-install-recommends [^\n]*\bgcc\b[^\n]*\blibpq-dev\b", build)
    assert "libpq-dev" not in final and "gcc" not in final
    # capsule-anchor without its [postgres] extra (psycopg[binary]); the
    # driver with its C extension, compiled here, never a wheel.
    assert ('/opt/witness/bin/pip install -c /src/constraints.txt --no-binary psycopg-c '
            '"/src/capsule-anchor" "psycopg[c]"') in build
    assert "capsule-anchor[postgres]" not in build
    # The build fails unless the extension links the system's libpq and no
    # bundled copy or binary driver is there.
    assert re.search(r"ldd /opt/witness/lib/python3\.12/site-packages/psycopg_c/pq\*\.so \| "
                     r"grep -Eq 'libpq\.so\.5 => /\(usr/\)\?lib/'", build)
    assert "! ls -d /opt/witness/lib/python3.12/site-packages/psycopg_c.libs" in build
    assert "! /opt/witness/bin/pip show psycopg-binary" in build
    assert "psycopg.pq.__impl__ == 'c'" in build
    # The runtime links Debian's libpq5, whose copyright file it ships.
    assert re.search(r"apt-get install -y --no-install-recommends [^\n]*\blibpq5\b", final)
    assert build.index("generate community") > build.index("psycopg.pq.__impl__")


def test_the_lock_has_the_source_built_driver_and_no_binary_wheel():
    pins = tpl.read_constraints([WITNESS / "constraints.txt"])
    assert "psycopg-binary" not in pins
    assert pins["psycopg-c"][1] == pins["psycopg"][1]
    assert CFG.built_from_source == ("psycopg-c",)
    listed = committed_packages()
    assert "psycopg-binary" not in listed and listed["psycopg-c"] == pins["psycopg-c"][1]
    text = (WITNESS / "THIRD_PARTY_LICENSES").read_text("utf-8")
    assert "Built from source in the image, not from a wheel: psycopg-c." in text
    assert "/usr/share/doc/libpq5/copyright" in text


def control_planes_script(witness: Path) -> Path | None:
    """The Control Plane's script: beside this repository (brutor-gateway-control), or in the
    same monorepo (control-plane), in that order; None when neither is there."""
    for repo, _, _ in repositories_of(witness):
        for control in (repo.parent / "brutor-gateway-control", repo / "control-plane"):
            script = control / "scripts" / "third_party_licenses.py"
            if script.is_file():
                return script
    return None


def test_the_control_planes_script_is_found_in_either_layout(tmp_path):
    private = tmp_path / "platform" / "this-repository" / "trial_bundle" / "witness"
    public = tmp_path / "mono" / "witness"
    for witness in (private, public):
        witness.mkdir(parents=True)
        assert control_planes_script(witness) is None
    for witness, control in ((private, tmp_path / "platform" / "brutor-gateway-control"),
                             (public, tmp_path / "mono" / "control-plane")):
        (control / "scripts").mkdir(parents=True)
        (control / "scripts" / "third_party_licenses.py").write_text("", "utf-8")
        assert control_planes_script(witness) == control / "scripts" / "third_party_licenses.py"


def test_the_script_is_the_control_planes_its_header_apart():
    control = control_planes_script(WITNESS)
    if control is None:
        pytest.skip("the Control Plane is neither beside this repository nor in it")

    def body(text: str) -> str:
        return text[text.index("Every package the image ships"):]

    assert body((WITNESS / "third_party_licenses.py").read_text("utf-8")) == body(control.read_text("utf-8"))
