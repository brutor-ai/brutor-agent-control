# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A request that fails validation is never answered with a secret it carried.

The framework's 422 repeats the `input` of every error. A sign-in with a
too-short password came back with the password; a sign-in with the user name
missing came back with the whole body, a VALID password included; a body sent
as text came back as one string. `app/core/validation_errors.py` is the one
mechanism that answers a validation failure now, for every route of both
editions.

The guard: every operation of the OpenAPI document of the edition under test
(the Community document in a Community run, the Enterprise document, a strict
superset, in an Enterprise run) whose request body or parameters have a
secret-bearing field — by name (password, secret, token, key, credential,
code, ...) or by type (`format: password`, which is how `SecretStr` is
published) — is sent bodies that cannot validate, each secret field holding a
marker, as a signed-in administrator. No marker may come back, in the answer
or in anything logged while the request was served.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.core import validation_errors
from app.core.validation_errors import REDACTED, is_secret_name, sanitize_errors
from tests.admin_endpoint_harness import build_admin_harness
from tests.app_factory import COMMUNITY, edition_under_test

# The test's own idea of a secret-bearing name: deliberately not the
# product's (`is_secret_name`), so that a word dropped from the product's
# list is noticed here.
SECRET_NAME = re.compile(r"pass|secret|token|key(?!word)|credential|(^|_)codes?($|_)|authorization|cookie", re.I)

SHORT = "Zq~7x"            # shorter than any minimum length of a secret
LONG = "MARK-{n:05d}-zQ9"  # one per field


def _build():
    from tests.app_factory import create_app

    app = create_app()
    return app, app.openapi()


REAL_APP, DOCUMENT = _build()
SCHEMAS = DOCUMENT.get("components", {}).get("schemas", {})


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------


def _resolve(schema: dict) -> dict:
    while "$ref" in schema:
        schema = SCHEMAS[schema["$ref"].rsplit("/", 1)[1]]
    return schema


def _alternatives(schema: dict) -> List[dict]:
    schema = _resolve(schema)
    out = [schema]
    for word in ("allOf", "anyOf", "oneOf"):
        for sub in schema.get(word, []):
            out += _alternatives(sub)
    return out


def _is_secret_schema(name: str, schema: dict) -> bool:
    if SECRET_NAME.search(name):
        return True
    return any(alt.get("format") == "password" or alt.get("writeOnly") for alt in _alternatives(schema))


def secret_paths(schema: dict, prefix: Tuple = (), depth: int = 0, seen: Tuple = ()) -> List[Tuple]:
    """Key paths of the secret-bearing properties of a body schema. An array
    is entered through its items (`"[]"` in the path)."""
    if depth > 5:
        return []
    out: List[Tuple] = []
    for alt in _alternatives(schema):
        marker = id(alt)
        if marker in seen:
            continue
        for name, sub in (alt.get("properties") or {}).items():
            if _is_secret_schema(name, sub):
                out.append(prefix + (name,))
            else:
                out += secret_paths(sub, prefix + (name,), depth + 1, seen + (marker,))
        if "items" in alt:
            out += secret_paths(alt["items"], prefix + ("[]",), depth + 1, seen + (marker,))
    return sorted(set(out))


def _place(body: Any, path: Tuple, value: Any) -> Any:
    head, rest = path[0], path[1:]
    if head == "[]":
        item = body[0] if isinstance(body, list) and body else {}
        return [_place(item, rest, value) if rest else value]
    body = body if isinstance(body, dict) else {}
    body[head] = _place(body.get(head), rest, value) if rest else value
    return body


def _operations() -> List[Tuple[str, str, List[Tuple], List[dict]]]:
    """(METHOD, path, secret body paths, secret parameters) of every
    operation that has a secret-bearing body field or parameter."""
    found = []
    for path, item in sorted(DOCUMENT["paths"].items()):
        for method, op in sorted(item.items()):
            if not isinstance(op, dict):
                continue
            paths: List[Tuple] = []
            for content in (op.get("requestBody") or {}).get("content", {}).values():
                paths += secret_paths(content.get("schema") or {})
            params = [
                p for p in op.get("parameters", [])
                if p["in"] in ("query", "header", "cookie") and _is_secret_schema(p["name"], p.get("schema") or {})
            ]
            if paths or params:
                found.append((method.upper(), path, sorted(set(paths)), params))
    return found


OPERATIONS = _operations()
WITH_BODY = [(m, p, paths) for m, p, paths, _ in OPERATIONS if paths]
WITH_PARAMS = [(m, p, params) for m, p, _, params in OPERATIONS if params]


def _url(path: str) -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", "nope", path)


# ---------------------------------------------------------------------------
# The application: the real routes and the real handlers, on the harness's
# database, signed in
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.buffer = io.StringIO()
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s %(context)s"))

    def emit(self, record):
        if record.name.startswith(("httpx", "httpcore")):
            return  # the test's own HTTP client writing down the URL it called
        if not hasattr(record, "context"):
            record.context = ""
        try:
            self.buffer.write(self.format(record) + "\n")
            # Whatever else was attached to the record (structured context).
            self.buffer.write(repr({k: v for k, v in record.__dict__.items() if k not in ("msg", "args")}) + "\n")
        except Exception:  # noqa: BLE001
            self.buffer.write(repr(record.__dict__) + "\n")


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([], logger_name="validation-error-secrets")
    harness.app.router.routes.extend(REAL_APP.router.routes)
    # What answers an exception is the application's, not the route's: take
    # the real application's handlers, as `create_app` installed them.
    harness.app.exception_handlers.update(REAL_APP.exception_handlers)
    REAL_APP.dependency_overrides = harness.app.dependency_overrides
    yield harness
    harness.close()


@pytest.fixture()
def logs():
    """Everything any logger emits while the test runs, at DEBUG."""
    capture = _Capture()
    root = logging.getLogger()
    named = [logging.getLogger(n) for n in list(logging.root.manager.loggerDict)]
    targets = [root] + [lg for lg in named if isinstance(lg, logging.Logger) and not lg.propagate]
    levels = [(lg, lg.level) for lg in [root] + [lg for lg in named if isinstance(lg, logging.Logger)]]
    for lg, _ in levels:
        lg.setLevel(logging.DEBUG)
    for lg in targets:
        lg.addHandler(capture)
    try:
        yield capture.buffer
    finally:
        for lg in targets:
            lg.removeHandler(capture)
        for lg, level in levels:
            lg.setLevel(level)


def _credentials(h) -> List[Dict[str, str]]:
    creds = [h.headers("t1")]
    if edition_under_test() != COMMUNITY:
        creds.append(h.sys_headers())
    return creds


def _send(h, method: str, path: str, **kwargs) -> Iterator[Any]:
    """The request as each administrator this edition has; answers that are
    a refusal of the caller (401/403) say nothing about validation."""
    client = TestClient(h.app, raise_server_exceptions=False)
    for headers in _credentials(h):
        extra = kwargs.pop("headers", None) or {}
        yield client.request(method, _url(path), headers={**headers, **extra}, **kwargs)
        kwargs["headers"] = extra


def _markers(paths: List[Tuple]) -> Dict[Tuple, str]:
    return {p: LONG.format(n=i) for i, p in enumerate(paths)}


def _bodies(paths: List[Tuple]) -> List[Tuple[str, dict, List[str]]]:
    """(what, request kwargs, the markers it carries): bodies that hold a
    marker in every secret field and do not validate."""
    marks = _markers(paths)
    short: Any = {}
    wrong_type: Any = {}
    plain: Any = {}
    for p in paths:
        short = _place(short, p, SHORT)
        wrong_type = _place(wrong_type, p, [marks[p]])
        plain = _place(plain, p, marks[p])
    every = list(marks.values())
    return [
        # each secret too short for any minimum, every other field missing
        ("short", {"json": short}, [SHORT]),
        # each secret of the wrong type (a list holding the marker)
        ("wrong type", {"json": wrong_type}, every),
        # the body is not an object
        ("not an object", {"json": [plain]}, every),
        # the body as text: the content type a forgotten `-H` sends
        ("as text", {"content": json.dumps(plain), "headers": {"content-type": "text/plain"}}, every),
    ]


def test_the_walk_found_the_operations():
    assert len(WITH_BODY) >= 40, len(WITH_BODY)
    names = {p for _, p, _ in WITH_BODY}
    assert "/v1/admin/users/tenant/login" in names
    assert "/v1/admin/users/me/change-password" in names
    assert "/v1/admin/users/login/2fa" in names


def test_create_app_installs_the_handler():
    assert (
        REAL_APP.exception_handlers[RequestValidationError]
        is validation_errors.request_validation_error_handler
    )


@pytest.mark.parametrize("method,path,paths", WITH_BODY, ids=[f"{m} {p}" for m, p, _ in WITH_BODY])
def test_no_secret_comes_back_from_an_invalid_body(h, logs, method, path, paths):
    """A body that validates after all (every field optional) is carried
    out, and what the route then answers or logs is its own business: the
    rule is about the refusal."""
    reached = 0
    for what, kwargs, marks in _bodies(paths):
        logs.seek(0)
        logs.truncate()
        for r in _send(h, method, path, **dict(kwargs)):
            if r.status_code != 422:
                logs.seek(0)
                logs.truncate()
                continue
            reached += 1
            for mark in marks:
                assert mark not in r.text, f"{method} {path} ({what}): {r.text[:400]}"
            logged = logs.getvalue()
            for mark in marks:
                assert mark not in logged, f"{method} {path} ({what}): a submitted secret was logged"
    assert reached, f"{method} {path}: no body was refused with 422, so nothing was shown"


def _bad_parameter_value(param: dict, mark: str) -> Optional[str]:
    """A value of the parameter that carries the marker and cannot validate,
    if its schema allows one to be made."""
    for alt in _alternatives(param.get("schema") or {}):
        if alt.get("type") in ("integer", "number", "boolean") or alt.get("enum") or alt.get("format") in (
            "uuid", "date-time", "date"
        ):
            return mark
        if "maxLength" in alt:
            return mark * (alt["maxLength"] // len(mark) + 2)
        if "pattern" in alt and not re.search(alt["pattern"], mark):
            return mark
    return None


@pytest.mark.parametrize("method,path,params", WITH_PARAMS, ids=[f"{m} {p}" for m, p, _ in WITH_PARAMS])
def test_no_secret_comes_back_from_an_invalid_parameter(h, logs, method, path, params):
    for i, param in enumerate(params):
        mark = LONG.format(n=i)
        value = _bad_parameter_value(param, mark)
        if value is None:
            continue  # any string validates: there is no error to echo it
        where = {"query": "params", "header": "headers", "cookie": "cookies"}[param["in"]]
        for r in _send(h, method, path, **{where: {param["name"]: value}}):
            if r.status_code == 422:
                assert mark not in r.text, f"{method} {path} {param['name']}: {r.text[:300]}"
                assert mark not in logs.getvalue(), f"{method} {path} {param['name']}: logged"


# A value in the query string is part of the request line, which the server's
# access log writes down (`access_log_format` in gunicorn.conf.py: `%(r)s`); a
# body is not. So no secret may travel in a query string. These are the query
# parameters with a secret-sounding name, each read and found not to be one.
QUERY_PARAMETERS_THAT_ARE_NOT_SECRETS = {
    "min_tokens": "a number: the smallest token count of the log rows to export",
    "new_key": "a provider's key, i.e. its short name (`openai`), on rename",
    "provider_key": "a provider's short name, as a filter",
}


def test_no_secret_travels_in_a_query_string():
    named = {p["name"] for _, _, params in WITH_PARAMS for p in params if p["in"] == "query"}
    assert named <= set(QUERY_PARAMETERS_THAT_ARE_NOT_SECRETS), (
        f"query parameters with a secret-bearing name: {sorted(named - set(QUERY_PARAMETERS_THAT_ARE_NOT_SECRETS))}. "
        "A secret belongs in the body or a header; if it is not a secret, say why in the list."
    )


# ---------------------------------------------------------------------------
# The four answers the secret was seen in, at the sign-in itself
# ---------------------------------------------------------------------------

LOGIN = "/v1/admin/users/tenant/login"
VALID_PASSWORD = "A-long-enough-password-1!"


def test_a_password_the_rule_refuses_is_not_echoed(h):
    """A sign-in compares what it is given; the password rule is applied
    where a password is set, and the refusal does not repeat it."""
    create = "/v1/admin/users/tenant"
    r = h.client.post(
        create, json={"tenant_id": "t1", "username": "newadmin", "email": "n@t1.example", "password": "sh0rt"},
        headers=h.headers("t1"),
    )
    assert r.status_code == 422
    assert "sh0rt" not in r.text
    (error,) = r.json()["detail"]
    assert error["loc"] == ["body", "password"] and error["input"] == REDACTED
    assert "at least 8 characters" in error["msg"]


def test_a_sign_in_with_a_short_password_is_a_failed_sign_in(h):
    r = h.client.post(LOGIN, json={"username": "admin", "password": "sh0rt", "tenant_id": "t1"})
    assert r.status_code == 401
    assert "sh0rt" not in r.text


def test_a_valid_password_is_not_echoed_when_another_field_is_missing(h):
    r = h.client.post(LOGIN, json={"password": VALID_PASSWORD, "tenant_id": "t1"})
    assert r.status_code == 422
    assert VALID_PASSWORD not in r.text
    assert r.json()["detail"] == [{
        "type": "missing", "loc": ["body", "username"], "msg": "Field required",
        "input": {"password": REDACTED, "tenant_id": "t1"},
    }]


def test_a_body_sent_as_text_is_not_echoed(h):
    r = h.client.post(
        LOGIN, content=json.dumps({"username": "admin", "password": VALID_PASSWORD}),
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 422
    assert VALID_PASSWORD not in r.text
    assert [e["input"] for e in r.json()["detail"]] == [REDACTED]


def test_a_body_that_is_a_list_is_not_echoed(h):
    r = h.client.post(LOGIN, json=[{"username": "admin", "password": VALID_PASSWORD}])
    assert r.status_code == 422
    assert VALID_PASSWORD not in r.text
    assert r.json()["detail"][0]["input"] == [{"username": "admin", "password": REDACTED}]


def test_what_is_not_secret_is_answered_as_before(h):
    """The answer is the framework's, field for field, where no secret is."""
    r = h.client.post(LOGIN, json={"username": "ab", "password": VALID_PASSWORD, "tenant_id": "t1"})
    assert r.status_code == 422
    assert r.json() == {"detail": [{
        "type": "string_too_short", "loc": ["body", "username"],
        "msg": "String should have at least 3 characters", "input": "ab", "ctx": {"min_length": 3},
    }]}


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "password", "new_password", "current_password", "newPassword", "passwd", "client_secret", "clientSecret",
    "secret", "api_key", "apiKey", "x-api-key", "X-API-Key", "key", "private_key", "access_token", "temp_token",
    "token", "refresh_token", "Authorization", "authorization", "cookie", "credentials", "aws_secret_access_key",
    "code", "backup_codes", "totp_code", "signing_key", "bearer", "encryption_key", "webhook_secret",
    "mysecretvalue", "PASSWORD",
])
def test_names_that_carry_a_secret(name):
    assert is_secret_name(name)


@pytest.mark.parametrize("name", [
    "username", "email", "tenant_id", "name", "description", "keyboard", "monkey", "tokenizer_free", "codec",
    "encoding", "passenger", "author", "id", "", None, 5,
])
def test_names_that_do_not(name):
    # `tokenizer_free` holds the fragment "token": over-matching is allowed
    # (it costs an echo), so only the plain cases are held here.
    if name == "tokenizer_free":
        assert is_secret_name(name)
    else:
        assert not is_secret_name(name)


def test_every_name_the_guard_calls_secret_is_secret_to_the_product():
    """The two lists agree on everything the document actually has."""
    missed = sorted({
        path[-1] for _, _, paths, _ in OPERATIONS for path in paths if not is_secret_name(path[-1])
    } | {
        p["name"] for _, _, _, params in OPERATIONS for p in params if not is_secret_name(p["name"])
    })
    assert not missed, f"secret-bearing by the guard's reading, not by the product's: {missed}"


def test_nested_and_listed_secrets_are_redacted():
    errors = [{
        "type": "missing", "loc": ["body", "name"], "msg": "Field required",
        "input": {"providers": [{"provider": "x", "api_key": "sk-1"}, {"auth": {"client_secret": "cs-2"}}],
                  "note": "kept"},
    }]
    assert sanitize_errors(errors)[0]["input"] == {
        "providers": [{"provider": "x", "api_key": REDACTED}, {"auth": REDACTED}], "note": "kept",
    }


def test_an_error_below_a_secret_field_is_redacted_whole():
    errors = [{"type": "string_type", "loc": ["body", "credentials", "inner", 0], "msg": "m", "input": {"a": "v"}}]
    assert sanitize_errors(errors)[0]["input"] == REDACTED


def test_a_cookie_is_always_redacted():
    errors = [{"type": "int_parsing", "loc": ["cookie", "tracking"], "msg": "m", "input": "abc"}]
    assert sanitize_errors(errors)[0]["input"] == REDACTED


def test_a_validators_own_message_does_not_carry_the_secret():
    errors = [
        {"type": "value_error", "loc": ["body", "password"], "msg": "Value error, hunter2-hunter2 is too common",
         "input": "hunter2-hunter2", "ctx": {"error": "hunter2-hunter2 is too common"}},
        {"type": "value_error", "loc": ["body"], "msg": "Value error, hunter2-hunter2 equals the user name",
         "input": {"username": "u", "password": "hunter2-hunter2"}, "ctx": {"error": {}}},
        {"type": "value_error", "loc": ["body", "pin"], "msg": "Value error, 12 is too short", "input": "12"},
    ]
    out = sanitize_errors(errors)
    assert "hunter2" not in json.dumps(out)
    assert out[0]["msg"] == f"Value error, {REDACTED} is too common"
    assert out[2]["msg"] == validation_errors.GENERIC_MESSAGE


def test_the_frameworks_own_messages_are_left_alone():
    """A fixed message that happens to contain a short secret is not mangled."""
    errors = [{"type": "string_too_short", "loc": ["body", "password"],
               "msg": "String should have at least 8 characters", "input": "at", "ctx": {"min_length": 8}}]
    assert sanitize_errors(errors) == [{**errors[0], "input": REDACTED}]


class _Typed(BaseModel):
    label: str = Field(..., min_length=3)
    blob: SecretStr                      # secret by TYPE: nothing in the name says so
    inner: Optional["_Inner"] = None


class _Inner(BaseModel):
    opaque: Optional[SecretStr] = None
    plain: int = 0


class _Checked(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def _not_common(cls, v):
        if v.startswith("hunter2"):
            raise ValueError(f"{v} is too common")
        return v


_Typed.model_rebuild()


def _small_app() -> TestClient:
    app = FastAPI()

    @app.post("/typed")
    def typed(body: _Typed):
        return {}

    @app.post("/checked")
    def checked(body: _Checked):
        return {}

    validation_errors.install(app)
    return TestClient(app)


def test_a_secret_by_type_is_redacted_whatever_its_name():
    r = _small_app().post("/typed", json={"blob": "by-type-1", "inner": {"opaque": "by-type-2", "plain": "x"}})
    assert r.status_code == 422, r.text
    assert "by-type-1" not in r.text and "by-type-2" not in r.text
    inputs = {tuple(e["loc"]): e["input"] for e in r.json()["detail"]}
    assert inputs[("body", "label")] == {"blob": REDACTED, "inner": {"opaque": REDACTED, "plain": "x"}}
    assert inputs[("body", "inner", "plain")] == "x"


def test_a_secret_by_type_of_the_wrong_type_is_redacted():
    r = _small_app().post("/typed", json={"label": "abc", "blob": ["by-type-3"]})
    assert r.status_code == 422, r.text
    assert "by-type-3" not in r.text


def test_a_validator_that_quotes_the_secret_is_cut():
    r = _small_app().post("/checked", json={"password": "hunter2-and-more"})
    assert r.status_code == 422, r.text
    assert "hunter2" not in r.text
    assert REDACTED in r.json()["detail"][0]["msg"]
