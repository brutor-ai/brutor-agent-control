# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""The EU AI Act choice fields take the vocabulary the console offers.

The bug class: the API accepts a value the console cannot show. `annex`,
`annex_iii_area` and `conformity_assessment` were free text in the API and
fixed lists in the console, so a profile declared through the API with
`annex: "III"` was sealed, read "not declared" on screen, and would have been
erased by the next save from the console (seen in the demo system).

Guards:
  * the API refuses a value outside each list and accepts every value in it;
  * the lists equal the console's, read from its source when the admin
    repository is checked out beside this one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from pydantic import ValidationError

from app.services.compliance.profile import (
    EU_ANNEX_III_AREAS,
    EU_ANNEXES,
    EU_CONFORMITY_ASSESSMENTS,
    EuAiActBlock,
)

FIELDS = {
    "annex": EU_ANNEXES,
    "annex_iii_area": EU_ANNEX_III_AREAS,
    "conformity_assessment": EU_CONFORMITY_ASSESSMENTS,
}
#: What the demo once declared, and other prose a person would type.
PROSE = {
    "annex": ["III", "Annex III", "annex iii", ""],
    "annex_iii_area": ["5(b) creditworthiness evaluation of natural persons", "5b", "9", ""],
    "conformity_assessment": ["internal control (Annex VI), demo", "Internal", ""],
}


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_every_listed_value_is_accepted(field):
    for value in FIELDS[field]:
        assert EuAiActBlock.model_validate({field: value}).model_dump(exclude_unset=True) == {field: value}


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_prose_is_refused_and_the_error_lists_the_choices(field):
    for value in PROSE[field]:
        with pytest.raises(ValidationError) as err:
            EuAiActBlock.model_validate({field: value})
        assert f"{field} must be one of" in str(err.value)
        for choice in FIELDS[field]:
            assert choice in str(err.value)


def test_not_declared_is_still_allowed():
    assert EuAiActBlock.model_validate({"annex": None, "annex_iii_area": None}).annex is None
    assert EuAiActBlock.model_validate({}).model_dump(exclude_unset=True) == {}


CONSOLE = (
    Path(__file__).resolve().parents[2]
    / "brutor-gateway-admin" / "src" / "lib" / "complianceEvidenceTypes.ts"
)


def _ts_list(source: str, name: str) -> tuple:
    m = re.search(rf"export const {name} = \[([^\]]*)\] as const", source)
    assert m, f"{name} not found in {CONSOLE.name}"
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


@pytest.mark.skipif(not CONSOLE.exists(), reason="brutor-gateway-admin is not checked out beside this repository")
@pytest.mark.parametrize("name,ours", [
    ("EU_ANNEXES", EU_ANNEXES),
    ("EU_ANNEX_III_AREAS", EU_ANNEX_III_AREAS),
    ("EU_CONFORMITY_ASSESSMENTS", EU_CONFORMITY_ASSESSMENTS),
])
def test_the_console_offers_exactly_what_the_api_accepts(name, ours):
    assert _ts_list(CONSOLE.read_text(), name) == ours
