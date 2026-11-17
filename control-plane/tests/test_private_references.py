# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Nothing in this repository cites a record its reader cannot open: an
internal design document, a review finding, a plan step, a decision, the
nickname of a build, the day something was decided. The rule or the reason
is written where it applies.

Both editions are in this repository, and what it says reaches the reader in
more places than its comments: the OpenAPI documents the Admin Console is
generated from, the database column comments, the CLI's output and the error
messages. The rules, the public standards that may be cited and the scanner's
own examples are in `tests/private_references.py`, which is the same file in
every Python repository of the product.

A migration is named by its full revision id (`a193_approval_window_epochs`),
which is the name of a file under `alembic/versions/`; the short number on
its own is the nickname of a build and is reported.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import private_references
from tests.private_references import Declared

ROOT = Path(__file__).resolve().parents[1]

# Tracked files the scan does not read: the scanner and this file spell out
# what they look for; the seed icons are base64 image data, in which any
# letter and number can stand together.
NOT_SCANNED = (
    "tests/private_references.py",
    "tests/test_private_references.py",
    "app/database/seed_icons.py",
)

# Public things this repository cites beyond the scanner's own lists.
DECLARED = Declared(
    rfcs={
        1123: "Requirements for Internet Hosts -- Application and Support",
        3161: "Internet X.509 Public Key Infrastructure Time-Stamp Protocol (TSP)",
        4180: "Common Format and MIME Type for Comma-Separated Values (CSV) Files",
        5322: "Internet Message Format",
        6962: "Certificate Transparency",
        9111: "HTTP Caching",
        9942: "CBOR Object Signing and Encryption (COSE) Receipts",
    },
    sections={
        "164.": "45 CFR Part 164, the HIPAA Security and Breach Notification Rules",
        "{section}": "a section of 45 CFR Part 164 filled into a HIPAA citation",
    },
    terms={
        "P50": "the 50th percentile of a latency distribution",
        "P95": "the 95th percentile of a latency distribution",
        "P99": "the 99th percentile of a latency distribution",
        "R1": "DeepSeek R1, a published model",
        "M3": "MiniMax M3, a published model",
        "K2": "Moonshot Kimi K2, a published model",
        "K3": "Moonshot Kimi K3, a published model",
        "F401": "pyflakes: a module is imported and not used",
        "F811": "pyflakes: a name is defined again before it is used",
        "A10": "NVIDIA A10, a GPU model",
        "a100": "NVIDIA A100, a GPU model, as a node label",
    },
    dates={"2026-07-28": "Model Context Protocol specification revision"},
    dated={
        "app/database/provider_data/": "model catalogues: release and retirement dates of models",
        "scripts/gen_framework_docs.py": "writes the framework catalogues' dates into the docs pages",
        "app/services/compliance/catalogs/": (
            "framework catalogues: the day each legal text was checked, the day obligations apply"
        ),
        "tests/fixtures/seed_recording_community.json": "recorded seed rows: the model catalogue",
        "enterprise/tests/fixtures/seed_recording_enterprise.json": "recorded seed rows: the model catalogue",
        "THIRD_PARTY_LICENSES": "generated: third-party licence texts, quoted as their authors wrote them",
        "enterprise/THIRD_PARTY_LICENSES": "generated: third-party licence texts, quoted as their authors wrote them",
    },
)

# References that stay for now, each with the reason. Each entry is the
# scanner's line for it; when the reason goes away the line disappears and the
# entry has to be deleted here (the second test fails until it is). Nothing is
# added to this list to make the first test pass.
KNOWN: dict = {}

# The Enterprise package (`enterprise/`) is not published: its reader works
# with the private repositories it talks to, reads or tests against, and it
# names them. The Community tree names none of them.
WORKS_WITH = "the Enterprise package works with it: it reads, calls or tests against it"
NAMED_BY_THE_ENTERPRISE_PACKAGE = {
    "brutor-discovery-sdk": WORKS_WITH,
    "brutor-gateway-kb-uploader": WORKS_WITH,
    "brutor-gateway-test": WORKS_WITH,
    "brutor-kb-connector-sync": WORKS_WITH,
    "brutor-license": WORKS_WITH,
    "brutor-mcp-registry": WORKS_WITH,
    "brutor-trial-service": WORKS_WITH,
}


def _named_by_the_enterprise_package(line: str) -> bool:
    name, _, rest = line.partition(":")
    kind, _, cited = rest.partition(": ")[2].partition(": ")
    return (
        name.startswith("enterprise/")
        and kind == "private repository or record"
        and cited.lower() in NAMED_BY_THE_ENTERPRISE_PACKAGE
    )


COMMUNITY_FILES = 1200


def _scan() -> tuple[list[str], int]:
    report, read = private_references.scan(str(ROOT), NOT_SCANNED, DECLARED)
    return [line for line in report if not _named_by_the_enterprise_package(line)], read


def test_nothing_cites_a_record_the_reader_cannot_open():
    report, read = _scan()
    # The Community tree alone (1,238 files in the public layout when this was set, the history being
    # one baseline revision); the Enterprise
    # half (`enterprise/tests/test_private_references.py`) holds a checkout
    # with `enterprise/` to more than 1,500.
    assert read > COMMUNITY_FILES, f"the scan read only {read} files"
    left = [line for line in report if line not in KNOWN]
    assert not left, (
        f"{len(left)} references to private records "
        "(write the rule out, or delete):\n" + "\n".join(left)
    )


def test_every_known_reference_is_still_there():
    report, _ = _scan()
    gone = sorted(set(KNOWN) - set(report))
    assert not gone, "no longer found; delete from KNOWN:\n" + "\n".join(gone)


def test_every_name_the_enterprise_package_may_carry_is_needed():
    report, _ = private_references.scan(str(ROOT), NOT_SCANNED, DECLARED)
    if not (ROOT / "enterprise").is_dir():
        pytest.skip("no enterprise/ in this checkout")
    named = {line.rsplit(": ", 1)[1].lower() for line in report if _named_by_the_enterprise_package(line)}
    for name, reason in NAMED_BY_THE_ENTERPRISE_PACKAGE.items():
        assert name in private_references.PRIVATE_REPOSITORIES, name
        assert len(reason) > 20, name
        assert name in named, f"enterprise/ no longer names {name}; delete it"


def test_the_reference_scan_finds_each_kind_and_leaves_public_standards_alone():
    private_references.check_the_scanner()


def test_the_scanner_is_the_same_file_as_the_trial_services():
    theirs = ROOT.parent / "brutor-trial-service" / "tests" / "private_references.py"
    if not theirs.is_file():
        pytest.skip("brutor-trial-service is not checked out beside this repository")
    mine = (ROOT / "tests" / "private_references.py").read_text(encoding="utf-8")
    assert mine == theirs.read_text(encoding="utf-8"), (
        "tests/private_references.py differs from brutor-trial-service/tests/private_references.py"
    )
