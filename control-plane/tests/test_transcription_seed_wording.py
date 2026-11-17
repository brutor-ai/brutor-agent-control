# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The seeded description of `gpt-transcribe` says what OpenAI's API reference
says.

The row said the model "takes `languages` (a list) instead of `language`".
The reference for `POST /v1/audio/transcriptions`
(https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
lists `language` (optional string, ISO-639-1) for the
endpoint without excepting any model, and `languages` (optional array) and
`keywords` as "Supported by `gpt-transcribe`". It does not say the model
refuses `language`. An administrator reads this text in the model catalog.
"""

from __future__ import annotations

import inspect
import re

from app.database.provider_data import openai_multimodal_models_init as seed


def _description() -> str:
    source = inspect.getsource(seed)
    start = source.index('"model_name": "gpt-transcribe"')
    block = source[start:source.index('"usage":', start)]
    return " ".join(re.findall(r'"((?:[^"\\]|\\.)*)"', block.split('"description":', 1)[1]))


def test_it_does_not_say_the_model_refuses_language():
    text = _description()
    assert "instead of `language`" not in text
    assert not re.search(r"(refus|reject|not accept|does not take|no)\w*\s+(the\s+)?(single\s+)?`language`", text)


def test_it_says_what_the_reference_says_about_the_two_parameters():
    text = _description()
    assert "`language`" in text and "ISO-639-1" in text
    assert "`languages`" in text and "`keywords`" in text
