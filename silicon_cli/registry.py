"""Crash-safe registry of known Silicon installations."""
from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from . import ui
from .config import REGISTRY_DIR, REGISTRY_FILE
from .host_lock import HostFileLock, ensure_private_directory
from .updater.io import atomic_write_json

MAX_REGISTRY_BYTES = 4 * 1024 * 1024


class RegistryCorruption(RuntimeError):
    pass


class RegistryConflict(RuntimeError):
    pass


def _validated_registry(value: object) -> dict:
    rows = value.get("installations") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise RegistryCorruption("Silicon registry has no installations list")
    names: set[str] = set()
    paths: set[str] = set()
    services: set[str] = set()
    containers: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RegistryCorruption("Silicon registry has an invalid installation")
        name = row.get("name")
        path = row.get("path")
        service = row.get("service", "")
        container = row.get("container_name", "")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(path, str)
            or not path
            or not isinstance(service, str)
            or not isinstance(container, str)
        ):
            raise RegistryCorruption("Silicon registry has an invalid installation")
        normalized_path = str(Path(path).expanduser().resolve())
        if (
            name in names
            or normalized_path in paths
            or (service and service in services)
            or (container and container in containers)
        ):
            raise RegistryCorruption("Silicon registry contains duplicate identities")
        names.add(name)
        paths.add(normalized_path)
        if service:
            services.add(service)
        if container:
            containers.add(container)
    return value


@dataclass
class Install:
    index: int
    name: str
    path: str
    pid_file: str
    runtime: str = "local"
    service: str = ""
    compose_file: str = ""
    image: str = ""
    container_name: str = ""

    @property
    def is_docker(self) -> bool:
        return self.runtime == "docker"


def _load() -> dict:
    try:
        metadata = REGISTRY_FILE.lstat()
    except FileNotFoundError:
        return {"installations": []}
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_REGISTRY_BYTES
    ):
        raise RegistryCorruption(f"Silicon registry is unsafe: {REGISTRY_FILE}")
    try:
        value = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryCorruption(
            f"Silicon registry is invalid: {REGISTRY_FILE}"
        ) from exc
    return _validated_registry(value)


def _save_unlocked(reg: dict) -> None:
    ensure_private_directory(REGISTRY_DIR)
    atomic_write_json(
        REGISTRY_FILE, _validated_registry(reg), mode=0o600
    )


def installs() -> list[Install]:
    reg = _load()
    out = []
    for i, inst in enumerate(reg.get("installations", [])):
        out.append(Install(
            i,
            inst["name"],
            inst["path"],
            inst.get("pid_file", ""),
            inst.get("runtime", "local"),
            inst.get("service", ""),
            inst.get("compose_file", ""),
            inst.get("image", ""),
            inst.get("container_name", ""),
        ))
    return out


def count() -> int:
    return len(_load().get("installations", []))


def register(
    name: str,
    path: str,
    pid_file: str | None = None,
    *,
    runtime: str = "local",
    service: str = "",
    compose_file: str = "",
    image: str = "",
    container_name: str = "",
    update_existing: bool = False,
) -> str:
    """Add an installation. Returns 'added' or 'exists'."""
    path = str(Path(path).expanduser().resolve())
    pid_file = pid_file or str(Path(path) / ".silicon.pid")
    with HostFileLock(REGISTRY_DIR / "registry.lock"):
        reg = _load()
        rows = reg.get("installations", [])
        for inst in rows:
            same_name = inst.get("name") == name
            same_path = str(Path(inst.get("path", "")).expanduser().resolve()) == path
            if same_name or same_path:
                if not (same_name and same_path):
                    raise RegistryConflict(
                        f"registry identity collision for '{name}' at {path}"
                    )
                if update_existing:
                    updated = {
                        "name": name,
                        "path": path,
                        "pid_file": pid_file,
                        "runtime": runtime,
                        "service": service,
                        "compose_file": compose_file,
                        "image": image,
                        "container_name": container_name,
                    }
                    if all(
                        inst.get(key, "") == value
                        for key, value in updated.items()
                    ):
                        return "exists"
                    inst.update(updated)
                    _save_unlocked(reg)
                    return "updated"
                return "exists"
        for inst in rows:
            if service and inst.get("service") == service:
                raise RegistryConflict(f"registry service collision: {service}")
            if container_name and inst.get("container_name") == container_name:
                raise RegistryConflict(
                    f"registry container collision: {container_name}"
                )
        rows.append({
            "name": name,
            "path": path,
            "pid_file": pid_file,
            "runtime": runtime,
            "service": service,
            "compose_file": compose_file,
            "image": image,
            "container_name": container_name,
        })
        _save_unlocked(reg)
        return "added"


def update_install(name: str, **fields) -> bool:
    with HostFileLock(REGISTRY_DIR / "registry.lock"):
        reg = _load()
        for inst in reg.get("installations", []):
            if inst.get("name") == name:
                inst.update(fields)
                _save_unlocked(reg)
                return True
        return False


def name_taken(name: str) -> bool:
    return any(i.name == name for i in installs())


def find(search: str | None = None) -> Install | None:
    """By name if given, else the install whose path contains the cwd."""
    rows = installs()
    if search:
        for i in rows:
            if i.name == search:
                return i
        return None
    cwd = os.getcwd()
    for i in rows:
        if cwd == i.path or cwd.startswith(i.path.rstrip("/") + "/"):
            return i
    return None


def is_multi_target(s: str) -> bool:
    if s in {"all", "*"}:
        return True
    parts = [p.strip() for p in s.split(",")]
    return len(parts) > 1 and all(bool(p) for p in parts)


def resolve_targets(selector: str) -> list[str]:
    """'all' | '*' | '1,2,4' | 'api-dev,copywriter' → install names."""
    rows = installs()
    if selector in {"all", "*"}:
        return [i.name for i in rows]
    by_name = {i.name: i.name for i in rows}
    out = []
    seen = set()
    for part in selector.split(","):
        part = part.strip()
        name = ""
        if part.isdigit():
            idx = int(part) - 1
            for i in rows:
                if i.index == idx:
                    name = i.name
                    break
        else:
            name = by_name.get(part, "")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def pick() -> Install:
    """Interactive picker; auto-selects when there's exactly one."""
    rows = installs()
    if not rows:
        ui.error("No silicon installations found. Run 'silicon install' first.")
        sys.exit(1)
    if len(rows) == 1:
        return rows[0]

    from .process import install_is_running
    sys.stderr.write(f"\n{ui.BOLD}Select a silicon instance:{ui.RESET}\n\n")
    for i in rows:
        running = install_is_running(i)
        status = f"{ui.GREEN}● running{ui.RESET}" if running else f"{ui.DIM}○ stopped{ui.RESET}"
        sys.stderr.write(f"  {ui.BOLD}{i.index + 1}){ui.RESET} {i.name:<20} {status}  {ui.DIM}{i.path}{ui.RESET}\n")
    sys.stderr.write("\n")
    choice = ui.ask("Choice", "1")
    try:
        target_idx = int(choice) - 1
    except ValueError:
        ui.error("Invalid choice")
        sys.exit(1)
    for i in rows:
        if i.index == target_idx:
            return i
    ui.error("Invalid choice")
    sys.exit(1)


def resolve_one(target: str | None) -> Install:
    """For single-target commands: by name, else cwd, else interactive pick."""
    if target:
        inst = find(target)
        if not inst:
            ui.error(f"Silicon '{target}' not found")
            sys.exit(1)
        return inst
    return find() or pick()
