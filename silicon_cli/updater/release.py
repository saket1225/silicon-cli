"""Immutable release identities, artifacts, and safe extraction."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import ssl
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import certifi

from .io import (
    UnsafePathError,
    atomic_write_json,
    ensure_real_directory,
    fsync_dir,
    hash_tree,
    sha256_file,
    validate_relative_path,
)

RELEASE_MANIFEST = "silicon-release.json"
TREE_EXCLUDED_NAMES = {RELEASE_MANIFEST}
TREE_EXCLUDED_PREFIXES = {".git"}
MAX_RELEASE_FILES = 100_000
MAX_RELEASE_FILE_BYTES = 256 * 1024 * 1024
MAX_RELEASE_TREE_BYTES = 512 * 1024 * 1024
SIGNED_VERSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}\Z")
RUNTIME_IMAGE_RE = re.compile(
    r"\A[a-z0-9][a-z0-9._:/-]{0,438}@sha256:[0-9a-f]{64}\Z"
)


def runtime_image_is_pinned(value: object) -> bool:
    """Return whether ``value`` is an immutable OCI image reference."""

    if not isinstance(value, str):
        return False
    return bool(
        RUNTIME_IMAGE_RE.fullmatch(value)
        and "://" not in value
        and "//" not in value
        and "/../" not in f"/{value.split('@', 1)[0]}/"
    )


class ReleaseVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseIdentity:
    version: str
    revision: str
    sequence: int
    tree_sha256: str
    artifact_sha256: str
    source: str
    trust: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "ReleaseIdentity":
        if not isinstance(value, dict):
            raise ReleaseVerificationError("release identity must be an object")
        required = {
            "version",
            "revision",
            "sequence",
            "tree_sha256",
            "artifact_sha256",
            "source",
            "trust",
        }
        missing = required - set(value)
        if missing:
            raise ReleaseVerificationError(
                f"release identity is missing: {', '.join(sorted(missing))}"
            )
        if set(value) != required:
            raise ReleaseVerificationError("release identity has unknown fields")
        sequence = value["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ReleaseVerificationError("release sequence must be a nonnegative integer")
        text_fields = {
            name: value[name]
            for name in ("version", "revision", "source", "trust")
        }
        if any(
            not isinstance(item, str)
            or not item
            or len(item) > 512
            or "\x00" in item
            for item in text_fields.values()
        ):
            raise ReleaseVerificationError("release identity has invalid text fields")
        identity = cls(
            version=text_fields["version"],
            revision=text_fields["revision"],
            sequence=sequence,
            tree_sha256=str(value["tree_sha256"]),
            artifact_sha256=str(value["artifact_sha256"]),
            source=text_fields["source"],
            trust=text_fields["trust"],
        )
        for label, digest in (
            ("tree", identity.tree_sha256),
            ("artifact", identity.artifact_sha256),
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ReleaseVerificationError(f"invalid {label} SHA-256")
        return identity


@dataclass(frozen=True)
class ReleaseManifest:
    identity: ReleaseIdentity
    files: dict[str, dict[str, object]]
    runtime_image: str = ""
    schema: int = 1

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "identity": self.identity.to_dict(),
            "files": self.files,
            "runtime_image": self.runtime_image,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ReleaseManifest":
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "identity",
            "files",
            "runtime_image",
        }:
            raise ReleaseVerificationError(
                "release manifest has unknown or missing fields"
            )
        if (
            not isinstance(value.get("schema"), int)
            or isinstance(value.get("schema"), bool)
            or value.get("schema") != 1
        ):
            raise ReleaseVerificationError("unsupported release manifest schema")
        files = value.get("files")
        if not isinstance(files, dict) or not files:
            raise ReleaseVerificationError(
                "release manifest files must be a non-empty object"
            )
        if len(files) > MAX_RELEASE_FILES:
            raise ReleaseVerificationError("release manifest has too many files")
        declared_total = 0
        for rel, metadata in files.items():
            if not isinstance(rel, str):
                raise ReleaseVerificationError(
                    "release manifest paths must be strings"
                )
            try:
                validate_relative_path(rel)
            except (TypeError, ValueError) as exc:
                raise ReleaseVerificationError(
                    f"invalid release manifest path: {rel!r}"
                ) from exc
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"sha256", "size", "mode"}
            ):
                raise ReleaseVerificationError(f"invalid metadata for {rel}")
            digest = metadata["sha256"]
            size = metadata["size"]
            mode = metadata["mode"]
            if (
                not isinstance(digest, str)
                or
                len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > MAX_RELEASE_FILE_BYTES
                or not isinstance(mode, int)
                or isinstance(mode, bool)
                or mode < 0
                or mode > 0o777
            ):
                raise ReleaseVerificationError(f"invalid metadata for {rel}")
            declared_total += size
            if declared_total > MAX_RELEASE_TREE_BYTES:
                raise ReleaseVerificationError(
                    "release manifest exceeds the unpacked size limit"
                )
        identity = ReleaseIdentity.from_dict(value.get("identity") or {})
        runtime_image = value.get("runtime_image")
        if identity.trust == "signed-ed25519":
            if not runtime_image_is_pinned(runtime_image):
                raise ReleaseVerificationError(
                    "signed release has no immutable runtime image digest"
                )
        elif runtime_image not in {"", None}:
            if not runtime_image_is_pinned(runtime_image):
                raise ReleaseVerificationError(
                    "release runtime image is not an immutable digest reference"
                )
        return cls(
            identity=identity,
            files=files,
            runtime_image=str(runtime_image or ""),
        )

    @classmethod
    def read(cls, path: Path) -> "ReleaseManifest":
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseVerificationError(f"invalid release manifest {path}: {exc}") from exc

    def write(self, path: Path) -> None:
        atomic_write_json(path, self.to_dict(), mode=0o644)


@dataclass(frozen=True)
class FetchedRelease:
    manifest: ReleaseManifest
    artifact: Path


class HttpManifestReleaseSource:
    """Fetch a publisher-signed manifest and its immutable artifact.

    Wire contract (schema 1)::

        {
          "schema": 1,
          "signed": {
            "identity": {ReleaseIdentity fields},
            "files": {"relative/path": {"sha256", "size", "mode"}},
            "artifact_url": "https://...",
            "artifact_size": 123,
            "runtime_image": "registry/repository@sha256:<digest>",
            "expires_at": 1780000000
          },
          "signatures": [
            {"key_id": "release-2026", "algorithm": "ed25519",
             "signature": "<base64>"}
          ]
        }

    ``verifier`` owns trusted root keys and must raise unless a signature over
    the canonical ``signed`` JSON is valid.  This keeps key rotation policy out
    of the transport and makes an unsigned HTTP response impossible to accept.
    """

    def __init__(
        self,
        manifest_url: str,
        *,
        verifier: Callable[[bytes, list[dict]], None],
        opener: Callable[..., object] | None = None,
        max_manifest_bytes: int = 4 * 1024 * 1024,
        max_artifact_bytes: int = 1024 * 1024 * 1024,
        max_manifest_ttl_seconds: int = 3600,
    ):
        parsed = urllib.parse.urlsplit(manifest_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ReleaseVerificationError(
                "signed release manifest URL must use HTTPS"
            )
        self.manifest_url = manifest_url
        self._manifest_origin = self._origin(manifest_url)
        self.verifier = verifier
        self.opener = opener or urllib.request.urlopen
        self.max_manifest_bytes = max_manifest_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.max_manifest_ttl_seconds = max_manifest_ttl_seconds
        self._tls_context = ssl.create_default_context(cafile=certifi.where())

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        parsed = urllib.parse.urlsplit(url)
        return (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower().rstrip("."),
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
        )

    def _open(self, url: str, *, timeout: int):
        try:
            response = self.opener(url, timeout=timeout, context=self._tls_context)
        except urllib.error.HTTPError as exc:
            raise ReleaseVerificationError(
                f"release server returned HTTP {exc.code}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ReleaseVerificationError(
                f"could not reach the signed release server: {exc}"
            ) from exc
        final_url = response.geturl() if hasattr(response, "geturl") else url
        if (
            self._origin(str(final_url)) != self._manifest_origin
            or urllib.parse.urlsplit(str(final_url)).scheme.lower() != "https"
        ):
            raise ReleaseVerificationError(
                "release server redirected outside its pinned HTTPS origin"
            )
        return response

    @staticmethod
    def _close_response(response: object) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # Closing a completed/failed response must not replace the
                # verification result that caused the cleanup.
                pass

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def fetch(self, staging: Path) -> FetchedRelease:
        response = self._open(self.manifest_url, timeout=15)
        try:
            try:
                raw = response.read(self.max_manifest_bytes + 1)
            except Exception as exc:
                raise ReleaseVerificationError(
                    f"could not read the signed release manifest: {exc}"
                ) from exc
        finally:
            self._close_response(response)
        if len(raw) > self.max_manifest_bytes:
            raise ReleaseVerificationError("release manifest exceeds size limit")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseVerificationError("release manifest is not valid JSON") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema", "signed", "signatures"}
            or envelope.get("schema") != 1
            or not isinstance(envelope.get("signed"), dict)
            or not isinstance(envelope.get("signatures"), list)
            or not envelope["signatures"]
        ):
            raise ReleaseVerificationError(
                "release manifest envelope has invalid fields"
            )
        signed = envelope["signed"]
        self.verifier(self._canonical(signed), envelope["signatures"])
        expected_fields = {
            "identity",
            "files",
            "artifact_url",
            "artifact_size",
            "runtime_image",
            "expires_at",
        }
        if set(signed) != expected_fields:
            raise ReleaseVerificationError(
                "signed release metadata has unknown or missing fields"
            )
        expires_at = signed["expires_at"]
        now = int(time.time())
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= now
            or expires_at > now + self.max_manifest_ttl_seconds
        ):
            raise ReleaseVerificationError(
                "signed release metadata has an invalid expiry"
            )
        artifact_url = str(signed["artifact_url"])
        parsed_artifact = urllib.parse.urlsplit(artifact_url)
        if (
            parsed_artifact.scheme.lower() != "https"
            or not parsed_artifact.netloc
            or parsed_artifact.username is not None
            or parsed_artifact.password is not None
            or parsed_artifact.fragment
            or parsed_artifact.query
            or self._origin(artifact_url) != self._manifest_origin
        ):
            raise ReleaseVerificationError(
                "release artifact URL must use the manifest's pinned HTTPS origin"
            )
        artifact_size = signed["artifact_size"]
        if (
            not isinstance(artifact_size, int)
            or isinstance(artifact_size, bool)
            or artifact_size < 0
            or artifact_size > self.max_artifact_bytes
        ):
            raise ReleaseVerificationError("release artifact size is invalid")
        identity = ReleaseIdentity.from_dict(signed["identity"])
        manifest = ReleaseManifest.from_dict(
            {
                "schema": 1,
                "identity": identity.to_dict(),
                "files": signed["files"],
                "runtime_image": signed["runtime_image"],
            }
        )
        if (
            manifest.identity.trust != "signed-ed25519"
            or manifest.identity.source != "glass"
            or manifest.identity.sequence <= 0
            or SIGNED_VERSION_RE.fullmatch(manifest.identity.version) is None
            or len(manifest.identity.revision) != 64
            or any(
                char not in "0123456789abcdef"
                for char in manifest.identity.revision
            )
        ):
            raise ReleaseVerificationError(
                "signed release identity has an unexpected trust boundary"
            )
        expected_artifact_path = (
            "/api/v1/silicon-release/artifacts/"
            f"{manifest.identity.tree_sha256}.tar"
        )
        if parsed_artifact.path != expected_artifact_path:
            raise ReleaseVerificationError(
                "signed release artifact URL does not match its tree identity"
            )
        if len(manifest.files) > MAX_RELEASE_FILES:
            raise ReleaseVerificationError("release manifest has too many files")
        declared_total = 0
        for metadata in manifest.files.values():
            size = int(metadata["size"])
            if size > MAX_RELEASE_FILE_BYTES:
                raise ReleaseVerificationError(
                    "release manifest contains an oversized file"
                )
            declared_total += size
            if declared_total > MAX_RELEASE_TREE_BYTES:
                raise ReleaseVerificationError(
                    "release manifest exceeds the unpacked size limit"
                )
        staging.mkdir(parents=True, exist_ok=True)
        artifact = staging / f"{identity.tree_sha256}.tar"
        temporary = artifact.with_name(f".{artifact.name}.{os.getpid()}.tmp")
        digest = hashlib.sha256()
        received = 0
        try:
            source = self._open(artifact_url, timeout=60)
            try:
                with temporary.open("wb") as output:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        received += len(block)
                        if (
                            received > artifact_size
                            or received > self.max_artifact_bytes
                        ):
                            raise ReleaseVerificationError(
                                "release artifact exceeded its signed size"
                            )
                        output.write(block)
                        digest.update(block)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                self._close_response(source)
            if received != artifact_size:
                raise ReleaseVerificationError(
                    "release artifact size does not match signed metadata"
                )
            if digest.hexdigest() != identity.artifact_sha256:
                raise ReleaseVerificationError(
                    "release artifact does not match signed SHA-256"
                )
            os.replace(temporary, artifact)
            fsync_dir(artifact.parent)
        finally:
            temporary.unlink(missing_ok=True)
        # Extraction rechecks the artifact, every declared member, and tree hash.
        verification_dir = staging / f".verify-{identity.tree_sha256}"
        safe_extract(artifact, verification_dir, manifest)
        shutil.rmtree(verification_dir, ignore_errors=True)
        return FetchedRelease(manifest, artifact)


def _manifest_metadata(source: Path) -> tuple[str, int, str]:
    declared = source / RELEASE_MANIFEST
    if not declared.exists():
        return "", 0, ""
    try:
        value = json.loads(declared.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(f"invalid {RELEASE_MANIFEST}: {exc}") from exc
    identity = value.get("identity") if isinstance(value, dict) else None
    if not isinstance(identity, dict):
        raise ReleaseVerificationError(f"{RELEASE_MANIFEST} has no identity")
    return (
        str(identity.get("version", "")),
        int(identity.get("sequence", 0)),
        str(identity.get("tree_sha256", "")),
    )


def create_artifact(
    source: Path,
    destination: Path,
    *,
    revision: str,
    source_label: str,
    trust: str,
) -> FetchedRelease:
    """Create a deterministic archive and verify any publisher tree digest.

    This is the explicit compatibility boundary for today's Git repository.
    When the repository has no signed publisher manifest, its exact fetched Git
    revision and locally derived tree hash are recorded as
    ``derived-local-git``.  That proves artifact integrity after download but
    does not claim publisher authenticity.
    """

    source = source.resolve(strict=True)
    tree_digest, files = hash_tree(
        source,
        excluded_prefixes=TREE_EXCLUDED_PREFIXES,
        excluded_names=TREE_EXCLUDED_NAMES,
    )
    version, sequence, declared_tree = _manifest_metadata(source)
    if declared_tree and declared_tree != tree_digest:
        raise ReleaseVerificationError(
            f"publisher tree digest mismatch: expected {declared_tree}, got {tree_digest}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
            for rel, metadata in sorted(files.items()):
                path = source / rel
                info = tarfile.TarInfo(rel)
                info.size = int(metadata["size"])
                info.mode = int(metadata["mode"])
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        artifact_digest = sha256_file(temporary)
        os.replace(temporary, destination)
        fsync_dir(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    identity = ReleaseIdentity(
        version=version or revision[:12] or tree_digest[:12],
        revision=revision or tree_digest,
        sequence=sequence,
        tree_sha256=tree_digest,
        artifact_sha256=artifact_digest,
        source=source_label,
        trust=trust,
    )
    return FetchedRelease(ReleaseManifest(identity, files), destination)


def verify_artifact(artifact: Path, manifest: ReleaseManifest) -> None:
    actual = sha256_file(artifact)
    expected = manifest.identity.artifact_sha256
    if actual != expected:
        raise ReleaseVerificationError(
            f"release artifact SHA-256 mismatch: expected {expected}, got {actual}"
        )


def safe_extract(
    artifact: Path, destination: Path, manifest: Optional[ReleaseManifest] = None
) -> None:
    """Extract only declared regular files into a new empty directory."""

    if manifest is not None:
        verify_artifact(artifact, manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        try:
            mode = os.lstat(destination).st_mode
        except OSError as exc:
            raise ReleaseVerificationError(
                f"could not inspect extraction destination {destination}: {exc}"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ReleaseVerificationError(
                f"refusing unsafe extraction destination {destination}"
            )
        if any(destination.iterdir()):
            raise ReleaseVerificationError(
                f"refusing to extract into non-empty {destination}"
            )
    else:
        try:
            os.mkdir(destination, 0o700)
            fsync_dir(destination.parent)
        except FileExistsError:
            raise ReleaseVerificationError(
                f"extraction destination changed while being created: {destination}"
            )
    mode = os.lstat(destination).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReleaseVerificationError(
            f"refusing unsafe extraction destination {destination}"
        )
    destination_root = destination.resolve(strict=True)
    seen: set[str] = set()
    try:
        with tarfile.open(artifact, "r:") as archive:
            for member in archive:
                rel_path = validate_relative_path(member.name)
                rel = rel_path.as_posix()
                if rel in seen:
                    raise ReleaseVerificationError(f"duplicate archive member: {rel}")
                seen.add(rel)
                if not member.isfile():
                    raise ReleaseVerificationError(
                        f"release archive contains a link or special entry: {rel}"
                    )
                if manifest is not None and rel not in manifest.files:
                    raise ReleaseVerificationError(
                        f"release archive contains undeclared file: {rel}"
                    )
                if manifest is not None:
                    expected = manifest.files[rel]
                    if (
                        member.size != expected["size"]
                        or member.mode & 0o777 != expected["mode"]
                    ):
                        raise ReleaseVerificationError(
                            f"archive member metadata does not match manifest: {rel}"
                        )
                # Build every target from the canonical root. On macOS,
                # TemporaryDirectory may spell the same directory through
                # /var while resolve() returns /private/var; mixing those
                # spellings would make the confinement check fail closed.
                target = destination_root / rel_path
                ensure_real_directory(
                    target.parent,
                    root=destination_root,
                )
                resolved_parent = target.parent.resolve(strict=True)
                if destination_root not in (
                    resolved_parent,
                    *resolved_parent.parents,
                ):
                    raise UnsafePathError(f"archive path escaped destination: {rel}")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ReleaseVerificationError(f"could not read archive member: {rel}")
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
                )
                try:
                    with os.fdopen(fd, "wb") as output:
                        fd = -1
                        shutil.copyfileobj(extracted, output)
                        output.flush()
                        os.chmod(tmp_name, member.mode & 0o777)
                        os.fsync(output.fileno())
                    os.replace(tmp_name, target)
                    fsync_dir(target.parent)
                finally:
                    if fd >= 0:
                        os.close(fd)
                    Path(tmp_name).unlink(missing_ok=True)
        if manifest is not None:
            if seen != set(manifest.files):
                missing = sorted(set(manifest.files) - seen)
                raise ReleaseVerificationError(
                    f"release archive is missing declared files: {', '.join(missing[:5])}"
                )
            actual_tree, actual_files = hash_tree(destination)
            if actual_tree != manifest.identity.tree_sha256:
                raise ReleaseVerificationError(
                    "extracted release tree does not match its immutable identity"
                )
            for rel, expected in manifest.files.items():
                if actual_files.get(rel) != expected:
                    raise ReleaseVerificationError(
                        f"extracted file metadata does not match manifest: {rel}"
                    )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def fetch_git_release(
    git_url: str,
    cache_staging: Path,
    *,
    ref: str = "refs/heads/main",
) -> FetchedRelease:
    """Resolve once, fetch that exact commit, then create a verified artifact."""

    if shutil.which("git") is None:
        raise ReleaseVerificationError(
            "Git is required for the current unsigned repository release source"
        )
    query = subprocess.run(
        ["git", "ls-remote", git_url, ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if query.returncode != 0 or not query.stdout.strip():
        raise ReleaseVerificationError(
            f"could not resolve exact release revision for {git_url}: "
            f"{query.stderr.strip() or 'ref not found'}"
        )
    revision = query.stdout.split()[0]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision.lower()):
        raise ReleaseVerificationError("Git returned an invalid release revision")
    checkout = Path(tempfile.mkdtemp(prefix="silicon-release-checkout-"))
    try:
        commands = [
            ["git", "init", "-q", str(checkout)],
            ["git", "-C", str(checkout), "remote", "add", "origin", git_url],
            ["git", "-C", str(checkout), "fetch", "-q", "--depth", "1", "origin", revision],
            ["git", "-C", str(checkout), "checkout", "-q", "--detach", "FETCH_HEAD"],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode:
                raise ReleaseVerificationError(
                    f"failed to fetch exact release {revision}: {result.stderr.strip()}"
                )
        shutil.rmtree(checkout / ".git", ignore_errors=True)
        cache_staging.mkdir(parents=True, exist_ok=True)
        artifact = cache_staging / f"{revision}.tar"
        return create_artifact(
            checkout,
            artifact,
            revision=revision,
            source_label=f"{git_url}@{revision}",
            trust="derived-local-git",
        )
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
