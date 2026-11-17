# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The walls between the test suite and the host's Docker daemon and
Kubernetes cluster stand (`tests/hermetic.py`, root `conftest.py`).

Held here, for both editions: the environment of the test process, the
guard on a real client and on a `docker` / `kubectl` / `helm` process, and
that no test module brings a real client of its own or takes a wall down.
What the deployment routes do behind the walls is in
`enterprise/tests/test_hermetic_deployment_routes.py`.
"""

from __future__ import annotations

import ast
import os
import socket
import subprocess
from pathlib import Path

import pytest

from tests import hermetic

REPO = Path(__file__).resolve().parents[1]
TEST_ROOTS = (REPO / "tests", REPO / "enterprise" / "tests")


@pytest.fixture()
def expected_violations():
    """A test that walks into a wall on purpose takes its own entries out
    of the list the session is failed for."""
    before = len(hermetic.VIOLATIONS)
    yield hermetic.VIOLATIONS
    del hermetic.VIOLATIONS[before:]


def test_the_root_conftest_put_the_walls_up(hermetic_host, request):
    assert hermetic_host is hermetic and hermetic.installed()
    # It is loaded in this session, and its fixture is in force for this
    # test without being asked for.
    plugins = [getattr(plugin, "__file__", None) for plugin in request.config.pluginmanager.get_plugins()]
    assert str(REPO / "conftest.py") in plugins
    assert "hermetic_host" in request.fixturenames
    definition = request._fixturemanager.getfixturedefs("hermetic_host", request.node)[-1]
    assert definition.scope == "session" and definition.baseid == ""
    # One conftest above both suites, and it is this repository's root.
    assert (REPO / "conftest.py").is_file()
    source = (REPO / "conftest.py").read_text(encoding="utf-8")
    assert "hermetic.install()" in source and "autouse=True" in source and 'scope="session"' in source
    for root in TEST_ROOTS:
        assert REPO in root.parents


def test_the_environment_names_no_daemon_and_no_cluster():
    assert os.environ["DOCKER_HOST"] == hermetic.DEAD_DOCKER_HOST
    assert not os.path.exists(hermetic.DEAD_DOCKER_SOCKET)
    assert os.environ["KUBECONFIG"] == hermetic.EMPTY_KUBECONFIG
    assert os.path.getsize(hermetic.EMPTY_KUBECONFIG) == 0
    for name in hermetic.REMOVED_ENV:
        assert name not in os.environ
    # A process the suite starts inherits it.
    out = subprocess.run(
        ["/usr/bin/env"], capture_output=True, text=True, check=True
    ).stdout
    assert f"DOCKER_HOST={hermetic.DEAD_DOCKER_HOST}" in out and f"KUBECONFIG={hermetic.EMPTY_KUBECONFIG}" in out


@pytest.mark.parametrize("program", sorted(hermetic.REFUSED_PROGRAMS))
def test_a_docker_kubectl_or_helm_process_is_not_started(program, expected_violations):
    for start in (
        lambda: subprocess.run([program, "version"], capture_output=True),
        lambda: subprocess.Popen(f"/usr/local/bin/{program} ps", shell=True),
        lambda: subprocess.check_output([f"/opt/homebrew/bin/{program}"]),
    ):
        with pytest.raises(hermetic.HermeticViolation):
            start()
    assert len(expected_violations) >= 3 and program in expected_violations[-1]


def test_a_real_docker_client_is_refused_and_the_dead_socket_is_not(expected_violations):
    docker = pytest.importorskip("docker")
    assert docker.from_env is hermetic.fake_docker_from_env
    assert isinstance(docker.from_env(), hermetic.FakeDockerClient)
    for build in (
        lambda: docker.DockerClient(base_url="unix:///var/run/docker.sock"),
        lambda: docker.DockerClient(),
        lambda: docker.APIClient(base_url="tcp://127.0.0.1:2375"),
        lambda: docker.APIClient(),
    ):
        with pytest.raises(hermetic.HermeticViolation):
            build()
    assert len(expected_violations) >= 4
    # The library's own `from_env` reads the environment of this process:
    # the socket that does not exist. The guard lets it by, and it reaches
    # nothing (the library asks the daemon for its version, and finds none).
    with pytest.raises(docker.errors.DockerException) as unreachable:
        docker.DockerClient.from_env()
    assert "No such file or directory" in str(unreachable.value)


def test_the_kubernetes_client_finds_no_cluster_and_sends_nothing(expected_violations):
    kubernetes = pytest.importorskip("kubernetes")
    from kubernetes.config.config_exception import ConfigException

    before = len(hermetic.KUBERNETES_CALLS)
    for load in (kubernetes.config.load_kube_config, kubernetes.config.load_incluster_config):
        with pytest.raises(ConfigException):
            load()
    assert [name for name, _ in hermetic.KUBERNETES_CALLS[before:]] == ["load_kube_config", "load_incluster_config"]
    # A client for a cluster is not built...
    cluster = kubernetes.client.Configuration()
    cluster.host = "https://cluster.example:6443"
    with pytest.raises(hermetic.HermeticViolation):
        kubernetes.client.ApiClient(cluster)
    # ... and one for the library's default host sends nothing.
    api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient(kubernetes.client.Configuration()))
    with pytest.raises(hermetic.HermeticViolation):
        api.list_namespace()
    assert len(expected_violations) >= 2


def test_the_fake_daemon_is_empty_and_changes_nothing():
    docker = pytest.importorskip("docker")
    client = docker.from_env()
    before = len(hermetic.DOCKER_CALLS)
    assert client.ping() is True and client.containers.list(all=True) == [] and client.images.list() == []
    assert client.containers.prune() == {"ContainersDeleted": None, "SpaceReclaimed": 0}
    assert client.images.prune() == {"ImagesDeleted": None, "SpaceReclaimed": 0}
    for call in (
        lambda: client.containers.get("x"),
        lambda: client.images.remove("x"),
        lambda: client.containers.run(image="x"),
        lambda: client.images.pull("x", tag="1"),
        lambda: client.images.build(path=".", tag="x"),
    ):
        with pytest.raises(docker.errors.DockerException):
            call()
    recorded = [name for name, _ in hermetic.DOCKER_CALLS[before:]]
    assert recorded == [
        "ping", "containers.list", "images.list", "containers.prune", "images.prune",
        "containers.get", "images.remove", "containers.run", "images.pull", "images.build",
    ]
    assert not hasattr(client, "api")


# --- the network -------------------------------------------------------------------


def _since(mark: int) -> list:
    return [(kind, destination, verdict) for _test, kind, destination, verdict in hermetic.NETWORK_CALLS[mark:]]


def test_the_gateway_of_the_session_is_a_port_nothing_listens_on():
    from app.constants.proxy import DEFAULT_PROXY_INTERNAL_URL, proxy_internal_url

    assert os.environ["BRUTOR_GATEWAY_INTERNAL_URL"] == hermetic.DEAD_GATEWAY_URL == "http://127.0.0.1:1"
    assert proxy_internal_url() == hermetic.DEAD_GATEWAY_URL != DEFAULT_PROXY_INTERNAL_URL
    # The address it replaces is one a developer's own gateway listens on.
    assert DEFAULT_PROXY_INTERNAL_URL.endswith(":8100")


@pytest.mark.parametrize("address", [("192.0.2.1", 443), ("203.0.113.7", 53), ("2001:db8::1", 443, 0, 0), ("198.51.100.5", 5432)])
def test_no_connection_leaves_this_machine(address, expected_violations):
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    mark = len(hermetic.NETWORK_CALLS)
    for attempt in ("connect", "connect_ex"):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            with pytest.raises(hermetic.HermeticViolation, match="outbound connection"):
                getattr(sock, attempt)(address)
    with pytest.raises(hermetic.HermeticViolation):
        socket.create_connection(address[:2], timeout=0.2)
    assert _since(mark) == [("connect", f"{address[0]}:{address[1]}", "refused")] * 3
    assert len(expected_violations) >= 3 and address[0] in expected_violations[-1]


@pytest.mark.parametrize("name", ["hub.docker.com", "tools.internal", "registry.example"])
def test_no_name_is_asked_of_a_dns_server(name, expected_violations):
    mark = len(hermetic.NETWORK_CALLS)
    with pytest.raises(hermetic.HermeticViolation, match="name lookup"):
        socket.getaddrinfo(name, 443)
    with pytest.raises(hermetic.HermeticViolation):
        socket.create_connection((name, 443), timeout=0.2)
    assert _since(mark) == [("resolve", name, "refused")] * 2
    # This machine's own names and literal addresses are not questions.
    mark = len(hermetic.NETWORK_CALLS)
    assert socket.getaddrinfo("localhost", 80) and socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo("192.0.2.1", 80) and socket.getaddrinfo(None, 80)
    assert _since(mark) == []


def test_an_http_client_of_the_product_is_stopped_before_anything_is_sent(expected_violations):
    """`httpx` is what the Docker Hub routes, the model probes and the
    registry calls use; `asyncio` resolves through the same function."""
    import asyncio

    import httpx

    with pytest.raises(hermetic.HermeticViolation):
        httpx.get("https://hub.docker.com/v2/repositories/mcp/", timeout=1.0)

    async def asked() -> None:
        async with httpx.AsyncClient(timeout=1.0) as client:
            await client.get("https://hub.docker.com/v2/repositories/mcp/")

    with pytest.raises(hermetic.HermeticViolation):
        asyncio.run(asked())
    with pytest.raises(hermetic.HermeticViolation):
        httpx.get("http://192.0.2.1/", timeout=1.0)
    assert [line for line in expected_violations if "hub.docker.com" in line]


def test_a_port_of_this_machine_the_suite_did_not_start_is_refused(expected_violations):
    mark = len(hermetic.NETWORK_CALLS)
    # A gateway, a control plane, a database, a Redis a developer may run.
    for port in (8100, 5050, 5432, 6379):
        for host in ("127.0.0.1", "::1", "localhost"):
            family = socket.AF_INET6 if host == "::1" else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                with pytest.raises(hermetic.HermeticViolation, match="the suite did not start"):
                    sock.connect((host, port))
    assert len(_since(mark)) == 12 and {verdict for _k, _d, verdict in _since(mark)} == {"refused"}
    assert len(expected_violations) >= 12
    # A port nothing listens on is the machine's to refuse, at once.
    mark = len(hermetic.NETWORK_CALLS)
    for port in sorted(hermetic.DEAD_PORTS):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(ConnectionRefusedError):
                sock.connect(("127.0.0.1", port))
    assert _since(mark) == [("connect", f"127.0.0.1:{port}", "loopback") for port in sorted(hermetic.DEAD_PORTS)]
    assert hermetic.DEAD_PORTS == {1, 9}
    # A Unix socket is a file: the dead Docker socket is not there.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        with pytest.raises(FileNotFoundError):
            sock.connect(hermetic.DEAD_DOCKER_SOCKET)


def _connect_from(filename: str, port: int) -> None:
    """Connect to a loopback port from code whose file is `filename`."""
    scope: dict = {}
    source = (
        "import socket\n"
        "def go(port):\n"
        "    with socket.create_connection(('127.0.0.1', port), timeout=2) as s:\n"
        "        return s.getpeername()\n"
    )
    exec(compile(source, filename, "exec"), scope)  # noqa: S102 - the file name is what is under test
    scope["go"](port)


def test_listed_code_may_reach_a_loopback_server_and_other_code_may_not(expected_violations):
    """The opt-in is by where the code is, so a conftest's session fixture
    is covered as a test of the directory is."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        assert port not in hermetic.DEAD_PORTS
        mark = len(hermetic.NETWORK_CALLS)
        _connect_from(str(REPO / "tests" / "live" / "conftest.py"), port)
        _connect_from(str(REPO / "tests" / "live" / "governance" / "helpers.py"), port)
        assert _since(mark) == [("connect", f"127.0.0.1:{port}", "opt-in tests/live/")] * 2
        before = len(expected_violations)
        for elsewhere in (
            str(REPO / "tests" / "test_something.py"),
            # (not a file under `app/`: the coverage run would look for it)
            str(REPO / "scripts" / "x.py"),
            str(REPO / "tests" / "live_not" / "conftest.py"),
            "/somewhere/else/tests/live/conftest.py",
        ):
            with pytest.raises(hermetic.HermeticViolation):
                _connect_from(elsewhere, port)
        assert len(expected_violations) == before + 4
    # Listed code gets a port of this machine, never the network.
    with pytest.raises(hermetic.HermeticViolation, match="outbound"):
        exec(  # noqa: S102
            compile("import socket\nsocket.create_connection(('192.0.2.1', 80), timeout=0.2)\n",
                    str(REPO / "tests" / "live" / "conftest.py"), "exec"),
            {},
        )


def test_the_code_that_may_reach_a_loopback_server_is_this():
    assert set(hermetic.LOOPBACK_OPT_IN) == {"tests/live/", "tests/test_outbound_http.py"}
    for entry, reason in hermetic.LOOPBACK_OPT_IN.items():
        assert (REPO / entry).exists() and len(reason) > 40, entry
    # What the live suites are deselected by: a plain run does not run them.
    assert "-m 'not live_api and not live_e2e'" in (REPO / "pyproject.toml").read_text(encoding="utf-8")


def test_the_test_database_of_a_run_is_reachable_and_only_on_this_machine(monkeypatch):
    monkeypatch.setenv("BRUTOR_PG_TEST_URL", "postgresql://u:p@localhost:25432/postgres")
    assert hermetic._test_database_endpoints() == {25432}
    monkeypatch.setenv("BRUTOR_PG_TEST_URL", "postgresql://u:p@db.example.com:5432/postgres")
    assert hermetic._test_database_endpoints() == set()
    # A run that names none reads the suite's placeholder: a dead port.
    monkeypatch.delenv("BRUTOR_PG_TEST_URL")
    assert hermetic._test_database_endpoints() == {1}
    # The live database is no test database.
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("BRUTOR_PG_ALLOW_LIVE_DB", raising=False)
    monkeypatch.setenv("BRUTOR_PG_TEST_URL", "postgresql://u:p@localhost:25433/ai_gateway")
    assert hermetic._test_database_endpoints() == set()


def test_the_network_wall_is_the_one_in_force():
    for guarded in (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo):
        assert getattr(guarded, "hermetic_guard", False) is True, guarded


# --- no test module goes round the walls ---------------------------------------

# Names a test module has no business calling: a real client, or the
# library functions the walls replaced.
REAL_CLIENTS = {
    ("docker", "DockerClient"), ("docker", "APIClient"),
    ("docker.client", "DockerClient"), ("docker.api", "APIClient"), ("docker.api.client", "APIClient"),
}
# The files that may name them: the walls and their own tests.
WALL_FILES = {"hermetic.py", "test_hermetic_host.py"}
# A test file that sets or removes `KUBECONFIG`, and why that is no way
# round: the name of the file, the reason.
KUBECONFIG_WRITERS = {
    "test_k8s_preflight.py": (
        "unit tests of `_find_kubeconfig`, a path lookup: stub files under tmp_path, `Path.home` patched to "
        "tmp_path, restored by monkeypatch; nothing loads a configuration (and the loaders are the fakes)"
    ),
}


# What the network wall replaced, and the opt-in it reads.
NETWORK_WALL = {"socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"}
# A test file that assigns one of them, and why that is no way round.
NETWORK_WALL_WRITERS = {
    "principal_matrix.py": (
        "`no_network`: for a block, every connection and lookup is refused (loopback too), and what was "
        "there before, the wall, is put back"
    ),
}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _offences(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = (node.module, alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            module, _, attr = name.rpartition(".")
            if (module, attr) in REAL_CLIENTS or imported.get(name) in REAL_CLIENTS:
                found.append(f"{path.name}:{node.lineno}: constructs {name}")
        # Taking a wall down: assigning to, or deleting, what it set.
        targets = []
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        for target in targets:
            text = ast.unparse(target)
            if "DOCKER_HOST" in text or "KUBECONFIG" in text or text in ("docker.from_env", "hermetic.VIOLATIONS"):
                found.append(f"{path.name}:{node.lineno}: writes {text}")
            if text in NETWORK_WALL or text.startswith(("hermetic.LOOPBACK_OPT_IN", "hermetic.DEAD_PORTS")):
                found.append(f"{path.name}:{node.lineno}: writes {text} (the network wall)")
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            arguments = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name.endswith(("setenv", "delenv", "environ.pop", "environ.setdefault")) and (
                set(arguments) & {"DOCKER_HOST", "KUBECONFIG", "KUBERNETES_SERVICE_HOST"}
            ):
                found.append(f"{path.name}:{node.lineno}: {name}({arguments[0]!r})")
            if name.endswith("VIOLATIONS.clear"):
                found.append(f"{path.name}:{node.lineno}: clears the list of violations")
    return found


def test_no_test_module_constructs_a_real_client_or_takes_a_wall_down():
    offences = []
    files = 0
    for root in TEST_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.name in WALL_FILES:
                continue
            files += 1
            found = _offences(path)
            if path.name in KUBECONFIG_WRITERS:
                found = [line for line in found if "KUBECONFIG" not in line]
            if path.name in NETWORK_WALL_WRITERS:
                found = [line for line in found if "(the network wall)" not in line]
            offences += found
    assert files > 500
    assert offences == [], "a test module goes round tests/hermetic.py"


def test_the_scan_sees_what_it_is_for(tmp_path):
    bad = tmp_path / "test_bad.py"
    bad.write_text(
        "import docker, os\n"
        "from docker import DockerClient as D\n"
        "docker.DockerClient(base_url='unix:///var/run/docker.sock')\n"
        "D()\n"
        "os.environ['DOCKER_HOST'] = 'unix:///var/run/docker.sock'\n"
        "del os.environ['KUBECONFIG']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.delenv('DOCKER_HOST')\n"
        "    monkeypatch.setenv('KUBECONFIG', '/home/brutor/.kube/config')\n"
        "    hermetic.VIOLATIONS.clear()\n"
        "import socket\n"
        "socket.socket.connect = lambda *a: None\n"
        "socket.getaddrinfo = real_getaddrinfo\n"
        "hermetic.LOOPBACK_OPT_IN['tests/'] = 'everything'\n"
        "hermetic.DEAD_PORTS = frozenset(range(65536))\n",
        encoding="utf-8",
    )
    assert len(_offences(bad)) == 11
    good = tmp_path / "test_good.py"
    good.write_text("import docker\nclient = docker.from_env()\nclient.containers.list()\n", encoding="utf-8")
    assert _offences(good) == []
