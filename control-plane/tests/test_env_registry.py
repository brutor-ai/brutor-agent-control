# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The environment of the control plane is read in one place, through one
definition per variable.

`app/env_registry.py` is the registry and the only module that reads the
environment; `app/env_vars.py` is the Community Edition's table. Held
here, for the Community Edition (the Enterprise Edition's share is
`enterprise/tests/test_env_registry.py`):

- the table is sound, and every name in it is the product's own
  (`BRUTOR_*`) or one that belongs to something else and is kept;
- nothing of the Community package, the entry points, Alembic's
  environment, Gunicorn's configuration or the scripts reads the
  environment by itself;
- every variable has a reader outside the table;
- a value is read as its type says, and a malformed one is refused, by
  name, wherever it is read: it never becomes the default;
- a malformed value stops every real entry point, one line per name,
  before anything connects, and no refusal, log line or printed registry
  carries a value (a name the service does not read is not refused: no
  retired name is kept, owner decision 5);
- `.env.example` and the configuration tables are what the registry
  generates;
- the Community package names no variable of the Enterprise Edition;
- a variable shared with another component has that component's name.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

from app import env_registry, env_vars
from app.env_registry import BOTH, ENTERPRISE, REGISTRY, EnvError, EnvVar, Registry
from tests import entry_points
from tests.app_factory import create_app
from tests.env_reads import offences, python_files

REPO = Path(__file__).resolve().parents[1]
COMMUNITY = REGISTRY.view(enterprise=False)

# Names that are not the product's own and are kept as their owner spells
# them, with whose they are.
KEPT_NAMES = {
    "DATABASE_URL": "the database's, shared by every component of an installation",
    "REDIS_URL": "Redis's, shared with the Core Gateway",
    "JWT_SECRET": "one of the two secrets every component shares",
    "JWT_ALGORITHM": "beside JWT_SECRET, shared with the Core Gateway",
    "ENCRYPTION_KEY": "one of the two secrets every component shares",
    "GUNICORN_WORKERS": "Gunicorn's number of workers, read by gunicorn.conf.py",
}


# --- the table -----------------------------------------------------------------------


def test_the_table_is_sound():
    assert COMMUNITY.defects() == []
    assert len(COMMUNITY.vars) == 33
    assert all(var.edition == BOTH for var in COMMUNITY.vars)
    # Registered once, under the name of its constant.
    for var in COMMUNITY.vars:
        assert getattr(env_vars, var.name) is var and REGISTRY.find(var.name) is var
    assert tuple(COMMUNITY.vars[:3]) == (env_vars.DATABASE_URL, env_vars.JWT_SECRET, env_vars.ENCRYPTION_KEY)
    assert sorted(var.name for var in COMMUNITY.vars if var.required) == ["DATABASE_URL", "ENCRYPTION_KEY", "JWT_SECRET"]


def test_every_name_is_the_products_own_or_a_kept_one():
    for var in COMMUNITY.vars:
        if var.name.startswith(env_registry.PRODUCT_PREFIX):
            continue
        assert var.name in KEPT_NAMES, var.name
    names = {var.name for var in COMMUNITY.vars}
    assert set(KEPT_NAMES) <= names, "a kept name is no longer registered"
    # One spelling each: booleans say so, durations carry their unit.
    for var in COMMUNITY.vars:
        if not var.name.startswith(env_registry.PRODUCT_PREFIX):
            continue
        if var.kind == env_registry.BOOLEAN:
            assert var.name.endswith("_ENABLED"), var.name
        assert not var.name.endswith(("_SECONDS", "_TIMEOUT", "_RECYCLE")), var.name
        assert "SYSADMIN" not in var.name and "PROXY" not in var.name and "DEPLOYMENT_DRIVER" not in var.name


def test_a_secret_has_no_default_and_a_placeholder_for_an_example():
    secrets = [var for var in COMMUNITY.vars if var.secret]
    assert {"JWT_SECRET", "ENCRYPTION_KEY", "DATABASE_URL", "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD",
            "BRUTOR_GATEWAY_SERVICE_API_KEY"} <= {var.name for var in secrets}
    for var in secrets:
        assert var.default is None and "<" in var.example and ">" in var.example, var.name
    for var in COMMUNITY.vars:
        if re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", var.name) and not var.name.endswith(("_KEY_ID",)):
            assert var.secret, f"{var.name} looks like a secret and is not marked as one"


def test_the_pool_has_one_default_and_it_is_the_engines(monkeypatch):
    """The pool size had three defaults (20, 5 and 5): the engine's, a
    getter nothing called, and the number Gunicorn printed at start. The
    overflow is the pool size."""
    from app.database.database_config import load_database_config

    monkeypatch.delenv("BRUTOR_CONTROL_PLANE_DB_POOL_SIZE", raising=False)
    config = load_database_config()
    assert (config.pool_size, config.max_overflow) == (20, 20)
    assert env_vars.BRUTOR_CONTROL_PLANE_DB_POOL_SIZE.default == "20"
    text = (REPO / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert "E.BRUTOR_CONTROL_PLANE_DB_POOL_SIZE.integer()" in text
    monkeypatch.setenv("BRUTOR_CONTROL_PLANE_DB_POOL_SIZE", "7")
    config = load_database_config()
    assert (config.pool_size, config.max_overflow) == (7, 7)


# --- nothing else reads the environment ------------------------------------------------

COMMUNITY_SOURCES = ("app", "main.py", "gunicorn.conf.py", "alembic/env.py", "scripts")
# A developer script's own credential of a third party: not a setting of
# the control plane, and not read by anything that runs in it.
NOT_A_SETTING = {"scripts/judge_eval/run_judge_eval.py": ["os.environ"]}


def test_nothing_reads_the_environment_but_the_registry():
    files = python_files(*COMMUNITY_SOURCES)
    assert len(files) > 600
    found = [line for path in files for line in offences(path)]
    allowed = [
        line for line in found
        if any(line.startswith(f"{path}:") and line.split(": ", 1)[1] in whats for path, whats in NOT_A_SETTING.items())
    ]
    assert [line for line in found if line not in allowed] == []
    assert len(allowed) == 1, "the exemption no longer applies: remove it"
    # The registry module is the one place, and it is not exempt from being read.
    assert offences(REPO / "app" / "env_registry.py"), "the registry module no longer reads the environment"


def test_the_scan_sees_what_it_is_for(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import os\n"
        "import os as o\n"
        "from os import environ, getenv as g\n"
        "from os.path import expandvars\n"
        "from dotenv import load_dotenv\n"
        "import dotenv\n"
        "a = os.environ['A']\n"
        "b = os.getenv(name)\n"
        "c = o.environ.get(build('X', 'Y'))\n"
        "d = environ.setdefault('D', '1')\n"
        "e = g('E')\n"
        "f = dict(os.environ)\n"
        "os.putenv('G', '1')\n"
        "h = os.path.expandvars('$H')\n"
        "i = expandvars('$I')\n",
        encoding="utf-8",
    )
    assert len(offences(bad)) == 11
    good = tmp_path / "good.py"
    good.write_text(
        "import os\n"
        "from app.env_vars import GUNICORN_WORKERS\n"
        "text = 'os.environ.get(\"X\") and os.getenv(\"Y\")'  # a string is not a read\n"
        "workers = GUNICORN_WORKERS.integer()\n"
        "here = os.path.dirname(__file__)\n",
        encoding="utf-8",
    )
    assert offences(good) == []


# --- every variable has a reader ------------------------------------------------------------


def _references(paths, names) -> dict:
    """name -> the files that load the definition of that name (as a bare
    name it imported, or as an attribute of the table module)."""
    found = {name: set() for name in names}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        label = path.relative_to(REPO).as_posix()
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            if name in found:
                found[name].add(label)
    return found


def test_every_variable_has_a_reader_outside_the_table():
    names = [var.name for var in COMMUNITY.vars]
    files = [path for path in python_files(*COMMUNITY_SOURCES) if path.name != "env_vars.py"]
    readers = _references(files, names)
    assert [name for name in names if not readers[name]] == []
    # Gunicorn's settings are read by its configuration file and nowhere else.
    for name in names:
        if name.startswith("GUNICORN_"):
            assert readers[name] == {"gunicorn.conf.py"}, name


# Variables whose effect a test of this file shows; the others are read by
# the code path named, each proven by the suite that covers that path.
def test_a_variable_changes_what_its_reader_does(monkeypatch):
    from app.constants.proxy import gateway_service_api_key, proxy_internal_url
    from app.services import outbound_policy
    from app.services.concurrency.bulk_runner import derive_concurrency_cap
    from app.services.config_manager import ConfigManager

    config = ConfigManager(validate=False)
    monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", "http://core.internal:8100/")
    assert proxy_internal_url() == "http://core.internal:8100"
    monkeypatch.setenv("BRUTOR_GATEWAY_SERVICE_API_KEY", "brk_service")
    assert gateway_service_api_key() == "brk_service"
    monkeypatch.setenv("BRUTOR_CORS_ORIGINS", "https://a.example, https://b.example")
    assert config.get_cors_origins() == ["https://a.example", "https://b.example"]
    monkeypatch.setenv("BRUTOR_VERSION", "9.9.9")
    monkeypatch.setenv("BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "127.0.0.1:5999")
    assert (config.get_version(), config.get_port(), config.get_host()) == ("9.9.9", 5999, "127.0.0.1")
    assert config.get_gateway_base_url() == "http://127.0.0.1:5999"
    monkeypatch.setenv("BRUTOR_CONTROL_PLANE_PUBLIC_URL", "https://control.example.com")
    assert config.get_gateway_base_url() == "https://control.example.com"
    monkeypatch.setenv("BRUTOR_GATEWAY_DB_POOL_SIZE", "50")
    assert derive_concurrency_cap() == 30
    monkeypatch.setenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", "MCP.Internal., *.corp.example, 10.0.0.0/8")
    assert outbound_policy.host_allowed("mcp.internal") and outbound_policy.host_allowed("a.corp.example")
    assert outbound_policy.check_url("http://10.1.2.3/") is None


# --- how a value is read ---------------------------------------------------------------------


def _registry_of(*variables) -> Registry:
    registry = Registry()
    registry.register(variables)
    return registry


@pytest.mark.parametrize("raw, value", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("On", True),
    ("false", False), ("0", False), ("no", False), ("OFF", False), (" true ", True),
])
def test_a_boolean_takes_the_one_set_of_spellings(monkeypatch, raw, value):
    monkeypatch.setenv("BRUTOR_LOG_JSON_ENABLED", raw)
    assert env_vars.BRUTOR_LOG_JSON_ENABLED.flag() is value


@pytest.mark.parametrize("raw", ["maybe", "2", "enabled", "t", "y", "", "tru e"])
def test_any_other_boolean_is_refused_and_never_the_default(monkeypatch, raw):
    monkeypatch.setenv("BRUTOR_LOG_JSON_ENABLED", raw)
    with pytest.raises(EnvError) as refused:
        env_vars.BRUTOR_LOG_JSON_ENABLED.flag()
    assert "BRUTOR_LOG_JSON_ENABLED is not valid: it must be one of true, false, 1, 0, yes, no, on, off" in str(refused.value)
    assert [problem.name for problem in COMMUNITY.problems(missing=False)] == ["BRUTOR_LOG_JSON_ENABLED"]


@pytest.mark.parametrize("name, raw", [
    ("BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "0.0.0.0:5o50"), ("BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "0.0.0.0:0"),
    ("BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "0.0.0.0:70000"), ("BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "5051"),
    ("BRUTOR_CONTROL_PLANE_LISTEN_ADDR", "::1:5050"),
    ("BRUTOR_CONTROL_PLANE_DB_POOL_SIZE", "0"), ("BRUTOR_CONTROL_PLANE_DB_POOL_SIZE", "many"),
    ("BRUTOR_CONTROL_PLANE_DB_POOL_SIZE", ""), ("BRUTOR_DB_COMMAND_TIMEOUT_SECS", "1m"),
    ("GUNICORN_WORKERS", "0"), ("GUNICORN_WORKERS", "two"), ("GUNICORN_WORKERS", "-1"), ("GUNICORN_WORKERS", "2.0"),
    ("BRUTOR_LOG_LEVEL", "LOUD"), ("BRUTOR_OUTBOUND_ALLOW_HOSTS", "http://x.example"), ("JWT_ALGORITHM", "RS256"),
    ("BRUTOR_LOG_JSON_ENABLED", "sure"),
    ("BRUTOR_GATEWAY_INTERNAL_URL", "core:8100"), ("BRUTOR_GATEWAY_INTERNAL_URL", "ftp://core"),
    ("BRUTOR_GATEWAY_INTERNAL_URL", ""), ("REDIS_URL", "localhost:6379"), ("DATABASE_URL", "ai_gateway"),
    ("BRUTOR_SHARED_MCP_SERVERS", "{not json"), ("BRUTOR_SHARED_MCP_SERVERS", '{"slug": "x"}'),
    ("BRUTOR_LOCAL_WITNESS_PINNED_KEY", "abc"), ("BRUTOR_LOCAL_WITNESS_URL", "witness:8000"),
])
def test_a_malformed_value_is_refused_by_name_where_it_is_read_and_at_start(monkeypatch, name, raw):
    variable = REGISTRY.find(name)
    monkeypatch.setenv(name, raw)
    for read in (variable.string, variable.integer if variable.kind == "integer" else variable.exact):
        with pytest.raises(EnvError) as refused:
            read()
        assert f"{name} is not valid: it must be " in str(refused.value)
        if raw and len(raw) > 2:
            assert raw not in str(refused.value)
    problems = COMMUNITY.problems(missing=False)
    assert [(problem.kind, problem.name) for problem in problems] == [("invalid", name)]
    with pytest.raises(EnvError):
        env_registry.check(enterprise=False, missing=False)
    # A `ValueError`, for the callers that always caught one.
    assert issubclass(EnvError, ValueError)


def test_unset_and_blank_are_the_default_where_the_type_allows_it(monkeypatch):
    for name in ("BRUTOR_LOG_LEVEL", "BRUTOR_CORS_ORIGINS", "BRUTOR_LOCAL_WITNESS_URL", "BRUTOR_GITHUB_TOKEN",
                 "BRUTOR_CONTROL_PLANE_LISTEN_ADDR"):
        monkeypatch.delenv(name, raising=False)
    assert env_vars.BRUTOR_LOG_LEVEL.require() == "INFO"
    assert env_vars.BRUTOR_CONTROL_PLANE_LISTEN_ADDR.require() == "0.0.0.0:5050"
    assert env_vars.BRUTOR_CORS_ORIGINS.items() == ["http://localhost:3001", "http://localhost:3002"]
    assert env_vars.BRUTOR_GITHUB_TOKEN.string() is None and not env_vars.BRUTOR_GITHUB_TOKEN.is_set()
    # A blank string or list is "unset"; so is the witness address, whose
    # empty value is how the witness is switched off.
    for name in ("BRUTOR_LOG_LEVEL", "BRUTOR_CORS_ORIGINS", "BRUTOR_LOCAL_WITNESS_URL", "BRUTOR_GITHUB_TOKEN"):
        monkeypatch.setenv(name, "  ")
    assert env_vars.BRUTOR_LOG_LEVEL.require() == "INFO"
    assert env_vars.BRUTOR_CORS_ORIGINS.items() == ["http://localhost:3001", "http://localhost:3002"]
    assert env_vars.BRUTOR_LOCAL_WITNESS_URL.string() is None and env_vars.BRUTOR_GITHUB_TOKEN.string() is None
    assert COMMUNITY.problems(missing=False) == []


def test_a_value_is_handed_out_in_its_stored_spelling_and_a_secret_exactly(monkeypatch):
    monkeypatch.setenv("BRUTOR_LOG_LEVEL", " debug ")
    monkeypatch.setenv("JWT_ALGORITHM", "HS512")
    assert env_vars.BRUTOR_LOG_LEVEL.require() == "DEBUG"
    assert env_vars.JWT_ALGORITHM.string() == "HS512"
    monkeypatch.setenv("BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD", " a password with spaces ")
    assert env_vars.BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD.exact() == " a password with spaces "


@pytest.mark.parametrize("raw", ["warn", "WARN", "Warn", "warning", "WARNING", " Warning "])
def test_warn_and_warning_are_the_same_log_level_in_any_case(monkeypatch, raw):
    """The packages set one BRUTOR_LOG_LEVEL (error/warn/info/debug) for every
    service, Python and Rust (owner decision 7): `warn` is the Rust spelling."""
    monkeypatch.setenv("BRUTOR_LOG_LEVEL", raw)
    assert env_vars.BRUTOR_LOG_LEVEL.require() == "WARNING"
    assert COMMUNITY.problems(missing=False) == []
    from app.launcher import log_settings

    assert log_settings(None, False)[0] == "WARNING"
    assert log_settings(raw, False)[0] == "WARNING"


def test_the_command_line_log_level_takes_warn_too():
    from app import launcher

    option = next(param for param in launcher.run.params if param.name == "log_level")
    for raw in ("warn", "WARN", "warning", "Warning", "debug"):
        assert option.type.convert(raw, option, None), raw


def test_nothing_is_read_that_is_not_registered():
    stray = EnvVar(name="BRUTOR_NOT_REGISTERED", kind="string", description="d", example="e", accepted="a")
    for read in (stray.string, stray.exact, stray.flag, stray.items, stray.is_set, stray.require):
        with pytest.raises(LookupError, match="BRUTOR_NOT_REGISTERED is not registered"):
            read()
    # ... nor a second definition under a registered name.
    twin = EnvVar(name="GUNICORN_WORKERS", kind="integer", description="d", example="1", accepted="a")
    with pytest.raises(LookupError):
        twin.integer()
    with pytest.raises(ValueError, match="a different variable is already registered as GUNICORN_WORKERS"):
        REGISTRY.register([twin])
    REGISTRY.register([env_vars.GUNICORN_WORKERS])  # the same definition again is skipped


def test_the_registry_says_what_is_wrong_with_a_table():
    def var(**over):
        return EnvVar(**{"name": "BRUTOR_X", "kind": "string", "description": "d", "example": "e", "accepted": "a", **over})

    view = _registry_of(
        var(name="BRUTOR_A", secret=True, default="s", example="plain"),
        var(name="BRUTOR_B", required=True, default="1"),
        var(name="BRUTOR_C", kind="integer", default="many", example="1"),
        var(name="BRUTOR_D", kind="boolean", example="perhaps"),
        var(name="BRUTOR_E", description=" ", accepted=""),
        var(name="BRUTOR_G", choices=("a", "b"), default="c"),
    ).view(enterprise=False)
    assert view.defects() == [
        "BRUTOR_A is a secret with a default",
        "BRUTOR_A is a secret whose example is not a <placeholder>",
        "BRUTOR_B is required and has a default",
        "the default of BRUTOR_C is not a valid value of it",
        "the example of BRUTOR_D is not a valid value of it",
        "BRUTOR_E has no description or no example",
        "BRUTOR_E does not say what it accepts",
        "the default of BRUTOR_G is not a valid value of it",
        "the example of BRUTOR_G is not a valid value of it",
    ]


# --- names the service does not read -------------------------------------------------------------------


def test_a_token_of_another_program_is_neither_read_nor_refused(monkeypatch):
    """`GITHUB_TOKEN` was a second name of `BRUTOR_GITHUB_TOKEN`. It is a
    name CI runners and developers' shells set for their own use: the
    control plane does not read it, and does not refuse a start for it."""
    monkeypatch.setenv("GITHUB_TOKEN", "somebody-elses")
    monkeypatch.delenv("BRUTOR_GITHUB_TOKEN", raising=False)
    assert env_vars.BRUTOR_GITHUB_TOKEN.string() is None
    assert COMMUNITY.problems(missing=False) == []


def test_the_community_factory_refuses_by_itself(monkeypatch):
    """Whatever the edition of the run: the Community application is not
    built over a malformed value."""
    from tests.app_factory import create_community_app

    monkeypatch.setenv("BRUTOR_LOG_JSON_ENABLED", "perhaps")
    with pytest.raises(EnvError, match="BRUTOR_LOG_JSON_ENABLED is not valid"):
        create_community_app()


def test_every_problem_is_reported_at_once_one_line_per_name(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")  # not read: not a problem
    monkeypatch.setenv("BRUTOR_LOG_JSON_ENABLED", "perhaps")
    monkeypatch.setenv("GUNICORN_WORKERS", "fifty")
    monkeypatch.delenv("ENCRYPTION_KEY")
    problems = COMMUNITY.problems()
    assert [(p.kind, p.name) for p in problems] == [
        ("invalid", "BRUTOR_LOG_JSON_ENABLED"), ("invalid", "GUNICORN_WORKERS"), ("missing", "ENCRYPTION_KEY"),
    ]
    lines = str(EnvError(problems)).splitlines()
    assert lines[0] == "the environment is refused:" and len(lines) == 4
    assert lines[-1] == "  ENCRYPTION_KEY is required and is not set"
    assert not [line for line in lines if "DEBUG" in line or "fifty" in line or "perhaps" in line]


# --- the real entry points -------------------------------------------------------------------------------

SECRET = "sentinel-secret-value-never-to-be-printed"
MALFORMED_ENVIRONMENT = {
    "BRUTOR_GATEWAY_SERVICE_API_KEY": SECRET,
    "BRUTOR_LOG_JSON_ENABLED": "perhaps",
    "BRUTOR_GATEWAY_DB_POOL_SIZE": "many",
    "LOG_LEVEL": "DEBUG",  # a name the service does not read: not refused
    "JWT_SECRET": f"{SECRET}-jwt-0123456789abcdef",
    "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": f"{SECRET}-password",
}
REFUSAL = [
    "BRUTOR_GATEWAY_DB_POOL_SIZE is not valid: it must be a whole number written in digits only",
    "BRUTOR_LOG_JSON_ENABLED is not valid: it must be one of true, false, 1, 0, yes, no, on, off",
]
COMMUNITY_ENTRY_POINTS = [
    ("script", "main.py", []),
    ("script", "main.py", ["init"]),
    ("script", "main.py", ["edition-reset"]),
    ("script", "main.py", ["admin-unlock", "admin"]),
    ("script", "main.py", ["--show-config"]),
    ("alembic", "", ["-c", "alembic.ini", "current"]),
    ("gunicorn-config", "gunicorn.conf.py", ["app.app:create_app", "--config", "gunicorn.conf.py"]),
]


def _id(entry) -> str:
    kind, target, arguments = entry
    return " ".join([kind if kind != "script" else target, *arguments[:2]])


@pytest.mark.parametrize("entry", COMMUNITY_ENTRY_POINTS, ids=_id)
def test_an_entry_point_refuses_a_malformed_value_before_it_does_anything(entry):
    kind, target, arguments = entry
    done = entry_points.run(kind, target, arguments, MALFORMED_ENVIRONMENT)
    assert done.status == 1, done.output
    assert done.refusal_lines() == REFUSAL, done.output
    # Nothing else was said, and nothing was started, installed or connected to.
    assert done.stdout == "", done.stdout
    assert done.stderr.count("\n") == 1 + len(REFUSAL), done.stderr
    assert SECRET not in done.output and "perhaps" not in done.output and "many" not in done.output


@pytest.mark.parametrize("entry", [COMMUNITY_ENTRY_POINTS[0], COMMUNITY_ENTRY_POINTS[1], COMMUNITY_ENTRY_POINTS[-1]], ids=_id)
def test_an_entry_point_that_runs_or_installs_refuses_a_missing_or_malformed_variable(entry):
    kind, target, arguments = entry
    malformed = (
        "BRUTOR_CONTROL_PLANE_LISTEN_ADDR is not valid: it must be host:port (an IPv6 address in brackets, "
        "[::]:5050), port 1 to 65535"
    )
    missing = "ENCRYPTION_KEY is required and is not set"
    secret = {"JWT_SECRET": f"{SECRET}-jwt-0123456789"}
    done = entry_points.run(kind, target, arguments, {"ENCRYPTION_KEY": "", "BRUTOR_CONTROL_PLANE_LISTEN_ADDR": "fifty", **secret})
    assert done.status == 1, done.output
    # Gunicorn's configuration says everything at once. The command line
    # says what is wrong with what is set first (every command does), and
    # what is missing where a command needs it.
    assert done.refusal_lines() == ([malformed, missing] if kind == "gunicorn-config" else [malformed]), done.output
    assert SECRET not in done.output and "fifty" not in done.output
    done = entry_points.run(kind, target, arguments, {"ENCRYPTION_KEY": "", **secret})
    assert done.status == 1 and done.refusal_lines() == [missing], done.output
    assert done.stdout == "" and SECRET not in done.output


def test_the_commands_that_serve_nothing_need_only_what_they_read():
    """`edition-reset` and `admin-unlock` work on the database: a missing
    secret of the application does not stop them (the database, which is
    not there, does)."""
    for arguments in (["edition-reset"], ["admin-unlock", "admin"]):
        done = entry_points.run("script", "main.py", arguments, {"ENCRYPTION_KEY": "", "JWT_SECRET": ""})
        assert done.refusal_lines() == [], done.output
        assert "the environment is refused" not in done.output


# --- what is printed and generated ----------------------------------------------------------------------------


def test_print_env_prints_the_registry_and_nothing_of_the_environment():
    done = entry_points.run("script", "main.py", ["--print-env"], {**MALFORMED_ENVIRONMENT, "GUNICORN_WORKERS": "fifty"})
    assert done.status == 0 and done.stderr == "", done.output
    document = json.loads(done.stdout)
    assert document == COMMUNITY.json() and done.stdout == COMMUNITY.json_text()
    assert set(document) == {"schema_version", "service", "variables"}
    assert document["schema_version"] == 1 and document["service"] == "control-plane"
    for variable in document["variables"]:
        assert set(variable) == {
            "name", "type", "default", "required", "secret", "edition", "description", "example", "accepted",
        }
        assert variable["type"] in env_registry.KINDS and variable["edition"] == "both"
        assert isinstance(variable["required"], bool)
    assert SECRET not in done.stdout and "fifty" not in done.stdout


def _generator():
    spec = importlib.util.spec_from_file_location("gen_env_docs", REPO / "scripts" / "gen_env_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMMUNITY_DOCUMENTS = (".env.example", "README.md", "docs/getting-started-guide.md")


def test_the_generated_files_are_what_the_registry_generates():
    generator = _generator()
    generated = generator.community_documents(lambda path: (REPO / path).read_text(encoding="utf-8"))
    assert tuple(generated) == COMMUNITY_DOCUMENTS
    for path, text in generated.items():
        assert (REPO / path).read_text(encoding="utf-8") == text, f"{path}: run `python scripts/gen_env_docs.py`"
    example = generated[".env.example"]
    for var in COMMUNITY.vars:
        assert re.search(rf"^(# )?{var.name}=", example, re.M), var.name
    # Required variables are written out, a secret with nothing after the `=`.
    assert "\nDATABASE_URL=\n" in example and "\nJWT_SECRET=\n" in example
    assert "\n# BRUTOR_CONTROL_PLANE_LISTEN_ADDR=127.0.0.1:5050\n" in example
    readme = generated["README.md"]
    assert readme.count("| `") >= len(COMMUNITY.vars)
    assert "Retired name" not in readme and "| `LOG_LEVEL` |" not in readme
    guide = generated["docs/getting-started-guide.md"]
    table = guide.split(generator.BEGIN, 1)[1].split(generator.END, 1)[0]
    assert re.findall(r"^\| `([A-Z_]+)`", table, re.M) == [
        "DATABASE_URL", "JWT_SECRET", "ENCRYPTION_KEY", "REDIS_URL", "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD",
    ]


def test_a_changed_definition_shows_in_every_generated_file(monkeypatch):
    generator = _generator()
    before = generator.community_documents(lambda path: (REPO / path).read_text(encoding="utf-8"))
    original = env_vars.GUNICORN_WORKERS.description
    object.__setattr__(env_vars.GUNICORN_WORKERS, "description", "A description nobody generated the files from.")
    try:
        after = generator.community_documents(lambda path: (REPO / path).read_text(encoding="utf-8"))
    finally:
        object.__setattr__(env_vars.GUNICORN_WORKERS, "description", original)
    assert after[".env.example"] != before[".env.example"] and after["README.md"] != before["README.md"]
    assert COMMUNITY.env_example("app/env_vars.py", generator.REGENERATE) == before[".env.example"]


def test_the_docs_directory_holds_the_guide():
    """`docs/` holds the getting-started guide and nothing else."""
    found = sorted(path.relative_to(REPO / "docs").as_posix() for path in (REPO / "docs").rglob("*") if path.is_file())
    assert found == [
        "getting-started-guide.md",
    ]


# --- the other edition's variables ---------------------------------------------------------------------------------


def _enterprise_names() -> list:
    """The names the Enterprise Edition's table defines, read from its
    source without loading it; empty where this tree has no Enterprise
    package."""
    table = REPO / "enterprise" / "brutor_enterprise" / "env_vars.py"
    if not table.is_file():
        return []
    names = []
    for node in ast.walk(ast.parse(table.read_text(encoding="utf-8"))):
        if isinstance(node, ast.keyword) and node.arg == "name" and isinstance(node.value, ast.Constant):
            names.append(node.value.value)
    return names


THIRD_PARTY_NAMES_IN = "app/database/catalog_data/"


def _token(name: str):
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def test_the_community_package_names_no_variable_of_the_enterprise_edition():
    names = _enterprise_names()
    if not names:
        pytest.skip("this tree has no Enterprise package: there is no list of its variables to look for")
    assert len(names) > 25 and "BRUTOR_LICENSE_KEY" in names
    files = python_files("app", "main.py", "gunicorn.conf.py", "alembic/env.py") + [REPO / "app" / "env_registry.py"]
    generated = [REPO / ".env.example", REPO / "README.md", REPO / "docs" / "getting-started-guide.md", REPO / "Dockerfile"]
    found = []
    for path in files + generated:
        text = path.read_text(encoding="utf-8")
        label = path.relative_to(REPO).as_posix()
        if path.name == "README.md":
            # The generated table and the configuration section; the README
            # also describes how the Enterprise Edition is run.
            text = text.split("## Configuration", 1)[1].split("\n## ", 1)[0]
        found += [f"{label}: {name}" for name in names if _token(name).search(text)]
    # A name that belongs to another program (a cloud SDK's credential
    # variables) is also what a server of the MCP catalog is configured
    # with: the catalog's data may spell it.
    found = [
        line for line in found
        if not (line.startswith(THIRD_PARTY_NAMES_IN) and not line.split(": ")[1].startswith("BRUTOR_"))
    ]
    assert found == []
    assert not {var.name for var in COMMUNITY.vars} & set(names)


def test_a_community_start_warns_of_the_product_variables_it_does_not_read_and_starts(monkeypatch):
    """An orchestrator sets `BRUTOR_*` names of its own, and an operator may
    hand a Community control plane a setting of the Enterprise Edition: it
    has no effect, is named, and does not stop the start."""
    if REGISTRY.holds_enterprise_variables():
        pytest.skip("an Enterprise run reads the Enterprise Edition's variables")
    monkeypatch.setenv("BRUTOR_LICENSE_KEY", "licence-value-that-must-not-leak")
    monkeypatch.setenv("BRUTOR_MISSPELT_SETING", "x")
    monkeypatch.setenv("BRUTOR_CORE_SERVICE_HOST", "10.0.0.7")
    monkeypatch.setenv("BRUTOR_CORE_PORT_8100_TCP_ADDR", "10.0.0.7")
    monkeypatch.setenv("SOMETHING_ELSE", "x")
    unread = env_registry.unread_product_variables(enterprise=False)
    assert "BRUTOR_LICENSE_KEY" in unread and "BRUTOR_MISSPELT_SETING" in unread
    assert not [name for name in unread if "SERVICE_HOST" in name or "_TCP_" in name or name == "SOMETHING_ELSE"]
    from app.core.logging_system import BrutorLogger

    warned = []
    monkeypatch.setattr(BrutorLogger, "warning", lambda self, message, *a, **k: warned.append(str(message)))
    assert create_app() is not None
    said = [message for message in warned if "does not read them" in message]
    assert len(said) == 1 and "BRUTOR_LICENSE_KEY" in said[0] and "BRUTOR_MISSPELT_SETING" in said[0]
    assert "Community Edition" in said[0] and "licence-value-that-must-not-leak" not in said[0]


def test_a_service_link_is_told_from_a_setting():
    for link in ("BRUTOR_CORE_SERVICE_HOST", "BRUTOR_CORE_SERVICE_PORT", "BRUTOR_CORE_SERVICE_PORT_HTTP",
                 "BRUTOR_CORE_PORT", "BRUTOR_CORE_PORT_8100_TCP", "BRUTOR_CORE_PORT_8100_TCP_PROTO",
                 "BRUTOR_CORE_PORT_8100_TCP_PORT", "BRUTOR_CORE_PORT_8100_TCP_ADDR"):
        assert env_registry.is_service_link(link), link
    for setting in ("BRUTOR_LICENSE_KEY", "BRUTOR_VERSION", "BRUTOR_GATEWAY_INTERNAL_URL", "BRUTOR_PORT_RANGE",
                    "BRUTOR_K8S_SERVICE_TYPE", "BRUTOR_MCP_DEPLOY_HOST_PORT_RANGE"):
        assert not env_registry.is_service_link(setting), setting


# --- the names other components read -----------------------------------------------------------------------------------

# variable of this table -> the components that read the same variable,
# under the same name: (repository beside this one, file at its HEAD). The
# core is not looked for beside this one: it is where `BRUTOR_CORE_REPO`
# says (`tests/core_repo.py`), and `CORE` stands for it.
CORE = "the core (BRUTOR_CORE_REPO)"
GATEWAY = (CORE, ".env.example")
KB_UPLOADER = (
    "brutor-gateway-kb-uploader",
    ".env.example",
)
KB_CONNECTOR_SYNC = (
    "brutor-kb-connector-sync",
    ".env.example",
)
ADMIN_CONSOLE = ("brutor-gateway-admin", "src/envRegistry.json")
SHARED = {
    "DATABASE_URL": [GATEWAY, KB_UPLOADER, KB_CONNECTOR_SYNC],
    "REDIS_URL": [GATEWAY, KB_UPLOADER],
    "JWT_SECRET": [GATEWAY],
    "JWT_ALGORITHM": [GATEWAY],
    "ENCRYPTION_KEY": [GATEWAY, KB_CONNECTOR_SYNC],
    "BRUTOR_CORS_ORIGINS": [GATEWAY],
    "BRUTOR_GATEWAY_DB_POOL_SIZE": [GATEWAY],
    "BRUTOR_OUTBOUND_ALLOW_HOSTS": [GATEWAY, KB_CONNECTOR_SYNC],
    "BRUTOR_GATEWAY_INTERNAL_URL": [KB_UPLOADER],
    "BRUTOR_GATEWAY_SERVICE_API_KEY": [KB_UPLOADER],
    "BRUTOR_CONTROL_PLANE_PUBLIC_URL": [ADMIN_CONSOLE],
    "BRUTOR_EMBEDDING_MODEL": [GATEWAY, KB_UPLOADER],
}


def sibling_names(repository: str, path: str):
    """The variable names `path` of the repository beside this one defines
    at its committed HEAD; None where the repository, or the file at its
    HEAD, is not there. `CORE` is the core, where `BRUTOR_CORE_REPO` says;
    None when it is not set."""
    if repository == CORE:
        from tests.core_repo import core_checkout_or_none

        checkout = core_checkout_or_none()
    else:
        checkout = REPO.parent / repository
    if checkout is None or not (checkout / ".git").exists():
        return None
    done = subprocess.run(
        ["git", "-C", str(checkout), "show", f"HEAD:{path}"], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        return None
    if path.endswith(".json"):
        return {entry["name"] for entry in json.loads(done.stdout).get("variables", [])}
    return set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]*)=", done.stdout, re.M))


def test_a_shared_variable_has_the_name_the_other_component_reads():
    assert set(SHARED) <= {var.name for var in COMMUNITY.vars}
    checked, absent, wrong = 0, set(), []
    for name, readers in SHARED.items():
        for repository, path in readers:
            names = sibling_names(repository, path)
            if names is None:
                absent.add(f"{repository}:{path}")
                continue
            checked += 1
            if name not in names:
                wrong.append(f"{name} is not a variable of {repository} ({path} at its HEAD)")
    assert wrong == []
    if absent:
        pytest.skip(
            f"{checked} pairs hold; not checked, because the repository is not beside this one or does not "
            f"have the file at its HEAD: {', '.join(sorted(absent))}"
        )
    assert checked == sum(len(readers) for readers in SHARED.values())
