# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The answer to a request that fails validation (422) never repeats a secret.

The framework's default answer carries, for every error, the
`input` that failed. A password shorter than eight characters came back in
`detail[].input`; worse, a sign-in with the user name left out came back with
the WHOLE body as the input of the missing field, password included, and a body
sent with the wrong content type came back as one string. A 422 is written to
proxy logs, browser consoles and bug reports.

One mechanism, for every route of both editions (`install` is called by
`create_app`): the framework's answer, unchanged, except that

  * the `input` of an error located at a secret-bearing field (or anywhere
    below one) is `[redacted]`;
  * inside an `input` that is an object or a list, the value of every
    secret-bearing key is `[redacted]`, at any depth;
  * an `input` that is the request body as text (wrong content type, or not
    an object) is `[redacted]` whole: it cannot be taken apart;
  * a cookie's value is always `[redacted]`;
  * every secret value that was submitted is cut out of the messages and
    contexts (a validator of this code base may quote what it refuses).

A field is secret-bearing by NAME (`is_secret_name`: password, secret, token,
key, credential, code, ... as a word of the name) or by TYPE (`SecretStr` /
`SecretBytes` anywhere in the annotation of a field of the route's request
models or parameters). Over-matching costs nothing but the echo of a harmless
value (`status_code`, a provider `key`); under-matching leaks.

`tests/test_validation_error_secrets.py` walks every operation of the edition's
OpenAPI document and holds this.
"""

from __future__ import annotations

import re
import typing
from typing import Any, FrozenSet, Iterable, List, Set

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, SecretBytes, SecretStr

REDACTED = "[redacted]"
GENERIC_MESSAGE = "Invalid value."

# Words that make a field name secret-bearing when they are one of its words
# (`new_password`, `clientSecret`, `x-api-key`, `temp_token`, `backup_codes`).
_SECRET_WORDS = frozenset({
    "password", "passwd", "pwd", "passphrase", "pass",
    "secret", "secrets",
    "token", "tokens", "jwt", "bearer",
    "key", "keys", "apikey",
    "credential", "credentials", "cred", "creds",
    "authorization", "auth", "cookie", "cookies", "session",
    "otp", "totp", "code", "codes", "pin",
    "signature", "sig", "hmac", "salt", "nonce",
    "dsn", "private",
})
# ... and fragments that do it wherever they occur (`newpassword`, `mysecretvalue`).
_SECRET_FRAGMENTS = ("password", "passwd", "secret", "token", "apikey", "credential", "passphrase")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[^a-z0-9]+")


def is_secret_name(name: Any) -> bool:
    """Whether a field, parameter or header of this name carries a secret."""
    if not isinstance(name, str) or not name:
        return False
    lowered = _CAMEL.sub("_", name).lower()
    if any(fragment in lowered.replace("_", "").replace("-", "") for fragment in _SECRET_FRAGMENTS):
        return True
    return any(word in _SECRET_WORDS for word in _SEPARATORS.split(lowered) if word)


def _mentions_secret_type(annotation: Any, seen: Set[int]) -> bool:
    if annotation in (SecretStr, SecretBytes):
        return True
    if isinstance(annotation, type) and issubclass(annotation, (SecretStr, SecretBytes)):
        return True
    return any(_mentions_secret_type(arg, seen) for arg in typing.get_args(annotation))


def _collect_secret_typed(annotation: Any, names: Set[str], seen: Set[int]) -> None:
    """Names (and aliases) of the fields declared `SecretStr` / `SecretBytes`
    in every model `annotation` reaches."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if id(annotation) in seen:
            return
        seen.add(id(annotation))
        for name, field in annotation.model_fields.items():
            if _mentions_secret_type(field.annotation, seen):
                names.add(name)
                for alias in (field.alias, field.validation_alias):
                    if isinstance(alias, str):
                        names.add(alias)
            _collect_secret_typed(field.annotation, names, seen)
        return
    for arg in typing.get_args(annotation):
        _collect_secret_typed(arg, names, seen)


def secret_typed_names(annotations: Iterable[Any]) -> FrozenSet[str]:
    names: Set[str] = set()
    seen: Set[int] = set()
    for annotation in annotations:
        _collect_secret_typed(annotation, names, seen)
    return frozenset(names)


#: id(route) -> (the route, its names). A route is not hashable; the route is
#: kept beside its names so that an id is never read for another object.
_ROUTE_NAMES: dict = {}


def _route_secret_names(route: Any) -> FrozenSet[str]:
    """The secret-typed names of one route: its body model(s), and its own
    query, header, path and cookie parameters."""
    cached = _ROUTE_NAMES.get(id(route))
    if cached is not None and cached[0] is route:
        return cached[1]
    names = _compute_route_secret_names(route)
    _ROUTE_NAMES[id(route)] = (route, names)
    return names


def _compute_route_secret_names(route: Any) -> FrozenSet[str]:
    from fastapi.openapi.utils import get_fields_from_routes

    names: Set[str] = set()
    annotations: List[Any] = []
    try:
        fields = get_fields_from_routes([route])
    except Exception:  # noqa: BLE001 — a route the framework cannot describe has no typed names
        fields = []
    for field in fields:
        annotation = field.field_info.annotation
        annotations.append(annotation)
        if _mentions_secret_type(annotation, set()):
            names.add(field.name)
            alias = getattr(field, "alias", None)
            if isinstance(alias, str):
                names.add(alias)
    return frozenset(names | secret_typed_names(annotations))


def _is_secret(name: Any, typed: FrozenSet[str]) -> bool:
    return is_secret_name(name) or (isinstance(name, str) and name in typed)


def _harvest(value: Any, into: Set[str]) -> None:
    """Every string inside a secret value: what must not come back."""
    if isinstance(value, str):
        if value:
            into.add(value)
    elif isinstance(value, bytes):
        if value:
            into.add(value.decode("utf-8", "replace"))
    elif isinstance(value, dict):
        for item in value.values():
            _harvest(item, into)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _harvest(item, into)
    elif value is not None and not isinstance(value, bool):
        into.add(str(value))


def _redact(value: Any, typed: FrozenSet[str], secrets: Set[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _is_secret(key, typed):
                _harvest(item, secrets)
                out[key] = REDACTED
            else:
                out[key] = _redact(item, typed, secrets)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, typed, secrets) for item in value]
    return value


#: Error types whose message is written by a validator of this code base
#: (`raise ValueError(...)`, an `assert`) and may therefore quote the input.
_VALIDATOR_ERROR_TYPES = frozenset({"value_error", "assertion_error"})


def _scrub_text(text: str, secrets: Set[str], *, written_by_a_validator: bool) -> str:
    """`text` without the submitted secrets. A secret of four characters or
    more is cut out wherever it occurs. A shorter one cannot be cut out
    without mangling the sentence (a one-letter password occurs in every
    sentence): where the sentence is a validator's own, it is replaced whole;
    the validation library's fixed sentences never quote the input and are
    left alone."""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret not in text:
            continue
        if len(secret) >= 4:
            text = text.replace(secret, REDACTED)
        elif written_by_a_validator:
            return GENERIC_MESSAGE
    return text


def _scrub(value: Any, secrets: Set[str], *, written_by_a_validator: bool) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, secrets, written_by_a_validator=written_by_a_validator)
    if isinstance(value, dict):
        return {k: _scrub(v, secrets, written_by_a_validator=written_by_a_validator) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, secrets, written_by_a_validator=written_by_a_validator) for v in value]
    return value


def sanitize_errors(errors: List[dict], typed: FrozenSet[str] = frozenset()) -> List[dict]:
    """`errors` (as the framework encodes them) without any secret value."""
    secrets: Set[str] = set()
    out: List[dict] = []
    for error in errors:
        error = dict(error)
        loc = list(error.get("loc") or ())
        where = loc[0] if loc else None
        # `loc[0]` is where the value came from (body, query, header, ...).
        located = where == "cookie" or any(_is_secret(part, typed) for part in loc[1:])
        if "input" in error:
            value = error["input"]
            if located:
                _harvest(value, secrets)
                error["input"] = REDACTED
            elif where == "body" and len(loc) == 1 and isinstance(value, (str, bytes)):
                # The body itself, unparsed. (Not harvested: it is not one value.)
                error["input"] = REDACTED
            else:
                error["input"] = _redact(value, typed, secrets)
        out.append(error)
    if secrets:
        for error in out:
            own = error.get("type") in _VALIDATOR_ERROR_TYPES
            if isinstance(error.get("msg"), str):
                error["msg"] = _scrub_text(error["msg"], secrets, written_by_a_validator=own)
            if "ctx" in error:
                error["ctx"] = _scrub(error["ctx"], secrets, written_by_a_validator=own)
    return out


async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    route = request.scope.get("route")
    typed = _route_secret_names(route) if route is not None else frozenset()
    detail = sanitize_errors(jsonable_encoder(exc.errors()), typed)
    return JSONResponse(status_code=422, content={"detail": detail})


def install(app: FastAPI) -> None:
    """Make `app` answer validation failures through this module."""
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
