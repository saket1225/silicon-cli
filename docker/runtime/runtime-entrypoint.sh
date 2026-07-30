#!/usr/bin/env bash
set -euo pipefail

SILICON_ROOT="${SILICON_ROOT:-/silicon}"
INSTANCE_NAME="${SILICON_INSTANCE_NAME:-silicon}"
SILICON_SHARED_HOME="${SILICON_SHARED_HOME:-/silicon-shared-home}"

export HOME="${SILICON_HOME_DIR:-$SILICON_ROOT/.home}"
export SILICON_HOME="${SILICON_CLI_HOME:-$HOME/.silicon}"
export SILICON_BROWSER_HOME="${SILICON_BROWSER_HOME:-$SILICON_ROOT/.silicon-browser}"
export PATH="/opt/silicon-runtime/bin:$PATH"

log() {
  printf '[silicon-runtime] %s\n' "$*" >&2
}

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

link_shared_dir() {
  local name="$1"
  local target="$HOME/$name"
  local shared="$SILICON_SHARED_HOME/$name"
  mkdir -p "$shared"
  if [ -L "$target" ]; then
    return
  fi
  if [ -e "$target" ]; then
    mv "$target" "$target.local.$(date +%s)"
  fi
  ln -s "$shared" "$target"
}

link_shared_file() {
  local name="$1"
  local target="$HOME/$name"
  local shared="$SILICON_SHARED_HOME/$name"
  mkdir -p "$(dirname "$shared")"
  if [ -L "$target" ]; then
    return
  fi
  if [ -e "$target" ]; then
    if [ ! -e "$shared" ]; then
      mv "$target" "$shared"
    else
      mv "$target" "$target.local.$(date +%s)"
    fi
  fi
  ln -s "$shared" "$target"
}

prepare_shared_auth() {
  mkdir -p "$HOME" "$SILICON_SHARED_HOME"
  if [ "$HOME" = "$SILICON_SHARED_HOME" ]; then
    mkdir -p "$HOME/.claude" "$HOME/.codex" "$HOME/.config"
    return
  fi
  link_shared_dir ".claude"
  link_shared_dir ".codex"
  link_shared_dir ".config"
  link_shared_file ".claude.json"
}

prepare_runtime() {
  if [ ! -d "$SILICON_ROOT" ]; then
    log "missing mount: $SILICON_ROOT"
    exit 1
  fi

  mkdir -p "$HOME" "$SILICON_HOME" "$SILICON_BROWSER_HOME"
  prepare_shared_auth
  cd "$SILICON_ROOT"
  local runtime_python
  runtime_python="$(command -v python3)"

  # Resolve the same atomically selected generation as silicon-cli.  Generation
  # pointers are instance-relative so the host path and /silicon mount can
  # differ without making the active code or requirements disappear.
  local release_root
  local environment_python
  local -a runtime_paths
  mapfile -d '' -t runtime_paths < <(python3 - "$SILICON_ROOT" <<'PY'
import hashlib
import json
import platform
import re
import sys
import sysconfig
from pathlib import Path

root = Path(sys.argv[1]).resolve()
pointer = root / ".silicon" / "current.json"
if not pointer.exists():
    sys.stdout.write(str(root) + "\0\0")
    raise SystemExit(0)
value = json.loads(pointer.read_text(encoding="utf-8"))
candidate = Path(str(value.get("release_path") or ""))
if not candidate.is_absolute():
    candidate = root / candidate
candidate = candidate.resolve()
releases = (root / ".silicon" / "releases").resolve()
if (
    value.get("kind") != "immutable-release"
    or releases not in candidate.parents
    or not (candidate / "main.py").is_file()
):
    raise SystemExit("invalid active Silicon generation pointer")
environment_python = ""
environment_value = str(value.get("environment_path") or "")
if environment_value:
    environment = Path(environment_value)
    if not environment.is_absolute():
        environment = root / environment
    if environment.is_symlink():
        raise SystemExit("active Silicon environment must not be a symlink")
    environment = environment.resolve()
    environments = (root / ".silicon" / "environments").resolve()
    python = environment / "bin" / "python"
    if environments not in environment.parents or not python.is_file():
        raise SystemExit("invalid active Silicon environment pointer")
    lockfile = candidate / "requirements.lock"
    marker = environment / ".silicon-environment.json"
    try:
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("active Silicon environment has no valid ready marker")
    lock_digest = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    implementation = str(getattr(sys.implementation, "name", "") or "python")
    cache_tag = str(getattr(sys.implementation, "cache_tag", "") or "")
    soabi = str(sysconfig.get_config_var("SOABI") or "")
    abi_flags = str(getattr(sys, "abiflags", "") or "")
    machine = str(platform.machine() or "unknown")
    platform_tag = str(sysconfig.get_platform() or sys.platform)
    descriptor = "|".join(
        (implementation, cache_tag, soabi, abi_flags, machine, platform_tag)
    )
    readable = "-".join(
        part
        for part in (
            implementation,
            cache_tag,
            soabi,
            machine,
            platform_tag,
        )
        if part
    ).lower()
    readable = re.sub(r"[^a-z0-9._-]+", "-", readable).strip(".-_")
    runtime_key = (
        f"{(readable or 'python-runtime')[:120]}-"
        f"{hashlib.sha256(descriptor.encode()).hexdigest()[:16]}"
    )
    runtime_identity = {
        "implementation": implementation,
        "cache_tag": cache_tag,
        "soabi": soabi,
        "abi_flags": abi_flags,
        "machine": machine,
        "platform": platform_tag,
        "key": runtime_key,
    }
    if (
        marker.is_symlink()
        or not isinstance(marker_value, dict)
        or set(marker_value)
        != {
            "requirements_sha256",
            "requirements_file",
            "require_hashes",
            "runtime",
        }
        or marker_value.get("requirements_sha256") != lock_digest
        or marker_value.get("requirements_file") != "requirements.lock"
        or marker_value.get("require_hashes") is not True
        or marker_value.get("runtime") != runtime_identity
    ):
        raise SystemExit("active Silicon environment does not match requirements.lock")
    environment_python = str(python)
sys.stdout.write(str(candidate) + "\0" + environment_python + "\0")
PY
)
  release_root="${runtime_paths[0]}"
  environment_python="${runtime_paths[1]:-}"
  if [ ! -f "$release_root/main.py" ]; then
    log "$release_root does not look like a Silicon instance; expected main.py"
    exit 1
  fi

  local dependency_file=""
  local -a pip_integrity_args=()
  if [ -f "$release_root/requirements.lock" ]; then
    dependency_file="$release_root/requirements.lock"
    pip_integrity_args=(--require-hashes)
  elif [ -f "$release_root/requirements.txt" ]; then
    if [ -f "$SILICON_ROOT/.silicon/current.json" ]; then
      log "active immutable generation has no hash-pinned requirements.lock"
      exit 1
    fi
    # Bootstrap-only compatibility for a legacy flat installation.
    dependency_file="$release_root/requirements.txt"
  fi

  if [ -n "$environment_python" ]; then
    log "using pre-staged active-generation dependency environment"
  elif [ -n "$dependency_file" ]; then
    local venv_python="$SILICON_ROOT/.venv/bin/python"
    local req_hash
    local marker="$SILICON_ROOT/.venv/.silicon_requirements.sha256"
    req_hash="$(hash_file "$dependency_file")"
    if [ ! -x "$venv_python" ]; then
      log "creating instance venv"
      python3 -m venv "$SILICON_ROOT/.venv"
    fi
    if [ ! -f "$marker" ] || [ "$(cat "$marker" 2>/dev/null || true)" != "$req_hash" ]; then
      log "installing active-generation Python dependencies"
      "$venv_python" -m pip install --upgrade pip >/dev/null
      "$venv_python" -m pip install "${pip_integrity_args[@]}" -r "$dependency_file"
      printf '%s\n' "$req_hash" > "$marker"
    fi
  fi

  if [ -n "$environment_python" ]; then
    export PATH="$(dirname "$environment_python"):$PATH"
  elif [ -x "$SILICON_ROOT/.venv/bin/python" ]; then
    export PATH="$SILICON_ROOT/.venv/bin:$PATH"
  fi

  python - "${SILICON_EXTEND_REQUIRED_VERSION:-0.1.3}" <<'PY'
from importlib import metadata
import sys

expected = sys.argv[1]
try:
    installed = metadata.version("silicon-extend")
    package = __import__("silicon_extend")
    entries = metadata.entry_points()
    if hasattr(entries, "select"):
        commands = entries.select(group="console_scripts", name="silicon-extend")
    else:
        commands = [
            entry
            for entry in entries.get("console_scripts", ())
            if entry.name == "silicon-extend"
        ]
except Exception:
    raise SystemExit("Silicon Extend is not installed in the active runtime")
if (
    installed != expected
    or getattr(package, "__version__", "") != expected
    or not tuple(commands)
):
    raise SystemExit(
        f"active Silicon Extend does not match required version {expected}"
    )
PY

  global_interface="/usr/local/bin/silicon-interface"
  local_interface="$SILICON_ROOT/.silicon-interface/bin/si"
  [ -x "$global_interface" ] \
    || die "Silicon Interface CLI is missing from the runtime image"
  global_interface_version="$("$global_interface" --version 2>/dev/null || true)"
  [ -n "$global_interface_version" ] \
    || die "Silicon Interface CLI in the runtime image is not executable"
  log "atomically activating Silicon Interface CLI from the runtime image"
  "$runtime_python" \
    /usr/local/libexec/silicon-activate-interface-cli.py \
    --root "$SILICON_ROOT" \
    --executable "$global_interface" \
    >/dev/null \
    || die "Silicon Interface CLI activation failed"
  local_interface_version="$("$local_interface" --version 2>/dev/null || true)"
  [ "$local_interface_version" = "$global_interface_version" ] \
    || die "Silicon Interface shim does not match the runtime image"

  "$runtime_python" - "$INSTANCE_NAME" "$SILICON_ROOT" <<'PY'
import sys
from pathlib import Path

from silicon_cli import registry

name = sys.argv[1] or "silicon"
root = Path(sys.argv[2]).resolve()
try:
    registry.register(name, str(root), str(root / ".silicon.pid"), update_existing=True)
except TypeError:
    registry.register(name, str(root), str(root / ".silicon.pid"))
PY
}

stop_runtime() {
  log "stopping Silicon"
  silicon stop --full "$INSTANCE_NAME" || true
}

terminate_runtime() {
  trap - TERM INT
  stop_runtime
  exit 0
}

if [ "${1:-}" = "auth" ]; then
  provider="${2:-all}"
  export HOME="$SILICON_SHARED_HOME"
  export SILICON_HOME="$HOME/.silicon"
  export SILICON_BROWSER_HOME="$HOME/.silicon-browser"
  mkdir -p "$HOME" "$SILICON_HOME" "$SILICON_BROWSER_HOME" "$HOME/.claude" "$HOME/.codex" "$HOME/.config"
  cd "$HOME"
  if [ "$provider" = "codex" ]; then
    log "Codex login uses the shared VM auth home: $HOME"
    printf '\nUsing Codex device auth for remote/headless server compatibility.\n'
    printf 'Follow the device-code instructions below. When sign-in is done, type `exit` to return to silicon.\n\n'
    codex login --device-auth || true
  elif [ "$provider" = "claude" ]; then
    log "Claude Code uses the shared VM auth home: $HOME"
    printf '\nRun `claude` in this shell and complete the sign-in flow.\n'
    printf 'When sign-in is done, type `exit` to return to silicon.\n\n'
  else
    log "Shared auth shell: $HOME"
    printf '\nRun `claude` and/or `codex login --device-auth` here.\n'
    printf 'When sign-in is done, type `exit` to return to silicon.\n\n'
  fi
  exec "${SHELL:-/bin/bash}"
fi

if [ "${1:-}" = "shared" ]; then
  shift
  export HOME="$SILICON_SHARED_HOME"
  export SILICON_HOME="$HOME/.silicon"
  export SILICON_BROWSER_HOME="$HOME/.silicon-browser"
  mkdir -p "$HOME" "$SILICON_HOME" "$SILICON_BROWSER_HOME" "$HOME/.claude" "$HOME/.codex" "$HOME/.config"
  cd "$HOME"
  if [ "$#" -eq 0 ]; then
    exec "${SHELL:-/bin/bash}"
  fi
  exec "$@"
fi

if [ "${1:-}" = "run" ]; then
  shift
  prepare_runtime
  exec "$@"
fi

if [ "${1:-}" = "shell" ]; then
  shift
  prepare_runtime
  exec "${SHELL:-/bin/bash}" "$@"
fi

# A legacy Silicon without the task-safe coordinator is updated only while
# fully offline. Refuse direct Compose/Docker restarts during that transaction.
# The host updater can recreate the service with the exact fence owner for its
# controlled health check; ordinary starts never receive that value.
python3 - "$SILICON_ROOT" <<'PY'
import json
import math
import os
import re
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
marker = root / ".silicon" / "maintenance" / "legacy-offline.json"
try:
    metadata = marker.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("legacy offline update fence is unsafe; run silicon update resume")
try:
    value = json.loads(marker.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit("legacy offline update fence is corrupt; run silicon update resume")
owner = value.get("update_id") if isinstance(value, dict) else None
if (
    not isinstance(value, dict)
    or set(value) != {"schema", "update_id", "created_at"}
    or value.get("schema") != 1
    or not isinstance(owner, str)
    or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", owner) is None
    or not isinstance(value.get("created_at"), (int, float))
    or isinstance(value.get("created_at"), bool)
    or not math.isfinite(value["created_at"])
):
    raise SystemExit("legacy offline update fence is invalid; run silicon update resume")
if os.environ.get("SILICON_LEGACY_UPDATE_FENCE_OWNER", "") != owner:
    raise SystemExit(
        "legacy offline update is in progress; run silicon update resume before starting"
    )
PY

prepare_runtime

# The Interface daemon PID belongs to the prior container namespace. Keep this
# one-shot marker outside the protected Interface snapshot so an updater-owned
# suspended boot can restore state first; process.start consumes it immediately
# before starting Interface.
python3 - "$SILICON_ROOT" <<'PY' \
  || die "could not publish the Interface daemon fresh-boot marker"
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
state = root / ".silicon"
state.mkdir(mode=0o700, exist_ok=True)
if state.is_symlink() or not state.is_dir():
    raise SystemExit("Silicon runtime state directory is unsafe")
marker = state / "interface-daemon-reset-required"
flags = os.O_CREAT | os.O_WRONLY
flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(marker, flags, 0o600)
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SystemExit("Interface daemon fresh-boot marker is unsafe")
    os.ftruncate(descriptor, 0)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(
    state,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY

# PIDs are namespaced to each container boot. Stale files from a previous
# container can point at an unrelated new process, so remove them before start.
rm -f \
  "$SILICON_ROOT/.silicon.pid" \
  "$SILICON_ROOT/.silicon.pid.meta.json" \
  "$SILICON_ROOT/.glass_agent.pid" \
  "$SILICON_ROOT/.silicon/runtime-health.json"

trap terminate_runtime TERM INT

log "starting $INSTANCE_NAME"
if [ -f "$SILICON_ROOT/.silicon/docker-start-suspended" ]; then
  rm -f "$SILICON_ROOT/.silicon/docker-start-suspended"
  log "service start suspended for transactional state restoration"
else
  SILICON_INTERFACE_RESET_DAEMON_PID=1 silicon start "$INSTANCE_NAME" \
    || log "initial start returned non-zero; container stays alive for inspection"
fi

while true; do
  sleep 3600 &
  wait $! || true
done
