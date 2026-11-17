# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The test suite never reaches a container runtime or a cluster of the host,
and never the network.

**Why.** The Enterprise application builds a `DockerDeploymentManager` when
its routers are built (`docker.from_env()`, a ping), and a route of that
manager does to the daemon what it says: `POST
/v1/admin/deployments/docker/system/prune/containers` removes every stopped
container of the host, `.../prune/images` its dangling images. Tests that
walk every route (`enterprise/tests/test_licence_route_gate.py`) called them
on the developer's own Docker daemon, on every run.

**The walls**, installed by the repository's root `conftest.py` before any
test module is imported, for both suites and both editions:

1. **Fakes at the library seam.** `docker.from_env()` returns a
   `FakeDockerClient`: an empty daemon that records every call in
   `DOCKER_CALLS` and touches nothing. `kubernetes.config.load_kube_config`
   and `load_incluster_config` record the call in `KUBERNETES_CALLS` and
   answer that there is no cluster. Every manager, driver, builder and
   adapter of the product reaches its runtime through one of these, so the
   managers of the application under test run their own code over a fake.
2. **The environment.** `DOCKER_HOST` names a socket that does not exist and
   `KUBECONFIG` an empty file, for this process and every process it starts
   (the import probes, a seed's subprocess). The in-cluster variables are
   removed.
3. **A guard.** Constructing a real `docker.DockerClient` / `docker.APIClient`
   for anything but that dead socket, constructing a `kubernetes` `ApiClient`
   for a host that is not the library's default, a request of the
   `kubernetes` REST client, and starting `docker`, `kubectl` or `helm` as a
   subprocess each raise `HermeticViolation` and are listed in `VIOLATIONS`;
   the session fails if the list is not empty when it ends.
4. **The network.** No connection and no name lookup leaves this machine,
   for any test: `socket.socket.connect`, `connect_ex` and
   `socket.getaddrinfo` raise `HermeticViolation` for a destination that is
   not loopback. (Three routes of the product ask Docker Hub; a model
   probe, a registry search and a capability discovery ask whatever URL
   they are given.) On loopback, a connection is let through to a port
   nothing listens on (`DEAD_PORTS`), to the test database a run names
   (`tests/pg_test_url.py`), and for the code listed in `LOOPBACK_OPT_IN`;
   any other port of this machine is a service the suite did not start,
   and is refused. The control plane's address of the gateway
   (`BRUTOR_GATEWAY_INTERNAL_URL`) is a dead port for the session.

A test that needs an answer from Docker gets the fake's (an empty daemon:
no container, no image, a prune that deletes nothing), plants a container
or an image in the fake (`plant_container`, `plant_image`: objects of this
module, in no daemon), or gives the manager a client of its own, as the
manager tests do.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

_DIR = tempfile.mkdtemp(prefix="brutor-hermetic-")
DEAD_DOCKER_SOCKET = os.path.join(_DIR, "no-docker.sock")
DEAD_DOCKER_HOST = f"unix://{DEAD_DOCKER_SOCKET}"
EMPTY_KUBECONFIG = os.path.join(_DIR, "kubeconfig")
# The in-cluster configuration of the kubernetes client, and what would
# select another runtime than the fake.
REMOVED_ENV = ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
REFUSED_PROGRAMS = frozenset({"docker", "docker-compose", "kubectl", "helm"})

# (operation, arguments) of every call the fakes received, in order.
DOCKER_CALLS: List[Tuple[str, Dict[str, Any]]] = []
KUBERNETES_CALLS: List[Tuple[str, Dict[str, Any]]] = []
# For each entry of `DOCKER_CALLS`, by index: the test that was running
# (`PYTEST_CURRENT_TEST`; empty during collection) and whether the suite's
# own `no_network` (tests/principal_matrix.py) was refusing connections,
# which is the only thing that stood between that call and a real daemon
# before the walls.
DOCKER_CALL_ORIGINS: List[Tuple[str, bool]] = []
# What was stopped at the guard: one line each.
VIOLATIONS: List[str] = []

_installed = False


class _Journal(list):
    """`DOCKER_CALLS`: every append also notes where the call came from."""

    def append(self, call: Any) -> None:  # type: ignore[override]
        import socket

        super().append(call)
        DOCKER_CALL_ORIGINS.append(
            (
                os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0],
                getattr(socket.socket.connect, "__name__", "") == "refuse",
            )
        )


DOCKER_CALLS = _Journal()


class HermeticViolation(RuntimeError):
    """A test reached for the host's container runtime or cluster, or for
    the network."""


def _violation(what: str) -> HermeticViolation:
    test = os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]
    what = f"{what} [{test or 'during collection'}]"
    VIOLATIONS.append(what)
    return HermeticViolation(
        f"{what}. The test suite never reaches a real Docker daemon, a Kubernetes cluster or the "
        "network (tests/hermetic.py): use the fake, or give the code under test a client of its own."
    )


# --- the fake daemon ---------------------------------------------------------


def _not_found(what: str) -> Exception:
    import docker.errors

    return docker.errors.NotFound(f"hermetic fake daemon: no such {what}")


def _no_daemon(operation: str) -> Exception:
    import docker.errors

    return docker.errors.APIError(f"hermetic fake daemon: {operation} is recorded and not carried out")


class FakeContainer:
    """A container a test planted in the fake daemon (`plant_container`):
    it has a name, labels and a log, records what is done to it and is in
    no daemon."""

    def __init__(self, store: "_Containers", name: str, labels: Optional[dict], status: str, image: str, logs: bytes):
        self._store = store
        self.name = name
        self.id = f"fake{abs(hash(name)):060d}"[:64]
        self.short_id = self.id[:12]
        self.status = status
        self._logs = logs
        self.attrs = {
            "State": {"Status": status, "Running": status == "running", "StartedAt": None},
            "NetworkSettings": {"Ports": {}},
            "Config": {"Image": image, "Labels": dict(labels or {}), "Env": []},
        }

    @property
    def labels(self) -> dict:
        return self.attrs["Config"]["Labels"]

    def _record(self, operation: str, **arguments: Any) -> None:
        DOCKER_CALLS.append((f"container.{operation}", {"name": self.name, **arguments}))

    def _state(self, status: str) -> None:
        self.status = status
        self.attrs["State"].update(Status=status, Running=status == "running")

    def reload(self) -> None:
        pass

    def start(self, **kwargs: Any) -> None:
        self._record("start")
        self._state("running")

    def stop(self, **kwargs: Any) -> None:
        self._record("stop")
        self._state("exited")

    def restart(self, **kwargs: Any) -> None:
        self._record("restart")
        self._state("running")

    def remove(self, **kwargs: Any) -> None:
        self._record("remove", force=bool(kwargs.get("force")))
        self._store.planted.pop(self.name, None)

    def logs(self, **kwargs: Any) -> bytes:
        self._record("logs")
        return self._logs

    def stats(self, **kwargs: Any) -> dict:
        self._record("stats")
        return {}


class FakeImage:
    """An image a test planted in the fake daemon (`plant_image`)."""

    def __init__(self, image_id: str, tags: List[str]):
        self.id = f"sha256:{image_id}"
        self.short_id = f"sha256:{image_id[:12]}"
        self.tags = list(tags)
        self.attrs = {"Size": 1024 * 1024, "Created": "2026-01-01T00:00:00Z"}


class _Containers:
    def __init__(self) -> None:
        # What tests planted, by name. Empty unless a test fills it.
        self.planted: Dict[str, FakeContainer] = {}

    def get(self, name: Any) -> Any:
        DOCKER_CALLS.append(("containers.get", {"name": name}))
        for container in self.planted.values():
            if name in (container.name, container.id, container.short_id):
                return container
        raise _not_found(f"container: {name}")

    def list(self, **kwargs: Any) -> list:
        DOCKER_CALLS.append(("containers.list", kwargs))
        return list(self.planted.values())

    def run(self, *args: Any, **kwargs: Any) -> Any:
        DOCKER_CALLS.append(("containers.run", {"image": kwargs.get("image", args[0] if args else None)}))
        raise _no_daemon("containers.run")

    def prune(self, filters: Optional[dict] = None) -> dict:
        DOCKER_CALLS.append(("containers.prune", {"filters": filters}))
        # What a daemon with nothing to prune answers: null, not an empty list.
        return {"ContainersDeleted": None, "SpaceReclaimed": 0}


class _Images:
    def __init__(self) -> None:
        self.planted: List[FakeImage] = []

    def _find(self, name: Any) -> Optional[FakeImage]:
        for image in self.planted:
            if name in image.tags or name in (image.id, image.short_id, image.short_id.replace("sha256:", "")):
                return image
        return None

    def get(self, name: Any) -> Any:
        DOCKER_CALLS.append(("images.get", {"name": name}))
        import docker.errors

        image = self._find(name)
        if image is None:
            raise docker.errors.ImageNotFound(f"hermetic fake daemon: no such image: {name}")
        return image

    def list(self, **kwargs: Any) -> list:
        DOCKER_CALLS.append(("images.list", kwargs))
        return list(self.planted)

    def build(self, **kwargs: Any) -> Any:
        DOCKER_CALLS.append(("images.build", {"tag": kwargs.get("tag")}))
        raise _no_daemon("images.build")

    def pull(self, repository: Any, tag: Any = None, **kwargs: Any) -> Any:
        DOCKER_CALLS.append(("images.pull", {"repository": repository, "tag": tag}))
        raise _no_daemon("images.pull")

    def remove(self, image: Any, force: bool = False, **kwargs: Any) -> None:
        DOCKER_CALLS.append(("images.remove", {"image": image, "force": force}))
        import docker.errors

        found = self._find(image)
        if found is None:
            raise docker.errors.ImageNotFound(f"hermetic fake daemon: no such image: {image}")
        # As a daemon does: a tag is untagged, an id removes the image.
        if image in found.tags:
            found.tags.remove(image)
            if found.tags:
                return
        self.planted.remove(found)

    def prune(self, filters: Optional[dict] = None) -> dict:
        DOCKER_CALLS.append(("images.prune", {"filters": filters}))
        return {"ImagesDeleted": None, "SpaceReclaimed": 0}


class _Networks:
    def __init__(self) -> None:
        # The names a test planted (`client.networks.planted.add(name)`).
        self.planted: set = set()

    def get(self, name: Any) -> Any:
        DOCKER_CALLS.append(("networks.get", {"name": name}))
        if name in self.planted:
            return type("FakeNetwork", (), {"name": name})()
        raise _not_found(f"network: {name}")

    def create(self, name: Any, **kwargs: Any) -> Any:
        DOCKER_CALLS.append(("networks.create", {"name": name}))
        raise _no_daemon("networks.create")


class FakeDockerClient:
    """An empty Docker daemon: it answers, records and changes nothing."""

    hermetic = True

    def __init__(self) -> None:
        self.containers = _Containers()
        self.images = _Images()
        self.networks = _Networks()

    def ping(self) -> bool:
        DOCKER_CALLS.append(("ping", {}))
        return True

    def version(self) -> dict:
        DOCKER_CALLS.append(("version", {}))
        return {"Version": "0.0.0-hermetic", "ApiVersion": "1.43", "Os": "linux", "Arch": "amd64"}

    def info(self) -> dict:
        DOCKER_CALLS.append(("info", {}))
        return {
            "Containers": 0, "ContainersRunning": 0, "ContainersPaused": 0, "ContainersStopped": 0,
            "Images": 0, "Driver": "hermetic", "MemTotal": 0, "NCPU": 0, "OperatingSystem": "hermetic",
            "ServerVersion": "0.0.0-hermetic", "Name": "hermetic",
        }

    def close(self) -> None:
        DOCKER_CALLS.append(("close", {}))

    def __getattr__(self, name: str) -> Any:
        # Anything the fake does not know is absent, not improvised.
        raise AttributeError(f"the hermetic fake Docker daemon has no '{name}' (tests/hermetic.py)")


def plant_container(
    client: FakeDockerClient, name: str, *, labels: Optional[dict] = None, status: str = "running",
    image: str = "planted:latest", logs: bytes = b"",
) -> FakeContainer:
    """Put a container into a fake daemon, for a test of what the product
    does to a container that exists. The test takes it out again
    (`client.containers.planted.clear()`)."""
    container = FakeContainer(client.containers, name, labels, status, image, logs)
    client.containers.planted[name] = container
    return container


def plant_image(client: FakeDockerClient, image_id: str, tags: List[str]) -> FakeImage:
    """Put an image into a fake daemon (`client.images.planted.clear()`
    takes it out)."""
    image = FakeImage(image_id, tags)
    client.images.planted.append(image)
    return image


def fake_docker_from_env(*args: Any, **kwargs: Any) -> FakeDockerClient:
    DOCKER_CALLS.append(("from_env", {}))
    return FakeDockerClient()


# --- installation --------------------------------------------------------------


def _wall_environment() -> None:
    with open(EMPTY_KUBECONFIG, "w", encoding="utf-8"):
        pass
    os.environ["DOCKER_HOST"] = DEAD_DOCKER_HOST
    os.environ["KUBECONFIG"] = EMPTY_KUBECONFIG
    os.environ["BRUTOR_GATEWAY_INTERNAL_URL"] = DEAD_GATEWAY_URL
    for name in REMOVED_ENV:
        os.environ.pop(name, None)


def _wall_docker() -> None:
    try:
        import docker
    except ImportError:  # a Community environment without the Enterprise libraries
        return

    def guarded(original: Any, kind: str) -> Any:
        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
            base_url = kwargs.get("base_url", args[0] if args else None)
            if base_url != DEAD_DOCKER_HOST:
                raise _violation(f"a real docker.{kind} was constructed (base_url={base_url!r})")
            original(self, *args, **kwargs)

        __init__.hermetic_guard = True  # type: ignore[attr-defined]
        return __init__

    docker.DockerClient.__init__ = guarded(docker.DockerClient.__init__, "DockerClient")  # type: ignore[method-assign]
    docker.APIClient.__init__ = guarded(docker.APIClient.__init__, "APIClient")  # type: ignore[method-assign]
    docker.from_env = fake_docker_from_env
    docker.client.from_env = fake_docker_from_env


def _wall_kubernetes() -> None:
    try:
        import kubernetes.client
        import kubernetes.client.rest
        import kubernetes.config
        from kubernetes.config.config_exception import ConfigException
    except ImportError:
        return

    def no_cluster(name: str) -> Any:
        def load(*args: Any, **kwargs: Any) -> None:
            KUBERNETES_CALLS.append((name, {"context": kwargs.get("context")}))
            raise ConfigException("hermetic test session: there is no Kubernetes cluster")

        load.hermetic = True  # type: ignore[attr-defined]
        return load

    kubernetes.config.load_kube_config = no_cluster("load_kube_config")
    kubernetes.config.load_incluster_config = no_cluster("load_incluster_config")

    default_host = kubernetes.client.Configuration().host
    original_init = kubernetes.client.ApiClient.__init__

    def api_client_init(self: Any, configuration: Any = None, *args: Any, **kwargs: Any) -> None:
        host = getattr(configuration, "host", None) if configuration is not None else None
        if host is None:
            host = kubernetes.client.Configuration.get_default_copy().host
        if host != default_host:
            raise _violation(f"a kubernetes ApiClient was constructed for a cluster (host={host!r})")
        original_init(self, configuration, *args, **kwargs)

    api_client_init.hermetic_guard = True  # type: ignore[attr-defined]
    kubernetes.client.ApiClient.__init__ = api_client_init  # type: ignore[method-assign]

    def request(self: Any, method: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        raise _violation(f"the kubernetes client was about to send {method} {url}")

    request.hermetic_guard = True  # type: ignore[attr-defined]
    kubernetes.client.rest.RESTClientObject.request = request  # type: ignore[method-assign]


def _program(args: Any) -> str:
    first = args if isinstance(args, (str, bytes, os.PathLike)) else (args[0] if args else "")
    first = os.fsdecode(first) if isinstance(first, (bytes, os.PathLike)) else str(first)
    return os.path.basename(first.split()[0]) if first.strip() else ""


def _wall_subprocesses() -> None:
    original_init = subprocess.Popen.__init__

    def popen_init(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        program = _program(args)
        if program in REFUSED_PROGRAMS:
            raise _violation(f"a test started '{program}' as a subprocess")
        original_init(self, args, *rest, **kwargs)

    popen_init.hermetic_guard = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = popen_init  # type: ignore[method-assign]


# --- the network ----------------------------------------------------------------------

# Where the control plane finds the gateway in a test session: a port
# nothing listens on. Its default (`http://127.0.0.1:8100`) is the address
# of a Core Gateway a developer runs on the same machine, and every cache
# invalidation and model probe of the suite was sent there.
DEAD_GATEWAY_URL = "http://127.0.0.1:1"
# Loopback ports nothing listens on, which the suite names where it wants a
# connection to fail at once: 1 (its placeholder, `tests/conftest.py`) and 9
# (discard).
DEAD_PORTS = frozenset({1, 9})
# Code that may connect to a loopback port of this machine, by its path
# under the repository: what it is, and why it is no way round the wall.
# A path that ends in `/` is a directory. Nothing is ever allowed to leave
# the machine.
LOOPBACK_OPT_IN: Dict[str, str] = {
    "tests/test_outbound_http.py": (
        "the outbound policy's own clients, tested against a local server the test starts and stops"
    ),
    "tests/live/": (
        "the live suites (`-m live_e2e`, `-m live_api`): they are deselected by default and exist to "
        "address a stack somebody started for them; their session fixture asks whether one is there"
    ),
}
# (test, "connect" | "resolve", destination, verdict) of every connection and
# name lookup the test process attempted through Python's sockets.
NETWORK_CALLS: List[Tuple[str, str, str, str]] = []

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _current_test() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]


def _text(host: Any) -> str:
    return host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)


def _is_literal_address(host: Any) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(_text(host).split("%", 1)[0])
        return True
    except ValueError:
        return False


def _is_loopback(host: Any) -> bool:
    import ipaddress

    host = _text(host)
    if host.lower() in ("localhost", "localhost.", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _test_database_endpoints() -> set:
    """The loopback port of the test database of this run, as every test
    reads it (`tests/pg_test_url.py`): a throwaway server where the run
    names one, the suite's dead placeholder where it names none."""
    from urllib.parse import urlsplit

    from tests.pg_test_url import pg_test_url

    try:
        parts = urlsplit(pg_test_url())
        if parts.hostname and _is_loopback(parts.hostname) and parts.port:
            return {parts.port}
    except (ValueError, RuntimeError):
        pass
    return set()


def _opted_in() -> Optional[str]:
    """The opt-in entry whose code is on the stack, if any."""
    import sys

    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith(_REPO + os.sep):
            relative = filename[len(_REPO) + 1:].replace(os.sep, "/")
            for entry in LOOPBACK_OPT_IN:
                if relative == entry or (entry.endswith("/") and relative.startswith(entry)):
                    return entry
        frame = frame.f_back
    return None


def _wall_network() -> None:
    """No connection leaves this machine, and none reaches a service of it
    that the suite did not start.

    Python's sockets only: `socket.socket.connect`/`connect_ex` and
    `socket.getaddrinfo`, which is what `httpx`, `aiohttp`, `requests`,
    `urllib` and `asyncio` use. A client that connects from C (libpq for
    the test database) is not seen here, and neither is a subprocess.
    """
    import socket

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def judge(sock: Any, address: Any) -> None:
        # A Unix socket is a file of this machine (the dead Docker socket).
        if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", object()) or not isinstance(address, tuple):
            return
        host, port = address[0], (address[1] if len(address) > 1 else None)
        destination = f"{_text(host)}:{port}"
        if not _is_loopback(host):
            NETWORK_CALLS.append((_current_test(), "connect", destination, "refused"))
            raise _violation(f"an outbound connection to {destination} was attempted")
        if port in DEAD_PORTS or port in _test_database_endpoints():
            NETWORK_CALLS.append((_current_test(), "connect", destination, "loopback"))
            return
        entry = _opted_in()
        if entry is not None:
            NETWORK_CALLS.append((_current_test(), "connect", destination, f"opt-in {entry}"))
            return
        NETWORK_CALLS.append((_current_test(), "connect", destination, "refused"))
        raise _violation(
            f"a connection to {destination} was attempted: a port of this machine that the suite did not "
            "start anything on (a developer's own gateway, control plane or database may be listening)"
        )

    def connect(self: Any, address: Any) -> Any:
        judge(self, address)
        return original_connect(self, address)

    def connect_ex(self: Any, address: Any) -> Any:
        judge(self, address)
        return original_connect_ex(self, address)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        # A name that is not this machine's is a question to a DNS server.
        if host is not None and host != "" and not _is_loopback(host) and not _is_literal_address(host):
            NETWORK_CALLS.append((_current_test(), "resolve", _text(host), "refused"))
            raise _violation(f"an outbound name lookup of {_text(host)} was attempted")
        return original_getaddrinfo(host, port, *args, **kwargs)

    for guard in (connect, connect_ex, getaddrinfo):
        guard.hermetic_guard = True  # type: ignore[attr-defined]
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo


def install() -> None:
    """Put the walls up. Called once, by the root `conftest.py`, at import."""
    global _installed
    if _installed:
        return
    _wall_environment()
    _wall_docker()
    _wall_kubernetes()
    _wall_subprocesses()
    _wall_network()
    _installed = True


def installed() -> bool:
    return _installed


# The operations of the fake daemon that change a real one.
DAEMON_CHANGING = (
    "containers.prune", "images.prune", "images.remove", "containers.run", "images.build", "images.pull",
    "networks.create", "container.start", "container.stop", "container.restart", "container.remove",
)


def journal_summary() -> List[str]:
    """What the fake daemon was asked in this session, one line per
    operation: how often, how often with no `no_network` around the call
    (those reached a real daemon before the walls), and from which tests."""
    lines = []
    for operation in sorted({name for name, _ in DOCKER_CALLS}):
        origins = [origin for (name, _), origin in zip(DOCKER_CALLS, DOCKER_CALL_ORIGINS) if name == operation]
        open_calls = [test for test, refused in origins if not refused]
        tests = sorted({test.split("::")[0] or "<collection>" for test in open_calls})
        mark = "CHANGES A DAEMON " if operation in DAEMON_CHANGING else ""
        lines.append(
            f"{mark}{operation}: {len(origins)} calls, {len(open_calls)} with connections allowed"
            + (f" (from {', '.join(tests[:6])}{', ...' if len(tests) > 6 else ''})" if tests else "")
        )
    lines.append(f"kubernetes config loads: {len(KUBERNETES_CALLS)}")
    return lines


def docker_calls(operation: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """What the fake daemon was asked, optionally one operation only."""
    return [call for call in DOCKER_CALLS if operation is None or call[0] == operation]
