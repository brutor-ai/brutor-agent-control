# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A compliance framework is "verified" only when it can show its sources.

No shipping support for a framework that has not been checked against the
real published text. `verified_against_instrument` used to default to True,
so five catalogs read as verified only because nobody had set the flag, and
none cited a source. Now:

* unverified is the default;
* a verified catalog has a final instrument, a `verified_on` date, and at
  least one `Source` on EVERY obligation, from an official publisher of that
  instrument, with the exact provision and a verbatim excerpt;
* a draft instrument (a bill, a decree not yet issued) or a licensed one
  (ISO, sold) can never be verified from here.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

import pytest

from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.catalogs.base import (
    INSTRUMENT_DRAFT,
    INSTRUMENT_FINAL,
    INSTRUMENT_LICENSED,
)

#: Where each framework's authoritative text is published. A source on any
#: other host (a law firm's summary, a blog, Wikipedia) does not count.
OFFICIAL_HOSTS = {
    "eu-ai-act": {"eur-lex.europa.eu"},
    "gdpr": {"eur-lex.europa.eu"},
    "dora": {"eur-lex.europa.eu"},
    "nis2": {"eur-lex.europa.eu"},
    "hipaa": {"www.ecfr.gov", "www.govinfo.gov", "www.hhs.gov"},
    "soc2": {"www.aicpa-cima.com", "us.aicpa.org", "www.aicpa.org"},
    "nist-ai-rmf": {"nvlpubs.nist.gov", "doi.org", "www.nist.gov", "airc.nist.gov"},
    "colorado": {"leg.colorado.gov"},
    "korea": {"www.law.go.kr", "law.go.kr", "elaw.klri.re.kr"},
    "brazil": {"www25.senado.leg.br", "www.camara.leg.br", "legis.senado.leg.br"},
    "iso-42001": {"www.iso.org"},
}

VERIFIED = sorted(k for k, f in CATALOGS.items() if f.verified_against_instrument)


def test_every_framework_names_its_official_publishers():
    assert set(CATALOGS) <= set(OFFICIAL_HOSTS), set(CATALOGS) - set(OFFICIAL_HOSTS)


def test_instrument_status_is_known():
    for key, fw in CATALOGS.items():
        assert fw.instrument_status in {INSTRUMENT_FINAL, INSTRUMENT_DRAFT, INSTRUMENT_LICENSED}, key


@pytest.mark.parametrize("key", sorted(CATALOGS))
def test_only_a_final_instrument_can_be_verified(key):
    fw = CATALOGS[key]
    if fw.instrument_status != INSTRUMENT_FINAL:
        assert not fw.verified_against_instrument, (
            f"{key}: a {fw.instrument_status} instrument cannot be verified"
        )


@pytest.mark.parametrize("key", VERIFIED)
def test_a_verified_framework_cites_the_published_text_for_every_obligation(key):
    fw = CATALOGS[key]
    assert fw.verified_on is not None and fw.verified_on <= date.today(), f"{key}: verified_on"
    hosts = OFFICIAL_HOSTS[key]
    missing, bad = [], []
    for ob in fw.obligations:
        if not ob.sources:
            missing.append(ob.key)
            continue
        for src in ob.sources:
            host = urlparse(src.url).netloc
            problems = []
            if urlparse(src.url).scheme != "https" or host not in hosts:
                problems.append(f"url {src.url!r} not on {sorted(hosts)}")
            if not src.provision.strip():
                problems.append("no provision")
            if fw.licensed_text:
                # Copyright forbids reproducing the text: a fingerprint, not an excerpt.
                if src.excerpt:
                    problems.append("licensed text reproduced in excerpt")
                if not re.fullmatch(r"[0-9a-f]{64}", src.excerpt_sha256 or ""):
                    problems.append("no passage fingerprint")
                words = src.excerpt_starts_with.split()
                if not 2 <= len(words) <= 8:
                    problems.append("locator must be 2-8 words")
                if src.excerpt_length < 20:
                    problems.append("no passage length")
            elif len(src.excerpt.strip()) < 20:
                problems.append("no verbatim excerpt")
            if src.checked_on > date.today():
                problems.append("checked_on in the future")
            if problems:
                bad.append(f"{ob.key}: {'; '.join(problems)}")
    assert not missing, f"{key}: obligations with no source: {missing}"
    assert not bad, f"{key}: " + " | ".join(bad)


@pytest.mark.parametrize("key", sorted(k for k, f in CATALOGS.items() if f.licensed_text))
def test_a_licensed_catalog_paraphrases_rather_than_quotes(key):
    """Copyright forbids reproducing ISO / AICPA text: no summary or title may
    carry a cited passage's opening words (SOC 2 CC7.2, for example)."""
    fw = CATALOGS[key]
    norm = lambda t: " ".join(t.lower().split())
    quoted = []
    for ob in fw.obligations:
        prose = norm(f"{ob.title} {ob.summary}")
        for src in ob.sources:
            locator = norm(src.excerpt_starts_with)
            if locator and locator in prose:
                quoted.append(f"{ob.key}: {src.excerpt_starts_with!r}")
    assert not quoted, f"{key}: summaries reproduce the licensed text: {quoted}"
