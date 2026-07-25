"""Small crash-safe filesystem primitives used by the updater."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable


class UnsafePathError(RuntimeError):
    pass


def fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_real_directory(path: Path, *, root: Path, mode: int = 0o700) -> Path:
    """Create a confined directory tree without following any symlink."""

    root = Path(root).resolve(strict=True)
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"directory escaped trusted root: {path}") from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            os.mkdir(cursor, mode)
            fsync_dir(cursor.parent)
            metadata = os.lstat(cursor)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UnsafePathError(
                f"trusted state path must be a real directory: {cursor}"
            )
    return cursor.resolve(strict=True)


def atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode,
    )


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def validate_relative_path(value: str) -> Path:
    if not value or "\x00" in value:
        raise UnsafePathError("empty or NUL-containing archive path")
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or normalized.startswith("/") or ":" in path.parts[0]:
        raise UnsafePathError(f"absolute archive path is forbidden: {value!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafePathError(f"non-canonical archive path is forbidden: {value!r}")
    return path


def regular_files(
    root: Path,
    *,
    excluded_prefixes: Iterable[str] = (),
    excluded_names: Iterable[str] = (),
) -> list[tuple[str, Path]]:
    """Return a stable, no-links inventory below ``root``."""

    root = root.resolve(strict=True)
    prefixes = tuple(p.rstrip("/") + "/" for p in excluded_prefixes)
    names = set(excluded_names)
    result: list[tuple[str, Path]] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        kept = []
        for name in sorted(directories):
            path = base / name
            rel = path.relative_to(root).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise UnsafePathError(f"symlinked directory is forbidden: {path}")
            if not stat.S_ISDIR(mode):
                raise UnsafePathError(f"special directory entry is forbidden: {path}")
            if (
                name in names
                or rel in names
                or any((rel + "/").startswith(p) for p in prefixes)
            ):
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if (
                name in names
                or rel in names
                or any(rel.startswith(p) for p in prefixes)
            ):
                continue
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise UnsafePathError(f"only regular files are allowed: {path}")
            result.append((rel, path))
    result.sort(key=lambda row: row[0])
    return result


def hash_tree(
    root: Path,
    *,
    excluded_prefixes: Iterable[str] = (),
    excluded_names: Iterable[str] = (),
) -> tuple[str, dict[str, dict[str, object]]]:
    """Hash names, modes, sizes and bytes so one digest identifies one tree."""

    tree = hashlib.sha256()
    files: dict[str, dict[str, object]] = {}
    for rel, path in regular_files(
        root,
        excluded_prefixes=excluded_prefixes,
        excluded_names=excluded_names,
    ):
        file_hash = sha256_file(path)
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        size = path.stat().st_size
        encoded = rel.encode("utf-8")
        tree.update(len(encoded).to_bytes(8, "big"))
        tree.update(encoded)
        tree.update(mode.to_bytes(4, "big"))
        tree.update(size.to_bytes(8, "big"))
        tree.update(bytes.fromhex(file_hash))
        files[rel] = {"sha256": file_hash, "size": size, "mode": mode}
    return tree.hexdigest(), files
