# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The control plane must not start on the example secrets from the trial
bundle's checked-in .env. Every admin, portal and gateway token is signed
with JWT_SECRET, so a template value is a public key to the whole platform.
"""

import pytest

from app.services.config_manager import ConfigManager, is_placeholder_secret


def test_placeholder_detection_matches_the_shipped_templates():
    assert is_placeholder_secret("your-super-secret-jwt-key-change-this-in-production")
    assert is_placeholder_secret("your-secure-encryption-key-here-at-least-32-characters-long")
    assert is_placeholder_secret("ChangeMe-please-0123456789abcdef0123456789")
    assert not is_placeholder_secret("0123456789abcdef0123456789abcdef0123456789abcdef")
    assert not is_placeholder_secret("test-jwt-secret-not-for-production")


def test_startup_validation_refuses_a_placeholder_jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "your-super-secret-jwt-key-change-this-in-production")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        ConfigManager(validate=True)


def test_startup_validation_accepts_a_real_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef")
    ConfigManager(validate=True)
