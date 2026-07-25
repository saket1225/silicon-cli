"""Small cross-process advisory lock for host-level CLI state."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only.
    fcntl = None
    import msvcrt


class HostLockBusy(RuntimeError):
    pass


def ensure_private_directory(path: Path) -> Path:
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"host state directory is unsafe: {path}")
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


class HostFileLock:
    def __init__(self, path: Path, *, timeout: float = 30.0):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self._descriptor = -1

    def acquire(self) -> None:
        if self._descriptor >= 0:
            raise RuntimeError("host lock is already held")
        ensure_private_directory(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"host lock is not a regular file: {self.path}")
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(
                            descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                    else:  # pragma: no cover - Windows only.
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        if os.fstat(descriptor).st_size == 0:
                            os.write(descriptor, b"\0")
                            os.fsync(descriptor)
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except (BlockingIOError, OSError) as exc:
                    if time.monotonic() >= deadline:
                        raise HostLockBusy(
                            f"another Silicon CLI operation owns {self.path}"
                        ) from exc
                    time.sleep(0.05)
            self._descriptor = descriptor
        except Exception:
            os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows only.
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "HostFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
