# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Each image of the Control Plane says what it is, and is published under the
name the product gives it.

The names, titles, licences and source repositories are one list,
`scripts/image-names.txt`. Each Dockerfile carries the labels of its edition
from that list; the image workflow writes the names out (a name derived from
the repository the workflow runs in would change with the repository), builds
each image from its own Dockerfile, and gives every image it publishes the two
labels only the build knows: the version and the revision.

This file checks the Community image (`Dockerfile`) and reads nothing under
`enterprise/`; the Enterprise half, `enterprise/tests/test_image_names.py`,
checks `enterprise/Dockerfile` with the same functions.

In the public repository (`tests/workflows.py`: the monorepo layout) the list
is the Community image's alone: that repository publishes the Community images
only, so no line of its list names an Enterprise image or edition (the export
drops the Enterprise row). There the names are written once, in the bake file
(`package/docker-bake.hcl`), which also gives every image the version and
revision labels; the component's workflow builds the image through its bake
target and names no published image (`bake_faults`).

Also here: the build backend's floor. The cut tooling writes a PEP 639 licence
expression (`license = "Apache-2.0"`) into the Community `pyproject.toml`,
which setuptools reads from 77 on.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

import pytest

from tests import workflows
from tests.source_trees import REPO

LIST = "scripts/image-names.txt"
# The image workflow: this repository's `.github/workflows/docker-build.yml`,
# or in the public repository the root workflow that publishes the images
# (`tests/workflows.py`).
WORKFLOW = "docker-build.yml"
VENDOR = "Brutor AI Ltd"
ORGANISATION = "https://github.com/brutor-ai/"
VERSION_LABELS = ("org.opencontainers.image.version", "org.opencontainers.image.revision")
SETUPTOOLS_FLOOR = 77


@dataclass(frozen=True)
class Image:
    edition: str
    name: str
    title: str
    licenses: str
    source: str
    dockerfile: str


def read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def images(text: str | None = None) -> list:
    text = read(LIST) if text is None else text
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) != 6:
            raise ValueError(f"{LIST}: six tab-separated columns: {line!r}")
        out.append(Image(*columns))
    return out


def variable_of(edition: str) -> str:
    """The variable of the image workflow that holds an edition's image name."""
    return "COMMUNITY_IMAGE_NAME" if edition == "community" else "IMAGE_NAME"


def labels(dockerfile: str) -> dict:
    """The LABELs of a Dockerfile's last stage (the image that is shipped),
    one quoted value per line."""
    last = dockerfile.rfind("\nFROM ")
    stage = dockerfile if last < 0 else dockerfile[last:]
    out: dict = {}
    for line in stage.splitlines():
        m = re.match(r"^\s*LABEL\s+(\S+?)=(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if not re.fullmatch(r'".*"', value):
            raise ValueError(f"one quoted value per LABEL line: {line}")
        if m.group(1) in out:
            raise ValueError(f"the label {m.group(1)} is set twice")
        out[m.group(1)] = value[1:-1]
    return out


def label_faults(image: Image, have: dict) -> list:
    faults = []
    want = [
        ("org.opencontainers.image.title", image.title),
        ("org.opencontainers.image.licenses", image.licenses),
        ("org.opencontainers.image.source", image.source),
        ("org.opencontainers.image.vendor", VENDOR),
        ("ai.brutor.edition", image.edition),
    ]
    for key, value in want:
        got = have.get(key)
        if got is None:
            faults.append(f"{key} is not set (it is {value!r})")
        elif got != value:
            faults.append(f"{key} is {got!r}, not {value!r}")
    if "Brutor Agent Control Control Plane" not in have.get("org.opencontainers.image.description", ""):
        faults.append("org.opencontainers.image.description names the product and the component")
    for key, value in have.items():
        if key in ("maintainer", "version", "description", "org.opencontainers.image.authors", *VERSION_LABELS):
            faults.append(f"the label {key} ({value!r}) is not set in a Dockerfile")
        if "github.com/" in value and not value.startswith(ORGANISATION):
            faults.append(f"{key} names a GitHub namespace that is not the organisation's: {value!r}")
    return faults


def derived_names(workflow: str) -> list:
    """Lines of a workflow, outside comments, that take a name from the
    repository the workflow runs in."""
    return [
        f"{number}: {line.strip()}"
        for number, line in enumerate(workflow.splitlines(), 1)
        if not line.lstrip().startswith("#") and re.search(r"github\.repository|GITHUB_REPOSITORY", re.sub(r"\s+", "", line))
    ]


def jobs(workflow: str) -> dict:
    """The jobs of a workflow, by name: the text of each, comments left out."""
    body = workflow[workflow.index("\njobs:\n") :]
    parts = re.split(r"\n {2}([\w-]+):\n", body)
    return {
        parts[i]: "\n".join(line for line in parts[i + 1].splitlines() if not line.lstrip().startswith("#"))
        for i in range(1, len(parts) - 1, 2)
    }


def workflow_faults(workflow: str, listed: list) -> list:
    """An image name not written out as the list has it, a job that builds one
    edition's Dockerfile under another edition's name, a published image
    without its version or revision, the metadata action's labels (they
    replace the Dockerfile's)."""
    faults = []
    if len(workflow.split("\n  REGISTRY: ghcr.io\n")) != 2:
        faults.append("REGISTRY is ghcr.io, set once")
    for image in listed:
        variable = variable_of(image.edition)
        repository = image.name.removeprefix("ghcr.io/")
        if len(workflow.split(f"\n  {variable}: {repository}\n")) != 2:
            faults.append(f"{variable} is {repository}, set once ({LIST})")
    if "steps.meta.outputs.labels" in workflow:
        faults.append("passes the metadata action's labels, which replace the Dockerfile's")
    by_dockerfile = {image.dockerfile: image for image in listed}
    built = 0
    for name, text in jobs(workflow).items():
        dockerfiles = re.findall(r"(?:\bfile:|\s-f)\s+\./(\S+)", text)
        if not dockerfiles:
            continue
        built += 1
        variables = set(re.findall(r"\b(COMMUNITY_IMAGE_NAME|IMAGE_NAME)\b", text))
        for dockerfile in dockerfiles:
            image = by_dockerfile.get(dockerfile)
            if image is None:
                faults.append(f"job {name} builds {dockerfile}, which {LIST} does not list")
                continue
            others = sorted(v for v in variables if v != variable_of(image.edition))
            if others:
                faults.append(f"job {name} builds {dockerfile} (the {image.edition} image) and names it {', '.join(others)}")
        if re.search(r"\bpush:\s*\$\{\{|\bpush:\s*true|buildx build[^\n]*--push|build --push", text):
            for label in VERSION_LABELS:
                if f"{label}=" not in text:
                    faults.append(f"job {name} publishes an image without {label}")
    if built == 0:
        faults.append("no job builds an image")
    return faults


COMPONENT = "control-plane"


def bake_faults(bake: str, workflow: str, listed: list) -> list:
    """The public layout: the bake file sets the registry once, publishes each
    listed image from this component's directory under the list's name, gives
    every image the version and revision labels and the control plane its
    version; the component's workflow builds through that target and names no
    published image; neither names an Enterprise image."""
    faults = []
    if len(bake.split('variable "REGISTRY" {\n  default = "ghcr.io"\n}\n')) != 2:
        faults.append("REGISTRY is ghcr.io, set once")
    for image in listed:
        repository = image.name.removeprefix("ghcr.io/")
        block = f'target "{COMPONENT}" {{\n  inherits = ["community"]\n  context = "{COMPONENT}"\n  tags = tags("{repository}")\n'
        if len(bake.split(block)) != 2:
            faults.append(f"the bake target publishes {repository} from {COMPONENT}/, once ({LIST})")
    community = re.search(r'^target "community" \{\n(.*?)^\}', bake, re.M | re.S)
    for label in VERSION_LABELS:
        if not community or f'"{label}" = ' not in community.group(1):
            faults.append(f"publishes an image without {label}")
    target = re.search(r'^target "%s" \{\n(.*?)^\}' % COMPONENT, bake, re.M | re.S)
    if not target or 'BRUTOR_VERSION = "${VERSION}"' not in target.group(1):
        faults.append("the control plane's BRUTOR_VERSION is not the image's version")
    if len(workflow.split(f"docker buildx bake -f {workflows.BAKE} {COMPONENT} --load")) != 2:
        faults.append(f"the workflow builds the image through {workflows.BAKE} ({COMPONENT}), once")
    if "brutor-ai/brutor-agent-control-" in workflow:
        faults.append(f"the workflow names a published image; the names are in {workflows.BAKE}")
    if "steps.meta.outputs.labels" in workflow:
        faults.append("passes the metadata action's labels, which replace the Dockerfile's")
    for text in (bake, workflow):
        if ENTERPRISE_IMAGE in text:
            faults.append("names an Enterprise image")
    return faults


def build_backend_floor(pyproject: str) -> int:
    """The lowest setuptools a `pyproject.toml`'s build system accepts."""
    requires = tomllib.loads(read(pyproject))["build-system"]["requires"]
    floors = [int(m.group(1)) for r in requires if (m := re.fullmatch(r"setuptools>=(\d+)(?:\.\d+)*", r.strip()))]
    assert len(floors) == 1, requires
    return floors[0]


def image_of(edition: str) -> Image:
    return next(image for image in images() if image.edition == edition)


ENTERPRISE_IMAGE = "brutor-agent-control-enterprise-"


def list_faults(text: str, layout: str) -> list:
    """The editions the list must have in a layout: both in this repository;
    in the public repository the Community one only, and no line (a comment
    included) names an Enterprise image or edition."""
    editions = [image.edition for image in images(text)]
    if layout == workflows.REPOSITORY:
        return [] if editions == ["community", "enterprise"] else [f"the editions are {editions}, not community and enterprise"]
    faults = [] if editions == ["community"] else [f"the editions are {editions}, not community alone"]
    faults += [
        f"line {number} names an Enterprise image: {line.strip()}"
        for number, line in enumerate(text.splitlines(), 1)
        if ENTERPRISE_IMAGE in line or line.startswith("enterprise\t")
    ]
    return faults


# --- the Community image -------------------------------------------------------------------------------------------


def test_the_images_are_the_two_editions_of_the_control_plane_under_the_names_the_product_gives_them():
    """Both editions in this repository; the Community one alone in the public repository."""
    assert list_faults(read(LIST), workflows.layout()) == []
    listed = images()
    for image in listed:
        community = image.edition == "community"
        assert image == Image(
            edition=image.edition,
            name=f"ghcr.io/brutor-ai/brutor-agent-control-{'' if community else 'enterprise-'}control-plane",
            title=f"Brutor Agent Control Control Plane ({'Community' if community else 'Enterprise'} Edition)",
            licenses="Apache-2.0" if community else "LicenseRef-Brutor-Proprietary",
            source=f"{ORGANISATION}brutor-agent-control{'' if community else '-enterprise'}",
            dockerfile="Dockerfile" if community else "enterprise/Dockerfile",
        )


def test_the_community_dockerfile_carries_the_labels_of_its_edition():
    image = image_of("community")
    assert label_faults(image, labels(read(image.dockerfile))) == []


def test_no_workflow_derives_a_name_from_the_repository_it_runs_in():
    found = workflows.workflow_files()
    assert len(found) >= 2
    for path in found:
        assert (path.name, derived_names(path.read_text(encoding="utf-8"))) == (path.name, [])


def test_the_image_workflow_names_builds_and_labels_each_image_as_the_list_says():
    if workflows.layout() == workflows.REPOSITORY:
        assert workflow_faults(workflows.workflow_text(WORKFLOW), images()) == []
    else:
        assert bake_faults(workflows.bake_text(), workflows.workflow_text("ci.yml"), images()) == []


def test_the_community_build_backend_reads_a_pep_639_licence():
    assert build_backend_floor("pyproject.toml") >= SETUPTOOLS_FLOOR


# --- the checks themselves -----------------------------------------------------------------------------------------

GOOD = "\n".join(
    [
        "FROM python AS builder",
        'LABEL stage="builder"',
        "FROM python",
        'LABEL org.opencontainers.image.title="Brutor Agent Control Control Plane (Community Edition)"',
        'LABEL org.opencontainers.image.description="Brutor Agent Control Control Plane, Community Edition"',
        'LABEL org.opencontainers.image.licenses="Apache-2.0"',
        'LABEL org.opencontainers.image.source="https://github.com/brutor-ai/brutor-agent-control"',
        'LABEL org.opencontainers.image.vendor="Brutor AI Ltd"',
        'LABEL ai.brutor.edition="community"',
        "",
    ]
)
COMMUNITY = Image(
    "community",
    "ghcr.io/brutor-ai/brutor-agent-control-control-plane",
    "Brutor Agent Control Control Plane (Community Edition)",
    "Apache-2.0",
    "https://github.com/brutor-ai/brutor-agent-control",
    "Dockerfile",
)


def test_the_checks_accept_the_labels_of_the_list_and_read_only_the_shipped_stage():
    assert label_faults(COMMUNITY, labels(GOOD)) == []
    assert "stage" not in labels(GOOD)


@pytest.mark.parametrize(
    "dockerfile, about",
    [
        (GOOD.replace('"Apache-2.0"', '"LicenseRef-Brutor-Proprietary"'), "licenses"),
        (GOOD.replace('LABEL org.opencontainers.image.licenses="Apache-2.0"\n', ""), "licenses"),
        (GOOD.replace('brutor-ai/brutor-agent-control"', 'someone/control-plane"'), "source"),
        (GOOD.replace("(Community Edition)", "(Enterprise Edition)"), "title"),
        (GOOD.replace("Brutor AI Ltd", "Brutor"), "vendor"),
        (GOOD.replace('="community"', '="enterprise"'), "ai.brutor.edition"),
        (GOOD + 'LABEL maintainer="Brutor AI Ltd"\n', "maintainer"),
        (GOOD + 'LABEL org.opencontainers.image.version="0.11.0"\n', "org.opencontainers.image.version"),
        (GOOD + 'LABEL org.opencontainers.image.revision="abc123"\n', "org.opencontainers.image.revision"),
        (re.sub(r"LABEL org\.opencontainers\.image\.description=.*\n", 'LABEL description="An API"\n', GOOD), "description"),
    ],
)
def test_the_checks_catch_a_wrong_label(dockerfile, about):
    assert any(about in fault for fault in label_faults(COMMUNITY, labels(dockerfile)))


def test_the_checks_catch_a_name_taken_from_the_repository_in_any_spelling():
    for line in (
        "  IMAGE_NAME: ${{ github.repository }}",
        "          images: ghcr.io/${{github.repository}}",
        '          docker build -t "ghcr.io/$GITHUB_REPOSITORY:test" .',
    ):
        assert len(derived_names(line)) == 1, line
    for line in ("  IMAGE_NAME: brutor-ai/brutor-agent-control-control-plane", "  # never ${{ github.repository }}"):
        assert derived_names(line) == [], line


def test_the_checks_catch_a_list_that_names_the_editions_otherwise():
    """Planted in the list of the layout the run is in: the public repository's
    with the Enterprise row put back (or only named in a comment), this
    repository's without its Enterprise row."""
    text = read(LIST)
    layout = workflows.layout()
    if layout == workflows.MONOREPO:
        row = (
            "enterprise\tghcr.io/brutor-ai/brutor-agent-control-enterprise-control-plane\t"
            "Brutor Agent Control Control Plane (Enterprise Edition)\tLicenseRef-Brutor-Proprietary\t"
            "https://github.com/brutor-ai/brutor-agent-control-enterprise\tenterprise/Dockerfile\n"
        )
        planted = [
            (text + row, "not community alone"),
            (text + row, "names an Enterprise image"),
            (text + "# ghcr.io/brutor-ai/brutor-agent-control-enterprise-control-plane\n", "names an Enterprise image"),
        ]
    else:
        planted = [("".join(line for line in text.splitlines(True) if not line.startswith("enterprise\t")), "not community and enterprise")]
    for plant, about in planted:
        assert plant != text, f"{about}: the plant changed nothing"
        faults = list_faults(plant, layout)
        assert any(about in fault for fault in faults), (about, faults)
    # Each layout's rule refuses the other layout's list.
    other = workflows.REPOSITORY if layout == workflows.MONOREPO else workflows.MONOREPO
    assert list_faults(text, other) != []


def test_the_checks_catch_a_workflow_that_names_builds_or_labels_an_image_otherwise():
    """Planted in what names the images in the layout the run is in: this
    repository's `docker-build.yml` (both editions, the metadata action), or
    the public repository's bake file and the component's workflow that
    builds through it (the Community image only)."""
    listed = images()
    if workflows.layout() == workflows.MONOREPO:
        bake, workflow = workflows.bake_text(), workflows.workflow_text("ci.yml")
        build = f"{workflows.BAKE} {COMPONENT} --load"
        name = "brutor-ai/brutor-agent-control-control-plane"
        for plant_bake, plant_workflow, about in [
            (bake.replace(f'tags("{name}")', 'tags("someone/control-plane")'), workflow, "the bake target publishes"),
            (bake.replace(f'context = "{COMPONENT}"', 'context = "elsewhere"'), workflow, "the bake target publishes"),
            (bake.replace('default = "ghcr.io"', 'default = "docker.io"'), workflow, "REGISTRY is ghcr.io"),
            (re.sub(r'\n\s*"org\.opencontainers\.image\.revision" = [^\n]*', "", bake), workflow, "without org.opencontainers.image.revision"),
            (re.sub(r'\n\s*"org\.opencontainers\.image\.version" = [^\n]*', "", bake), workflow, "without org.opencontainers.image.version"),
            (bake.replace('BRUTOR_VERSION = "${VERSION}"', 'BRUTOR_VERSION = "dev"'), workflow, "BRUTOR_VERSION"),
            (bake + "# ghcr.io/brutor-ai/brutor-agent-control-enterprise-control-plane\n", workflow, "names an Enterprise image"),
            (bake, workflow.replace(build, f"{workflows.BAKE} core-gateway --load"), "builds the image through"),
            (bake, workflow + f"  COMMUNITY_IMAGE_NAME: {name}\n", "names a published image"),
            (bake, workflow + "          labels: ${{ steps.meta.outputs.labels }}\n", "the metadata action's labels"),
        ]:
            assert (plant_bake, plant_workflow) != (bake, workflow), f"{about}: the plant changed nothing"
            faults = bake_faults(plant_bake, plant_workflow, listed)
            assert any(about in fault for fault in faults), (about, faults)
        return
    workflow = workflows.workflow_text(WORKFLOW)
    planted = [
        (workflow.replace("\n  COMMUNITY_IMAGE_NAME: brutor-ai/brutor-agent-control-control-plane\n", "\n"), "COMMUNITY_IMAGE_NAME is"),
        (re.sub(r"\n[^\n]*org\.opencontainers\.image\.revision=[^\n]*", "", workflow), "without org.opencontainers.image.revision"),
        (re.sub(r"\n[^\n]*org\.opencontainers\.image\.version=[^\n]*", "", workflow), "without org.opencontainers.image.version"),
    ]
    planted += [
        (workflow.replace("\n  IMAGE_NAME: brutor-ai/", "\n  IMAGE_NAME: someone/"), "IMAGE_NAME is"),
        (workflow.replace("file: ./enterprise/Dockerfile", "file: ./Dockerfile"), "the community image) and names it IMAGE_NAME"),
        (re.sub(r"labels: \|\n(\s+org\..*\n)+", "labels: ${{ steps.meta.outputs.labels }}\n", workflow), "the metadata action's labels"),
    ]
    for text, about in planted:
        assert text != workflow, f"{about}: the plant changed nothing"
        faults = workflow_faults(text, listed)
        assert any(about in fault for fault in faults), (about, faults)


def test_the_checks_read_the_list_as_six_tab_separated_columns():
    with pytest.raises(ValueError, match="six tab-separated columns"):
        images("community ghcr.io/x title Apache-2.0 https://x Dockerfile\n")


def test_the_checks_read_the_build_backend_floor():
    assert build_backend_floor("pyproject.toml") > 0
