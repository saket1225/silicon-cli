"""Docker runtime backend for one-container-per-Silicon installs."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

from . import interface_cli, registry, runtime_contract, ui
from .config import REGISTRY_DIR
from .host_lock import HostFileLock, ensure_private_directory
from .updater.io import atomic_write_bytes, atomic_write_json
from .updater.release import runtime_image_is_pinned

CONFIG_FILE = REGISTRY_DIR / "docker.json"
DEFAULT_ROOT = Path.home() / "silicons"
DEFAULT_IMAGE = ""
DEFAULT_IMAGE_REPOSITORY = "ghcr.io/teamofsilicons/silicon-runtime"
CONTAINER_PATH = "/silicon"
CONTAINER_HOME = f"{CONTAINER_PATH}/.home"
CONTAINER_SHARED_HOME = "/silicon-shared-home"
CONTAINER_INTERFACE_ACTIVATOR = (
    "/usr/local/libexec/silicon-activate-interface-cli.py"
)
CONTAINER_INTERFACE_EXECUTABLE = "/usr/local/bin/silicon-interface"
AUTH_FILE = ".silicon-auth.json"
AUTH_PROVIDERS = {"claude", "codex"}
UNPINNED_IMAGE_OPT_IN = "SILICON_DOCKER_ALLOW_UNPINNED_IMAGE"
SILICON_EXTEND_VERSION = "0.1.4"
FULL_STOP_EXEC_TIMEOUT_SECONDS = 30.0

_CONTAINER_PROCESS_IDENTITY_HELPER = r"""
def _process_birth_identity(process_id):
    if process_id <= 0:
        return ""
    try:
        os.kill(process_id, 0)
    except OSError:
        return ""
    proc_stat = Path(f"/proc/{process_id}/stat")
    if proc_stat.is_file():
        try:
            raw = proc_stat.read_text(encoding="utf-8")
            closing = raw.rfind(")")
            if closing < 0:
                return ""
            fields = raw[closing + 2 :].split()
            start_ticks = fields[19]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()
            if boot_id and start_ticks.isdigit():
                return f"linux:{boot_id}:{start_ticks}"
        except (OSError, IndexError, ValueError):
            return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(process_id), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    started = " ".join(result.stdout.split())
    return f"ps:{started}" if result.returncode == 0 and started else ""
"""


def _truthy(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def _falsey(value: str | None) -> bool:
    return str(value or "").lower() in {"0", "false", "no", "off"}


def _safe_name(value: str) -> str:
    raw = (value or "silicon").strip().lower()
    safe = re.sub(r"[^a-z0-9_.-]+", "-", raw)
    safe = re.sub(r"-+", "-", safe).strip("._-")
    return safe or "silicon"


def service_name(name: str) -> str:
    return f"silicon-{_safe_name(name)}"


def container_name(name: str) -> str:
    return f"silicon-{_safe_name(name)}"


def _json(value: str) -> str:
    return json.dumps(str(value))


def host_user() -> str:
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return f"{os.getuid()}:{os.getgid()}"
    return ""


def runtime_opted_out() -> bool:
    runtime = os.environ.get("SILICON_RUNTIME", "").strip().lower()
    return runtime in {"local", "host", "native", "none", "off"} or _falsey(os.environ.get("SILICON_RUNTIME_DOCKER"))


def _save_config(config: dict) -> None:
    ensure_private_directory(CONFIG_FILE.parent)
    atomic_write_json(CONFIG_FILE, config, mode=0o600)


def _allow_unpinned_image() -> bool:
    """Explicit local-development escape hatch; never enabled by default."""

    return _truthy(os.environ.get(UNPINNED_IMAGE_OPT_IN))


def _require_runtime_image(image: object, *, context: str) -> str:
    value = str(image or "").strip()
    if runtime_image_is_pinned(value):
        return value
    if value and _allow_unpinned_image():
        ui.warn(
            f"{context} is using an unpinned Docker image because "
            f"{UNPINNED_IMAGE_OPT_IN}=1. This is for local development only."
        )
        return value
    raise RuntimeError(
        f"{context} has no published, immutable runtime image. Fetch a "
        "published Silicon release first; the image must be "
        "registry/repository@sha256:<digest>. Mutable tags such as :latest "
        f"are refused. Set {UNPINNED_IMAGE_OPT_IN}=1 only for isolated local "
        "development."
    )


def load_config(required: bool = False) -> dict:
    env_enabled = _truthy(os.environ.get("SILICON_RUNTIME_DOCKER")) or os.environ.get("SILICON_RUNTIME") == "docker"
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            data = {}
    elif required and not env_enabled:
        ui.error("Docker runtime is not initialized. Run: silicon docker init")
        sys.exit(1)

    root = Path(os.environ.get("SILICON_DOCKER_ROOT") or data.get("root") or DEFAULT_ROOT).expanduser()
    compose_file = Path(
        os.environ.get("SILICON_DOCKER_COMPOSE")
        or data.get("compose_file")
        or root / "compose.yml"
    ).expanduser()
    shared_home = Path(
        os.environ.get("SILICON_DOCKER_SHARED_HOME")
        or data.get("shared_home")
        or root / ".shared-home"
    ).expanduser()
    # Once a digest has been persisted, it is part of the selected release
    # state and must not be retargeted by a later process environment. The
    # environment value is bootstrap-only for an as-yet unbound runtime.
    image = (
        data.get("image")
        or os.environ.get("SILICON_RUNTIME_IMAGE")
        or DEFAULT_IMAGE
    )
    env_sudo = os.environ.get("SILICON_DOCKER_SUDO")
    docker_sudo = _truthy(env_sudo) if env_sudo is not None else bool(data.get("docker_sudo", False))
    return {
        "enabled": bool(data.get("enabled", False) or env_enabled),
        "root": str(root),
        "compose_file": str(compose_file),
        "shared_home": str(shared_home),
        "image": image,
        "docker_sudo": docker_sudo,
    }


def config_for_install(inst: registry.Install) -> dict:
    cfg = load_config()
    if inst.path:
        cfg["root"] = str(Path(inst.path).expanduser().resolve().parent)
    if inst.compose_file:
        cfg["compose_file"] = inst.compose_file
    if inst.image:
        cfg["image"] = inst.image
    return cfg


def enabled() -> bool:
    if _truthy(os.environ.get("SILICON_CONTAINER_MODE")):
        return False
    return bool(load_config().get("enabled"))


def init(
    root: str | None = None,
    image: str | None = None,
    *,
    shared_home: str | None = None,
    docker_sudo: bool | None = None,
    quiet: bool = False,
    write_compose: bool = True,
) -> None:
    chosen_root = Path(root).expanduser() if root else DEFAULT_ROOT
    chosen_root = chosen_root.resolve()
    current = load_config()
    chosen_image = image or str(current.get("image") or DEFAULT_IMAGE)
    if chosen_image and (image is not None or write_compose):
        _require_runtime_image(chosen_image, context="Docker runtime setup")
    chosen_shared_home = (
        Path(shared_home).expanduser().resolve()
        if shared_home
        else Path(current.get("shared_home") if not root else chosen_root / ".shared-home").expanduser().resolve()
    )
    if docker_sudo is None:
        docker_sudo = bool(current.get("docker_sudo", False))
    chosen_root.mkdir(parents=True, exist_ok=True)
    chosen_shared_home.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "enabled": True,
        "root": str(chosen_root),
        "compose_file": str(chosen_root / "compose.yml"),
        "shared_home": str(chosen_shared_home),
        "image": chosen_image,
        "docker_sudo": docker_sudo,
    }
    _save_config(config)
    if write_compose:
        render_compose(config)
    if not quiet:
        ui.success(f"Docker runtime enabled. Instances root: {chosen_root}")
        ui.info(f"Compose file: {config['compose_file']}")
        ui.info(f"Shared auth home: {chosen_shared_home}")
        if chosen_image:
            ui.info(f"Runtime image: {chosen_image}")
        else:
            ui.info(
                "Runtime image: awaiting the digest committed to the "
                "published Stemcell tag"
            )
        if docker_sudo:
            ui.info("Docker commands will run through sudo for this runtime.")


def target_path(name_or_path: str | None) -> Path:
    cfg = load_config(required=True)
    root = Path(cfg["root"]).expanduser()
    if name_or_path:
        p = Path(name_or_path).expanduser()
        return p.resolve() if p.is_absolute() or len(p.parts) > 1 or name_or_path in {".", ".."} else (root / _safe_name(name_or_path)).resolve()
    name = ui.ask("New silicon folder name", "silicon")
    if not name:
        ui.error("A folder name is required.")
        sys.exit(1)
    return (root / _safe_name(name)).resolve()


def register_instance(name: str, path: str | Path, *, image: str | None = None) -> registry.Install:
    cfg = load_config(required=True)
    root = Path(cfg["root"])
    root.mkdir(parents=True, exist_ok=True)
    abs_path = Path(path).expanduser().resolve()
    svc = service_name(name)
    cname = container_name(name)
    img = _require_runtime_image(
        image or cfg["image"],
        context=f"Docker Silicon '{name}'",
    )
    registry.register(
        name,
        str(abs_path),
        str(abs_path / ".silicon.pid"),
        runtime="docker",
        service=svc,
        compose_file=cfg["compose_file"],
        image=img,
        container_name=cname,
        update_existing=True,
    )
    render_compose(cfg)
    inst = registry.find(name)
    if inst is None:
        ui.error(f"Could not register Docker silicon '{name}'.")
        sys.exit(1)
    return inst


def _docker_installs(compose_file: str | None = None) -> list[registry.Install]:
    rows = [i for i in registry.installs() if i.is_docker]
    if compose_file:
        rows = [i for i in rows if not i.compose_file or Path(i.compose_file) == Path(compose_file)]
    return rows


def render_compose(
    config: dict | None = None,
    *,
    update_fence_owners: dict[str, str] | None = None,
    pinned_targets: Iterable[str] = (),
) -> Path:
    cfg = config or load_config(required=True)
    compose = Path(cfg["compose_file"]).expanduser()
    compose.parent.mkdir(parents=True, exist_ok=True)
    selected = set(pinned_targets)
    with HostFileLock(compose.with_name(f".{compose.name}.lock")):
        # Read the registry only after taking the render lock. Concurrent
        # registrations therefore cannot leave an older compose snapshot last.
        rows = _docker_installs(str(compose))
        lines = ["name: silicon-runtime", "", "services:"]
        shared_home = str(Path(cfg["shared_home"]).expanduser().resolve())
        Path(shared_home).mkdir(parents=True, exist_ok=True)
        if not rows:
            lines.append("  # Services are added by `silicon new` or `silicon pull`.")
        for inst in rows:
            svc = inst.service or service_name(inst.name)
            cname = inst.container_name or container_name(inst.name)
            configured_image = str(inst.image or cfg["image"] or "").strip()
            if not selected or inst.name in selected:
                image = _require_runtime_image(
                    configured_image,
                    context=f"Docker Silicon '{inst.name}'",
                )
            else:
                # A targeted migration must not retarget or block unrelated
                # legacy fleet members. Preserve their existing registry
                # image verbatim while requiring the selected target to use
                # a published immutable digest.
                if (
                    not configured_image
                    or len(configured_image) > 512
                    or any(
                        character in configured_image
                        for character in "\x00\r\n"
                    )
                ):
                    raise RuntimeError(
                        f"Docker Silicon '{inst.name}' has an invalid "
                        "legacy runtime image"
                    )
                image = configured_image
            user = host_user()
            path = str(Path(inst.path).expanduser().resolve())
            fence_owner = str(
                (update_fence_owners or {}).get(inst.name) or ""
            )
            lines.extend([
                f"  {svc}:",
                f"    image: {_json(image)}",
                f"    container_name: {_json(cname)}",
                "    restart: unless-stopped",
                "    healthcheck:",
                "      test: [\"CMD\", \"python3\", \"/usr/local/libexec/silicon-runtime-healthcheck.py\"]",
                "      interval: 15s",
                "      timeout: 5s",
                "      retries: 4",
                "      start_period: 120s",
                *([f"    user: {_json(user)}"] if user else []),
                "    environment:",
                f"      SILICON_INSTANCE_NAME: {_json(inst.name)}",
                f"      SILICON_SHARED_HOME: {_json(CONTAINER_SHARED_HOME)}",
                '      SILICON_CONTAINER_MODE: "1"',
                *(
                    [
                        "      SILICON_LEGACY_UPDATE_FENCE_OWNER: "
                        + _json(fence_owner)
                    ]
                    if fence_owner
                    else []
                ),
                "    volumes:",
                f"      - {_json(path + ':' + CONTAINER_PATH)}",
                f"      - {_json(shared_home + ':' + CONTAINER_SHARED_HOME)}",
                "",
            ])
        atomic_write_bytes(
            compose,
            ("\n".join(lines).rstrip() + "\n").encode("utf-8"),
            mode=0o600,
        )
    return compose


def _cmd(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, text=True, capture_output=capture)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _sudo_prefix() -> list[str] | None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if not shutil.which("sudo"):
        return None
    return ["sudo"]


def _docker_cmd(config: dict | None = None) -> list[str]:
    cfg = config if config is not None else load_config()
    return [*(["sudo"] if cfg.get("docker_sudo") else []), "docker"]


def _manual_docker_steps() -> None:
    ui.info(
        "Install Docker Engine and the Compose v2 plugin from your operating "
        "system's trusted package repository, then rerun the same command."
    )
    ui.info("Vendor instructions: https://docs.docker.com/engine/install/")
    ui.info(
        "Verify the repository signing key/fingerprint using Docker's "
        "published instructions before installing packages."
    )
    ui.info("After installation, enable the daemon with your service manager.")


def _install_docker_engine() -> bool:
    # Never download and execute a mutable network script as root. Even an
    # explicit legacy auto-install environment variable cannot weaken this
    # trust boundary.
    ui.error(
        "Silicon does not automatically install Docker or execute "
        "get.docker.com as root."
    )
    _manual_docker_steps()
    return False


def _ensure_docker_binary(install: bool) -> None:
    if shutil.which("docker"):
        return
    if install:
        _install_docker_engine()
    else:
        _manual_docker_steps()
    ui.error("Docker was not found on PATH.")
    sys.exit(127)


def _ensure_compose(config: dict) -> None:
    result = _cmd([*_docker_cmd(config), "compose", "version"])
    if result.returncode == 0:
        return
    ui.error("Docker Compose v2 plugin is not available.")
    ui.info("Install the Docker Compose plugin, then rerun the same silicon command.")
    ui.info("Ubuntu/Debian package: sudo apt-get install docker-compose-plugin")
    sys.exit(1)


def _ensure_daemon(config: dict) -> dict:
    result = _cmd([*_docker_cmd(config), "info"])
    if result.returncode == 0:
        return config

    if _is_linux() and shutil.which("systemctl"):
        sudo = _sudo_prefix()
        if sudo is not None:
            ui.info("Starting Docker daemon...")
            _run([*sudo, "systemctl", "enable", "--now", "docker"])
            result = _cmd([*_docker_cmd(config), "info"])
            if result.returncode == 0:
                return config

    sudo = _sudo_prefix()
    if sudo is not None and not config.get("docker_sudo"):
        result = _cmd(["sudo", "docker", "info"])
        if result.returncode == 0:
            config = {**config, "docker_sudo": True}
            ui.warn("Current shell cannot access Docker directly; using sudo docker for Silicon commands.")
            ui.info("For non-sudo Docker access later: sudo usermod -aG docker $USER && newgrp docker")
            return config

    stderr = (result.stderr or result.stdout or "").strip()
    ui.error("Docker daemon is not reachable." + (f" {stderr}" if stderr else ""))
    ui.info("Start Docker, then rerun the same silicon command:")
    ui.info("  sudo systemctl enable --now docker")
    sys.exit(1)


def _ensure_image(config: dict, *, refresh: bool = False) -> None:
    image = _require_runtime_image(
        config.get("image") or DEFAULT_IMAGE,
        context="Docker runtime",
    )
    pinned = runtime_image_is_pinned(image)

    def local_image_exists() -> bool:
        if not pinned:
            return (
                _cmd(
                    [*_docker_cmd(config), "image", "inspect", image]
                ).returncode
                == 0
            )
        inspected = _cmd(
            [
                *_docker_cmd(config),
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image,
            ]
        )
        if inspected.returncode != 0:
            return False
        try:
            digests = json.loads(str(inspected.stdout or ""))
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(digests, list) and image in digests

    local_before = local_image_exists()
    # Refreshing an immutable digest cannot yield different content.
    if local_before and (pinned or not refresh):
        return

    qualifier = "immutable " if pinned else ""
    ui.info(f"Pulling {qualifier}Silicon runtime image: {image}")
    pulled = _run([*_docker_cmd(config), "pull", image])
    if pulled.returncode == 0 and local_image_exists():
        return
    if local_before:
        ui.warn(f"Could not re-pull Docker image: {image}")
        if pinned:
            ui.info("Using the already verified immutable local image.")
        else:
            ui.info("Using the existing local-development image.")
        return
    raise RuntimeError(
        f"Could not pull and verify the pinned Docker image: {image}. "
        "Authenticate to its registry if it is private, then retry."
    )


def ensure_ready(
    *,
    auto_init: bool = False,
    install: bool = True,
    pull_image: bool = True,
    root: str | None = None,
    image: str | None = None,
    refresh_image: bool = False,
    quiet: bool = False,
    write_compose: bool = True,
) -> dict:
    """Check Docker and initialize the Silicon runtime when requested."""
    if _truthy(os.environ.get("SILICON_CONTAINER_MODE")):
        return load_config()
    if runtime_opted_out():
        return load_config()

    cfg = load_config()
    if root:
        cfg["root"] = str(Path(root).expanduser().resolve())
        cfg["compose_file"] = str(Path(cfg["root"]) / "compose.yml")
        cfg["shared_home"] = str(Path(cfg["root"]) / ".shared-home")
    if image:
        cfg["image"] = image

    if not cfg.get("enabled") and not auto_init:
        load_config(required=True)

    _ensure_docker_binary(install)
    cfg = _ensure_daemon(cfg)
    _ensure_compose(cfg)

    if auto_init or not cfg.get("enabled") or not CONFIG_FILE.exists():
        init(
            cfg["root"],
            cfg["image"],
            shared_home=cfg.get("shared_home"),
            docker_sudo=bool(cfg.get("docker_sudo")),
            quiet=quiet,
            write_compose=write_compose,
        )
        cfg = load_config(required=True)
    else:
        _save_config({**cfg, "enabled": True})
        if write_compose:
            render_compose(cfg)

    if pull_image:
        _ensure_image(cfg, refresh=refresh_image)
    return cfg


def prepare_release_image(image: str) -> dict:
    """Pull and verify a published image without changing active runtime state."""

    pinned = str(image or "").strip()
    if not runtime_image_is_pinned(pinned):
        raise RuntimeError(
            "Docker releases require the immutable runtime image digest "
            "committed in the published Stemcell Git tag"
        )
    cfg = {**load_config(required=True), "image": pinned}
    _ensure_image(cfg)
    return cfg


def inspect_runtime_contract(config: dict, image: str) -> dict:
    """Inspect a runtime image without treating an outdated package as fatal."""

    selected = str(image or "").strip()
    if not selected or len(selected) > 512 or any(
        character in selected for character in "\x00\r\n"
    ):
        raise RuntimeError("Silicon runtime image identity is invalid")
    result = _run(
        [
            *_docker_cmd(config),
            "run",
            "--rm",
            "--pull",
            "never",
            "--entrypoint",
            "/opt/silicon-runtime/bin/python",
            selected,
            "-I",
            "-c",
            runtime_contract.DOCKER_PROBE_SCRIPT,
            runtime_contract.docker_contract_json(),
        ],
        capture=True,
    )
    stdout = str(getattr(result, "stdout", "") or "")
    payload: dict = {}
    for line in reversed(stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if not payload:
        raw = str(
            getattr(result, "stderr", "")
            or stdout
            or f"Docker exited with {result.returncode}"
        ).strip()
        raise RuntimeError(
            "the Silicon runtime dependency probe returned no inventory: "
            + raw[-2000:]
        )
    payload["returncode"] = int(result.returncode)
    return payload


def verify_runtime_contract(config: dict, image: str) -> dict[str, str]:
    """Prove the pinned image contains the complete supported toolchain."""

    selected = _require_runtime_image(
        image,
        context="Silicon runtime dependency verification",
    )
    payload = inspect_runtime_contract(config, selected)
    failures = payload.get("failures") if isinstance(payload, dict) else None
    if payload.get("returncode") != 0 or not isinstance(failures, list) or failures:
        if isinstance(failures, list) and failures:
            detail = "; ".join(str(item) for item in failures)
        else:
            detail = "the runtime probe failed without structured diagnostics"
        raise RuntimeError(
            "the published Silicon runtime image is missing or has outdated "
            f"required dependencies: {detail}. Publish a fresh runtime image "
            "containing Silicon CLI, Silicon Browser, Silicon Extend, Silicon "
            "Interface CLI, Claude Code, Codex, Node 22+, Python, and Git; "
            "then commit that digest in the published Stemcell tag."
        )
    versions = payload.get("versions")
    if not isinstance(versions, dict):
        raise RuntimeError(
            "the published Silicon runtime dependency probe returned no version "
            "inventory"
        )
    ui.success("Verified the complete published Silicon runtime toolchain.")
    return {
        str(name): str(version)
        for name, version in versions.items()
    }


def bind_release_runtime(
    image: str,
    *,
    installs: Iterable[registry.Install] = (),
    pull: bool = True,
) -> dict:
    """Verify and persist the exact image committed by a release.

    The image is pulled and content-address verified before either config or
    registry metadata changes. With no selected install this establishes the
    bootstrap default. With one selected install it changes only that row, so
    a rolling update cannot move unrelated Silicons to the candidate digest.
    Writes are atomic and retrying converges on the same digest.
    """

    pinned = str(image or "").strip()
    if pull:
        candidate = {
            **prepare_release_image(pinned),
            "enabled": True,
        }
    else:
        if not runtime_image_is_pinned(pinned):
            raise RuntimeError(
                "Docker releases require the immutable runtime image digest "
                "committed in the published Stemcell Git tag"
            )
        candidate = {
            **load_config(required=True),
            "enabled": True,
            "image": pinned,
        }
    selected = [inst for inst in installs if inst.is_docker]
    if len(selected) > 1:
        raise RuntimeError(
            "bind_release_runtime activates one Docker Silicon at a time"
        )
    if selected:
        inst = selected[0]
        registered = registry.find(inst.name)
        if (
            registered is None
            or Path(registered.path).expanduser().resolve()
            != Path(inst.path).expanduser().resolve()
        ):
            raise RuntimeError(
                f"could not bind runtime image for unregistered Silicon "
                f"'{inst.name}'"
            )
        if not registry.update_install(inst.name, image=pinned):
            raise RuntimeError(
                f"could not bind runtime image for unregistered Silicon "
                f"'{inst.name}'"
            )
        inst.image = pinned
        # Keep the global/default digest unchanged. Compose resolves this
        # target from its registry row and every other target from its own row.
        render_compose(
            load_config(required=True),
            pinned_targets={inst.name},
        )
    else:
        _save_config(candidate)
        render_compose(candidate)
    return candidate


def active_generation_runtime_image(inst: registry.Install) -> str:
    """Return the pinned image bound to the selected code generation."""

    from .updater.generation import GenerationError, GenerationStore

    try:
        value = GenerationStore(Path(inst.path)).current()
    except (OSError, GenerationError, ValueError) as exc:
        raise RuntimeError(
            f"'{inst.name}' has an invalid active generation; refusing to "
            "select a Docker runtime image"
        ) from exc
    if value.get("kind") == "legacy-flat":
        return ""
    image = str(value.get("runtime_image") or "")
    return image if runtime_image_is_pinned(image) else ""


def maintenance_coordinator_available(inst: registry.Install) -> bool:
    """Inspect the selected host-mounted code without starting a container."""

    from .config import active_release_root

    coordinator = active_release_root(inst.path) / "core" / "maintenance.py"
    return coordinator.is_file() and not coordinator.is_symlink()


def _legacy_offline_fence_owner(inst: registry.Install) -> str:
    marker = (
        Path(inst.path).expanduser().resolve()
        / ".silicon"
        / "maintenance"
        / "legacy-offline.json"
    )
    if not marker.exists() and not marker.is_symlink():
        return ""
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError("legacy Docker update fence is unsafe")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("legacy Docker update fence is corrupt") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "update_id", "created_at"}
        or value.get("schema") != 1
        or not isinstance(value.get("update_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value["update_id"])
        or not isinstance(value.get("created_at"), (int, float))
        or isinstance(value.get("created_at"), bool)
        or not math.isfinite(value["created_at"])
    ):
        raise RuntimeError("legacy Docker update fence is invalid")
    return value["update_id"]


def _auth_path(config: dict) -> Path:
    return Path(config["shared_home"]).expanduser().resolve() / AUTH_FILE


def _read_auth(config: dict) -> dict:
    path = _auth_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_auth(config: dict, updates: dict) -> None:
    path = _auth_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_auth(config)
    data.update(updates)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _select_auth_providers(args: list[str], *, prompt: bool) -> list[str]:
    selected: list[str] = []
    for arg in args:
        value = arg.strip().lower()
        if value in {"--all", "all"}:
            selected = ["claude", "codex"]
        elif value in {"--claude", "claude"}:
            selected.append("claude")
        elif value in {"--codex", "codex"}:
            selected.append("codex")
        elif value:
            ui.error(f"Unknown docker login option: {arg}")
            sys.exit(1)
    if selected:
        out = []
        for provider in selected:
            if provider not in out:
                out.append(provider)
        return out
    if not prompt or not ui.interactive():
        return []
    out = []
    if ui.confirm("Set up shared Claude Code account for this VM?", default_yes=True):
        out.append("claude")
    if ui.confirm("Set up shared Codex account for this VM?", default_yes=True):
        out.append("codex")
    return out


def _auth_container(config: dict, provider: str) -> int:
    shared_home = Path(config["shared_home"]).expanduser().resolve()
    shared_home.mkdir(parents=True, exist_ok=True)
    user = host_user()
    image = _require_runtime_image(
        config.get("image"),
        context="Docker authentication runtime",
    )
    cmd = [
        *_docker_cmd(config),
        "run",
        "--rm",
        "-it",
        *(["--user", user] if user else []),
        "-e",
        f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
        "-v",
        f"{shared_home}:{CONTAINER_SHARED_HOME}",
        "--entrypoint",
        "/usr/local/bin/silicon-runtime-entrypoint",
        image,
        "auth",
        provider,
    ]
    return _run(cmd).returncode


def _shared_tool_container(config: dict, tool: str, args: list[str]) -> int:
    if tool not in AUTH_PROVIDERS:
        ui.error(f"Unknown shared runtime tool: {tool}")
        return 1
    shared_home = Path(config["shared_home"]).expanduser().resolve()
    shared_home.mkdir(parents=True, exist_ok=True)
    user = host_user()
    image = _require_runtime_image(
        config.get("image"),
        context=f"Docker {tool} runtime",
    )
    command = [tool, *args] if args else [tool]
    cmd = [
        *_docker_cmd(config),
        "run",
        "--rm",
        *(["-it"] if ui.interactive() else []),
        *(["--user", user] if user else []),
        "-e",
        f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
        "-v",
        f"{shared_home}:{CONTAINER_SHARED_HOME}",
        "--entrypoint",
        "/usr/local/bin/silicon-runtime-entrypoint",
        image,
        "shared",
        *command,
    ]
    return _run(cmd).returncode


def run_shared_tool(tool: str, args: list[str] | None = None) -> None:
    actual_args = args or []
    if not ui.interactive() and not actual_args:
        ui.error(f"silicon {tool} must be run from an interactive terminal.")
        sys.exit(1)
    cfg = ensure_ready(auto_init=True, install=True, pull_image=True)
    code = _shared_tool_container(cfg, tool, actual_args)
    if code:
        sys.exit(code)


def login(args: list[str] | None = None, *, config: dict | None = None, prompt: bool = True) -> None:
    if not ui.interactive():
        ui.error("Shared Claude/Codex login must be run from an interactive terminal.")
        ui.info("Run: silicon docker login")
        sys.exit(1)
    cfg = config or ensure_ready(auto_init=True, install=True, pull_image=True)
    providers = _select_auth_providers(args or [], prompt=prompt)
    if not providers:
        ui.warn("No Claude/Codex account setup selected.")
        _write_auth(cfg, {"skipped": True})
        return

    for provider in providers:
        label = "Claude Code" if provider == "claude" else "Codex"
        ui.info(f"Opening shared {label} setup shell.")
        ui.info("Complete the sign-in flow in the container. When finished, exit the shell.")
        code = _auth_container(cfg, provider)
        if code != 0:
            ui.warn(f"{label} setup shell exited with code {code}.")
        if ui.confirm(f"Did you finish signing in to {label}? Type y if signed in.", default_yes=False):
            _write_auth(cfg, {provider: True, "skipped": False})
            ui.success(f"{label} marked as signed in for this VM.")
        else:
            ui.warn(f"{label} was not marked as signed in. You can retry with: silicon docker login {provider}")


def maybe_prompt_login(config: dict) -> None:
    if not ui.interactive():
        ui.info("To set up shared Claude/Codex accounts later, run: silicon docker login")
        return
    status = _read_auth(config)
    if status.get("claude") or status.get("codex") or status.get("skipped"):
        return
    if ui.confirm("Set up shared Claude/Codex accounts before installing Silicons?", default_yes=True):
        login([], config=config, prompt=True)
    else:
        _write_auth(config, {"skipped": True})
        ui.info("Skipping shared Claude/Codex login. You can run later: silicon docker login")


def ensure_pull_runtime() -> bool:
    if runtime_opted_out():
        ui.info("SILICON_RUNTIME is set to local/host; pulling without Docker runtime.")
        return False
    # At this point no published Stemcell tag has been fetched yet, so there is
    # deliberately no image to pull. ``stemcell.prepare_hydration`` binds the
    # digest committed to that exact tag before any target or secret is written.
    ensure_ready(
        auto_init=True,
        install=True,
        pull_image=False,
        write_compose=False,
    )
    return True


def _run(
    cmd: list[str],
    *,
    check: bool = False,
    capture: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    try:
        kwargs = {
            "check": check,
            "text": True,
            "capture_output": capture,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        ui.error(f"Command not found: {cmd[0]}")
        sys.exit(127)


def _compose_args(inst: registry.Install) -> list[str]:
    cfg = config_for_install(inst)
    return [*_docker_cmd(cfg), "compose", "-f", cfg["compose_file"]]


def _exec_args(
    inst: registry.Install,
    command: Iterable[str],
    *,
    workdir: str = CONTAINER_PATH,
    extra_environment: Iterable[str] = (),
) -> list[str]:
    return [
        *_docker_cmd(config_for_install(inst)),
        "exec",
        "-w",
        workdir,
        "-e",
        f"HOME={CONTAINER_HOME}",
        "-e",
        f"SILICON_HOME={CONTAINER_HOME}/.silicon",
        "-e",
        f"SILICON_BROWSER_HOME={CONTAINER_PATH}/.silicon-browser",
        "-e",
        "SILICON_CONTAINER_MODE=1",
        "-e",
        f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
        *[
            item
            for value in extra_environment
            for item in ("-e", value)
        ],
        inst.container_name or container_name(inst.name),
        *command,
    ]


def container_running(inst: registry.Install) -> bool:
    cname = inst.container_name or container_name(inst.name)
    result = _run([*_docker_cmd(config_for_install(inst)), "inspect", "-f", "{{.State.Running}}", cname], capture=True)
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def silicon_running(inst: registry.Install) -> bool:
    if silicon_child_status(inst) is not None:
        return True
    metadata = Path(inst.path).expanduser().resolve() / ".silicon.pid.meta.json"
    if metadata.exists() or metadata.is_symlink():
        # A current supervisor published metadata but it failed validation:
        # never downgrade that evidence to the legacy watchdog-only signal.
        return False
    if not container_running(inst):
        return False
    result = _run(
        _exec_args(
            inst,
            [
                "sh",
                "-lc",
                'test -s /silicon/.silicon.pid '
                '&& kill -0 "$(cat /silicon/.silicon.pid)"',
            ],
        ),
        capture=True,
    )
    # Pre-metadata runtime images remain conservatively "running" so an update
    # still drains their work instead of stopping a potentially active task.
    return result.returncode == 0


def silicon_child_status(inst: registry.Install) -> dict | None:
    """Return validated main-child health from inside the PID namespace."""

    if not container_running(inst):
        return None
    script = (
        r"""
import json
import os
import stat
import subprocess
import time
from pathlib import Path

"""
        + _CONTAINER_PROCESS_IDENTITY_HELPER
        + r"""
root = Path("/silicon").resolve()
pid_path = root / ".silicon.pid"
meta_path = root / ".silicon.pid.meta.json"
try:
    if pid_path.is_symlink() or meta_path.is_symlink():
        raise ValueError("linked health file")
    pid_metadata = pid_path.stat()
    meta_metadata = meta_path.stat()
    if (
        not stat.S_ISREG(pid_metadata.st_mode)
        or pid_metadata.st_size > 128
    ):
        raise ValueError("invalid supervisor pid file")
    if (
        not stat.S_ISREG(meta_metadata.st_mode)
        or meta_metadata.st_size > 16 * 1024
    ):
        raise ValueError("invalid child metadata file")
    supervisor = int(pid_path.read_text(encoding="utf-8").strip())
    value = json.loads(meta_path.read_text(encoding="utf-8"))
    child = int(value["child_pid"])
    recorded_supervisor = int(value["supervisor_pid"])
    supervisor_identity = str(value["supervisor_identity"])
    child_identity = str(value["child_identity"])
    started_at = float(value["started_at"])
    generation = Path(str(value["generation"])).resolve(strict=True)
    if (
        value.get("schema") != 1
        or supervisor <= 0
        or child <= 0
        or recorded_supervisor != supervisor
        or not supervisor_identity
        or not child_identity
    ):
        raise ValueError("invalid child metadata")
    os.kill(supervisor, 0)
    os.kill(child, 0)
    if (
        _process_birth_identity(supervisor) != supervisor_identity
        or _process_birth_identity(child) != child_identity
    ):
        raise ValueError("process birth identity changed")
    pointer = root / ".silicon" / "current.json"
    if pointer.exists():
        selected = json.loads(pointer.read_text(encoding="utf-8"))
        active = Path(str(selected["release_path"]))
        if not active.is_absolute():
            active = root / active
        active = active.resolve(strict=True)
        releases = (root / ".silicon" / "releases").resolve()
        if (
            selected.get("kind") != "immutable-release"
            or releases not in active.parents
            or not (active / "main.py").is_file()
        ):
            raise ValueError("invalid active generation")
    else:
        active = root
    if generation != active:
        raise ValueError("child belongs to a stale generation")
    now = time.time()
    if started_at > now + 5:
        raise ValueError("child start time is in the future")
    readiness = {}
    health_path = root / ".silicon" / "runtime-health.json"
    try:
        if (
            health_path.is_symlink()
            or not stat.S_ISREG(health_path.stat().st_mode)
            or health_path.stat().st_size > 16 * 1024
        ):
            raise ValueError("invalid readiness file")
        health = json.loads(health_path.read_text(encoding="utf-8"))
        health_pid = int(health["pid"])
        health_root = Path(str(health["code_root"])).resolve(strict=True)
        ready_at = float(health["ready_at"])
        heartbeat_at = float(health["heartbeat_at"])
        if (
            health.get("schema") == 1
            and health.get("ready") is True
            and health_pid == child
            and health_root == generation
            and ready_at <= heartbeat_at
        ):
            readiness = {
                "application_ready": True,
                "ready_at": ready_at,
                "heartbeat_at": heartbeat_at,
                "phase": str(health.get("phase") or ""),
            }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        readiness = {}
    print(
        json.dumps(
            {
                "supervisor_pid": supervisor,
                "child_pid": child,
                "generation": str(generation),
                "started_at": started_at,
                "uptime_seconds": max(0.0, now - started_at),
                **readiness,
            },
            sort_keys=True,
        )
    )
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
"""
    )
    result = _run(
        _exec_args(inst, ["python3", "-c", script]),
        capture=True,
    )
    lines = result.stdout.strip().splitlines()
    if result.returncode or not lines:
        return None
    try:
        value = json.loads(lines[-1])
        if (
            not isinstance(value, dict)
            or int(value["supervisor_pid"]) <= 0
            or int(value["child_pid"]) <= 0
            or float(value["started_at"]) < 0
            or float(value["uptime_seconds"]) < 0
            or not str(value["generation"])
        ):
            return None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value


def silicon_healthy(
    inst: registry.Install, *, min_uptime: float = 5.0
) -> bool:
    status = silicon_child_status(inst)
    return bool(
        status is not None
        and float(status["uptime_seconds"]) >= max(0.0, float(min_uptime))
    )


def silicon_ready(
    inst: registry.Install,
    *,
    min_uptime: float = 5.0,
    max_heartbeat_age: float = 5.0,
) -> bool:
    """Require a stable child plus a fresh app-owned readiness heartbeat."""

    status = silicon_child_status(inst)
    if (
        status is None
        or float(status["uptime_seconds"]) < max(0.0, float(min_uptime))
        or status.get("application_ready") is not True
    ):
        return False
    try:
        heartbeat_at = float(status["heartbeat_at"])
        ready_at = float(status["ready_at"])
    except (KeyError, TypeError, ValueError):
        return False
    age = time.time() - heartbeat_at
    return bool(
        0 <= age <= max(0.1, float(max_heartbeat_age))
        and ready_at <= heartbeat_at
    )


def _wait_for_container(inst: registry.Install, seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if container_running(inst):
            return True
        time.sleep(0.5)
    return False


def _exec_silicon(
    inst: registry.Install,
    args: list[str],
    *,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    return _run(
        _exec_args(inst, ["silicon", *args]),
        check=check,
        timeout=timeout,
    )


def maintenance_command(
    inst: registry.Install,
    command: list[str],
    *,
    check: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run an arbitrary command in a short-lived runtime container.

    The instance and shared authentication home are mounted exactly as they are
    for the long-running service.  This is used to stage Linux dependencies on
    the host's behalf without coupling them to the host Python/OS.
    """

    cfg = config_for_install(inst)
    image = _require_runtime_image(
        inst.image or cfg.get("image") or DEFAULT_IMAGE,
        context=f"Docker Silicon '{inst.name}'",
    )
    shared_home = Path(cfg["shared_home"]).expanduser().resolve()
    shared_home.mkdir(parents=True, exist_ok=True)
    env = [
        "-e", f"SILICON_INSTANCE_NAME={inst.name}",
        "-e", "SILICON_CONTAINER_MODE=1",
        "-e", f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
    ]
    user = host_user()
    volume = [
        "-v", f"{Path(inst.path).expanduser().resolve()}:{CONTAINER_PATH}",
        "-v", f"{shared_home}:{CONTAINER_SHARED_HOME}",
    ]
    cmd = [
        *_docker_cmd(cfg),
        "run",
        "--rm",
        "--entrypoint",
        "/usr/local/bin/silicon-runtime-entrypoint",
        *(["--user", user] if user else []),
        *env,
        *volume,
        image,
        "run",
        *command,
    ]
    return _run(cmd, check=check, capture=capture)


def _ephemeral_command(
    inst: registry.Install,
    command: list[str],
    *,
    workdir: str = CONTAINER_PATH,
    extra_environment: Iterable[str] = (),
    image: str | None = None,
    check: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a mounted-instance command without starting the service entrypoint."""

    cfg = config_for_install(inst)
    selected_image = _require_runtime_image(
        image or inst.image or cfg.get("image") or DEFAULT_IMAGE,
        context=f"Docker Silicon '{inst.name}'",
    )
    shared_home = Path(cfg["shared_home"]).expanduser().resolve()
    shared_home.mkdir(parents=True, exist_ok=True)
    user = host_user()
    cmd = [
        *_docker_cmd(cfg),
        "run",
        "--rm",
        "--entrypoint",
        command[0],
        *(["--user", user] if user else []),
        "-w",
        workdir,
        "-e",
        f"HOME={CONTAINER_HOME}",
        "-e",
        f"SILICON_HOME={CONTAINER_HOME}/.silicon",
        "-e",
        f"SILICON_BROWSER_HOME={CONTAINER_PATH}/.silicon-browser",
        "-e",
        "SILICON_CONTAINER_MODE=1",
        "-e",
        f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
        *[
            item
            for value in extra_environment
            for item in ("-e", value)
        ],
        "-v",
        f"{Path(inst.path).expanduser().resolve()}:{CONTAINER_PATH}",
        "-v",
        f"{shared_home}:{CONTAINER_SHARED_HOME}",
        selected_image,
        *command[1:],
    ]
    return _run(cmd, check=check, capture=capture)


def maintenance_silicon(
    inst: registry.Install,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return maintenance_command(inst, ["silicon", *args], check=check)


def _container_path(inst: registry.Install, host_path: str | Path) -> str:
    root = Path(inst.path).expanduser().resolve()
    resolved = Path(host_path).expanduser().resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Docker instance command path escaped {root}: {resolved}"
        ) from exc
    return str(Path(CONTAINER_PATH) / relative)


def _host_path(inst: registry.Install, container_path: str | Path) -> Path:
    root = Path(inst.path).expanduser().resolve()
    raw = Path(container_path)
    if not raw.is_absolute():
        raw = Path(CONTAINER_PATH) / raw
    try:
        relative = raw.relative_to(CONTAINER_PATH)
    except ValueError as exc:
        raise ValueError(
            f"Docker command path escaped {CONTAINER_PATH}: {raw}"
        ) from exc
    resolved = (root / relative).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Docker command path escaped {root}: {resolved}")
    return resolved


def _active_container_python(inst: registry.Install) -> str:
    from .updater.generation import GenerationStore

    root = Path(inst.path).expanduser().resolve()
    generations = GenerationStore(root)
    environment = generations.resolve_environment(generations.current())
    if environment is not None:
        environment_python = environment / "bin" / "python"
        if not environment_python.is_file():
            raise RuntimeError(
                "active Docker Silicon environment has no Python executable"
            )
        return _container_path(inst, environment_python)
    legacy_python = (
        root / ".venv" / "bin" / "python"
    )
    if legacy_python.is_file():
        return f"{CONTAINER_PATH}/.venv/bin/python"
    return "python3"


def run_active_python(
    inst: registry.Install,
    arguments: list[str],
    *,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run active Stemcell code in the live or an isolated runtime container."""

    from .config import active_release_root

    release = active_release_root(inst.path)
    container_release = _container_path(inst, release)
    command = [_active_container_python(inst), *arguments]
    extra_environment = (
        f"SILICON_DATA_ROOT={CONTAINER_PATH}",
        f"SILICON_RELEASE_ROOT={container_release}",
    )
    if container_running(inst):
        return _run(
            _exec_args(
                inst,
                command,
                workdir=container_release,
                extra_environment=extra_environment,
            ),
            capture=capture,
        )
    return _ephemeral_command(
        inst,
        command,
        workdir=container_release,
        extra_environment=extra_environment,
        capture=capture,
    )


def verify_silicon_extend(inst: registry.Install) -> None:
    """Require the active Docker Python environment to expose Extend."""

    script = r"""
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
    raise SystemExit(1)
if (
    installed != expected
    or getattr(package, "__version__", "") != expected
    or not tuple(commands)
):
    raise SystemExit(1)
"""
    result = run_active_python(
        inst,
        ["-I", "-c", script, SILICON_EXTEND_VERSION],
        capture=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Docker Silicon '{inst.name}' does not have the required "
            f"Silicon Extend {SILICON_EXTEND_VERSION} runtime. Ensure the "
            "published runtime image and Stemcell requirements lock include it, "
            "then rerun the same pull."
        )


def prepare_environment(
    inst: registry.Install,
    release: str | Path,
    *,
    image: str | None = None,
) -> Path | None:
    """Prepare a Linux dependency generation before the task drain begins."""

    release_path = Path(release).expanduser().resolve()
    lockfile = release_path / "requirements.lock"
    requirements = release_path / "requirements.txt"
    if not lockfile.is_file():
        if requirements.is_file():
            raise RuntimeError(
                "published Docker release has requirements.txt but no "
                "hash-pinned requirements.lock"
            )
        return None
    container_release = _container_path(inst, release_path)
    script = r"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

release = Path(sys.argv[1]).resolve()
root = Path("/silicon").resolve()
releases = (root / ".silicon" / "releases").resolve()
if releases not in release.parents:
    raise SystemExit("candidate release escaped the immutable generation root")
requirements = release / "requirements.lock"
if not requirements.is_file():
    raise SystemExit("candidate has no hash-pinned requirements.lock")
digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
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
    for part in (implementation, cache_tag, soabi, machine, platform_tag)
    if part
).lower()
readable = re.sub(r"[^a-z0-9._-]+", "-", readable).strip(".-_")
platform_key = (
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
    "key": platform_key,
}
environment_root = root / ".silicon" / "environments"
environment_root.mkdir(parents=True, exist_ok=True)
if environment_root.is_symlink() or root not in environment_root.resolve().parents:
    raise SystemExit("dependency environment root is unsafe")
environment = environment_root / f"{digest}-{platform_key}"
marker = environment / ".silicon-environment.json"
if environment.is_symlink():
    raise SystemExit("dependency environment target is unsafe")

def ready():
    try:
        if marker.is_symlink():
            return False
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        value.get("requirements_sha256") == digest
        and value.get("requirements_file") == "requirements.lock"
        and value.get("require_hashes") is True
        and value.get("runtime") == runtime_identity
        and (environment / "bin" / "python").is_file()
    )

if not ready():
    temporary = environment.with_name(f".{environment.name}.{os.getpid()}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    if environment.exists() or environment.is_symlink():
        if environment.is_symlink() or not environment.is_dir():
            raise SystemExit("dependency environment target is unsafe")
        shutil.rmtree(environment)
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", "--copies", str(temporary)],
            check=True,
        )
        subprocess.run(
            [
                str(temporary / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "-r",
                str(requirements),
            ],
            check=True,
        )
        marker_tmp = temporary / ".silicon-environment.json.tmp"
        marker_tmp.write_text(
            json.dumps(
                {
                    "requirements_sha256": digest,
                    "requirements_file": "requirements.lock",
                    "require_hashes": True,
                    "runtime": runtime_identity,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(marker_tmp, temporary / ".silicon-environment.json")
        os.replace(temporary, environment)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)

print(json.dumps({"environment_path": str(environment)}))
"""
    result = _ephemeral_command(
        inst,
        ["python3", "-c", script, container_release],
        workdir=container_release,
        extra_environment=(
            f"SILICON_DATA_ROOT={CONTAINER_PATH}",
            f"SILICON_RELEASE_ROOT={container_release}",
        ),
        image=image,
        capture=True,
    )
    lines = result.stdout.strip().splitlines()
    if result.returncode or not lines:
        raise RuntimeError(
            "could not prepare Docker dependency environment: "
            + (result.stderr.strip() or result.stdout.strip() or "no response")
        )
    try:
        value = json.loads(lines[-1])
        environment = _host_path(inst, str(value["environment_path"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Docker dependency builder returned an invalid environment path"
        ) from exc
    environment_root = (
        Path(inst.path).expanduser().resolve() / ".silicon" / "environments"
    ).resolve()
    if environment_root not in environment.parents:
        raise RuntimeError("Docker dependency environment escaped its safe root")
    return environment


def glass_agent_running(inst: registry.Install) -> bool:
    if not container_running(inst):
        return False
    script = (
        r"""
import json
import os
import stat
import subprocess
from pathlib import Path

"""
        + _CONTAINER_PROCESS_IDENTITY_HELPER
        + r"""
root = Path("/silicon").resolve()
pid_path = root / ".glass_agent.pid"
meta_path = root / ".glass_agent.pid.meta.json"
try:
    if pid_path.is_symlink():
        raise ValueError("linked sidecar pid file")
    pid_metadata = pid_path.stat()
    if (
        not stat.S_ISREG(pid_metadata.st_mode)
        or pid_metadata.st_size <= 0
        or pid_metadata.st_size > 128
    ):
        raise ValueError("invalid sidecar pid file")
    process_id = int(pid_path.read_text(encoding="utf-8").strip())
    if process_id <= 0:
        raise ValueError("invalid sidecar pid")
    current_identity = _process_birth_identity(process_id)
    if not current_identity:
        raise ValueError("sidecar is not alive")
    if meta_path.exists() or meta_path.is_symlink():
        if meta_path.is_symlink():
            raise ValueError("linked sidecar identity file")
        identity_metadata = meta_path.stat()
        if (
            not stat.S_ISREG(identity_metadata.st_mode)
            or identity_metadata.st_size <= 0
            or identity_metadata.st_size > 16 * 1024
        ):
            raise ValueError("invalid sidecar identity file")
        value = json.loads(meta_path.read_text(encoding="utf-8"))
        recorded_identity = str(value["identity"])
        if (
            value.get("schema") != 1
            or int(value["pid"]) != process_id
            or not recorded_identity
            or recorded_identity != current_identity
        ):
            raise ValueError("sidecar process birth identity changed")
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
"""
    )
    result = _run(
        _exec_args(
            inst,
            ["python3", "-c", script],
        ),
        capture=True,
    )
    return result.returncode == 0


def interface_daemon_running(inst: registry.Install) -> bool:
    """Verify the recorded Interface PID is an Interface listener process."""

    script = r"""
import os
import stat
from pathlib import Path

pid_path = Path("/silicon/.silicon-interface/daemon.pid")
descriptor = -1
try:
    before = pid_path.lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > 32
    ):
        raise ValueError("invalid Interface daemon PID file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(pid_path, flags)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not os.path.samestat(before, opened)
    ):
        raise ValueError("Interface daemon PID file changed")
    payload = os.read(descriptor, 33)
    if len(payload) > 32:
        raise ValueError("oversized Interface daemon PID")
    process_id = int(payload.decode("ascii").strip())
    if process_id <= 0:
        raise ValueError("invalid Interface daemon PID")
    os.kill(process_id, 0)
    command = Path(f"/proc/{process_id}/cmdline").read_bytes().split(b"\0")
    arguments = [item.decode("utf-8", "replace") for item in command if item]
    if (
        not any(Path(item).name == "silicon-interface.mjs" for item in arguments)
        or "daemon" not in arguments
        or "run" not in arguments
    ):
        raise ValueError("recorded PID is not an Interface daemon")
except (OSError, UnicodeError, ValueError):
    raise SystemExit(1)
finally:
    if descriptor >= 0:
        os.close(descriptor)
"""
    result = _run(
        _exec_args(inst, ["python3", "-c", script]),
        capture=True,
    )
    return result.returncode == 0


def _container_supports_interface_activation(
    inst: registry.Install,
) -> bool:
    result = _run(
        _exec_args(
            inst,
            ["test", "-x", CONTAINER_INTERFACE_ACTIVATOR],
        ),
        capture=True,
    )
    return result.returncode == 0


def _reset_container_interface_pid(inst: registry.Install) -> None:
    script = (
        "from pathlib import Path;"
        "path=Path('/silicon/.silicon-interface/daemon.pid');"
        "path.unlink(missing_ok=True)"
    )
    result = _run(
        _exec_args(inst, ["python3", "-c", script]),
        capture=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "could not reset the stale container Interface daemon PID"
            + (f": {detail}" if detail else "")
        )


def _start_current_container_interface(
    inst: registry.Install,
    *,
    reset_pid: bool,
) -> None:
    command = (
        "import sys;"
        "from silicon_cli import process,registry;"
        "process._start_interface_daemon(registry.resolve_one(sys.argv[1]))"
    )
    result = _run(
        _exec_args(
            inst,
            ["python3", "-c", command, inst.name],
            extra_environment=(
                ("SILICON_INTERFACE_RESET_DAEMON_PID=1",)
                if reset_pid
                else ()
            ),
        ),
        capture=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(
            "the selected runtime could not start Silicon Interface"
            + suffix
        )


def _start_legacy_container_interface(
    inst: registry.Install,
    *,
    reset_pid: bool,
) -> None:
    """Start Interface directly for the immediately preceding runtime contract."""

    if reset_pid:
        _reset_container_interface_pid(inst)
    result = _run(
        _exec_args(
            inst,
            [
                CONTAINER_INTERFACE_EXECUTABLE,
                "daemon",
                "start",
            ],
            extra_environment=(
                f"SILICON_INTERFACE_ROOT={CONTAINER_PATH}",
            ),
        ),
        capture=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(
            "the rollback runtime could not start its Silicon Interface daemon"
            + suffix
        )


def stop_interface_daemon(
    inst: registry.Install,
    *,
    required: bool = False,
) -> bool:
    """Stop the selected image's Interface listener before protected snapshots."""

    result = _run(
        _exec_args(
            inst,
            [
                CONTAINER_INTERFACE_EXECUTABLE,
                "daemon",
                "stop",
            ],
            extra_environment=(
                f"SILICON_INTERFACE_ROOT={CONTAINER_PATH}",
            ),
        ),
        capture=True,
    )
    ok = result.returncode == 0
    if required and not ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(
            "Silicon Interface daemon could not be stopped safely"
            + suffix
        )
    return ok


def start_one(
    inst: registry.Install,
    *,
    start_main: bool = True,
    start_agent: bool = True,
    start_interface: bool | None = None,
    reconcile: bool = True,
    allow_legacy_fence: bool = False,
) -> None:
    restore_interface = (
        interface_cli.daemon_required(inst.path)
        if start_interface is None
        else bool(start_interface)
    )
    ensure_ready(
        auto_init=False,
        install=True,
        pull_image=False,
        quiet=True,
        write_compose=False,
    )
    if reconcile:
        # Normal starts must converge any interrupted transaction before the
        # container can accept new work. Updater-owned restores explicitly
        # pass reconcile=False to avoid recursion while resuming that same
        # transaction.
        from . import update

        update.reconcile_before_start(inst)
    fence_owner = _legacy_offline_fence_owner(inst)
    if fence_owner and not allow_legacy_fence:
        ui.error(
            f"'{inst.name}' has a legacy offline update in progress; run "
            f"'silicon update resume {inst.name}' before starting it."
        )
        return
    active_image = active_generation_runtime_image(inst)
    if active_image and active_image != inst.image:
        bind_release_runtime(active_image, installs=[inst])
    cfg = config_for_install(inst)
    _ensure_image(cfg)
    render_compose(
        cfg,
        update_fence_owners=(
            {inst.name: fence_owner}
            if fence_owner and allow_legacy_fence
            else None
        ),
        pinned_targets={inst.name},
    )
    svc = inst.service or service_name(inst.name)
    suspend_marker = (
        Path(inst.path).expanduser().resolve()
        / ".silicon"
        / "docker-start-suspended"
    )
    was_running = container_running(inst)
    suspend_start = not was_running and (not start_main or not reconcile)
    if suspend_start:
        suspend_marker.parent.mkdir(parents=True, exist_ok=True)
        suspend_marker.touch()
    ui.info(f"Starting Docker service '{svc}' for '{inst.name}'...")
    try:
        _run([*_compose_args(inst), "up", "-d", svc], check=True)
        if not _wait_for_container(inst):
            ui.error(
                f"Container for '{inst.name}' did not become healthy enough "
                "to exec into."
            )
            return
        if suspend_start:
            acknowledgement_deadline = time.monotonic() + 20.0
            while (
                suspend_marker.exists()
                and container_running(inst)
                and time.monotonic() < acknowledgement_deadline
            ):
                time.sleep(0.1)
            if suspend_marker.exists():
                raise RuntimeError(
                    f"Container for '{inst.name}' did not acknowledge its "
                    "transactional start suspension"
                )
        if start_main:
            # If the container was already alive but its Silicon was stopped,
            # restart just the Silicon process. If the entrypoint already
            # started it, this is a no-op.
            for _ in range(5):
                if silicon_running(inst):
                    break
                if reconcile:
                    result = _exec_silicon(inst, ["start", inst.name])
                else:
                    result = _run(
                        _exec_args(
                            inst,
                            [
                                "python3",
                                "-c",
                                (
                                    "import sys;"
                                    "from silicon_cli import process;"
                                    "process._start_one_unlocked("
                                    "sys.argv[1],start_agent=False,"
                                    "reconcile_updates=False)"
                                ),
                                inst.name,
                            ],
                            extra_environment=(
                                (
                                    "SILICON_INTERFACE_RESET_DAEMON_PID=1",
                                )
                                if not was_running
                                else ()
                            ),
                        )
                    )
                if result.returncode == 0 or silicon_running(inst):
                    break
                time.sleep(2)
        elif silicon_running(inst):
            _exec_silicon(inst, ["stop", inst.name])

        if restore_interface:
            current_activation = _container_supports_interface_activation(
                inst
            )
            if not interface_daemon_running(inst):
                if current_activation:
                    _start_current_container_interface(
                        inst,
                        reset_pid=not was_running,
                    )
                else:
                    _start_legacy_container_interface(
                        inst,
                        reset_pid=not was_running,
                    )
            if not interface_daemon_running(inst):
                raise RuntimeError(
                    f"Silicon Interface daemon for '{inst.name}' did not start"
                )

        if start_agent:
            if not glass_agent_running(inst):
                _exec_silicon(inst, ["agent", "start", inst.name])
        elif glass_agent_running(inst):
            _exec_silicon(inst, ["agent", "stop", inst.name])
    finally:
        if fence_owner and allow_legacy_fence:
            # Keep the durable Compose source fail-closed. The created
            # container retains the transaction-scoped owner only for this
            # controlled start/restart.
            render_compose(
                config_for_install(inst),
                pinned_targets={inst.name},
            )
        # A failed compose start must not accidentally suppress a later,
        # unrelated normal start. A live container owns the marker until its
        # entrypoint acknowledges and removes it.
        if not container_running(inst):
            suspend_marker.unlink(missing_ok=True)
    ui.success(f"'{inst.name}' Docker service is running.")


def restore_one(
    inst: registry.Install,
    *,
    container: bool,
    main: bool,
    glass_agent: bool,
    interface: bool = False,
    reconcile: bool = False,
    allow_legacy_fence: bool = False,
) -> None:
    """Restore the exact Docker/service state observed before maintenance."""

    if not container:
        if container_running(inst):
            stop_one(inst, full=True)
        return
    start_one(
        inst,
        start_main=main,
        start_agent=glass_agent,
        start_interface=interface,
        reconcile=reconcile,
        allow_legacy_fence=allow_legacy_fence,
    )


def stop_one(inst: registry.Install, *, full: bool = False) -> None:
    svc = inst.service or service_name(inst.name)
    if full:
        if container_running(inst):
            try:
                _exec_silicon(
                    inst,
                    ["stop", "--full", inst.name],
                    timeout=FULL_STOP_EXEC_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                ui.warn(
                    f"In-container stop for '{inst.name}' timed out; "
                    "forcing the Compose stop so rollback can continue."
                )
        _run([*_compose_args(inst), "stop", svc])
        ui.success(f"'{inst.name}' container stopped.")
        return

    if not container_running(inst):
        ui.warn(f"'{inst.name}' container is not running.")
        return
    _exec_silicon(inst, ["stop", inst.name])


def restart_one(inst: registry.Install) -> None:
    stop_one(inst, full=False)
    time.sleep(1)
    start_one(inst)


def run_silicon(inst: registry.Install, args: list[str]) -> int:
    ensure_ready(auto_init=False, install=True, pull_image=False, quiet=True)
    _ensure_image(config_for_install(inst))
    if container_running(inst):
        return _exec_silicon(inst, args).returncode
    return maintenance_silicon(inst, args).returncode


def debug(inst: registry.Install) -> None:
    log_file = Path(inst.path) / ".silicon.log"
    if not log_file.exists():
        ui.error(f"No log file found at {log_file}")
        sys.exit(1)
    print(f"\n{ui.BOLD}{ui.CYAN}Debugging '{inst.name}'{ui.RESET} (Docker)")
    print(f"{ui.DIM}  Log: {log_file}{ui.RESET}")
    print(f"{ui.DIM}  Press Ctrl+C to detach{ui.RESET}\n")
    try:
        subprocess.run(["tail", "-f", str(log_file)])
    except KeyboardInterrupt:
        pass


def print_status(inst: registry.Install) -> None:
    container = "running" if container_running(inst) else "stopped"
    silicon = "running" if silicon_running(inst) else "stopped"
    color = ui.GREEN if silicon == "running" else ui.DIM
    print(f"\n{ui.BOLD}{inst.name}{ui.RESET} {color}● {silicon}{ui.RESET} (Docker container {container})")
    print(f"{ui.DIM}  Path: {inst.path}{ui.RESET}")
    print(f"{ui.DIM}  Service: {inst.service or service_name(inst.name)}{ui.RESET}")
    print(f"{ui.DIM}  Compose: {inst.compose_file}{ui.RESET}\n")


def parse_init_args(args: list[str]) -> tuple[str | None, str | None, str | None]:
    root = None
    image = None
    shared_home = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--root" and i + 1 < len(args):
            root = args[i + 1]
            i += 2
        elif arg.startswith("--root="):
            root = arg.split("=", 1)[1]
            i += 1
        elif arg == "--image" and i + 1 < len(args):
            image = args[i + 1]
            i += 2
        elif arg.startswith("--image="):
            image = arg.split("=", 1)[1]
            i += 1
        elif arg == "--shared-home" and i + 1 < len(args):
            shared_home = args[i + 1]
            i += 2
        elif arg.startswith("--shared-home="):
            shared_home = arg.split("=", 1)[1]
            i += 1
        else:
            ui.error(f"Unknown docker init option: {arg}")
            sys.exit(1)
    return root, image, shared_home


def cmd_docker(args: list[str]) -> None:
    sub = args[0] if args else "status"
    if sub in {"init", "bootstrap"}:
        root, image, shared_home = parse_init_args(args[1:])
        cfg = ensure_ready(
            auto_init=True,
            install=True,
            pull_image=False,
            root=root,
            image=image,
            write_compose=False,
        )
        if shared_home:
            init(
                cfg["root"],
                cfg["image"],
                shared_home=shared_home,
                docker_sudo=bool(cfg.get("docker_sudo")),
                write_compose=False,
            )
            cfg = load_config(required=True)
        if image:
            cfg = bind_release_runtime(image)
        elif runtime_image_is_pinned(cfg.get("image")):
            _ensure_image(cfg)
            render_compose(cfg)
        else:
            ui.info(
                "Docker prerequisites are ready. The next published `silicon "
                "pull` binds and pulls its immutable runtime image digest."
            )
        return
    if sub == "doctor":
        root, image, shared_home = parse_init_args(args[1:])
        cfg = ensure_ready(
            auto_init=False,
            install=False,
            pull_image=False,
            root=root,
            image=image,
            write_compose=False,
        )
        if shared_home:
            ui.error("--shared-home is only supported by silicon docker init")
            raise SystemExit(1)
        if image:
            cfg = bind_release_runtime(image)
        else:
            _ensure_image(cfg)
            render_compose(cfg)
        ui.success("Docker runtime and immutable image digest are ready.")
        return
    if sub == "login":
        login(args[1:])
        return
    if sub == "status":
        cfg = load_config()
        if not cfg.get("enabled"):
            ui.warn("Docker runtime is not enabled. Run: silicon docker init")
            return
        ui.info(f"Root: {cfg['root']}")
        ui.info(f"Compose: {cfg['compose_file']}")
        ui.info(f"Shared auth home: {cfg['shared_home']}")
        ui.info(f"Image: {cfg['image']}")
        ui.info(f"Docker command: {'sudo docker' if cfg.get('docker_sudo') else 'docker'}")
        return
    if sub == "compose":
        print(load_config(required=True)["compose_file"])
        return
    ui.error("Usage: silicon docker <init|bootstrap|doctor|login|status|compose> [--root PATH] [--image IMAGE]")
    sys.exit(1)
