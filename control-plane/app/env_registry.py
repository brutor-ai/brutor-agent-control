# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The environment-variable registry: the one place the control plane reads
its environment.

A variable is defined once, as an `EnvVar` (name, type, default, whether it
is required, whether it is a secret, edition, description, example and
accepted values). The Community Edition's
table of them is `app/env_vars.py`; the Enterprise Edition's is in its own
package and joins this registry through its extension
(`Extension.env_vars`). Everything else follows from the table:

* configuration is read through the definition (`JWT_SECRET.require()`,
  `BRUTOR_LOG_LEVEL.string()`), so a name that is not registered cannot be
  read, and nothing else in either package touches `os.environ`;
* a value that is not of its variable's type, and a required variable
  that is not set, stop the process at start, one line per name
  (`View.problems`, `refuse_or_exit`);
* `.env.example`, the configuration tables of the READMEs and of the
  getting-started guide, and the JSON printed by `--print-env` are
  generated from it (`View.env_example`, `View.markdown`, `View.json`).

No value of any variable is ever written by this module: not in an error,
not in a log line, not in the generated files.

The JSON document (`View.json`) lists the variables in the schema (version
1) the Core Gateway and the worker services print.
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Version of the JSON document `View.json` produces.
SCHEMA_VERSION = 1
# The flag that makes an entry point print the registry as JSON and exit.
PRINT_FLAG = "--print-env"
# The prefix of the product's own variables.
PRODUCT_PREFIX = "BRUTOR_"
# Short name of this service, as the packages call it.
SERVICE = "control-plane"

# What a value must look like.
STRING = "string"      # any text
INTEGER = "integer"    # a whole number written in digits only
NUMBER = "number"      # a decimal number
BOOLEAN = "boolean"    # true, false, 1, 0, yes, no, on, off (any case)
URL = "url"            # scheme://...
PATH = "path"          # a file-system path
LIST = "list"          # comma-separated items
KINDS = (STRING, INTEGER, NUMBER, BOOLEAN, URL, PATH, LIST)

# Which edition of the product reads a variable.
BOTH = "both"
ENTERPRISE = "enterprise"

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def parse_bool(raw: str) -> Optional[bool]:
    """The one spelling of a boolean every variable accepts; None for
    anything else."""
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def _looks_like_url(raw: str) -> bool:
    scheme, separator, rest = raw.partition("://")
    return (
        bool(separator)
        and bool(rest)
        and scheme[:1].isalpha()
        and all(c.isalnum() or c in "+-." for c in scheme)
        and not any(c.isspace() for c in raw)
    )


def _is_decimal(raw: str) -> bool:
    try:
        value = float(raw)
    except ValueError:
        return False
    return value == value and value not in (float("inf"), float("-inf"))


_EXPECTED = {
    INTEGER: "a whole number written in digits only",
    NUMBER: "a decimal number",
    BOOLEAN: "one of true, false, 1, 0, yes, no, on, off",
    URL: "a URL (scheme://host...)",
}
# An empty value of these kinds is a mistake, not "unset".
_REFUSES_EMPTY = (INTEGER, NUMBER, BOOLEAN, URL)


def _kind_mismatch(kind: str, raw: str) -> Optional[str]:
    """What a value of `kind` must be, when `raw` is not one."""
    if kind == INTEGER:
        ok = raw.isascii() and raw.isdigit()
    elif kind == NUMBER:
        ok = _is_decimal(raw)
    elif kind == BOOLEAN:
        ok = parse_bool(raw) is not None
    elif kind == URL:
        ok = _looks_like_url(raw)
    else:
        ok = True
    return None if ok else _EXPECTED[kind]


class EnvError(ValueError):
    """Why the environment is refused: every problem found, one per line.
    Holds names only, never a value."""

    def __init__(self, problems: Sequence["Problem"]) -> None:
        self.problems = tuple(problems)
        super().__init__("the environment is refused:" + "".join(f"\n  {problem}" for problem in self.problems))


@dataclass(frozen=True)
class Problem:
    """One reason the environment is refused. `kind` is `missing` (a
    required variable is not set) or `invalid` (a variable is set to
    something that is not of its type)."""

    kind: str
    name: str
    detail: str = ""

    def __str__(self) -> str:
        if self.kind == "missing":
            # `detail` of a variable that is required only with another: why.
            return f"{self.name} is required and is not set" + (f" ({self.detail})" if self.detail else "")
        return f"{self.name} is not valid: it must be {self.detail}"


def _environment() -> Mapping[str, str]:
    """The process environment. The only place it is read."""
    return os.environ


@dataclass(frozen=True, eq=False)
class EnvVar:
    """One environment variable: its single definition, and the way to
    read it."""

    name: str
    kind: str
    description: str
    example: str
    # Accepted values, in words.
    accepted: str
    # The value used when the variable is not set, written as it would be
    # in the environment. None: no default (the description says what
    # happens when it is unset).
    default: Optional[str] = None
    # The service refuses to start without it.
    required: bool = False
    # Never printed. A secret has no default and its example is a
    # `<placeholder>`.
    secret: bool = False
    edition: str = BOTH
    # The values it takes, when they are a closed list. Compared after
    # `normalize`.
    choices: Tuple[str, ...] = ()
    # Bounds of an integer or a decimal number, inclusive.
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # How a value is brought to its stored spelling before it is compared
    # with `choices` and handed out (`str.lower`, `str.upper`).
    normalize: Optional[Callable[[str], str]] = None
    # A further rule: what the value must be, when it is not; else None.
    check: Optional[Callable[[str], Optional[str]]] = None
    # An empty value means "unset" even for a type that otherwise refuses
    # one: for a variable whose empty value is a way to switch it off.
    empty_unsets: bool = False

    # --- validation -------------------------------------------------------

    def _spelled(self, raw: str) -> str:
        return self.normalize(raw) if self.normalize else raw

    def mismatch(self, raw: str) -> Optional[str]:
        """What a value of this variable must be, when `raw` is not one."""
        expected = _kind_mismatch(self.kind, raw.strip() if self.kind != STRING else raw)
        if expected:
            return expected
        if self.choices and self._spelled(raw.strip()) not in self.choices:
            return "one of " + ", ".join(self.choices)
        if self.kind in (INTEGER, NUMBER) and (self.minimum is not None or self.maximum is not None):
            value = float(raw.strip())
            if (self.minimum is not None and value < self.minimum) or (
                self.maximum is not None and value > self.maximum
            ):
                return f"{self.accepted} (in range)"
        if self.check is not None:
            return self.check(raw)
        return None

    def refuses_empty(self) -> bool:
        """An empty value of this variable is a mistake, not "unset"."""
        return self.kind in _REFUSES_EMPTY and not self.empty_unsets

    # --- reading -----------------------------------------------------------

    def _registered(self) -> None:
        if REGISTRY.find(self.name) is not self:
            raise LookupError(f"{self.name} is not registered: nothing is read that is not in the registry")

    def is_set(self) -> bool:
        """Set in the environment to something (not merely defaulted)."""
        self._registered()
        value = _environment().get(self.name)
        return value is not None and value.strip() != ""

    def _raw(self) -> Optional[str]:
        """The value as set, or the default; None when there is neither.
        A value that is not of the variable's type is refused here too,
        whenever it is read: it never becomes the default."""
        self._registered()
        value = _environment().get(self.name)
        if value is None:
            return self.default
        if value.strip() == "":
            if self.refuses_empty():
                raise EnvError([Problem("invalid", self.name, f"{_EXPECTED[self.kind]} (it is set but empty)")])
            return self.default
        expected = self.mismatch(value)
        if expected:
            raise EnvError([Problem("invalid", self.name, expected)])
        return value

    def string(self) -> Optional[str]:
        """The value (trimmed, in its stored spelling), or the default."""
        raw = self._raw()
        return None if raw is None else self._spelled(raw.strip())

    def exact(self) -> Optional[str]:
        """The value exactly as it is set (a password, a key), or the
        default."""
        return self._raw()

    def require(self) -> str:
        """`string()` of a required variable or one with a default."""
        value = self.string()
        if value is None:
            raise EnvError([Problem("missing", self.name)])
        return value

    def integer(self) -> Optional[int]:
        raw = self._raw()
        return None if raw is None else int(raw.strip())

    def number(self) -> Optional[float]:
        raw = self._raw()
        return None if raw is None else float(raw.strip())

    def flag(self) -> bool:
        """A boolean; unset with no default is false."""
        raw = self._raw()
        return bool(raw is not None and parse_bool(raw))

    def items(self) -> List[str]:
        """Comma-separated items, trimmed, empty ones dropped."""
        raw = self._raw()
        return [] if raw is None else [item.strip() for item in raw.split(",") if item.strip()]

    # --- writing: a command-line option that stands for the variable ----------

    def override(self, value: str) -> None:
        """Set the variable for this process and what it starts (a
        command-line option that takes precedence over the environment)."""
        self._registered()
        os.environ[self.name] = value

    def set_default(self, value: str) -> None:
        """Set the variable for this process where it is not set: for a
        tool that imports the application without running it (the models
        want a database URL to exist, and nothing connects to it)."""
        self._registered()
        os.environ.setdefault(self.name, value)


_SERVICE_LINK = re.compile(
    r"^[A-Z0-9_]+_(SERVICE_HOST|SERVICE_PORT(_[A-Z0-9_]+)?|PORT(_\d+_(TCP|UDP|SCTP)(_(PROTO|PORT|ADDR))?)?)$"
)


def is_service_link(name: str) -> bool:
    """A name a container platform injects for a service of the same
    namespace (`X_SERVICE_HOST`, `X_SERVICE_PORT`, `X_PORT`,
    `X_PORT_8100_TCP_ADDR`): not a setting anybody wrote."""
    return bool(_SERVICE_LINK.match(name))


def _wrap(text: str, width: int = 76) -> List[str]:
    return textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False) or [""]


class View:
    """The variables of one edition: what an entry point of that edition
    checks the environment against, prints and documents."""

    def __init__(self, title: str, variables: Sequence[EnvVar]) -> None:
        self.service = SERVICE
        self.title = title
        self.vars: Tuple[EnvVar, ...] = tuple(variables)

    def find(self, name: str) -> Optional[EnvVar]:
        return next((var for var in self.vars if var.name == name), None)

    def problems(
        self,
        environment: Optional[Mapping[str, str]] = None,
        *,
        missing: bool = True,
        reported_elsewhere: Sequence[EnvVar] = (),
    ) -> List[Problem]:
        """Everything wrong with `environment` (the process's own by
        default): values that are not of their variable's type, and, with
        `missing`, required variables that are not set (except those in
        `reported_elsewhere`: a check that follows says more about them).
        Names only."""
        environment = _environment() if environment is None else environment
        found: List[Problem] = []
        for var in self.vars:
            value = environment.get(var.name)
            if value is None:
                continue
            if value.strip() == "":
                if var.refuses_empty():
                    found.append(Problem("invalid", var.name, f"{_EXPECTED[var.kind]} (it is set but empty)"))
                continue
            expected = var.mismatch(value)
            if expected:
                found.append(Problem("invalid", var.name, expected))
        found.sort(key=str)
        if missing:
            found += [
                Problem("missing", var.name)
                for var in self.vars
                if var.required and var not in reported_elsewhere and not (environment.get(var.name) or "").strip()
            ]
        return found

    def unregistered(self, names: Optional[Iterable[str]] = None, prefix: str = PRODUCT_PREFIX) -> List[str]:
        """The names (of the process environment by default) that start
        with `prefix` and that this view does not register, sorted. A container platform's service links are left out
        (`is_service_link`). Names only."""
        names = _environment() if names is None else names
        return sorted({
            name for name in names
            if name.startswith(prefix)
            and self.find(name) is None
            and not is_service_link(name)
        })

    def defects(self) -> List[str]:
        """What is wrong with the registry itself. Empty for a sound one."""
        defects: List[str] = []
        seen = set()
        for var in self.vars:
            if var.name in seen:
                defects.append(f"{var.name} is registered twice")
            seen.add(var.name)
            if var.kind not in KINDS:
                defects.append(f"{var.name} has an unknown type {var.kind!r}")
                continue
            if var.edition not in (BOTH, ENTERPRISE):
                defects.append(f"{var.name} has an unknown edition {var.edition!r}")
            if not var.description.strip() or not var.example.strip():
                defects.append(f"{var.name} has no description or no example")
            if not var.accepted.strip():
                defects.append(f"{var.name} does not say what it accepts")
            if var.required and var.default is not None:
                defects.append(f"{var.name} is required and has a default")
            if var.secret and var.default is not None:
                defects.append(f"{var.name} is a secret with a default")
            if var.secret and not ("<" in var.example and ">" in var.example):
                defects.append(f"{var.name} is a secret whose example is not a <placeholder>")
            if var.default is not None and var.mismatch(var.default):
                defects.append(f"the default of {var.name} is not a valid value of it")
            if not var.secret and var.mismatch(var.example):
                defects.append(f"the example of {var.name} is not a valid value of it")
        return defects

    # --- generated documents --------------------------------------------------

    def json(self) -> dict:
        """The registry as a JSON document (what `--print-env` prints):

            {"schema_version": 1, "service": "control-plane",
             "variables": [{"name", "type", "default", "required", "secret",
                            "edition", "description", "example", "accepted"}]}
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "service": self.service,
            "variables": [
                {
                    "name": var.name,
                    "type": var.kind,
                    "default": var.default,
                    "required": var.required,
                    "secret": var.secret,
                    "edition": var.edition,
                    "description": var.description,
                    "example": var.example,
                    "accepted": var.accepted,
                }
                for var in self.vars
            ],
        }

    def json_text(self) -> str:
        return json.dumps(self.json(), indent=2, ensure_ascii=False) + "\n"

    def env_example(self, source: str, regenerate: str) -> str:
        """The `.env.example` file. Required variables are written out (a
        secret with nothing after the `=`), the others commented out with
        their example."""
        out = [
            f"# {self.title}: environment variables",
            "#",
            f"# Generated from {source}; do not edit by hand. To regenerate:",
            f"#   {regenerate}",
            "#",
            "# Copy to .env and fill in the required values: cp .env.example .env",
            "# The service loads .env from its working directory at start.",
        ]
        for var in self.vars:
            out.append("")
            out += [f"# {line}" for line in _wrap(var.description)]
            if var.required:
                default = "required"
            elif var.default is not None:
                default = f"default {var.default}"
            else:
                default = "no default"
            out.append(f"# {var.kind}; {default}{'; secret' if var.secret else ''}; edition: {var.edition}")
            out += [f"# {line}" for line in _wrap(f"Accepted: {var.accepted}")]
            if var.required and var.secret:
                out += [f"# Example: {var.example}", f"{var.name}="]
            elif var.required:
                out.append(f"{var.name}={var.example}")
            else:
                out.append(f"# {var.name}={var.example}")
        return "\n".join(out) + "\n"

    def markdown(self, only: Optional[Callable[[EnvVar], bool]] = None) -> str:
        """The configuration table of a README: the variables (those `only`
        accepts)."""

        def cell(text: str) -> str:
            return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")

        def code(text: str) -> str:
            return text.replace("|", "\\|")

        out = [
            "| Variable | Type | Default | Edition | Description | Accepted | Example |",
            "|---|---|---|---|---|---|---|",
        ]
        for var in self.vars:
            if only is not None and not only(var):
                continue
            if var.required:
                default = "**required**"
            elif var.default is not None:
                default = f"`{code(var.default)}`"
            else:
                default = "none"
            out.append(
                f"| `{var.name}`{' (secret)' if var.secret else ''} | {var.kind} | {default} | {var.edition} "
                f"| {cell(var.description)} | {cell(var.accepted)} | `{code(var.example)}` |"
            )
        return "\n".join(out) + "\n"


class Registry:
    """Every variable the process may read. The Community Edition's are
    registered when `app.env_vars` is imported; an extension's
    (`Extension.env_vars`) when it is composed."""

    def __init__(self) -> None:
        self._vars: Dict[str, EnvVar] = {}

    def register(self, variables: Iterable[EnvVar] = ()) -> None:
        """Add definitions. All of them, or none. The same definition
        registered again is skipped (an extension composed into a second
        application); a different one under a taken name is refused."""
        new_vars = dict(self._vars)
        for var in variables:
            present = new_vars.get(var.name)
            if present is not None and present is not var:
                raise ValueError(f"env registry: a different variable is already registered as {var.name}")
            new_vars[var.name] = var
        self._vars = new_vars

    def find(self, name: str) -> Optional[EnvVar]:
        return self._vars.get(name)

    def holds_enterprise_variables(self) -> bool:
        """Whether the Enterprise Edition's variables have joined: the
        process has loaded the Enterprise package."""
        return any(var.edition == ENTERPRISE for var in self._vars.values())

    def view(self, enterprise: bool) -> View:
        """The variables of the Community Edition, or of the Enterprise
        Edition (the Community ones and those the Enterprise extension
        registered)."""
        if not enterprise:
            return View(
                "Brutor Agent Control, Control Plane (Community Edition)",
                [var for var in self._vars.values() if var.edition == BOTH],
            )
        # Whatever was loaded first: the Community Edition's, then the
        # Enterprise Edition's.
        variables = [var for var in self._vars.values() if var.edition == BOTH] + [
            var for var in self._vars.values() if var.edition != BOTH
        ]
        return View("Brutor Agent Control, Control Plane (Enterprise Edition)", variables)


# The registry the process reads through.
REGISTRY = Registry()


# --- the process ---------------------------------------------------------------


def load_dotenv_file() -> None:
    """Load `.env` into the process environment: the one in or above the
    working directory, else the one in or above the source tree. A
    variable that is already set keeps its value."""
    from dotenv import find_dotenv, load_dotenv

    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    else:
        load_dotenv()


def print_requested(arguments: Optional[Sequence[str]] = None) -> bool:
    """True when the entry point was started with `--print-env`."""
    return PRINT_FLAG in (sys.argv[1:] if arguments is None else arguments)


def check(enterprise: bool, *, missing: bool = True, reported_elsewhere: Sequence[EnvVar] = ()) -> View:
    """Refuse the process environment (`EnvError`) for a value that is not
    of its variable's type and, with `missing`, a required variable that is not set (see
    `View.problems`). Returns the view it was checked against."""
    view = REGISTRY.view(enterprise)
    problems = view.problems(missing=missing, reported_elsewhere=reported_elsewhere)
    if problems:
        raise EnvError(problems)
    return view


def refuse_or_exit(
    enterprise: bool, *, missing: bool = False, reported_elsewhere: Sequence[EnvVar] = ()
) -> View:
    """`check` for an entry point: the refusal is printed, one line per
    name and never a value, and the process exits with status 1 before
    anything else reads configuration or connects."""
    try:
        return check(enterprise, missing=missing, reported_elsewhere=reported_elsewhere)
    except EnvError as refusal:
        print(f"ERROR: {refusal}", file=sys.stderr)
        raise SystemExit(1) from None


def unread_product_variables(enterprise: bool) -> List[str]:
    """The `BRUTOR_*` names that are set and that this edition does not
    read (an orchestrator injects such names for its service links; a
    setting of the other edition is one too). They do not stop the start:
    the caller says so in its log, by name."""
    return REGISTRY.view(enterprise).unregistered()


def child_environment(without: Sequence[str] = ()) -> Dict[str, str]:
    """A copy of the process environment for a child process, without the
    names in `without`."""
    return {name: value for name, value in _environment().items() if name not in without}


def expand_references(text: str) -> str:
    """`text` with `$NAME` and `${NAME}` replaced by the value of a
    variable that is set; a reference to one that is not set stays as it
    is written. For a setting whose value names another variable (a
    secret an orchestrator injects under its own name)."""
    return os.path.expandvars(text)
