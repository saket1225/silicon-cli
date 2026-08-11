"""Glass sync — pull a silicon from Glass and run backups."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import (
    backup_runtime,
    docker_runtime,
    interface_cli,
    process,
    pull_transaction,
    registry,
    runtime_contract,
    stemcell,
    ui,
)
from .host_lock import HostFileLock, ensure_private_directory
from .http_transport import (
    UnsafeGlassURL,
    glass_endpoint,
    open_pinned,
    validate_glass_server,
)
from .config import (
    GLASS_SERVER_URL,
    REGISTRY_DIR,
    active_environment_python,
    active_release_root,
    python_run_cmd,
    runtime_environment,
)
from .updater.io import (
    atomic_write_bytes,
    atomic_write_json,
    ensure_real_directory,
    fsync_dir,
    hash_tree,
)
from .updater.generation import GenerationStore
from .updater.lock import InstanceLock, UpdateLocked


@dataclass
class PullOpts:
    """Non-interactive answers for ``silicon pull``, so Glass's setup agent can
    drive it headlessly over SSH. Any field left at its default falls back to the
    old interactive behaviour (or the non-TTY default when not on a terminal)."""

    assume_yes: bool = False        # take every prompt's default without asking
    brain: str | None = None        # "claude" | "codex" | "both"
    brain_order: list[str] = field(default_factory=list)
    backup: bool | None = None      # None = ask/default; True/False = force
    name: str | None = None         # instance name for a single-silicon pull

    def setup_config_kwargs(self) -> dict:
        """Brain overrides passed to ``stemcell.choose_setup_config``."""
        order = list(self.brain_order)
        brain = self.brain
        if brain == "both" and not order:
            order = ["claude", "codex"]
            brain = "claude"
        if brain in ("claude", "codex") and not order:
            order = [brain]
        return {"brain": brain if brain in ("claude", "codex") else None,
                "brain_order": order or None}

BACKUP_HOUR_UTC = 23
BACKUP_MINUTE_UTC = 59
BACKUP_HEARTBEAT_SECONDS = 30.0
BACKUP_RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0, 600.0)
BACKUP_HEARTBEAT_STALE_SECONDS = 3 * BACKUP_HEARTBEAT_SECONDS
BACKUP_PID_NAME = ".glass-push.pid"
BACKUP_LEASE_NAME = ".glass-push.pid.meta.json"
BACKUP_STATUS_NAME = "backup-supervisor.json"
BACKUP_SCHEDULE_NAME = "backup-schedule.json"
BACKUP_STATE_MAX_BYTES = 64 * 1024
PROVIDER_API_KEYS = (
    ("GEMINI_API_KEY", "Gemini"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("ELEVENLABS_API_KEY", "ElevenLabs"),
    ("DEEPGRAM_API_KEY", "Deepgram"),
    ("GIPHY_API_KEY", "GIPHY"),
    ("STEEL_API_KEY", "Steel"),
)
MAX_GLASS_JSON_RESPONSE_BYTES = 1024 * 1024
MAX_PULL_PRIVATE_FILE_BYTES = 1024 * 1024
BROWSER_PROFILE_SESSION_MAX_BYTES = 64 * 1024


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _urlopen(req, *, timeout: int | None = None):
    return open_pinned(req, timeout=timeout, context=_ssl_context())


def _read_json_object(response) -> dict:
    """Read one bounded UTF-8 JSON object without buffering an unbounded body."""

    content_length = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        content_length = headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("Glass returned an invalid Content-Length") from exc
        if declared_length < 0 or declared_length > MAX_GLASS_JSON_RESPONSE_BYTES:
            raise ValueError("Glass JSON response is too large")

    raw = response.read(MAX_GLASS_JSON_RESPONSE_BYTES + 1)
    if len(raw) > MAX_GLASS_JSON_RESPONSE_BYTES:
        raise ValueError("Glass JSON response is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Glass returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Glass JSON response must be an object")
    return value


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def _get_json_with_silicon_key(url: str, api_key: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        headers={
            "X-Silicon-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "silicon-cli",
        },
    )
    try:
        with _urlopen(req, timeout=30) as resp:
            return resp.status, _read_json_object(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _read_json_object(e)
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


def _post_json_with_team_key(url: str, team_key: str, body=None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "X-Team-Key": team_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "silicon-cli",
        },
    )
    try:
        with _urlopen(req, timeout=60) as resp:
            return resp.status, _read_json_object(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _read_json_object(e)
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


def _post_json(url: str, body=None, *, timeout: int = 60) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "silicon-cli",
        },
    )
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return resp.status, _read_json_object(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _read_json_object(e)
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


def _team_slug_from_silicon(silicon: dict) -> str:
    return str(
        silicon.get("team")
        or silicon.get("owner_team_slug")
        or silicon.get("team_slug")
        or ""
    ).strip()


def _team_api_keys_url(server: str, team_slug: str) -> str:
    return glass_endpoint(
        server,
        f"/api/v1/teams/{urllib.parse.quote(team_slug)}/api-keys",
    )


def _fetch_team_api_keys(server: str, api_key: str, team_slug: str) -> tuple[int, dict]:
    return _get_json_with_silicon_key(_team_api_keys_url(server, team_slug), api_key)


def _team_key_rows(body: dict) -> list[dict]:
    rows = body.get("keys") if isinstance(body, dict) else None
    return rows if isinstance(rows, list) else []


def _key_row_by_name(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get("key_name") or "").strip().upper()
            if name:
                out[name] = row
    return out


def _configured_team_key_names(rows: list[dict]) -> list[str]:
    by_name = _key_row_by_name(rows)
    configured = []
    for key_name, _label in PROVIDER_API_KEYS:
        row = by_name.get(key_name, {})
        if (
            row.get("configured")
            or row.get("team_configured")
            or row.get("center_configured")
            or row.get("server_fallback_configured")
        ):
            configured.append(key_name)
    return configured


def _display_team_api_keys(rows: list[dict]) -> None:
    by_name = _key_row_by_name(rows)
    ui.info("Provider API token status from Glass (secrets are not returned):")
    for key_name, label in PROVIDER_API_KEYS:
        row = by_name.get(key_name, {})
        source = str(row.get("source") or "").strip()
        if row.get("team_configured") or source == "team":
            status = "team override"
        elif row.get("center_configured") or source == "center":
            status = "center managed"
        elif row.get("server_fallback_configured") or source == "server":
            status = "server fallback"
        elif row.get("configured"):
            status = "Glass managed"
        elif row.get("server_fallback_configured"):
            status = "server fallback only"
        else:
            status = "missing"
        ui.info(f"  {label} ({key_name}): {status}")


def _provider_key_env_from_rows(team_slug: str, rows: list[dict], prompt: str = "") -> dict[str, str]:
    if not rows:
        ui.warn("Glass did not return provider API token metadata.")
        return {}

    _display_team_api_keys(rows)
    configured = _configured_team_key_names(rows)
    missing = [key_name for key_name, _label in PROVIDER_API_KEYS if key_name not in configured]
    if missing:
        ui.warn("Some provider API tokens are not saved in Glass for this team.")
        ui.info("Set them in Glass > API keys, then rerun silicon pull if these silicons need them.")
        return {}

    return {
        "SILICON_PROVIDER_KEYS_SOURCE": "glass",
        "SILICON_PROVIDER_KEYS_TEAM": team_slug,
        "SILICON_PROVIDER_KEYS": ",".join(configured),
    }


def _choose_glass_provider_keys(server: str, api_key: str, silicon: dict) -> dict[str, str]:
    team_slug = _team_slug_from_silicon(silicon)
    if not team_slug:
        ui.warn("Glass did not return a team slug; provider API token status was not checked.")
        return {}

    code, body = _fetch_team_api_keys(server, api_key, team_slug)
    if not (200 <= code < 300):
        ui.warn(body.get("detail") or body.get("error") or f"Could not read provider API tokens from Glass (HTTP {code}).")
        return {}

    return _provider_key_env_from_rows(
        team_slug,
        _team_key_rows(body),
        "Use these Glass-managed provider API tokens for this silicon?",
    )


def _safe_instance_name(raw: str, fallback: str = "silicon") -> str:
    value = (raw or "").strip().lower()
    value = "".join(c if c.isalnum() or c in "._-" else "-" for c in value)
    value = "-".join(part for part in value.split("-") if part)
    return value.strip("._-") or fallback


def _choose_target(label: str, silicon_id: str) -> tuple[str, Path]:
    name = label
    if registry.name_taken(name):
        suffix = silicon_id[-6:].lower() if silicon_id else uuid_hex()[:6]
        name = f"{name}-{suffix}"

    if docker_runtime.enabled():
        base = Path(docker_runtime.load_config(required=True)["root"]).expanduser()
    else:
        base = Path.cwd()
    target = base / name
    if not target.exists():
        return name, target

    if not ui.interactive():
        ui.error(f"Target folder already exists: {target}")
        sys.exit(1)

    while target.exists():
        name = ui.ask("Target folder name", f"{label}-{uuid_hex()[:6]}")
        target = base / _safe_instance_name(name, "silicon")
    return target.name, target


def _write_dotenv(
    path: Path,
    values: dict[str, str],
    *,
    remove_keys: set[str] | None = None,
) -> None:
    lines = []
    existing = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, value = raw.split("=", 1)
                existing[key.strip()] = value.strip()
            else:
                lines.append(raw)
    existing.update({k: v for k, v in values.items() if v is not None})
    for key in remove_keys or set():
        existing.pop(key, None)
    rendered = [line for line in lines if line.strip()]
    rendered.extend(f"{key}={value}" for key, value in existing.items())
    path.write_text("\n".join(rendered).rstrip() + "\n")


def _write_private_json(path: Path, payload: dict) -> None:
    """Atomically write a secret-bearing JSON file with owner-only access."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_dir(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _scrub_env_py_credentials(path: Path) -> None:
    """Keep the compatibility name empty and remove retired secret aliases."""

    if not path.exists():
        return
    rendered: list[str] = []
    wrote_empty_glass_key = False
    for line in path.read_text().splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key == "SILICON_UPDATE_AUTH_KEY":
            continue
        if key == "GLASS_API_KEY":
            if not wrote_empty_glass_key:
                rendered.append('GLASS_API_KEY = ""')
                wrote_empty_glass_key = True
            continue
        rendered.append(line)
    path.write_text("\n".join(rendered).rstrip() + ("\n" if rendered else ""))


def _seed_glass_files(
    target: Path,
    *,
    server: str,
    api_key: str,
    silicon: dict,
    instance_name: str,
    provider_key_env: dict[str, str] | None = None,
) -> None:
    silicon_id = str(silicon.get("silicon_id") or "").strip()
    silicon_name = str(silicon.get("name") or instance_name).strip()
    glass = {
        "server_url": server,
        "silicon_id": silicon_id,
        "silicon_username": silicon_name,
        "name": silicon_name,
        "api_key": api_key,
    }
    target.mkdir(parents=True, exist_ok=True)
    _write_private_json(target / ".glass.json", glass)
    env_values = {
        "GLASS_SERVER_URL": server,
        "SILICON_ID": silicon_id,
        "SILICON_NAME": silicon_name,
    }
    if provider_key_env:
        env_values.update(provider_key_env)
    _write_dotenv(
        target / ".env",
        env_values,
        remove_keys={"GLASS_API_KEY", "SILICON_UPDATE_AUTH_KEY"},
    )
    if (target / ".env").exists():
        os.chmod(target / ".env", 0o600)
    _scrub_env_py_credentials(target / "env.py")

    config = {}
    sj = target / "silicon.json"
    if sj.exists():
        try:
            config = json.loads(sj.read_text())
        except json.JSONDecodeError:
            config = {}
    public_glass = (
        dict(config.get("glass"))
        if isinstance(config.get("glass"), dict)
        else {}
    )
    public_glass.update({key: value for key, value in glass.items() if key != "api_key"})
    public_glass.pop("api_key", None)
    public_glass.pop("silicon_api_key", None)
    config.update(
        {
            "name": silicon_name,
            "address": instance_name,
            "silicon_id": silicon_id,
            "glass": public_glass,
        }
    )
    config.setdefault("run", "python main.py")
    config.setdefault("brain", "claude")
    config.setdefault(
        "workers",
        {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]},
    )
    config.setdefault("brain_order", [config.get("brain", "claude")])
    sj.write_text(json.dumps(config, indent=4) + "\n")


def _manifest_backup_now(
    path: str,
    note: str = "manual",
    *,
    instance_name: str | None = None,
    installation: registry.Install | None = None,
) -> bool:
    """Secure customizations, then run the active canonical backup provider."""

    root = Path(path).resolve()
    try:
        release_root = active_release_root(root)
    except RuntimeError as exc:
        ui.error(
            f"Could not resolve the active release for "
            f"'{instance_name or root.name}': {exc}"
        )
        return False
    canonical_backup = release_root / "core" / "backup.py"
    if not canonical_backup.is_file():
        label = instance_name or root.name
        ui.error(
            f"'{label}' uses an old Stemcell without canonical backups. "
            f"First run: silicon stop --full {label}. "
            f"Then run: silicon update {label}"
        )
        return False

    runner = (
        "import sys\n"
        "from core.backup import run_backup\n"
        "raise SystemExit(0 if run_backup(sys.argv[1], note=sys.argv[2]) else 1)\n"
    )
    transaction_id = f"backup-{uuid_hex()}"
    try:
        ensure_real_directory(root / ".silicon", root=root)
        with InstanceLock(root, transaction_id):
            backup_runtime.capture_active_customizations(root)
            if installation is not None and installation.is_docker:
                result = docker_runtime.run_active_python(
                    installation,
                    [
                        "-c",
                        runner,
                        docker_runtime.CONTAINER_PATH,
                        note,
                    ],
                    capture=False,
                )
            else:
                result = subprocess.run(
                    [python_run_cmd(root), "-c", runner, str(root), note],
                    cwd=release_root,
                    env=runtime_environment(root),
                )
    except (
        OSError,
        RuntimeError,
        UpdateLocked,
        backup_runtime.BackupSafetyError,
    ) as exc:
        ui.error(
            f"Could not safely back up '{instance_name or root.name}': {exc}"
        )
        return False
    return result.returncode == 0


def _backup_state_paths(path: str | Path) -> tuple[Path, Path, Path]:
    root = Path(path).resolve()
    return (
        root / BACKUP_PID_NAME,
        root / BACKUP_LEASE_NAME,
        root / ".silicon" / BACKUP_STATUS_NAME,
    )


def _backup_schedule_path(path: str | Path) -> Path:
    return Path(path).resolve() / ".silicon" / BACKUP_SCHEDULE_NAME


def _backup_schedule(path: str | Path) -> dict:
    schedule_path = _backup_schedule_path(path)
    if schedule_path.is_symlink():
        raise RuntimeError("backup schedule intent must not be a symbolic link")
    if schedule_path.exists() and not schedule_path.is_file():
        raise RuntimeError("backup schedule intent must be a regular file")
    value = _read_backup_json(schedule_path)
    if not value:
        return {"schema": 1, "enabled": False}
    if (
        value.get("schema") != 1
        or not isinstance(value.get("enabled"), bool)
        or not isinstance(value.get("name"), str)
        or not value.get("name")
        or not isinstance(value.get("updated_at"), (int, float))
        or isinstance(value.get("updated_at"), bool)
    ):
        raise RuntimeError("backup schedule intent is corrupt")
    return value


def _set_backup_schedule(path: str | Path, name: str, *, enabled: bool) -> dict:
    root = Path(path).resolve()
    state = ensure_real_directory(root / ".silicon", root=root)
    value = {
        "schema": 1,
        "enabled": bool(enabled),
        "name": str(name),
        "schedule": {
            "hour_utc": BACKUP_HOUR_UTC,
            "minute_utc": BACKUP_MINUTE_UTC,
        },
        "updated_at": time.time(),
    }
    atomic_write_json(state / BACKUP_SCHEDULE_NAME, value, mode=0o600)
    return value


def _read_backup_json(path: Path) -> dict:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if (
        path.is_symlink()
        or not path.is_file()
        or metadata.st_size > BACKUP_STATE_MAX_BYTES
    ):
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _inspect_process_command(pid: int) -> tuple[bool, str]:
    proc_command = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_command.is_file():
            return (
                True,
                proc_command.read_bytes().replace(b"\0", b" ").decode(
                    errors="replace"
                ),
            )
        inspected = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        return inspected.returncode == 0, inspected.stdout
    except (OSError, subprocess.SubprocessError):
        return False, ""


def _backup_process_matches(pid: int, token: str) -> bool:
    """Avoid treating an unrelated process that reused a stale PID as ours."""

    if not _pid_alive(pid) or not token:
        return False
    inspected, command = _inspect_process_command(pid)
    if not inspected:
        # When the platform denies command inspection, a live local PID plus a
        # private lease is safer than accidentally starting a duplicate loop.
        return True
    return "_backup_loop" in command and token in command


def _backup_supervisor_info(path: str | Path) -> dict:
    root = Path(path).resolve()
    pid_path, lease_path, status_path = _backup_state_paths(root)
    lease = _read_backup_json(lease_path)
    status = _read_backup_json(status_path)
    try:
        pid = int(lease.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    try:
        raw_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        raw_pid = 0
    token = str(lease.get("token") or "")
    verified_identity = (
        lease.get("schema") == 1
        and str(lease.get("host") or "") == socket.gethostname()
        and Path(str(lease.get("path") or "")).resolve() == root
        and pid == raw_pid
        and _backup_process_matches(pid, token)
    )
    probable_unverified_loop = False
    if not verified_identity and _pid_alive(raw_pid):
        inspected, command = _inspect_process_command(raw_pid)
        probable_unverified_loop = (
            not inspected
            or ("_backup_loop" in command and str(root) in command)
        )
    try:
        heartbeat = float(lease.get("heartbeat_at") or 0.0)
    except (TypeError, ValueError):
        heartbeat = 0.0
    responsive = bool(
        verified_identity
        and heartbeat
        and time.time() - heartbeat <= BACKUP_HEARTBEAT_STALE_SECONDS
    )
    return {
        **status,
        "running": bool(verified_identity or probable_unverified_loop),
        "verified_identity": bool(verified_identity),
        "responsive": responsive,
        "pid": pid if verified_identity else raw_pid,
        "token": token,
        "lease": lease,
    }


def _cleanup_stale_backup_lease(path: str | Path) -> None:
    pid_path, lease_path, _status_path = _backup_state_paths(path)
    pid_path.unlink(missing_ok=True)
    lease_path.unlink(missing_ok=True)


def _write_backup_status(root: Path, value: dict) -> None:
    _pid_path, _lease_path, status_path = _backup_state_paths(root)
    ensure_real_directory(status_path.parent, root=root)
    atomic_write_json(status_path, value, mode=0o600)


def _install_backup_lease(
    root: Path,
    *,
    name: str,
    pid: int,
    token: str,
) -> dict:
    pid_path, lease_path, _status_path = _backup_state_paths(root)
    now = time.time()
    lease = {
        "schema": 1,
        "pid": pid,
        "token": token,
        "host": socket.gethostname(),
        "path": str(root),
        "name": name,
        "started_at": now,
        "heartbeat_at": now,
    }
    atomic_write_bytes(pid_path, (str(pid) + "\n").encode("ascii"), mode=0o600)
    atomic_write_json(lease_path, lease, mode=0o600)
    _write_backup_status(
        root,
        {
            **lease,
            "state": "starting",
            "next_run_at": None,
            "consecutive_failures": 0,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": "",
        },
    )
    return lease


def _open_backup_log(root: Path):
    path = root / ".glass-push.log"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or current.st_ino != metadata.st_ino
            or current.st_dev != metadata.st_dev
        ):
            raise RuntimeError("backup supervisor log must be a local regular file")
        return os.fdopen(descriptor, "a", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def _heartbeat_backup_lease(
    root: Path,
    *,
    pid: int,
    token: str,
    status: dict,
) -> None:
    _pid_path, lease_path, _status_path = _backup_state_paths(root)
    lease = _read_backup_json(lease_path)
    if (
        lease.get("schema") != 1
        or lease.get("pid") != pid
        or lease.get("token") != token
    ):
        raise RuntimeError("scheduled backup supervisor lost its lease")
    now = time.time()
    lease["heartbeat_at"] = now
    atomic_write_json(lease_path, lease, mode=0o600)
    status.update(lease)
    status["heartbeat_at"] = now
    _write_backup_status(root, status)


def _registered_install(path: str | Path, name: str) -> registry.Install | None:
    root = Path(path).resolve()
    candidate = registry.find(name)
    if candidate is not None and Path(candidate.path).resolve() == root:
        return candidate
    for item in registry.installs():
        if Path(item.path).resolve() == root:
            return item
    return None


def _wait_with_backup_heartbeat(
    stop: threading.Event,
    seconds: float,
    heartbeat,
) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if stop.wait(min(BACKUP_HEARTBEAT_SECONDS, remaining)):
            return True
        heartbeat()
    return True


def backup_loop(
    path: str,
    name: str | None = None,
    token: str | None = None,
) -> None:
    root = Path(path).resolve()
    ensure_real_directory(root / ".silicon", root=root)
    label = name or root.name
    token = str(token or "")
    pid = os.getpid()
    if not token:
        token = uuid_hex()
        with InstanceLock(root, f"backup-supervisor-claim-{token}"):
            current = _backup_supervisor_info(root)
            if current.get("running"):
                return
            _cleanup_stale_backup_lease(root)
            _install_backup_lease(
                root,
                name=label,
                pid=pid,
                token=token,
            )
    else:
        # The parent installs the lease immediately after spawning. Avoid a
        # child/parent scheduling race without ever accepting a different token.
        for _attempt in range(100):
            lease = _read_backup_json(_backup_state_paths(root)[1])
            if lease.get("pid") == pid and lease.get("token") == token:
                break
            time.sleep(0.02)
        else:
            return

    stop = threading.Event()

    def request_stop(_signum=None, _frame=None):
        stop.set()

    for signum in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if signum is not None:
            try:
                signal.signal(signum, request_stop)
            except (OSError, ValueError):
                pass

    status = _read_backup_json(_backup_state_paths(root)[2])
    status.update(
        {
            "schema": 1,
            "pid": pid,
            "token": token,
            "host": socket.gethostname(),
            "path": str(root),
            "name": label,
            "state": "waiting",
            "consecutive_failures": 0,
            "last_error": "",
        }
    )
    install = _registered_install(root, label)

    def heartbeat() -> None:
        _heartbeat_backup_lease(
            root,
            pid=pid,
            token=token,
            status=status,
        )

    try:
        while not stop.is_set():
            wait = _seconds_until_next_backup()
            status["state"] = "waiting"
            status["next_run_at"] = time.time() + wait
            heartbeat()
            ui.info(f"Next scheduled backup for '{label}' at 23:59 GMT.")
            if _wait_with_backup_heartbeat(stop, wait, heartbeat):
                break

            succeeded = False
            for attempt in range(len(BACKUP_RETRY_DELAYS) + 1):
                status["state"] = "running"
                status["next_run_at"] = None
                status["last_attempt_at"] = time.time()
                heartbeat()
                ui.info(f"Running scheduled backup for '{label}'...")
                try:
                    succeeded = _manifest_backup_now(
                        str(root),
                        note="scheduled",
                        instance_name=label,
                        installation=install,
                    )
                except Exception as exc:
                    succeeded = False
                    status["last_error"] = str(exc)[:500]
                if succeeded:
                    status["state"] = "waiting"
                    status["consecutive_failures"] = 0
                    status["last_success_at"] = time.time()
                    status["last_error"] = ""
                    heartbeat()
                    break

                failures = int(status.get("consecutive_failures") or 0) + 1
                status["consecutive_failures"] = failures
                status["last_error"] = (
                    status.get("last_error") or "canonical backup failed"
                )
                if attempt >= len(BACKUP_RETRY_DELAYS):
                    status["state"] = "failed_until_next_schedule"
                    heartbeat()
                    break
                retry_wait = BACKUP_RETRY_DELAYS[attempt]
                status["state"] = "retrying"
                status["next_run_at"] = time.time() + retry_wait
                heartbeat()
                if _wait_with_backup_heartbeat(
                    stop,
                    retry_wait,
                    heartbeat,
                ):
                    break
            if stop.is_set():
                break
    finally:
        current = _read_backup_json(_backup_state_paths(root)[1])
        if current.get("pid") == pid and current.get("token") == token:
            status["state"] = "stopped"
            status["next_run_at"] = None
            status["heartbeat_at"] = time.time()
            _write_backup_status(root, status)
            _cleanup_stale_backup_lease(root)


def _seconds_until_next_backup(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    target = now.replace(
        hour=BACKUP_HOUR_UTC,
        minute=BACKUP_MINUTE_UTC,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _team_pull_url(server: str) -> str:
    return glass_endpoint(server, "/api/v1/teams/setup-pull")


def _team_pull_close_url(server: str, action: str) -> str:
    if action not in {"commit", "abort"}:
        raise ValueError("invalid team pull action")
    return glass_endpoint(server, f"/api/v1/teams/setup-pull/{action}")


def _browser_profile_setup_start_url(server: str) -> str:
    return glass_endpoint(server, "/api/v1/browser-profiles/setup/start")


def _browser_profile_setup_finish_url(server: str) -> str:
    return glass_endpoint(server, "/api/v1/browser-profiles/setup/finish")


def _browser_profile_session_path(session_id: str) -> Path:
    identifier = str(session_id or "").strip()
    if not identifier or len(identifier) > 512 or "\x00" in identifier:
        raise ValueError("browser profile session id is invalid")
    root = ensure_private_directory(REGISTRY_DIR)
    sessions = ensure_private_directory(root / "browser-profile-sessions")
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return sessions / f"{digest}.json"


def _save_browser_profile_session(
    *,
    token: str,
    session_id: str,
    before_ids: list[str],
    provider: str,
    server: str,
) -> Path:
    path = _browser_profile_session_path(session_id)
    atomic_write_json(
        path,
        {
            "schema": 1,
            "token": token,
            "session_id": session_id,
            "before_profile_ids": before_ids,
            "provider": provider,
            "server": server,
            "created_at": time.time(),
        },
        mode=0o600,
    )
    return path


def _load_browser_profile_session(session_id: str) -> tuple[Path, dict]:
    path = _browser_profile_session_path(session_id)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(
            "browser profile setup session was not found on this host"
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > BROWSER_PROFILE_SESSION_MAX_BYTES
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077)
    ):
        raise ValueError("browser profile setup session file is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("browser profile setup session file is invalid") from exc
    expected = {
        "schema",
        "token",
        "session_id",
        "before_profile_ids",
        "provider",
        "server",
        "created_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema") != 1
        or value.get("session_id") != session_id
        or not isinstance(value.get("token"), str)
        or not 1 <= len(value["token"]) <= 8192
        or "\x00" in value["token"]
        or not isinstance(value.get("before_profile_ids"), list)
        or not all(
            isinstance(item, str) and item and len(item) <= 512
            for item in value["before_profile_ids"]
        )
        or not isinstance(value.get("provider"), str)
        or not isinstance(value.get("server"), str)
        or not isinstance(value.get("created_at"), (int, float))
    ):
        raise ValueError("browser profile setup session file has invalid fields")
    value["server"] = validate_glass_server(value["server"])
    return path, value


def _browser_profile_finish_command(session_id: str) -> str:
    return f"silicon browser-profile finish {shlex.quote(session_id)}"


def _browser_profile_provider_label(provider: str | None) -> str:
    """Return the provider's user-facing name, or an empty generic fallback."""
    return {
        "browserbase": "Browserbase",
        "steel": "Steel",
    }.get(str(provider or "").strip().lower(), "")


def browser_profile_finish(
    token: str | None,
    session_id: str | None,
    before_ids_csv: str | None = None,
    provider: str | None = None,
) -> None:
    token_or_session = (token or "").strip()
    session_id = (session_id or "").strip()
    pending_path = None
    pending = None
    if token_or_session and not session_id:
        try:
            pending_path, pending = _load_browser_profile_session(token_or_session)
        except (OSError, ValueError) as exc:
            ui.error(str(exc))
            sys.exit(1)
        token = pending["token"]
        session_id = pending["session_id"]
        before_ids_csv = ",".join(pending["before_profile_ids"])
        provider = provider or pending["provider"]
        server = pending["server"]
    else:
        token = token_or_session
        try:
            server = validate_glass_server(GLASS_SERVER_URL)
        except UnsafeGlassURL as exc:
            ui.error(str(exc))
            sys.exit(1)
    if not token or not session_id:
        ui.error(
            "Usage: silicon browser-profile finish <session_id>"
        )
        sys.exit(1)
    before_ids = [p.strip() for p in (before_ids_csv or "").split(",") if p.strip()]
    ui.info("Finishing browser profile setup with Glass...")
    code, body = _post_json(
        _browser_profile_setup_finish_url(server),
        {"token": token, "session_id": session_id, "before_profile_ids": before_ids},
        timeout=120,
    )
    if not (200 <= code < 300):
        ui.error(body.get("detail") or body.get("error") or f"Glass could not finish the profile setup (HTTP {code}).")
        sys.exit(1)
    profile = body.get("profile") or {}
    name = profile.get("name") or profile.get("id") or "browser profile"
    assigned = body.get("assigned", 0)
    provider_label = _browser_profile_provider_label(provider or body.get("provider"))
    profile_kind = f"{provider_label} profile" if provider_label else "browser profile"
    if pending_path is not None:
        pending_path.unlink(missing_ok=True)
        fsync_dir(pending_path.parent)
    ui.success(f"Saved {profile_kind} '{name}' and assigned it to {assigned} silicon(s).")


def browser_profile_setup(token: str | None) -> None:
    token = (token or "").strip()
    if not token:
        token = ui.read_secret("Glass browser profile setup token").strip()
    if not token:
        ui.error("A Glass browser profile setup token is required.")
        sys.exit(1)
    try:
        server = validate_glass_server(GLASS_SERVER_URL)
    except UnsafeGlassURL as exc:
        ui.error(str(exc))
        sys.exit(1)
    ui.info("Starting browser profile setup with Glass...")
    code, body = _post_json(_browser_profile_setup_start_url(server), {"token": token}, timeout=120)
    if not (200 <= code < 300):
        ui.error(body.get("detail") or body.get("error") or f"Glass could not start the profile setup (HTTP {code}).")
        sys.exit(1)

    session_id = str(body.get("session_id") or "").strip()
    viewer_url = str(body.get("viewer_url") or body.get("debug_url") or "").strip()
    provider = str(body.get("provider") or "").strip()
    before_ids = [str(p) for p in (body.get("before_profile_ids") or []) if p]
    if not session_id or not viewer_url:
        ui.error("Glass returned an incomplete browser setup session.")
        sys.exit(1)
    try:
        _save_browser_profile_session(
            token=token,
            session_id=session_id,
            before_ids=before_ids,
            provider=provider,
            server=server,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        ui.error(f"Could not save the private browser setup session: {exc}")
        sys.exit(1)

    provider_label = _browser_profile_provider_label(provider)
    session_kind = f"{provider_label} setup" if provider_label else "Browser setup"
    ui.success(f"{session_kind} session started.")
    ui.info(f"Viewer URL: {viewer_url}")
    try:
        webbrowser.open(viewer_url)
    except Exception:
        pass

    finish_cmd = _browser_profile_finish_command(session_id)
    if not ui.interactive():
        ui.info("When the browser is configured, finish with:")
        print(f"  {finish_cmd}")
        return

    print()
    ui.info("Use the browser window to log in or configure the profile.")
    ui.info("Press Enter here when you're done. Ctrl+C leaves the session open; finish later with:")
    print(f"  {finish_cmd}")
    try:
        input()
    except KeyboardInterrupt:
        print()
        ui.warn("Setup session left open.")
        ui.info(f"Finish later with: {finish_cmd}")
        return

    browser_profile_finish(session_id, None)


def _silicon_display(silicon: dict) -> str:
    name = str(silicon.get("name") or "").strip()
    sid = str(silicon.get("silicon_id") or "").strip()
    return f"{name} ({sid})" if name and sid else name or sid or "silicon"


def _select_silicons_for_custom_settings(silicons: list[dict]) -> set[str]:
    if not ui.interactive() or len(silicons) <= 1:
        return set()
    if ui.confirm("Use these same settings for all silicons?", default_yes=True):
        return set()

    ui.info("Select silicons that need different settings. You can use numbers, names, or silicon IDs.")
    lookup: dict[str, str] = {}
    for idx, silicon in enumerate(silicons, start=1):
        sid = str(silicon.get("silicon_id") or "").strip()
        name = str(silicon.get("name") or "").strip()
        ui.info(f"  {idx}. {_silicon_display(silicon)}")
        if sid:
            lookup[str(idx)] = sid
            lookup[sid.lower()] = sid
        if name:
            lookup[name.lower()] = sid

    raw = ui.ask("Different settings for", "")
    selected: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        key = part.strip().lower()
        if key and key in lookup:
            selected.add(lookup[key])
    if raw.strip() and not selected:
        ui.warn("No matching silicons selected; using the default settings for all.")
    return selected


def _team_setup_configs(silicons: list[dict], opts: PullOpts | None = None) -> dict[str, dict]:
    kw = opts.setup_config_kwargs() if opts else {}
    base = stemcell.choose_setup_config("Default setup for all team silicons", **kw)
    # Glass may specify a brain per silicon (from the Create Team wizard). When it
    # does, honor those and skip the interactive per-silicon prompt.
    glass_has_brains = any((s.get("brain") or "").strip() for s in silicons)
    custom = set()
    if not glass_has_brains and not (opts and (opts.assume_yes or opts.brain)):
        custom = _select_silicons_for_custom_settings(silicons)
    configs: dict[str, dict] = {}
    for silicon in silicons:
        sid = str(silicon.get("silicon_id") or "").strip()
        b = (silicon.get("brain") or "").strip().lower()
        if b in ("claude", "codex"):
            order = [x for x in (silicon.get("brain_order") or [b]) if x in ("claude", "codex")] or [b]
            configs[sid] = stemcell.choose_setup_config("", brain=b, brain_order=order)
        else:
            configs[sid] = base
    for silicon in silicons:
        sid = str(silicon.get("silicon_id") or "").strip()
        if sid in custom:
            configs[sid] = stemcell.choose_setup_config(f"Setup for {_silicon_display(silicon)}")
    return configs


def _provider_keys_from_team_pull(body: dict) -> dict[str, str]:
    team = body.get("team") if isinstance(body, dict) else {}
    team_slug = str((team or {}).get("slug") or "").strip()
    api_keys = body.get("api_keys") if isinstance(body, dict) else {}
    if not team_slug:
        ui.warn("Glass did not return a team slug; provider API token status was not checked.")
        return {}
    return _provider_key_env_from_rows(
        team_slug,
        _team_key_rows(api_keys if isinstance(api_keys, dict) else {}),
        "Use these Glass-managed provider API tokens for all pulled silicons?",
    )


def _want_backups(opts: PullOpts | None) -> bool:
    """Whether to enable daily backups. An explicit ``--backup/--no-backup`` wins;
    otherwise ask (or take the non-TTY default). ``--yes`` implies yes."""
    if opts and opts.backup is not None:
        return opts.backup
    if opts and opts.assume_yes:
        return True
    if not ui.interactive():
        return False
    return ui.confirm("Enable daily 23:59 UTC backups for all pulled silicons?")


def _pull_runtime_requested(journal: pull_transaction.PullJournal) -> bool:
    if journal.value["runtime"]:
        return journal.value["runtime"] == "docker"
    return docker_runtime.runtime_requested()


def _prepare_runtime_for_release(prepared, *, use_docker: bool) -> tuple[Path, str]:
    if not use_docker:
        runtime_contract.verify_host_pull_runtime()
        parent = Path.cwd().resolve(strict=True)
        if parent.is_symlink() or not parent.is_dir():
            raise RuntimeError(f"pull destination is unsafe: {parent}")
        return parent, ""

    runtime_image = str(prepared.release.manifest.runtime_image or "")
    if not runtime_image:
        raise RuntimeError(
            "the published Silicon release has no digest-pinned runtime image"
        )
    config = docker_runtime.ensure_ready(
        auto_init=True,
        install=True,
        pull_image=True,
        image=runtime_image,
        refresh_image=False,
    )
    docker_runtime.verify_runtime_contract(config, runtime_image)
    docker_runtime.maybe_prompt_login(config)
    parent = Path(config["root"]).expanduser().resolve(strict=True)
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError(f"Docker pull destination is unsafe: {parent}")
    return parent, runtime_image


def _bounded_instance_name(value: str, fallback: str) -> str:
    safe = _safe_instance_name(value, fallback)
    return safe[:120].rstrip("._-") or fallback[:120]


def _plan_pull_items(
    journal: pull_transaction.PullJournal,
    silicons: list[dict],
    setup_configs: dict[str, dict],
    parent: Path,
) -> list[dict]:
    installs = registry.installs()
    reserved_names = {install.name for install in installs}
    reserved_paths = {
        str(Path(install.path).expanduser().resolve()) for install in installs
    }
    reserved_services = {
        install.service for install in installs if install.service
    }
    reserved_containers = {
        install.container_name for install in installs if install.container_name
    }
    planned_names: set[str] = set()
    planned_paths: set[str] = set()
    items: list[dict] = []
    for silicon in silicons:
        silicon_id = str(silicon.get("silicon_id") or "").strip()
        silicon_name = str(silicon.get("name") or "").strip()
        requested = str(silicon.get("_instance_name") or silicon_name)
        fallback = f"silicon-{silicon_id[-6:].lower()}"
        base = _bounded_instance_name(requested, fallback)
        suffix = _safe_instance_name(silicon_id[-8:], uuid_hex()[:8])
        digest_suffix = pull_transaction.credential_fingerprint(
            silicon_id, silicon_name
        )[:10]
        candidates = [
            base,
            _bounded_instance_name(f"{base}-{suffix}", fallback),
            _bounded_instance_name(
                f"{base[:100].rstrip('._-')}-{digest_suffix}", fallback
            ),
        ]
        chosen = ""
        for candidate in dict.fromkeys(candidates):
            final = (parent / candidate).resolve(strict=False)
            stage = parent / (
                f".{candidate}.silicon-pull-{journal.transaction_id[:12]}"
            )
            if (
                candidate in reserved_names
                or candidate in planned_names
                or str(final) in reserved_paths
                or str(final) in planned_paths
                or final.exists()
                or final.is_symlink()
                or stage.exists()
                or stage.is_symlink()
                or docker_runtime.service_name(candidate) in reserved_services
                or docker_runtime.container_name(candidate)
                in reserved_containers
            ):
                continue
            chosen = candidate
            break
        if not chosen:
            raise RuntimeError(
                f"no collision-free deterministic target is available for "
                f"{_silicon_display(silicon)}"
            )
        item = pull_transaction.planned_item(
            silicon_id=silicon_id,
            silicon_name=silicon_name,
            name=chosen,
            parent=parent,
            transaction_id=journal.transaction_id,
            setup_config=setup_configs.get(silicon_id) or {},
        )
        planned_names.add(chosen)
        planned_paths.add(item["final_path"])
        items.append(item)
    if not items:
        raise RuntimeError("Glass returned no Silicons to pull.")
    return items


def _verify_private_regular(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_PULL_PRIVATE_FILE_BYTES
    ):
        raise RuntimeError(f"pull secret file is unsafe: {path}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"pull secret file is not owner-only: {path}")


def _verify_staged_pull(
    stage: Path,
    item: dict,
    api_key: str,
    prepared,
    *,
    install_deps: bool,
) -> None:
    metadata = stage.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"pull stage is unsafe: {stage}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"pull stage is not owner-only: {stage}")
    for secret_path in (
        stage / ".glass.json",
        stage / ".env",
        pull_transaction.PullJournal.marker_path(stage),
    ):
        _verify_private_regular(secret_path)
    glass = json.loads((stage / ".glass.json").read_text(encoding="utf-8"))
    if (
        not isinstance(glass, dict)
        or glass.get("api_key") != api_key
        or glass.get("silicon_id") != item["silicon_id"]
    ):
        raise RuntimeError("staged Glass credentials do not match this pull")
    if api_key in (stage / ".env").read_text(encoding="utf-8"):
        raise RuntimeError("staged Glass credential leaked into .env")
    if api_key in (stage / "silicon.json").read_text(encoding="utf-8"):
        raise RuntimeError("staged Glass credential leaked into silicon.json")

    expected_tree = prepared.release.manifest.identity.tree_sha256
    generation = GenerationStore(stage).current()
    if (
        generation.get("kind") != "immutable-release"
        or generation.get("upstream_tree_sha256") != expected_tree
        or generation.get("materialized_tree_sha256") != expected_tree
    ):
        raise RuntimeError("staged Silicon generation identity is invalid")
    active = active_release_root(stage)
    actual_tree, _files = hash_tree(active)
    if actual_tree != expected_tree:
        raise RuntimeError("staged Silicon generation failed tree verification")
    if install_deps and (active / "requirements.lock").is_file():
        if not active_environment_python(stage):
            raise RuntimeError(
                "staged Silicon dependency environment failed verification"
            )


def _stage_pull_items(
    journal: pull_transaction.PullJournal,
    silicons_by_id: dict[str, dict],
    credentials_by_id: dict[str, str],
    prepared,
) -> None:
    install_deps = journal.value["runtime"] == "local"
    if journal.state not in {"PLANNED", "STAGING", "STAGED"}:
        return
    if journal.state != "STAGED":
        journal.set_state("STAGING")
    for index, item in enumerate(journal.items):
        if item["staged"]:
            stage = Path(item["stage_path"])
            final = Path(item["final_path"])
            stage_exists = stage.exists() or stage.is_symlink()
            final_exists = final.exists() or final.is_symlink()
            if stage_exists == final_exists:
                raise RuntimeError(
                    f"pull recovery target is ambiguous for '{item['name']}'"
                )
            root = final if final_exists else stage
            journal.verify_marker(root, item)
            api_key = credentials_by_id.get(item["silicon_id"], "")
            if not api_key:
                raise RuntimeError(
                    f"Glass did not replay the credential for {item['silicon_id']}"
                )
            _verify_staged_pull(
                root,
                item,
                api_key,
                prepared,
                install_deps=install_deps,
            )
            if install_deps:
                runtime_contract.verify_local_interface_install(root)
            continue
        silicon = silicons_by_id.get(item["silicon_id"])
        api_key = credentials_by_id.get(item["silicon_id"], "")
        if silicon is None or not api_key:
            raise RuntimeError(
                f"Glass did not replay the credential for {item['silicon_id']}"
            )
        stage = journal.prepare_stage(item)
        stable_silicon = {
            **silicon,
            "name": item["silicon_name"],
            "silicon_id": item["silicon_id"],
        }
        _seed_glass_files(
            stage,
            server=journal.value["server"],
            api_key=api_key,
            silicon=stable_silicon,
            instance_name=item["name"],
            provider_key_env=journal.value["provider_key_env"],
        )
        stemcell.hydrate(
            str(stage),
            setup_config=item["setup_config"],
            install_deps=install_deps,
            setup_interface=False,
            register_install=False,
            prepared=prepared,
        )
        if install_deps:
            interface_cli.setup(
                stage,
                required=True,
                start_daemon=False,
            )
        journal.write_stage_marker(item)
        _verify_staged_pull(
            stage,
            item,
            api_key,
            prepared,
            install_deps=install_deps,
        )
        journal.update_item(index, staged=True)
    journal.set_state("STAGED")


def _close_team_pull_claim(
    journal: pull_transaction.PullJournal,
    api_key: str,
    action: str,
) -> tuple[bool, str]:
    code, body = _post_json_with_team_key(
        _team_pull_close_url(journal.value["server"], action),
        api_key,
        {"pull_transaction_id": journal.transaction_id},
    )
    if 200 <= code < 300 and body.get("claim_state") == (
        "committed" if action == "commit" else "aborted"
    ):
        return True, ""
    detail = (
        body.get("detail")
        or body.get("error")
        or f"Glass setup pull {action} failed (HTTP {code})"
    )
    return False, str(detail)


def _refresh_team_pull_claim_credentials(
    journal: pull_transaction.PullJournal,
    api_key: str,
) -> None:
    """Renew the pending lease and atomically reconcile replacement keys.

    Glass may have revoked an old pending claim while this host was offline.
    Reopening the same transaction returns replacements only after the old keys
    are revoked. Updating the marked stage/final secret files here makes even a
    post-rename, post-expiry recovery converge safely.
    """

    code, body = _post_json_with_team_key(
        _team_pull_url(journal.value["server"]),
        api_key,
        {"pull_transaction_id": journal.transaction_id},
    )
    if not 200 <= code < 300:
        raise RuntimeError(
            body.get("detail")
            or body.get("error")
            or f"Glass could not renew the setup pull claim (HTTP {code})"
        )
    if body.get("pull_transaction_id") != journal.transaction_id:
        raise RuntimeError("Glass renewed a different pull transaction identity")
    if body.get("claim_state") == "committed":
        # The prior commit response was lost. Existing final credentials are
        # already authoritative and the idempotent commit endpoint will agree.
        return
    if body.get("claim_state") != "pending":
        raise RuntimeError("Glass setup pull claim is not pending")
    rows = [
        row
        for row in (body.get("silicons") or [])
        if isinstance(row, dict)
        and str(row.get("silicon_id") or "").strip()
    ]
    by_id = {
        str(row.get("silicon_id") or ""): str(row.get("api_key") or "")
        for row in rows
    }
    planned_ids = {item["silicon_id"] for item in journal.items}
    if (
        len(rows) != len(planned_ids)
        or set(by_id) != planned_ids
        or any(
            not key.startswith("scs_live_") or not 24 <= len(key) <= 128
            for key in by_id.values()
        )
    ):
        raise RuntimeError(
            "Glass did not replay every credential in the setup pull claim"
        )
    for item in journal.items:
        stage = Path(item["stage_path"])
        final = Path(item["final_path"])
        if final.exists() and not stage.exists():
            root = final
        elif stage.exists() and not final.exists():
            root = stage
        else:
            raise RuntimeError(
                f"pull credential target is ambiguous for '{item['name']}'"
            )
        journal.verify_marker(root, item)
        glass_path = root / ".glass.json"
        _verify_private_regular(glass_path)
        try:
            glass = json.loads(glass_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"pull credential store is invalid: {glass_path}"
            ) from exc
        if (
            not isinstance(glass, dict)
            or glass.get("silicon_id") != item["silicon_id"]
            or glass.get("server_url") != journal.value["server"]
        ):
            raise RuntimeError(
                f"pull credential store identity changed: {glass_path}"
            )
        glass["api_key"] = by_id[item["silicon_id"]]
        _write_private_json(glass_path, glass)
        _verify_private_regular(glass_path)


def _register_pulled_item(
    journal: pull_transaction.PullJournal,
    item: dict,
) -> registry.Install:
    final = Path(item["final_path"])
    if journal.value["runtime"] == "docker":
        install = docker_runtime.register_instance(
            item["name"],
            final,
            image=journal.value["runtime_image"],
        )
    else:
        registry.register(item["name"], str(final))
        install = registry.find(item["name"])
        if install is None:
            raise RuntimeError(f"could not register pulled Silicon '{item['name']}'")
    if (
        install.name != item["name"]
        or Path(install.path).expanduser().resolve() != final.resolve()
    ):
        raise RuntimeError(f"registry identity mismatch for '{item['name']}'")
    if journal.value["runtime"] == "docker" and (
        install.container_name != docker_runtime.container_name(item["name"])
        or install.service != docker_runtime.service_name(item["name"])
        or install.image != journal.value["runtime_image"]
    ):
        raise RuntimeError(f"Docker identity mismatch for '{item['name']}'")
    return install


def _prepare_pulled_docker_runtime(
    journal: pull_transaction.PullJournal,
    item: dict,
    install: registry.Install,
) -> None:
    """Build and bind the Linux dependency environment before first boot."""

    final = Path(item["final_path"]).resolve()
    transaction_id = str(
        getattr(journal, "transaction_id", "") or "pull-runtime"
    )
    with InstanceLock(final, f"{transaction_id}-runtime"):
        store = GenerationStore(final)
        generation = store.current()
        release = store.resolve_release(generation)
        ui.info(
            f"Prebuilding Docker runtime dependencies for '{item['name']}'..."
        )
        environment = docker_runtime.prepare_environment(
            install,
            release,
            image=str(journal.value.get("runtime_image") or install.image or ""),
        )
        if environment is None:
            return
        relative = environment.resolve().relative_to(final).as_posix()
        expected_prefix = ".silicon/environments/"
        if not relative.startswith(expected_prefix):
            raise RuntimeError(
                "prepared Docker dependency environment escaped its generation store"
            )
        if generation.get("environment_path") == relative:
            return
        updated = dict(generation)
        updated["environment_path"] = relative
        # Restore republishes the already-authenticated generation metadata
        # without creating a false rollback edge in generation history.
        store.restore(updated)


def _finish_pull(journal: pull_transaction.PullJournal) -> None:
    if journal.state == "CLAIM_COMMITTED":
        journal.set_state("POSTCOMMIT")
    if journal.state not in {"POSTCOMMIT", "COMPLETE"}:
        return
    for index, item in enumerate(journal.items):
        final = Path(item["final_path"])
        journal.verify_marker(final, item)
        if not item["registered"]:
            _register_pulled_item(journal, item)
            journal.update_item(index, registered=True)
        if not item["interface_attempted"]:
            if journal.value["runtime"] == "local":
                interface_cli.setup(final, required=True)
            # Docker registration atomically renders the compose interface.
            journal.update_item(index, interface_attempted=True)
        if not item["started"]:
            install = registry.find(item["name"])
            if install is None:
                raise RuntimeError(
                    f"could not resolve pulled Silicon '{item['name']}'"
                )
            if (
                journal.value["runtime"] == "docker"
                and journal.value.get("runtime_image")
            ):
                _prepare_pulled_docker_runtime(journal, item, install)
            process.start_one(item["name"])
            install = registry.find(item["name"])
            if install is None or not process.install_is_running(install):
                raise RuntimeError(
                    f"pulled Silicon '{item['name']}' did not become healthy"
                )
            if journal.value["runtime"] == "docker":
                ui.info(
                    f"Verifying Silicon Extend for pulled Silicon "
                    f"'{item['name']}'..."
                )
                docker_runtime.verify_silicon_extend(install)
            journal.update_item(index, started=True)
        if journal.value["backups"] and not item["backup_attempted"]:
            ui.info(f"Running initial backup for '{item['name']}'...")
            ok = _manifest_backup_now(
                str(final),
                note="initial",
                instance_name=item["name"],
                installation=_registered_install(final, item["name"]),
            )
            journal.update_item(index, backup_attempted=True)
            if ok:
                _start_backup_loop(str(final), item["name"])
            else:
                ui.warn(
                    f"Initial backup failed. Retry with: "
                    f"silicon backup {item['name']} now"
                )
    journal.set_state("COMPLETE")


def _recover_or_prepare_release(
    journal: pull_transaction.PullJournal,
    *,
    use_docker: bool,
):
    expected_tree = journal.value["release_tree_sha256"] or None
    return stemcell.prepare_hydration(
        install_deps=not use_docker,
        expected_tree_sha256=expected_tree,
    )


def _abort_precommit(
    journal: pull_transaction.PullJournal,
    *,
    team_key: str | None,
) -> None:
    if journal.items:
        journal.cleanup_precommit(mark_aborted=False)
    if team_key is not None:
        closed, detail = _close_team_pull_claim(journal, team_key, "abort")
        if not closed:
            ui.warn(
                f"Local pull staging was cleaned, but Glass could not revoke "
                f"the pending credential claim: {detail}. Rerun the same pull "
                "to resume cleanup safely."
            )
            return
    journal.set_state("ABORTED")


def _execute_planned_pull(
    journal: pull_transaction.PullJournal,
    *,
    prepared,
    silicons: list[dict],
    team_key: str | None,
) -> None:
    by_id = {
        str(silicon.get("silicon_id") or ""): silicon for silicon in silicons
    }
    credentials = {
        silicon_id: str(silicon.get("api_key") or "")
        for silicon_id, silicon in by_id.items()
    }
    try:
        if (
            journal.value["runtime"] == "local"
            and journal.state
            not in {"CLAIM_COMMITTED", "POSTCOMMIT", "COMPLETE"}
        ):
            providers: set[str] = set()
            for item in journal.items:
                setup_config = item.get("setup_config")
                if not isinstance(setup_config, dict):
                    setup_config = {}
                order = setup_config.get("brain_order")
                if isinstance(order, list):
                    providers.update(
                        str(provider)
                        for provider in order
                        if provider in {"claude", "codex"}
                    )
                brain = str(setup_config.get("brain") or "claude")
                providers.add("codex" if brain == "codex" else "claude")
            runtime_contract.verify_host_providers(providers)
        _stage_pull_items(journal, by_id, credentials, prepared)
        if team_key is not None and journal.state in {
            "STAGED",
            "COMMITTING",
            "RENAMED",
        }:
            _refresh_team_pull_claim_credentials(journal, team_key)
        if journal.state in {"STAGED", "COMMITTING", "RENAMED"}:
            journal.reconcile_and_commit()
        if team_key is not None and journal.state == "RENAMED":
            closed, detail = _close_team_pull_claim(
                journal, team_key, "commit"
            )
            if not closed:
                raise RuntimeError(
                    f"Glass could not commit the setup credential claim: {detail}. "
                    "The staged folders are safe; rerun the same pull to resume."
                )
            journal.set_state("CLAIM_COMMITTED")
        elif team_key is None and journal.state == "RENAMED":
            journal.set_state("CLAIM_COMMITTED")
        _finish_pull(journal)
    except (Exception, KeyboardInterrupt):
        if not any(item["renamed"] for item in journal.items):
            _abort_precommit(journal, team_key=team_key)
        raise


def _pull_team(
    api_key: str,
    server: str,
    opts: PullOpts | None,
    journal: pull_transaction.PullJournal,
) -> None:
    use_docker = _pull_runtime_requested(journal)
    with _recover_or_prepare_release(journal, use_docker=use_docker) as prepared:
        parent, runtime_image = _prepare_runtime_for_release(
            prepared, use_docker=use_docker
        )
        ui.info("Checking team setup token with Glass...")
        code, body = _post_json_with_team_key(
            _team_pull_url(server),
            api_key,
            {"pull_transaction_id": journal.transaction_id},
        )
        if not (200 <= code < 300):
            raise RuntimeError(
                body.get("detail")
                or body.get("error")
                or f"Glass rejected the team token (HTTP {code}). "
                "Rerun the same pull; its idempotency identity is saved."
            )
        if body.get("pull_transaction_id") != journal.transaction_id:
            raise RuntimeError("Glass returned a different pull transaction identity")
        try:
            team = body.get("team") if isinstance(body, dict) else {}
            team_name = str(
                (team or {}).get("name")
                or (team or {}).get("slug")
                or "team"
            ).strip()
            silicons = [
                silicon
                for silicon in (body.get("silicons") or [])
                if isinstance(silicon, dict)
                and str(silicon.get("silicon_id") or "").strip()
            ]
            if journal.state == "INIT":
                if not silicons:
                    raise RuntimeError(
                        f"Glass returned no Silicons for team '{team_name}'."
                    )
                provider_key_env = _provider_keys_from_team_pull(body)
                setup_configs = _team_setup_configs(silicons, opts)
                journal.initialize(
                    team_name=team_name,
                    runtime="docker" if use_docker else "local",
                    runtime_image=runtime_image,
                    release_tree_sha256=(
                        prepared.release.manifest.identity.tree_sha256
                    ),
                    environment_path=str(prepared.environment or ""),
                    backups=_want_backups(opts),
                    provider_key_env=provider_key_env,
                    items=_plan_pull_items(
                        journal, silicons, setup_configs, parent
                    ),
                )
            else:
                if (
                    journal.value["runtime"]
                    != ("docker" if use_docker else "local")
                    or journal.value["runtime_image"] != runtime_image
                    or journal.value["release_tree_sha256"]
                    != prepared.release.manifest.identity.tree_sha256
                    or journal.value["environment_path"]
                    != str(prepared.environment or "")
                ):
                    raise RuntimeError(
                        "pull recovery runtime or release identity changed"
                    )
                response_ids = {
                    str(silicon.get("silicon_id") or "")
                    for silicon in silicons
                }
                planned_ids = {item["silicon_id"] for item in journal.items}
                if silicons and response_ids != planned_ids:
                    raise RuntimeError(
                        "Glass replayed a different set of team Silicons"
                    )
                if not silicons and journal.state not in {
                    "RENAMED",
                    "CLAIM_COMMITTED",
                    "POSTCOMMIT",
                    "COMPLETE",
                }:
                    raise RuntimeError(
                        "Glass did not replay pending setup credentials"
                    )
            _execute_planned_pull(
                journal,
                prepared=prepared,
                silicons=silicons,
                team_key=api_key,
            )
        except (Exception, KeyboardInterrupt):
            if (
                journal.state != "ABORTED"
                and not any(item["renamed"] for item in journal.items)
            ):
                _abort_precommit(journal, team_key=api_key)
            raise
    ui.success(
        f"Pulled {len(journal.items)} Silicon(s) from team "
        f"'{journal.value['team_name']}'."
    )
    for item in journal.items:
        ui.info(f"  {item['name']}: {item['final_path']}")


def _pull_single(
    api_key: str,
    server: str,
    opts: PullOpts | None,
    journal: pull_transaction.PullJournal,
) -> None:
    use_docker = _pull_runtime_requested(journal)
    with _recover_or_prepare_release(journal, use_docker=use_docker) as prepared:
        parent, runtime_image = _prepare_runtime_for_release(
            prepared, use_docker=use_docker
        )
        ui.info("Checking token with Glass...")
        code, silicon = _get_json_with_silicon_key(
            glass_endpoint(server, "/api/v1/silicons/me"), api_key
        )
        if not (200 <= code < 300):
            raise RuntimeError(
                silicon.get("detail")
                or silicon.get("error")
                or f"Glass rejected the token (HTTP {code})."
            )
        silicon_id = str(silicon.get("silicon_id") or "").strip()
        silicon_name = str(silicon.get("name") or "").strip()
        if not silicon_id:
            raise RuntimeError("Glass did not return a silicon_id for this token.")
        silicon["api_key"] = api_key
        if journal.state == "INIT":
            requested_name = (
                opts.name if opts and opts.name else silicon_name
            )
            silicon["_instance_name"] = requested_name
            provider_key_env = _choose_glass_provider_keys(
                server, api_key, silicon
            )
            setup_config = (
                stemcell.choose_setup_config(
                    "", **opts.setup_config_kwargs()
                )
                if opts and opts.brain
                else {}
            )
            journal.initialize(
                team_name="",
                runtime="docker" if use_docker else "local",
                runtime_image=runtime_image,
                release_tree_sha256=(
                    prepared.release.manifest.identity.tree_sha256
                ),
                environment_path=str(prepared.environment or ""),
                backups=_want_backups(opts),
                provider_key_env=provider_key_env,
                items=_plan_pull_items(
                    journal,
                    [silicon],
                    {silicon_id: setup_config},
                    parent,
                ),
            )
        elif (
            len(journal.items) != 1
            or journal.items[0]["silicon_id"] != silicon_id
            or journal.value["runtime"] != (
                "docker" if use_docker else "local"
            )
            or journal.value["runtime_image"] != runtime_image
            or journal.value["release_tree_sha256"]
            != prepared.release.manifest.identity.tree_sha256
            or journal.value["environment_path"]
            != str(prepared.environment or "")
        ):
            raise RuntimeError(
                "single-Silicon pull recovery identity changed"
            )
        _execute_planned_pull(
            journal,
            prepared=prepared,
            silicons=[silicon],
            team_key=None,
        )
    item = journal.items[0]
    ui.success(
        f"Pulled Glass Silicon '{item['silicon_name'] or item['silicon_id']}' "
        f"into {item['final_path']}"
    )
    ui.info(f"Registered as '{item['name']}'.")


def pull(api_token: str | None, opts: PullOpts | None = None) -> None:
    api_key = (api_token or "").strip()
    if not api_key:
        ui.info("Paste the team setup token generated from Glass.")
        api_key = ui.read_secret("Glass team setup token").strip()
    if not api_key:
        ui.error("Usage: silicon pull <api_token>")
        sys.exit(1)
    try:
        server = validate_glass_server(GLASS_SERVER_URL)
        kind = "team" if api_key.startswith("sct_live_") else "single"
        with HostFileLock(registry.REGISTRY_DIR / "pull.lock", timeout=1.0):
            journal = pull_transaction.PullJournal.open_or_create(
                registry.REGISTRY_DIR,
                kind=kind,
                server=server,
                credential=api_key,
            )
            if journal.state == "COMPLETE":
                raise RuntimeError("this pull transaction is already complete")
            if kind == "team":
                _pull_team(api_key, server, opts, journal)
            else:
                _pull_single(api_key, server, opts, journal)
    except (UnsafeGlassURL, pull_transaction.PullJournalError, RuntimeError) as exc:
        ui.error(str(exc))
        sys.exit(1)


def _start_backup_loop(
    path: str,
    name: str,
    *,
    persist_intent: bool = True,
    quiet: bool = False,
) -> bool:
    root = Path(path).resolve()
    token = uuid_hex()
    try:
        ensure_real_directory(root / ".silicon", root=root)
        if persist_intent:
            _set_backup_schedule(root, name, enabled=True)
        with InstanceLock(root, f"backup-supervisor-start-{token}"):
            current = _backup_supervisor_info(root)
            if current.get("running"):
                responsiveness = (
                    "healthy"
                    if current.get("responsive")
                    else "alive but its heartbeat is stale"
                )
                if not quiet:
                    ui.warn(
                        f"Backup loop already running for '{name}' "
                        f"(PID {current.get('pid')}, {responsiveness})."
                    )
                return False
            _cleanup_stale_backup_lease(root)
            if not quiet:
                ui.info("Starting daily 23:59 GMT backup loop in background...")
            with _open_backup_log(root) as log:
                cmd = [
                    sys.executable,
                    "-m",
                    "silicon_cli.cli",
                    "_backup_loop",
                    str(root),
                    name,
                    token,
                ]
                proc = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            _install_backup_lease(
                root,
                name=name,
                pid=proc.pid,
                token=token,
            )
    except (OSError, RuntimeError, UpdateLocked) as exc:
        ui.error(f"Could not start the backup supervisor for '{name}': {exc}")
        return False
    if not quiet:
        ui.success(
            f"Daily backups running (PID {proc.pid}). "
            f"Logs: {root}/.glass-push.log"
        )
        ui.info(f"Use 'silicon push {name} now' for a manual backup anytime.")
    return True


def _stop_backup_loop(path: str, name: str) -> bool:
    root = Path(path).resolve()
    try:
        _set_backup_schedule(root, name, enabled=False)
    except (OSError, RuntimeError) as exc:
        ui.error(
            f"Could not durably disable scheduled backups for '{name}': {exc}"
        )
        return False
    current = _backup_supervisor_info(root)
    if not current.get("running"):
        _cleanup_stale_backup_lease(root)
        ui.warn(f"No verified backup loop is running for '{name}'.")
        return False
    if not current.get("verified_identity"):
        ui.warn(
            f"A probable backup loop for '{name}' is alive at PID "
            f"{current.get('pid')}, but its private lease is damaged. Refusing "
            "to signal an unverified process."
        )
        return False
    pid = int(current["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        _cleanup_stale_backup_lease(root)
        ui.warn(f"Backup loop for '{name}' had already stopped.")
        return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _backup_process_matches(
        pid,
        str(current["token"]),
    ):
        time.sleep(0.1)
    if _backup_process_matches(pid, str(current["token"])):
        ui.warn(
            f"Backup loop for '{name}' is still finishing an in-flight backup "
            f"(PID {pid})."
        )
        return False
    _cleanup_stale_backup_lease(root)
    ui.success(f"Stopped backup loop for '{name}'.")
    return True


def _show_backup_status(path: str, name: str) -> None:
    try:
        schedule = _backup_schedule(path)
    except RuntimeError as exc:
        ui.warn(f"Backup schedule intent for '{name}' is corrupt: {exc}")
        schedule = {"enabled": False}
    current = _backup_supervisor_info(path)
    if current.get("running"):
        state = str(current.get("state") or "unknown")
        heartbeat = float(current.get("heartbeat_at") or 0)
        age = max(0, int(time.time() - heartbeat)) if heartbeat else -1
        health = "healthy" if current.get("responsive") else "heartbeat stale"
        ui.info(
            f"Backup supervisor for '{name}': {state}, {health}, "
            f"PID {current.get('pid')}, heartbeat {age}s ago."
        )
        if current.get("next_run_at"):
            ui.info(
                "Next attempt: "
                + datetime.fromtimestamp(
                    float(current["next_run_at"]),
                    tz=timezone.utc,
                ).isoformat()
            )
        ui.info(
            "Automatic restart after host/CLI startup: "
            + ("enabled" if schedule.get("enabled") else "disabled")
        )
        return
    state = str(current.get("state") or "stopped")
    ui.info(f"Backup supervisor for '{name}' is not running (last state: {state}).")
    ui.info(
        "Persistent schedule intent: "
        + ("enabled" if schedule.get("enabled") else "disabled")
    )
    if current.get("last_success_at"):
        ui.info(
            "Last successful backup: "
            + datetime.fromtimestamp(
                float(current["last_success_at"]),
                tz=timezone.utc,
            ).isoformat()
        )


def reconcile_backup_supervisor(
    inst: registry.Install,
    *,
    quiet: bool = False,
) -> bool:
    """Restart one enabled host-owned schedule after a host/CLI restart."""

    try:
        schedule = _backup_schedule(inst.path)
    except (OSError, RuntimeError) as exc:
        ui.warn(
            f"Could not reconcile backup schedule for '{inst.name}': {exc}"
        )
        return False
    if not schedule.get("enabled"):
        return False
    current = _backup_supervisor_info(inst.path)
    if current.get("running"):
        return True
    name = str(schedule.get("name") or inst.name)
    if not quiet:
        runtime = "Docker" if inst.is_docker else "local"
        ui.info(
            f"Restarting persisted {runtime} backup supervisor for '{name}'..."
        )
    return _start_backup_loop(
        inst.path,
        name,
        persist_intent=False,
        quiet=quiet,
    )


def reconcile_backup_schedules(*, quiet: bool = True) -> None:
    """Best-effort boot reconciliation for every registered installation."""

    for inst in registry.installs():
        try:
            reconcile_backup_supervisor(inst, quiet=quiet)
        except Exception as exc:
            ui.warn(
                f"Could not reconcile backup schedule for '{inst.name}': {exc}"
            )


def push(target: str | None, subcmd: str | None) -> None:
    inst = registry.resolve_one(target)
    if not (Path(inst.path) / ".glass.json").exists():
        ui.error(f"'{inst.name}' is not connected to Glass. No .glass.json found.")
        sys.exit(1)
    if subcmd == "now":
        ui.info(f"Pushing '{inst.name}' to Glass...")
        ok = _manifest_backup_now(
            inst.path,
            note="manual",
            instance_name=inst.name,
            installation=inst,
        )
        ui.success("Backup complete.") if ok else ui.error("Push failed.")
    elif subcmd == "stop":
        _stop_backup_loop(inst.path, inst.name)
    elif subcmd == "status":
        _show_backup_status(inst.path, inst.name)
    else:
        current = _backup_supervisor_info(inst.path)
        if current.get("running"):
            health = (
                "healthy"
                if current.get("responsive")
                else "alive with a stale or unavailable heartbeat"
            )
            ui.warn(
                f"Backup loop already running for '{inst.name}' "
                f"(PID {current.get('pid')}, {health})."
            )
            return
        ui.info(f"Starting daily 23:59 GMT backup loop for '{inst.name}'...")
        ok = _manifest_backup_now(
            inst.path,
            note="manual",
            instance_name=inst.name,
            installation=inst,
        )
        if ok:
            _start_backup_loop(inst.path, inst.name)
        else:
            ui.error("Initial backup failed; the daily loop was not started.")
