# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Bedrock guardrail probe uses the entry's own credential or refuses (the
control plane's half of the rule the gateway holds for its Bedrock detector).

An entry with no `aws_access_key_id` was probed with whatever credential the
control-plane process could resolve (its environment, a shared credentials
file, an instance role): the operator's AWS identity, used for a tenant's
guardrail, and a "healthy" verdict for an entry the gateway, which reads the
entry's own credential and nothing else, cannot use.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.services.guardrails import provider_probe

CONFIG = {"guardrail_id": "gr-1", "region": "eu-west-1"}
KEYS = {"aws_access_key_id": "AKIAEXAMPLE", "aws_secret_access_key": "secret-SENTINEL"}


@pytest.fixture()
def boto(monkeypatch):
    """A stand-in for boto3 that records how a client was asked for."""
    calls = []

    class Client:
        def get_guardrail(self, **params):
            return {"name": "g"}

    def client(service, **kwargs):
        calls.append((service, kwargs))
        return Client()

    boto3 = types.ModuleType("boto3")
    boto3.client = client
    botocore = types.ModuleType("botocore")
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kw: kw
    exceptions = types.ModuleType("botocore.exceptions")
    for name in ("ClientError", "EndpointConnectionError", "NoCredentialsError"):
        setattr(exceptions, name, type(name, (Exception,), {}))
    for name, module in (("boto3", boto3), ("botocore", botocore), ("botocore.config", config),
                         ("botocore.exceptions", exceptions)):
        monkeypatch.setitem(sys.modules, name, module)
    # What the process could resolve on its own.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAOPERATOR")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "operator-secret")
    return calls


def _probe(config):
    return asyncio.run(provider_probe.probe_provider("bedrock", None, None, config))


@pytest.mark.parametrize(
    "config, missing",
    [
        (CONFIG, "aws_access_key_id"),
        ({**CONFIG, "aws_access_key_id": "AKIAEXAMPLE"}, "aws_secret_access_key"),
        ({**CONFIG, "aws_secret_access_key": "s"}, "aws_access_key_id"),
        ({**CONFIG, "aws_access_key_id": " ", "aws_secret_access_key": "s"}, "aws_access_key_id"),
    ],
)
def test_an_entry_without_its_own_credential_is_not_probed_with_the_process_identity(boto, config, missing):
    result = _probe(config)
    assert result.status == "unhealthy"
    assert missing in result.error and "own" in result.error
    assert boto == [], "a client was made without the entry's credential"


def test_an_entry_with_its_own_credential_is_probed_with_it(boto):
    result = _probe({**CONFIG, **KEYS, "aws_session_token": "t"})
    assert result.status == "healthy", result.error
    ((service, kwargs),) = boto
    assert service == "bedrock"
    assert kwargs["aws_access_key_id"] == "AKIAEXAMPLE" and kwargs["aws_secret_access_key"] == "secret-SENTINEL"
    assert kwargs["aws_session_token"] == "t" and kwargs["region_name"] == "eu-west-1"
