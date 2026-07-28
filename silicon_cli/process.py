"""Process supervision — start/stop instances with an auto-restart watchdog.

Mirrors the bash watchdog: a detached supervisor process runs `python -u main.py`,
restarts it on exit (with crash-loop detection), honors a .silicon.stop sentinel,
writes .silicon.log + .silicon.pid (the pid is the *watchdog's*, so a stop signal
reaches the supervisor which then kills its child and cleans up).
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import docker_runtime, glassagent, interface_cli, registry, ui
from .config import (
    active_release_root,
    legacy_offline_update_fenced,
    python_run_cmd,
    runtime_environment,
)
from .updater.io import atomic_write_bytes
from .updater.lock import InstanceLock, UpdateLocked

RESTART_DELAY = 5
MAX_RAPID = 5
RAPID_WINDOW = 60
WATCHDOG_PUBLICATION_TIMEOUT = 15.0
LIFECYCLE_LOCK_TIMEOUT = 30.0
CONTAINER_INTERFACE_ACTIVATOR = Path(
    "/usr/local/libexec/silicon-activate-interface-cli.py"
)
CONTAINER_INTERFACE_EXECUTABLE = Path(
    "/usr/local/bin/silicon-interface"
)


class _RuntimeLifecycleLock(InstanceLock):
    """A distinct kernel lock for start/stop publication transactions."""

    def __init__(self, instance: str | os.PathLike, operation: str):
        super().__init__(
            Path(instance),
            f"runtime-{operation}-{os.getpid()}-{time.time_ns()}",
        )
        self.path = (
            Path(instance) / ".silicon" / "runtime-lifecycle.lock"
        )


@contextmanager
def _runtime_lifecycle_lock(
    instance: str | os.PathLike,
    operation: str,
    *,
    timeout: float = LIFECYCLE_LOCK_TIMEOUT,
) -> Iterator[None]:
    root = Path(instance).expanduser().resolve(strict=True)
    state = root / ".silicon"
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        raise RuntimeError("Silicon runtime state directory is unsafe")
    state.mkdir(mode=0o700, exist_ok=True)
    if state.is_symlink() or not state.is_dir():
        raise RuntimeError("Silicon runtime state directory is unsafe")
    lock = _RuntimeLifecycleLock(root, operation)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            lock.acquire()
            break
        except UpdateLocked as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"timed out waiting to {operation} this Silicon safely"
                ) from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


def runtime_meta_file(path: str | os.PathLike) -> Path:
    return Path(path) / ".silicon.pid.meta.json"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except (OSError, RuntimeError):
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_runtime_meta(path: Path, value: dict) -> None:
    """Publish the supervised child identity atomically.

    The public PID file identifies the durable watchdog.  Update validation
    additionally needs proof that the *current generation's child* stayed
    alive, otherwise a crash-looping candidate could look healthy merely
    because its supervisor survived.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("runtime metadata path is unsafe")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = -1
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def get_pid(pid_file: str) -> str | None:
    path = Path(pid_file)
    descriptor = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > 32
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(
            before, opened
        ):
            return None
        payload = os.read(descriptor, 33)
        if len(payload) > 32:
            return None
        pid = payload.decode("ascii").strip()
        return pid if pid.isdigit() and int(pid) > 0 else None
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validated_pid_path(
    instance: str | os.PathLike, pid_file: str | os.PathLike
) -> Path:
    root = Path(instance).expanduser().resolve(strict=True)
    path = Path(pid_file).expanduser()
    if not path.is_absolute():
        path = root / path
    if path.parent.resolve(strict=True) != root:
        raise RuntimeError("Silicon PID file escaped its instance root")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("Silicon PID file is unsafe")
    return path


def _publish_pid(
    instance: str | os.PathLike,
    pid_file: str | os.PathLike,
    pid: int,
) -> None:
    path = _validated_pid_path(instance, pid_file)
    atomic_write_bytes(path, f"{pid}\n".encode("ascii"), mode=0o600)


def _publish_stop_sentinel(instance: str | os.PathLike) -> Path:
    root = Path(instance).expanduser().resolve(strict=True)
    path = root / ".silicon.stop"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("Silicon stop sentinel is unsafe")
    atomic_write_bytes(path, b"", mode=0o600)
    return path


def _await_watchdog_publication(
    pid_path: Path,
    *,
    timeout: float = WATCHDOG_PUBLICATION_TIMEOUT,
    poll_interval: float = 0.05,
) -> bool:
    """Gate child creation on the parent's durable PID publication.

    A detached watchdog can outlive a CLI that crashes immediately after
    spawning it.  Until the atomic PID file names this exact watchdog it must
    not launch Silicon.  A later start publishing a different watchdog also
    causes this stale waiter to exit.
    """

    own_pid = os.getpid()
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        published = get_pid(str(pid_path))
        if published is not None:
            return int(published) == own_pid
        if pid_path.exists() or pid_path.is_symlink():
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.001, float(poll_interval)))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _process_identity(pid: int) -> str:
    """Return a boot-scoped process birth identity, not merely a reusable PID."""

    if pid <= 0 or not _alive(pid):
        return ""
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            raw = proc_stat.read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2 :].split()
            start_ticks = fields[19]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()
            if boot_id and start_ticks.isdigit():
                return f"linux:{boot_id}:{start_ticks}"
        except (OSError, IndexError, ValueError):
            return ""
    if os.name == "nt":  # pragma: no cover - Windows CI is not available.
        try:
            import ctypes
            from ctypes import wintypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ""
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not ctypes.windll.kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return ""
                value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                return f"windows:{value}"
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    started = " ".join(result.stdout.split())
    return f"ps:{started}" if result.returncode == 0 and started else ""


def _legacy_watchdog_matches(pid: int, pid_file: str) -> bool:
    if not _alive(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    command = result.stdout.strip()
    instance = str(Path(pid_file).resolve().parent)
    return bool(
        result.returncode == 0
        and "_watchdog" in command
        and "silicon_cli" in command
        and instance in command
    )


def _owned_watchdog_pid(pid_file: str) -> tuple[int, bool] | None:
    """Return (PID, fully_verified) for a supervisor owned by this instance."""

    raw = get_pid(pid_file)
    if raw is None:
        return None
    pid = int(raw)
    if is_running(pid_file):
        return pid, True
    # Corrupt or interrupted metadata must not make a live watchdog invisible
    # at the stop boundary. Command ownership is sufficient only to stop or
    # refuse a duplicate start; it is never sufficient for health.
    if _legacy_watchdog_matches(pid, pid_file):
        return pid, False
    return None


def is_running(pid_file: str) -> bool:
    pid = get_pid(pid_file)
    if not pid:
        return False
    try:
        parsed = int(pid)
    except ValueError:
        return False
    metadata_path = runtime_meta_file(Path(pid_file).parent.resolve())
    if metadata_path.exists() or metadata_path.is_symlink():
        try:
            if metadata_path.is_symlink() or metadata_path.stat().st_size > 16 * 1024:
                return _legacy_watchdog_matches(parsed, pid_file)
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
            if bool(
                isinstance(value, dict)
                and int(value["supervisor_pid"]) == parsed
                and str(value["supervisor_identity"])
                == _process_identity(parsed)
                and bool(value["supervisor_identity"])
            ):
                return True
            return _legacy_watchdog_matches(parsed, pid_file)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return _legacy_watchdog_matches(parsed, pid_file)
    return _legacy_watchdog_matches(parsed, pid_file)


def install_is_running(inst: registry.Install) -> bool:
    if inst.is_docker:
        return docker_runtime.silicon_running(inst)
    return is_running(inst.pid_file)


def runtime_child_status(
    path: str | os.PathLike,
    pid_file: str | os.PathLike,
) -> dict | None:
    """Return a verified live-child projection for a current local generation."""

    supervisor_raw = get_pid(str(pid_file))
    if not supervisor_raw:
        return None
    try:
        supervisor_pid = int(supervisor_raw)
    except ValueError:
        return None
    if supervisor_pid <= 0 or not _alive(supervisor_pid):
        return None

    metadata_path = runtime_meta_file(path)
    try:
        if metadata_path.is_symlink() or metadata_path.stat().st_size > 16 * 1024:
            return None
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
        child_pid = int(value["child_pid"])
        recorded_supervisor = int(value["supervisor_pid"])
        supervisor_identity = str(value["supervisor_identity"])
        child_identity = str(value["child_identity"])
        started_at = float(value["started_at"])
        generation = Path(str(value["generation"])).resolve(strict=True)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or child_pid <= 0
        or recorded_supervisor != supervisor_pid
        or not _alive(child_pid)
        or not supervisor_identity
        or not child_identity
        or _process_identity(supervisor_pid) != supervisor_identity
        or _process_identity(child_pid) != child_identity
    ):
        return None
    try:
        active = active_release_root(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if generation != active:
        return None
    return {
        "supervisor_pid": supervisor_pid,
        "child_pid": child_pid,
        "generation": str(generation),
        "started_at": started_at,
        "uptime_seconds": max(0.0, time.time() - started_at),
    }


def runtime_healthy(
    path: str | os.PathLike,
    pid_file: str | os.PathLike,
    *,
    min_uptime: float = 5.0,
) -> bool:
    status = runtime_child_status(path, pid_file)
    return bool(
        status is not None
        and float(status["uptime_seconds"]) >= max(0.0, float(min_uptime))
    )


def runtime_ready(
    path: str | os.PathLike,
    pid_file: str | os.PathLike,
    *,
    min_uptime: float = 5.0,
    max_heartbeat_age: float = 5.0,
) -> bool:
    """Require both supervisor child health and the app's ready heartbeat."""

    status = runtime_child_status(path, pid_file)
    if (
        status is None
        or float(status["uptime_seconds"]) < max(0.0, float(min_uptime))
    ):
        return False
    health_path = Path(path) / ".silicon" / "runtime-health.json"
    try:
        if health_path.is_symlink() or health_path.stat().st_size > 16 * 1024:
            return False
        value = json.loads(health_path.read_text(encoding="utf-8"))
        child_pid = int(value["pid"])
        code_root = Path(str(value["code_root"])).resolve(strict=True)
        heartbeat_at = float(value["heartbeat_at"])
        ready_at = float(value["ready_at"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    now = time.time()
    return bool(
        isinstance(value, dict)
        and value.get("schema") == 1
        and value.get("ready") is True
        and child_pid == status["child_pid"]
        and code_root == Path(str(status["generation"]))
        and 0 <= now - heartbeat_at <= max(0.1, float(max_heartbeat_age))
        and ready_at <= heartbeat_at
    )


def _floater_pids(path: str, skip: int | None = None) -> list[int]:
    """PIDs of python processes running this dir's main.py (orphans)."""
    main_py = str(Path(path) / "main.py")
    release_prefix = str(Path(path) / ".silicon" / "releases") + os.sep
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True, text=True).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if not m:
            continue
        pid, cmd = int(m.group(1)), m.group(2)
        if "python" in cmd and (
            main_py in cmd or (release_prefix in cmd and "main.py" in cmd)
        ):
            if skip is not None and pid == skip:
                continue
            if pid == os.getpid():
                continue
            pids.append(pid)
    return pids


def kill_floaters(path: str, skip: int | None = None) -> None:
    for pid in _floater_pids(path, skip):
        ui.warn(f"Killing orphaned process (PID {pid}) from {path}")
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


# --------------------------------------------------------------- watchdog
def watchdog_loop(name: str, path: str, pid_file: str) -> None:
    """Runs as the detached `silicon _watchdog` process."""
    try:
        pid_path = _validated_pid_path(path, pid_file)
    except (OSError, RuntimeError):
        return
    log_file = Path(path) / ".silicon.log"
    stop_file = Path(path) / ".silicon.stop"
    metadata_file = runtime_meta_file(path)
    if not _await_watchdog_publication(pid_path):
        return
    child: subprocess.Popen | None = None

    def _terminate(signum=None, frame=None):
        if child and child.poll() is None:
            try:
                child.terminate()
                for _ in range(6):
                    if child.poll() is not None:
                        break
                    time.sleep(0.5)
                if child.poll() is None:
                    child.kill()
            except Exception:
                pass
        try:
            pid_path.unlink()
        except OSError:
            pass
        metadata_file.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    restart_times: list[float] = []
    metadata_file.unlink(missing_ok=True)
    while True:
        kill_floaters(path, skip=os.getpid())
        generation = active_release_root(path).resolve(strict=True)
        main_py = str(generation / "main.py")
        py = python_run_cmd(path)
        with open(log_file, "a") as lf:
            child = subprocess.Popen(
                [py, "-u", main_py],
                cwd=path,
                env=runtime_environment(path),
                stdout=lf,
                stderr=subprocess.STDOUT,
            )
            try:
                supervisor_identity = _process_identity(os.getpid())
                child_identity = _process_identity(child.pid)
                if not supervisor_identity or not child_identity:
                    raise RuntimeError(
                        "could not establish process birth identities"
                    )
                _write_runtime_meta(
                    metadata_file,
                    {
                        "schema": 1,
                        "supervisor_pid": os.getpid(),
                        "supervisor_identity": supervisor_identity,
                        "child_pid": child.pid,
                        "child_identity": child_identity,
                        "generation": str(generation),
                        "started_at": time.time(),
                    },
                )
            except Exception as exc:
                lf.write(
                    f"[silicon-watchdog] {time.ctime()}: could not publish "
                    f"child health metadata: {exc}\n"
                )
                child.terminate()
                child.wait()
                child = None
                pid_path.unlink(missing_ok=True)
                return
            exit_code = child.wait()
        metadata_file.unlink(missing_ok=True)
        child = None

        if stop_file.exists() or stop_file.is_symlink():
            stop_file.unlink(missing_ok=True)
            pid_path.unlink(missing_ok=True)
            metadata_file.unlink(missing_ok=True)
            break

        now = time.time()
        restart_times.append(now)
        cutoff = now - RAPID_WINDOW
        restart_times = [t for t in restart_times if t >= cutoff]
        if len(restart_times) >= MAX_RAPID:
            with open(log_file, "a") as lf:
                lf.write(f"[silicon-watchdog] {time.ctime()}: '{name}' crashed {MAX_RAPID} times "
                         f"in {RAPID_WINDOW}s. Giving up.\n")
            pid_path.unlink(missing_ok=True)
            metadata_file.unlink(missing_ok=True)
            break

        with open(log_file, "a") as lf:
            lf.write(f"[silicon-watchdog] {time.ctime()}: '{name}' exited (code {exit_code}). "
                     f"Restarting in {RESTART_DELAY}s...\n")
        time.sleep(RESTART_DELAY)


# --------------------------------------------------------------- start/stop
def _spawn_watchdog(name: str, path: str, pid_file: str) -> int:
    """Launch the detached watchdog; return its PID."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "silicon_cli.cli", "_watchdog", path, name, pid_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach so it survives this CLI exiting
    )
    return proc.pid


def _reconcile_backup_schedule(inst: registry.Install) -> None:
    """Best-effort restart of a durable host-owned backup schedule."""

    try:
        # Local import avoids a module cycle: sync owns backup orchestration and
        # imports process for normal Silicon lifecycle commands.
        from . import sync

        sync.reconcile_backup_supervisor(inst, quiet=True)
    except Exception as exc:
        ui.warn(
            f"Could not reconcile scheduled backups for '{inst.name}': {exc}"
        )


def _reconcile_glass_terminal_state(inst: registry.Install) -> None:
    """Non-blockingly retry a terminal maintenance projection after reboot."""

    try:
        from .updater.maintenance import (
            schedule_pending_terminal_reconciliation,
        )

        schedule_pending_terminal_reconciliation(Path(inst.path))
    except Exception as exc:
        ui.warn(
            f"Could not schedule Glass maintenance reconciliation for "
            f"'{inst.name}': {exc}"
        )


def _start_interface_daemon(
    inst: registry.Install,
) -> None:
    """Start Interface only after the instance's durable state is ready.

    A container PID cannot survive an image boot, so a fresh container start
    drops the persisted daemon PID before asking the active image's local
    wrapper to start its bundled Interface CLI. Host-local starts preserve the
    PID so the command remains idempotent.
    """

    truthy = {"1", "true", "yes", "on"}
    container_mode = (
        os.environ.get("SILICON_CONTAINER_MODE", "").strip().lower()
        in truthy
    )
    reset_container_pid = (
        os.environ.pop(
            "SILICON_INTERFACE_RESET_DAEMON_PID",
            "",
        ).strip().lower()
        in truthy
    )
    reset_marker = (
        Path(inst.path)
        / ".silicon"
        / "interface-daemon-reset-required"
    )
    if container_mode:
        try:
            marker_metadata = reset_marker.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                "Could not inspect the Silicon Interface fresh-boot marker: "
                f"{exc}"
            ) from exc
        else:
            if (
                stat.S_ISLNK(marker_metadata.st_mode)
                or not stat.S_ISREG(marker_metadata.st_mode)
                or marker_metadata.st_nlink != 1
            ):
                raise RuntimeError(
                    "Silicon Interface fresh-boot marker is unsafe; "
                    "the daemon was not started."
                )
            reset_container_pid = True
    if container_mode and reset_container_pid:
        if not _activate_container_interface(inst):
            raise RuntimeError(
                "the selected runtime image could not activate "
                "Silicon Interface"
            )
        daemon_pid = (
            Path(inst.path) / ".silicon-interface" / "daemon.pid"
        )
        try:
            daemon_pid.unlink(missing_ok=True)
        except IsADirectoryError as exc:
            raise RuntimeError(
                "Silicon Interface daemon PID path is unsafe; "
                "the daemon was not started."
            ) from exc
    required = interface_cli.daemon_required(inst.path)
    started = interface_cli.start_daemon(
        inst.path,
        required=required,
    )
    if required and not started:
        # Keep this guard even though the concrete implementation raises for a
        # required failure. It also makes injected/test implementations obey the
        # same fail-closed lifecycle contract.
        raise RuntimeError(
            "Silicon Interface daemon is required but could not be started"
        )
    if container_mode and reset_container_pid:
        reset_marker.unlink(missing_ok=True)


def _activate_container_interface(inst: registry.Install) -> bool:
    """Re-activate image-owned launchers after any suspended state restore."""

    activator = CONTAINER_INTERFACE_ACTIVATOR
    runtime_interface = CONTAINER_INTERFACE_EXECUTABLE
    try:
        resolved_interface = runtime_interface.resolve(strict=True)
    except (OSError, RuntimeError):
        resolved_interface = runtime_interface
    if (
        activator.is_symlink()
        or not activator.is_file()
        or not os.access(activator, os.X_OK)
        or not resolved_interface.is_file()
        or not os.access(resolved_interface, os.X_OK)
    ):
        ui.warn(
            "The selected runtime image cannot activate Silicon Interface."
        )
        return False
    try:
        result = subprocess.run(
            [
                str(activator),
                "--root",
                str(Path(inst.path).resolve()),
                "--executable",
                str(runtime_interface),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        ui.warn(f"Silicon Interface activation failed: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        ui.warn(f"Silicon Interface activation failed{suffix}")
        return False
    return True


def _start_one_unlocked(
    target: str | None,
    *,
    start_agent: bool = True,
    reconcile_updates: bool = True,
) -> None:
    inst = registry.resolve_one(target)
    _reconcile_glass_terminal_state(inst)
    if inst.is_docker:
        docker_runtime.start_one(inst)
        _reconcile_backup_schedule(inst)
        return
    if reconcile_updates:
        # Local import avoids a module cycle: update owns process lifecycle
        # hooks and intentionally suppresses this reconciliation while it is
        # restarting services inside the same transaction.
        from . import update

        update.reconcile_before_start(inst)
    if legacy_offline_update_fenced(inst.path):
        ui.error(
            f"'{inst.name}' has a legacy offline update in progress; run "
            f"'silicon update resume {inst.name}' before starting it."
        )
        return
    owned_watchdog = _owned_watchdog_pid(inst.pid_file)
    if owned_watchdog is not None:
        pid, verified = owned_watchdog
        if not verified:
            ui.error(
                f"'{inst.name}' has a live watchdog but unsafe runtime "
                "metadata; stop it before starting another supervisor."
            )
            return
        ui.warn(f"'{inst.name}' is already running (PID {pid})")
        _start_interface_daemon(inst)
        if start_agent:
            glassagent.start(inst.path)
        _reconcile_backup_schedule(inst)
        return

    kill_floaters(inst.path)
    _start_interface_daemon(inst)
    pid_path = _validated_pid_path(inst.path, inst.pid_file)
    pid_path.unlink(missing_ok=True)
    (Path(inst.path) / ".silicon.stop").unlink(missing_ok=True)
    runtime_meta_file(inst.path).unlink(missing_ok=True)

    ui.info(f"Starting '{inst.name}' (with auto-restart)...")
    pid = _spawn_watchdog(inst.name, inst.path, inst.pid_file)
    try:
        _publish_pid(inst.path, inst.pid_file, pid)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        raise

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if runtime_healthy(inst.path, inst.pid_file, min_uptime=1.0):
            break
        if not _alive(pid):
            break
        time.sleep(0.25)
    if runtime_healthy(inst.path, inst.pid_file, min_uptime=1.0):
        ui.success(f"'{inst.name}' started (PID {pid})")
        ui.info(f"Auto-restart enabled. Logs: {inst.path}/.silicon.log")
    else:
        ui.error(f"'{inst.name}' failed to start. Check logs: {inst.path}/.silicon.log")
        try:
            _publish_stop_sentinel(inst.path)
        except RuntimeError as exc:
            # SIGTERM still shuts down the watchdog directly. Do not leave a
            # failed supervisor alive merely because its sentinel was unsafe.
            ui.warn(str(exc))
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        for _ in range(10):
            if not _alive(pid):
                break
            time.sleep(0.1)
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        Path(inst.pid_file).unlink(missing_ok=True)
        (Path(inst.path) / ".silicon.stop").unlink(missing_ok=True)
        runtime_meta_file(inst.path).unlink(missing_ok=True)

    if start_agent and runtime_healthy(
        inst.path,
        inst.pid_file,
        min_uptime=1.0,
    ):
        glassagent.start(inst.path)
    _reconcile_backup_schedule(inst)


def start_one(
    target: str | None,
    *,
    start_agent: bool = True,
    reconcile_updates: bool = True,
) -> None:
    inst = registry.resolve_one(target)
    if inst.is_docker:
        _start_one_unlocked(
            target,
            start_agent=start_agent,
            reconcile_updates=reconcile_updates,
        )
        return
    with _runtime_lifecycle_lock(inst.path, "start"):
        _start_one_unlocked(
            target,
            start_agent=start_agent,
            reconcile_updates=reconcile_updates,
        )


def _stop_one_unlocked(target: str | None, full: bool = False) -> None:
    inst = registry.resolve_one(target)
    if inst.is_docker:
        docker_runtime.stop_one(inst, full=full)
        return
    owned_watchdog = _owned_watchdog_pid(inst.pid_file)
    if owned_watchdog is None:
        ui.warn(f"'{inst.name}' is not running")
        kill_floaters(inst.path)
        Path(inst.pid_file).unlink(missing_ok=True)
        (Path(inst.path) / ".silicon.stop").unlink(missing_ok=True)
        runtime_meta_file(inst.path).unlink(missing_ok=True)
        if full:
            interface_cli.stop_daemon(inst.path, required=True)
            glassagent.stop(inst.path)
        return

    pid, verified = owned_watchdog
    if not verified:
        ui.warn(
            f"'{inst.name}' has unsafe runtime metadata; stopping its "
            "command-verified watchdog before clearing ownership."
        )
    _publish_stop_sentinel(inst.path)  # tell the watchdog not to restart
    ui.info(f"Stopping '{inst.name}' (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    for _ in range(10):
        if not _alive(pid):
            break
        time.sleep(0.5)
    if _alive(pid):
        ui.warn("Force stopping...")
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        for _ in range(10):
            if not _alive(pid):
                break
            time.sleep(0.1)
    if _alive(pid) and _legacy_watchdog_matches(pid, inst.pid_file):
        raise RuntimeError(
            f"'{inst.name}' watchdog is still alive; refusing to claim it stopped"
        )

    kill_floaters(inst.path)
    Path(inst.pid_file).unlink(missing_ok=True)
    (Path(inst.path) / ".silicon.stop").unlink(missing_ok=True)
    runtime_meta_file(inst.path).unlink(missing_ok=True)
    ui.success(f"'{inst.name}' stopped")

    if full:
        interface_cli.stop_daemon(inst.path, required=True)
        glassagent.stop(inst.path)
    else:
        ui.info("Glass agent still running (use --full to stop it too).")


def stop_one(target: str | None, full: bool = False) -> None:
    inst = registry.resolve_one(target)
    if inst.is_docker:
        _stop_one_unlocked(target, full=full)
        return
    with _runtime_lifecycle_lock(inst.path, "stop"):
        _stop_one_unlocked(target, full=full)


def _multi(target: str, verb: str, fn) -> bool:
    """Dispatch a multi-target selector. Returns True if it handled it."""
    if not (target and registry.is_multi_target(target)):
        return False
    names = registry.resolve_targets(target)
    if not names:
        ui.error("No matching installations")
        sys.exit(1)
    if target in {"all", "*"}:
        joined = ", ".join(names)
        if not ui.confirm(f"Are you sure you want to {verb} the following silicons: {joined}?"):
            return True
    for n in names:
        fn(n)
    return True


def start(target: str | None) -> None:
    if _multi(target or "", "start", start_one):
        return
    start_one(target)


def stop(target: str | None, full: bool = False) -> None:
    if _multi(target or "", "stop", lambda n: stop_one(n, full)):
        return
    stop_one(target, full)


def restart(target: str | None) -> None:
    inst = registry.resolve_one(target) if target and not registry.is_multi_target(target) else None
    if inst and inst.is_docker:
        docker_runtime.restart_one(inst)
        return
    stop(target)
    time.sleep(1)
    start(target)
