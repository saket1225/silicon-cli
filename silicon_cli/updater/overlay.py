"""Content-addressed, backup-visible local customization overlays."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from .io import (
    atomic_write_json,
    ensure_real_directory,
    fsync_dir,
    regular_files,
    sha256_file,
    validate_relative_path,
)
from .policy import RUNTIME_EXACT, RUNTIME_PREFIXES, is_runtime_path

MAX_OVERLAY_MANIFEST_BYTES = 256 * 1024 * 1024


class OverlayError(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: object, label: str) -> str:
    digest = str(value or "")
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise OverlayError(f"{label} is not a canonical SHA-256 digest")
    return digest


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_manifest_json(path: Path) -> dict:
    try:
        before = path.lstat()
    except OSError as exc:
        raise OverlayError(f"could not inspect customization overlay: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > MAX_OVERLAY_MANIFEST_BYTES
    ):
        raise OverlayError(
            "customization overlay manifest must be a bounded regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OverlayError(
            f"could not securely open customization overlay: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                raise OverlayError(
                    "customization overlay changed while opening"
                )
            payload = handle.read(MAX_OVERLAY_MANIFEST_BYTES + 1)
            after = os.fstat(handle.fileno())
            if (
                len(payload) > MAX_OVERLAY_MANIFEST_BYTES
                or _stat_signature(opened) != _stat_signature(after)
            ):
                raise OverlayError(
                    "customization overlay changed while reading"
                )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OverlayError(f"could not read customization overlay: {exc}") from exc
    if not isinstance(value, dict):
        raise OverlayError("customization overlay manifest must be an object")
    return value


def _require_real_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OverlayError(f"{label} is unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OverlayError(f"{label} must be a real directory")


def _inventory(root: Path, *, local: bool = False) -> dict[str, Path]:
    return dict(
        regular_files(
            root,
            excluded_prefixes=RUNTIME_PREFIXES if local else {".git/"},
            excluded_names=RUNTIME_EXACT if local else set(),
        )
    )


class OverlayStore:
    def __init__(self, instance: Path):
        self.instance = Path(instance).resolve()
        self.root = self.instance / ".silicon" / "overlays"
        self.objects = self.root / "objects" / "sha256"
        self.manifests = self.root / "manifests"

    def capture(self, base: Path, local: Path, *, base_tree_sha256: str) -> dict:
        base_files = _inventory(base)
        local_files = _inventory(local, local=True)
        entries = []
        tombstones = []
        self.objects.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        for rel in sorted(set(base_files) | set(local_files), key=os.fsencode):
            if is_runtime_path(rel):
                continue
            base_path = base_files.get(rel)
            local_path = local_files.get(rel)
            if base_path is not None and local_path is not None:
                if (
                    sha256_file(base_path) == sha256_file(local_path)
                    and stat.S_IMODE(base_path.stat().st_mode)
                    == stat.S_IMODE(local_path.stat().st_mode)
                ):
                    continue
            if local_path is None:
                tombstones.append(rel)
                continue
            digest = sha256_file(local_path)
            destination = self.objects / digest[:2] / digest[2:]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                temporary = destination.with_name(
                    f".{destination.name}.{os.getpid()}.tmp"
                )
                shutil.copyfile(local_path, temporary)
                os.chmod(temporary, 0o400)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                if sha256_file(temporary) != digest:
                    temporary.unlink(missing_ok=True)
                    raise OverlayError(f"customization changed while copying: {rel}")
                os.replace(temporary, destination)
                fsync_dir(destination.parent)
            elif destination.is_symlink() or sha256_file(destination) != digest:
                raise OverlayError(f"customization object is corrupt: {digest}")
            entries.append(
                {
                    "path": rel,
                    "sha256": digest,
                    "size": local_path.stat().st_size,
                    "mode": stat.S_IMODE(local_path.stat().st_mode),
                }
            )
        body = {
            "schema": 1,
            "base_tree_sha256": base_tree_sha256,
            "files": entries,
            "tombstones": tombstones,
        }
        root_hash = hashlib.sha256(_canonical(body)).hexdigest()
        manifest = {**body, "root_hash": root_hash}
        path = self.manifests / f"{root_hash}.json"
        if not path.exists():
            atomic_write_json(path, manifest, mode=0o400)
        self.verify(root_hash)
        return {"root_hash": root_hash, "manifest_path": str(path)}

    def load(self, root_hash: str) -> dict:
        root_hash = _digest(root_hash, "overlay root hash")
        _require_real_directory(
            self.instance / ".silicon",
            "Silicon state directory",
        )
        _require_real_directory(self.root, "customization overlay store")
        _require_real_directory(
            self.manifests,
            "customization overlay manifest directory",
        )
        path = self.manifests / f"{root_hash}.json"
        value = _read_manifest_json(path)
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "base_tree_sha256",
            "files",
            "tombstones",
            "root_hash",
        }:
            raise OverlayError("customization overlay manifest has invalid fields")
        body = {key: value[key] for key in value if key != "root_hash"}
        if (
            value.get("schema") != 1
            or hashlib.sha256(_canonical(body)).hexdigest() != root_hash
            or value.get("root_hash") != root_hash
        ):
            raise OverlayError("customization overlay root hash is invalid")
        _digest(value.get("base_tree_sha256"), "overlay base tree")
        return value

    def verify(self, root_hash: str) -> dict:
        value = self.load(root_hash)
        _require_real_directory(
            self.root / "objects",
            "customization overlay object directory",
        )
        _require_real_directory(
            self.objects,
            "customization overlay SHA-256 directory",
        )
        if not isinstance(value["files"], list):
            raise OverlayError("customization overlay files must be an array")
        paths: set[str] = set()
        for entry in value["files"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"path", "sha256", "size", "mode"}
            ):
                raise OverlayError("invalid customization overlay entry")
            rel = str(entry["path"])
            try:
                validate_relative_path(rel)
            except Exception as exc:
                raise OverlayError(f"invalid customization path: {rel}") from exc
            digest = str(entry["sha256"])
            size = entry["size"]
            mode = entry["mode"]
            if (
                rel in paths
                or is_runtime_path(rel)
                or _unsafe_digest(digest)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(mode, int)
                or isinstance(mode, bool)
                or mode < 0
                or mode > 0o777
            ):
                raise OverlayError(f"duplicate or forbidden customization: {rel}")
            paths.add(rel)
            source = self.objects / digest[:2] / digest[2:]
            _require_real_directory(
                source.parent,
                f"customization object prefix {digest[:2]}",
            )
            try:
                before = source.lstat()
                parent = source.parent.lstat()
                object_root = self.objects.lstat()
            except OSError as exc:
                raise OverlayError(
                    f"missing customization object: {rel}"
                ) from exc
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(parent.st_mode)
                or not stat.S_ISDIR(parent.st_mode)
                or stat.S_ISLNK(object_root.st_mode)
                or not stat.S_ISDIR(object_root.st_mode)
                or sha256_file(source) != digest
                or _stat_signature(before)
                != _stat_signature(source.lstat())
                or before.st_size != size
            ):
                raise OverlayError(f"missing customization object: {rel}")
        ordered_paths = [str(entry["path"]) for entry in value["files"]]
        if ordered_paths != sorted(paths, key=os.fsencode):
            raise OverlayError("customization overlay files are not canonical")
        tombstones = value["tombstones"]
        if (
            not isinstance(tombstones, list)
            or tombstones != sorted(set(tombstones), key=os.fsencode)
            or any(
                str(path) in paths
                or is_runtime_path(str(path))
                or _unsafe_overlay_path(str(path))
                for path in tombstones
            )
        ):
            raise OverlayError("invalid customization tombstones")
        return value

    def apply(self, root_hash: str, destination: Path) -> None:
        value = self.verify(root_hash)
        root = destination.resolve(strict=True)
        for rel in value["tombstones"]:
            target = root / rel
            existed = target.exists() or target.is_symlink()
            target.unlink(missing_ok=True)
            if existed:
                fsync_dir(target.parent)
        for entry in value["files"]:
            target = root / entry["path"]
            ensure_real_directory(target.parent, root=root)
            source = self.objects / entry["sha256"][:2] / entry["sha256"][2:]
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            descriptor = -1
            try:
                temporary.unlink(missing_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, int(entry["mode"]))
                with (
                    os.fdopen(descriptor, "wb") as output,
                    source.open("rb") as input_file,
                ):
                    descriptor = -1
                    shutil.copyfileobj(input_file, output)
                    if hasattr(os, "fchmod"):
                        os.fchmod(output.fileno(), int(entry["mode"]))
                    output.flush()
                    os.fsync(output.fileno())
                if not hasattr(os, "fchmod"):
                    os.chmod(temporary, int(entry["mode"]))
                    with temporary.open("rb") as handle:
                        os.fsync(handle.fileno())
                os.replace(temporary, target)
                fsync_dir(target.parent)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)


def _unsafe_overlay_path(value: str) -> bool:
    try:
        validate_relative_path(value)
        return False
    except Exception:
        return True


def _unsafe_digest(value: str) -> bool:
    try:
        _digest(value, "customization object")
        return False
    except OverlayError:
        return True
