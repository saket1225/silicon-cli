"""One kernel-released update lock per Silicon instance."""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from pathlib import Path

from .io import fsync_dir

try:  # Unix, including every supported production host.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows.
    fcntl = None
    import msvcrt


class UpdateLocked(RuntimeError):
    pass


class AdvisoryLockError(RuntimeError):
    pass


_ADVISORY_LOCKS_GUARD = threading.Lock()
_ADVISORY_LOCKS: dict[str, threading.Lock] = {}


def _advisory_thread_lock(path: Path) -> threading.Lock:
    key = str(path.absolute())
    with _ADVISORY_LOCKS_GUARD:
        return _ADVISORY_LOCKS.setdefault(key, threading.Lock())


class AdvisoryFileLock:
    """Blocking, persistent advisory lock shared by independent components.

    The in-process mutex is required because OS file-lock semantics differ for
    separate descriptors owned by one process. The kernel lock supplies the
    corresponding cross-process exclusion. The file is never unlinked, which
    prevents a second lock domain from being created at the same pathname.
    """

    def __init__(self, path: Path, *, label: str):
        self.path = Path(path)
        self.label = label
        self.acquired = False
        self._descriptor = -1
        self._thread_lock = _advisory_thread_lock(self.path)

    @staticmethod
    def _lock(descriptor: int) -> None:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

    @staticmethod
    def _unlock(descriptor: int) -> None:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

    def acquire(self) -> None:
        if self.acquired:
            raise RuntimeError(f"{self.label} is already held")
        self._thread_lock.acquire()
        descriptor = -1
        locked = False
        try:
            parent = self.path.parent
            if parent.is_symlink():
                raise AdvisoryLockError(
                    f"{self.label} parent must not be a symbolic link"
                )
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent_metadata = parent.lstat()
            if (
                stat.S_ISLNK(parent_metadata.st_mode)
                or not stat.S_ISDIR(parent_metadata.st_mode)
            ):
                raise AdvisoryLockError(
                    f"{self.label} parent must be a real directory"
                )
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            opened = os.fstat(descriptor)
            current = self.path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or not os.path.samestat(opened, current)
            ):
                raise AdvisoryLockError(
                    f"{self.label} must be an unredirected regular file"
                )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:  # pragma: no cover - Windows
                os.chmod(self.path, 0o600)
            self._lock(descriptor)
            locked = True
            current = self.path.lstat()
            if (
                stat.S_ISLNK(current.st_mode)
                or not os.path.samestat(opened, current)
            ):
                raise AdvisoryLockError(
                    f"{self.label} changed while it was being acquired"
                )
            fsync_dir(parent)
            self._descriptor = descriptor
            self.acquired = True
        except OSError as exc:
            raise AdvisoryLockError(
                f"could not acquire {self.label}: {exc}"
            ) from exc
        finally:
            if not self.acquired:
                if descriptor >= 0:
                    try:
                        if locked:
                            try:
                                self._unlock(descriptor)
                            except OSError:
                                pass
                    finally:
                        try:
                            os.close(descriptor)
                        finally:
                            self._thread_lock.release()
                else:
                    self._thread_lock.release()

    def release(self) -> None:
        if not self.acquired:
            return
        descriptor = self._descriptor
        self._descriptor = -1
        self.acquired = False
        try:
            self._unlock(descriptor)
        finally:
            try:
                os.close(descriptor)
            finally:
                self._thread_lock.release()

    def __enter__(self) -> "AdvisoryFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class InstanceLock:
    """Hold an advisory OS lock whose ownership dies with the process.

    The lock file is intentionally persistent. Unlinking a locked inode permits
    another process to create and lock a different inode at the same path.
    Kernel ownership avoids PID-reuse and stale-lock races after a crash.
    """

    def __init__(self, instance: Path, transaction_id: str):
        self.path = Path(instance) / ".silicon" / "update.lock"
        self.transaction_id = transaction_id
        self.acquired = False
        self._descriptor = -1

    @staticmethod
    def _lock(descriptor: int) -> None:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

    @staticmethod
    def _unlock(descriptor: int) -> None:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

    def acquire(self) -> None:
        if self.acquired:
            raise RuntimeError("update lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(self.path), flags, 0o600)
        except OSError as exc:
            raise UpdateLocked(f"could not open update lock {self.path}: {exc}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise UpdateLocked(
                    f"update lock is not a local regular file: {self.path}"
                )
            try:
                self._lock(descriptor)
            except (BlockingIOError, OSError) as exc:
                try:
                    owner = self.path.read_text(encoding="utf-8").strip()
                except OSError:
                    owner = "unknown owner"
                raise UpdateLocked(
                    f"another update owns {self.path}: {owner}"
                ) from exc
            payload = (
                json.dumps(
                    {
                        "schema": 1,
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "transaction_id": self.transaction_id,
                        "acquired_at": time.time(),
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while recording update lock")
                view = view[written:]
            os.fsync(descriptor)
            fsync_dir(self.path.parent)
            self._descriptor = descriptor
            self.acquired = True
        except Exception:
            try:
                self._unlock(descriptor)
            except OSError:
                pass
            os.close(descriptor)
            raise

    def release(self) -> None:
        if not self.acquired:
            return
        descriptor = self._descriptor
        self._descriptor = -1
        self.acquired = False
        try:
            self._unlock(descriptor)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
