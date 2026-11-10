from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import select
import signal
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
PROC = Path("/proc")


@contextmanager
def startup_lock(port: int) -> Iterator[None]:
    directory = ROOT / "data" / "run"
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / f"startup-{port}.lock",
                 os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: str
    argv: tuple[str, ...]
    cwd: Path
    unit: str | None


def _read_process(pid: int) -> Process | None:
    directory = PROC / str(pid)
    try:
        if directory.stat().st_uid != os.getuid():
            return None
        executable = directory.joinpath("exe").readlink()
        if not executable.name.startswith("python"):
            return None
        fields = directory.joinpath("stat").read_text().rpartition(")")[2].split()
        argv = tuple(os.fsdecode(value) for value in directory.joinpath("cmdline").read_bytes().split(b"\0")
                     if value)
        cwd = directory.joinpath("cwd").readlink()
        units = [
            line.rsplit("/", 1)[-1]
            for line in directory.joinpath("cgroup").read_text().splitlines()
            if line.rsplit("/", 1)[-1].endswith(".service")
        ]
        return Process(pid, int(fields[1]), fields[19], argv, cwd, units[0] if units else None)
    except (FileNotFoundError, ProcessLookupError):
        return None


def _main_arguments(process: Process) -> tuple[str, ...] | None:
    arguments = process.argv[1:]
    while arguments and arguments[0] in ("-u", "-B", "-E", "-s", "-S"):
        arguments = arguments[1:]
    if not arguments:
        return None
    script = Path(arguments[0])
    if not script.is_absolute():
        script = process.cwd / script
    if script.resolve() in ((ROOT / "main.py").resolve(), (ROOT / ".venv/bin/doorlock").resolve()):
        return arguments[1:]
    return None


def _is_backend(process: Process) -> bool:
    arguments = _main_arguments(process)
    if arguments is not None:
        return "--backend-only" in arguments
    return process.cwd == ROOT and process.argv[1:3] == ("-m", "vision.app")


def _is_launcher(process: Process) -> bool:
    arguments = _main_arguments(process)
    return arguments is not None and not set(arguments).intersection(
        ("--backend-only", "--audio-only", "--ui-only")
    )


def _socket_inodes(port: int) -> set[str]:
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (PROC / "net" / name).read_text().splitlines()[1:]
        except FileNotFoundError:
            if name == "tcp6":
                continue
            raise
        for line in lines:
            fields = line.split()
            if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(f"socket:[{fields[9]}]")
    return inodes


def _find_backend(port: int) -> Process | None:
    sockets = _socket_inodes(port)
    if not sockets:
        return None
    owners: list[Process] = []
    for directory in PROC.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            for fd in directory.joinpath("fd").iterdir():
                try:
                    target = str(fd.readlink())
                except FileNotFoundError:
                    continue
                if target in sockets:
                    process = _read_process(int(directory.name))
                    if process is None or not _is_backend(process):
                        return None
                    owners.append(process)
                    break
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            print(f"Warning: cannot inspect process {directory.name}; it will not be terminated", file=sys.stderr)
            continue
    return owners[0] if len(owners) == 1 else None


def _stop(process: Process, timeout: float) -> None:
    if process.pid == os.getpid():
        raise RuntimeError("Refusing to terminate the current launcher")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Safe automatic restart requires Linux pidfd support")
    try:
        fd = os.pidfd_open(process.pid)
    except ProcessLookupError:
        return
    try:
        current = _read_process(process.pid)
        if current is None:
            if select.select([fd], [], [], 0)[0]:
                return
            raise RuntimeError(f"Cannot confirm the identity of process {process.pid}; refusing to terminate it")
        # A pidfd plus start time prevents signalling an unrelated, recycled PID.
        if (current.started, current.argv, current.cwd) != (process.started, process.argv, process.cwd):
            raise RuntimeError(f"Process {process.pid} changed identity; refusing to terminate it")
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            if not select.select([fd], [], [], timeout)[0]:
                print(f"Warning: process {process.pid} did not stop; sending SIGKILL", file=sys.stderr)
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                if not select.select([fd], [], [], 3)[0]:
                    raise RuntimeError(f"Process {process.pid} did not exit after SIGKILL")
        except ProcessLookupError:
            return
    finally:
        os.close(fd)


def _restart_command(process: Process) -> str:
    try:
        cgroup = (PROC / str(process.pid) / "cgroup").read_text()
    except OSError:
        cgroup = ""
    scope = "--user " if "/user@" in cgroup else ""
    sudo = "" if scope else "sudo "
    return f"{sudo}systemctl {scope}restart {process.unit}"


def reuse_or_stop_backend(
    port: int,
    ready: bool,
    recheck: Callable[[], bool] | None = None,
    wait: float = 0.0,
) -> bool:
    """Return True to reuse, or stop a verified project instance before a new launch."""
    try:
        backend = _find_backend(port)
        if backend is None:
            raise RuntimeError(
                f"Port {port} is occupied, but its owner is not a verified backend "
                f"of {ROOT}; no process was terminated"
            )
        try:
            parent = _read_process(backend.parent)
        except PermissionError:
            print(
                f"Warning: cannot inspect parent process {backend.parent}; "
                "it will not be terminated",
                file=sys.stderr,
            )
            parent = None
        launcher = (
            parent if parent is not None and _is_launcher(parent)
            and int(parent.started) <= int(backend.started) else None
        )
        managed = launcher or backend
        if managed.unit:
            if ready:
                return True
            if recheck is not None and wait > 0:
                print(
                    f"Info: {managed.unit} is not ready yet (camera reconnecting?); "
                    f"waiting up to {wait:.0f}s",
                    file=sys.stderr,
                )
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    time.sleep(1)
                    if recheck():
                        return True
            raise RuntimeError(
                f"Backend on port {port} is managed by {managed.unit} but is not ready; "
                "check the camera connection and the service log, then restart it through "
                f"the service manager: `{_restart_command(managed)}` (with VISION_SOURCE=auto). "
                "It was not killed, because the service manager would restart it"
            )
        if launcher is None and ready:
            return True
        if launcher is not None:
            print(f"Info: stopping previous desktop instance pid={launcher.pid}", file=sys.stderr)
            _stop(launcher, 30)
        print(f"Info: releasing previous backend pid={backend.pid} on port {port}", file=sys.stderr)
        _stop(backend, 8)
        return False
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot safely inspect or stop the backend on port {port}: {exc}. "
            "Check process ownership and /proc access; no unverified process will be killed"
        ) from exc
