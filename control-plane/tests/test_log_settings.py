# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The log level and the JSON log are settings of the environment, under
Gunicorn and under `python main.py`.

`BRUTOR_LOG_LEVEL` set only the development server's own level and the
application logged at INFO whatever it said; `BRUTOR_LOG_JSON_ENABLED` (as
`BRUTOR_LOG_JSON_ENABLED`) was read by a function nothing called. Under Gunicorn
the application factory is called without a logger, so the factory is
where both take effect; `python main.py` sets the log up itself, from the
same two variables unless `--log-level` or `--json-logs` say otherwise.
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging_system import StructuredFormatter
from tests.app_factory import create_app


@pytest.fixture()
def root_logger():
    """The root logger as it was, afterwards."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield root
    root.setLevel(level)
    root.handlers[:] = handlers


def _json(root) -> bool:
    return any(isinstance(handler.formatter, StructuredFormatter) for handler in root.handlers)


def test_unset_the_application_logs_text_at_info(root_logger, monkeypatch):
    monkeypatch.delenv("BRUTOR_LOG_LEVEL", raising=False)
    monkeypatch.delenv("BRUTOR_LOG_JSON_ENABLED", raising=False)
    create_app()
    assert root_logger.level == logging.INFO and not _json(root_logger)


@pytest.mark.parametrize("level", ["DEBUG", "warning", "ERROR", "Critical"])
def test_the_factory_gunicorn_calls_logs_at_the_configured_level(root_logger, monkeypatch, level):
    monkeypatch.setenv("BRUTOR_LOG_LEVEL", level)
    create_app()
    assert root_logger.level == getattr(logging, level.upper())


@pytest.mark.parametrize("value, expected", [("true", True), ("1", True), ("on", True), ("false", False), ("no", False)])
def test_the_factory_gunicorn_calls_logs_json_when_told_to(root_logger, monkeypatch, value, expected):
    monkeypatch.setenv("BRUTOR_LOG_JSON_ENABLED", value)
    create_app()
    assert _json(root_logger) is expected


def test_a_logger_handed_to_the_factory_is_left_as_it_is(root_logger, monkeypatch):
    """`python main.py` sets the log up before it builds the application."""
    from app.core.logging_system import setup_logging

    monkeypatch.setenv("BRUTOR_LOG_LEVEL", "ERROR")
    logger = setup_logging("DEBUG", use_json=True)
    create_app(logger=logger)
    assert root_logger.level == logging.DEBUG and _json(root_logger)


@pytest.mark.parametrize(
    "options, environment, level, as_json",
    [
        ({}, {}, "INFO", False),
        ({}, {"BRUTOR_LOG_LEVEL": "debug", "BRUTOR_LOG_JSON_ENABLED": "yes"}, "DEBUG", True),
        ({"log_level": "ERROR"}, {"BRUTOR_LOG_LEVEL": "DEBUG"}, "ERROR", False),
        ({"json_logs": True}, {"BRUTOR_LOG_JSON_ENABLED": "false"}, "INFO", True),
    ],
)
def test_python_main_py_takes_both_from_the_environment_unless_an_option_says_otherwise(
    monkeypatch, options, environment, level, as_json
):
    from app import launcher

    for name in ("BRUTOR_LOG_LEVEL", "BRUTOR_LOG_JSON_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert launcher.log_settings(options.get("log_level"), options.get("json_logs", False)) == (level, as_json)


@pytest.mark.parametrize("name, value", [("BRUTOR_LOG_LEVEL", "LOUD"), ("BRUTOR_LOG_JSON_ENABLED", "maybe")])
def test_a_value_that_is_neither_refuses_the_start(root_logger, monkeypatch, name, value):
    from app.env_registry import EnvError

    monkeypatch.setenv(name, value)
    with pytest.raises(EnvError) as refused:
        create_app()
    assert name in str(refused.value) and value not in str(refused.value)
