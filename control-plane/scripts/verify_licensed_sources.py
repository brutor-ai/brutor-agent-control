#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Re-check a licensed-text catalog's sources against a licensed copy.

ISO and AICPA texts are copyrighted against reproduction, so their catalogs
cite each passage by a fingerprint (SHA-256 of the normalised passage, its
first words and its length) instead of quoting it. Anyone holding a licensed
copy of the same edition can confirm every fingerprint with:

    .venv-test/bin/python scripts/verify_licensed_sources.py iso-42001 /path/to/ISO_IEC_42001_2023.pdf
    .venv-test/bin/python scripts/verify_licensed_sources.py soc2 /path/to/Trust-services-criteria.pdf

Text is extracted with `pdftotext` (poppler), reading order, no layout.
`--fingerprint "<passage>"` prints the three source fields for an author
adding a source.  Exit 0 only when every fingerprint matches.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import env_vars  # noqa: E402

# The application is imported, not run: its models want these to exist.
env_vars.DATABASE_URL.set_default("postgresql://verify:verify@localhost/verify")
env_vars.JWT_SECRET.set_default("verify-licensed-sources-not-a-real-secret-0000")
env_vars.ENCRYPTION_KEY.set_default("a" * 43 + "=")

from app.services.compliance.catalogs import CATALOGS  # noqa: E402
from app.services.compliance.catalogs.base import normalise_passage  # noqa: E402


def extract(pdf: str) -> str:
    out = subprocess.run(["pdftotext", "-enc", "UTF-8", pdf, "-"], check=True, capture_output=True)
    return normalise_passage(out.stdout.decode("utf-8", "replace"))


def fingerprint(passage: str) -> dict:
    norm = normalise_passage(passage)
    return {
        "excerpt_sha256": hashlib.sha256(norm.encode("utf-8")).hexdigest(),
        "excerpt_starts_with": " ".join(norm.split()[:6]),
        "excerpt_length": len(norm),
    }


def check(framework_id: str, pdf: str) -> int:
    fw = CATALOGS[framework_id]
    text = extract(pdf)
    total = bad = 0
    for ob in fw.obligations:
        for src in ob.sources:
            if not src.excerpt_sha256:
                continue
            total += 1
            start, found = 0, False
            while (i := text.find(src.excerpt_starts_with, start)) >= 0:
                passage = text[i:i + src.excerpt_length]
                if hashlib.sha256(passage.encode("utf-8")).hexdigest() == src.excerpt_sha256:
                    found = True
                    break
                start = i + 1
            if not found:
                bad += 1
                print(f"MISMATCH {ob.key} {src.provision}: starts {src.excerpt_starts_with!r}")
    print(f"{framework_id}: {total - bad} of {total} fingerprints match")
    return 1 if bad or total == 0 else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fingerprint":
        print(fingerprint(sys.argv[2]))
        sys.exit(0)
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(check(sys.argv[1], sys.argv[2]))
