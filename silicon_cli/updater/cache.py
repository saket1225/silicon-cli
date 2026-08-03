"""Machine-wide content-addressed release and dependency cache."""
from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Callable

from .io import atomic_write_json, fsync_dir, read_json, sha256_file
from .lock import AdvisoryFileLock, InstanceLock, UpdateLocked
from .release import (
    FetchedRelease,
    ReleaseManifest,
    ReleaseVerificationError,
    safe_extract,
    verify_artifact,
)


class ReleaseCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.releases = self.root / "releases"
        self.environments = self.root / "environments"

    def _operation_lock(self, label: str) -> AdvisoryFileLock:
        """Serialize shared-cache mutations without rejecting peer workers.

        Fleet preflight intentionally runs in parallel.  A non-blocking
        ``InstanceLock`` made those workers interpret ordinary cache
        contention as a conflicting update, so every worker except the first
        failed before activation.  The cache is shared infrastructure, not an
        instance transaction: callers must wait briefly for it instead.
        """

        return AdvisoryFileLock(
            self.root / ".silicon" / "update.lock",
            label=label,
        )

    @staticmethod
    def _real_directory(path: Path, *, create: bool = False) -> Path:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ReleaseVerificationError(
                f"release cache directory is unavailable: {path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ReleaseVerificationError(
                f"release cache directory is unsafe: {path}"
            )
        return path

    @staticmethod
    def _regular_file(path: Path, *, required: bool = True) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if required:
                raise ReleaseVerificationError(
                    f"release cache file is missing: {path}"
                )
            return False
        except OSError as exc:
            raise ReleaseVerificationError(
                f"release cache file is unavailable: {path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ReleaseVerificationError(
                f"release cache file is unsafe: {path}"
            )
        return True

    def _release_directory(self, tree_sha256: str, *, create: bool) -> Path:
        if (
            len(tree_sha256) != 64
            or any(char not in "0123456789abcdef" for char in tree_sha256)
        ):
            raise ReleaseVerificationError("release cache tree identity is invalid")
        self._real_directory(self.root, create=True)
        self._real_directory(self.releases, create=True)
        directory = self.releases / tree_sha256
        return self._real_directory(directory, create=create)

    def store(self, fetched: FetchedRelease) -> FetchedRelease:
        self._real_directory(self.root, create=True)
        with self._operation_lock(
            "release cache store "
            f"{fetched.manifest.identity.tree_sha256[:16]}",
        ):
            return self._store_locked(fetched)

    def _store_locked(self, fetched: FetchedRelease) -> FetchedRelease:
        identity = fetched.manifest.identity
        self._regular_file(Path(fetched.artifact))
        release_dir = self._release_directory(
            identity.tree_sha256, create=True
        )
        artifact = release_dir / "release.tar"
        manifest_path = release_dir / "manifest.json"
        artifact_exists = self._regular_file(artifact, required=False)
        manifest_exists = self._regular_file(manifest_path, required=False)
        if manifest_exists:
            existing = ReleaseManifest.from_dict(read_json(manifest_path))
            if existing != fetched.manifest:
                raise ReleaseVerificationError(
                    f"cache identity collision for {identity.tree_sha256}"
                )
        if artifact_exists:
            verify_artifact(artifact, fetched.manifest)
        if artifact_exists and manifest_exists:
            verify_artifact(artifact, existing)
            return FetchedRelease(existing, artifact)
        if not artifact_exists:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".release.", suffix=".tmp", dir=str(release_dir)
            )
            temporary = Path(temporary_name)
            try:
                with (
                    os.fdopen(descriptor, "wb") as output,
                    Path(fetched.artifact).open("rb") as source,
                ):
                    descriptor = -1
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                if sha256_file(temporary) != identity.artifact_sha256:
                    raise ReleaseVerificationError(
                        "artifact changed while entering the cache"
                    )
                os.replace(temporary, artifact)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
        if not manifest_exists:
            fetched.manifest.write(manifest_path)
        fsync_dir(release_dir)
        return FetchedRelease(fetched.manifest, artifact)

    def load(self, tree_sha256: str) -> FetchedRelease:
        self._real_directory(self.root, create=True)
        with self._operation_lock(
            f"release cache load {tree_sha256[:16]}"
        ):
            release_dir = self._release_directory(tree_sha256, create=False)
            manifest_path = release_dir / "manifest.json"
            artifact = release_dir / "release.tar"
            self._regular_file(manifest_path)
            self._regular_file(artifact)
            manifest = ReleaseManifest.from_dict(read_json(manifest_path))
            if manifest.identity.tree_sha256 != tree_sha256:
                raise ReleaseVerificationError(
                    "release cache path and manifest identity disagree"
                )
            verify_artifact(artifact, manifest)
            return FetchedRelease(manifest, artifact)

    def materialize(self, release: FetchedRelease, destination: Path) -> None:
        self._real_directory(self.root, create=True)
        self._regular_file(Path(release.artifact))
        with self._operation_lock(
            "release cache extract "
            f"{release.manifest.identity.tree_sha256[:16]}",
        ):
            safe_extract(release.artifact, destination, release.manifest)

    def prepare_environment(
        self,
        candidate: Path,
        *,
        runner: Callable[[list[str]], int],
        environments_root: Path | None = None,
    ) -> Path | None:
        """Build dependencies beside the active environment and reuse by hash."""

        lockfile = candidate / "requirements.lock"
        requirements = candidate / "requirements.txt"
        if not lockfile.is_file():
            if requirements.is_file():
                raise RuntimeError(
                    "release has requirements.txt but no reproducible, "
                    "hash-pinned requirements.lock"
                )
            return None
        requirement_hash = sha256_file(lockfile)
        runtime_identity = runtime_platform_identity()
        platform_key = runtime_identity["key"]
        environment_root = Path(environments_root or self.environments)
        environment = environment_root / f"{requirement_hash}-{platform_key}"
        ready = environment / ".silicon-environment.json"
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )

        def environment_ready() -> bool:
            if environment.is_symlink():
                raise RuntimeError(
                    "shared dependency cache target is a symbolic link"
                )
            if environment.exists() and not environment.is_dir():
                raise RuntimeError(
                    "shared dependency cache target is not a real directory"
                )
            if not environment.is_dir():
                return False
            if ready.is_symlink() or python.is_symlink():
                raise RuntimeError(
                    "shared dependency cache contains a linked readiness "
                    "marker or interpreter"
                )
            if not ready.is_file() or not python.is_file():
                return False
            try:
                value = read_json(ready)
            except (OSError, ValueError):
                return False
            return (
                value.get("requirements_sha256") == requirement_hash
                and value.get("requirements_file") == "requirements.lock"
                and value.get("require_hashes") is True
                and value.get("runtime") == runtime_identity
            )

        if environment_ready():
            return environment
        environment_root.mkdir(parents=True, exist_ok=True)
        if environment_root.is_symlink() or not environment_root.is_dir():
            raise RuntimeError(
                "shared dependency environment root is not a real directory"
            )
        cache_lock = InstanceLock(
            environment_root, f"environment-{requirement_hash}-{platform_key}"
        )
        deadline = time.monotonic() + 600
        while True:
            try:
                cache_lock.acquire()
                break
            except UpdateLocked:
                if environment_ready():
                    return environment
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "timed out waiting for the shared dependency environment"
                    )
                time.sleep(0.25)
        temporary = environment.with_name(f".{environment.name}.{os.getpid()}.tmp")
        try:
            if environment_ready():
                return environment
            if environment.exists() or environment.is_symlink():
                if environment.is_symlink() or not environment.is_dir():
                    raise RuntimeError(
                        "shared dependency cache target is not a real directory"
                    )
                shutil.rmtree(environment)
            if temporary.is_symlink():
                raise RuntimeError(
                    "staged dependency cache target is a symbolic link"
                )
            shutil.rmtree(temporary, ignore_errors=True)
            if runner(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--copies",
                    str(temporary),
                ]
            ) != 0:
                shutil.rmtree(temporary, ignore_errors=True)
                raise RuntimeError("could not create staged dependency environment")
            staged_python = temporary / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            if staged_python.is_symlink() or not staged_python.is_file():
                shutil.rmtree(temporary, ignore_errors=True)
                raise RuntimeError(
                    "staged dependency environment has no Python interpreter"
                )
            if runner(
                [
                    str(staged_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--require-hashes",
                    "-r",
                    str(lockfile),
                ]
            ) != 0:
                shutil.rmtree(temporary, ignore_errors=True)
                raise RuntimeError("could not install staged dependencies")
            atomic_write_json(
                temporary / ".silicon-environment.json",
                {
                    "requirements_sha256": requirement_hash,
                    "requirements_file": "requirements.lock",
                    "require_hashes": True,
                    "runtime": runtime_identity,
                },
            )
            os.replace(temporary, environment)
            fsync_dir(environment.parent)
            return environment
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
            cache_lock.release()


def runtime_platform_identity() -> dict[str, str]:
    """Return the exact interpreter/ABI/platform identity of a reusable venv."""

    implementation = str(getattr(sys.implementation, "name", "") or "python")
    cache_tag = str(getattr(sys.implementation, "cache_tag", "") or "")
    soabi = str(sysconfig.get_config_var("SOABI") or "")
    abi_flags = str(getattr(sys, "abiflags", "") or "")
    machine = str(platform.machine() or "unknown")
    platform_tag = str(sysconfig.get_platform() or sys.platform)
    descriptor = "|".join(
        (
            implementation,
            cache_tag,
            soabi,
            abi_flags,
            machine,
            platform_tag,
        )
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
    if not readable:
        readable = "python-runtime"
    # Bound filename length while retaining a collision-resistant identity.
    import hashlib

    key = f"{readable[:120]}-{hashlib.sha256(descriptor.encode()).hexdigest()[:16]}"
    return {
        "implementation": implementation,
        "cache_tag": cache_tag,
        "soabi": soabi,
        "abi_flags": abi_flags,
        "machine": machine,
        "platform": platform_tag,
        "key": key,
    }
