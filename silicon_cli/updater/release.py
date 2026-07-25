"""Immutable release identities, artifacts, and safe extraction."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

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
MAX_PUBLISHED_GIT_REFS = 100_000
MAX_PUBLISHED_GIT_METADATA_BYTES = 16 * 1024 * 1024
RUNTIME_IMAGE_RE = re.compile(
    r"\A[a-z0-9][a-z0-9._:/-]{0,438}@sha256:[0-9a-f]{64}\Z"
)
PUBLISHED_GIT_TAG_RE = re.compile(
    r"\Av(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})\Z"
)
PUBLISHED_GIT_TRUST = "git-semver-tag"
LEGACY_PUBLISHED_TRUST = "signed-ed25519"
PUBLISHED_GIT_SOURCE_RE = re.compile(
    r"\Agit\+https://github\.com/"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git"
    r"@refs/tags/(?P<tag>v(?:0|[1-9][0-9]{0,2})"
    r"\.(?:0|[1-9][0-9]{0,2})"
    r"\.(?:0|[1-9][0-9]{0,2}))"
    r"#[0-9a-f]{40}\Z"
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
class PublishedGitRelease:
    """One advertised stable Git tag resolved to an immutable commit."""

    tag: str
    version: str
    sequence: int
    tag_object: str
    revision: str


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
        if identity.trust in {LEGACY_PUBLISHED_TRUST, PUBLISHED_GIT_TRUST}:
            if not runtime_image_is_pinned(runtime_image):
                raise ReleaseVerificationError(
                    "published release has no immutable runtime image digest"
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


def release_identity_is_authoritative(identity: ReleaseIdentity) -> bool:
    """Whether an identity may raise the durable anti-rollback floor.

    Git-published stable tags are the active release authority. The legacy
    Glass identity remains accepted only so installations that already
    recorded its floor can migrate without weakening rollback protection.
    """

    if identity.sequence <= 0:
        return False
    if identity.trust == PUBLISHED_GIT_TRUST:
        match = PUBLISHED_GIT_SOURCE_RE.fullmatch(identity.source)
        if match is None:
            return False
        _parts, version, sequence = _published_tag_version(match.group("tag"))
        return (
            identity.version == version
            and identity.sequence == sequence
            and len(identity.revision) == 40
            and all(
                char in "0123456789abcdef"
                for char in identity.revision
            )
        )
    return (
        identity.trust == LEGACY_PUBLISHED_TRUST
        and identity.source == "glass"
    )


def stable_release_version_parts(
    value: object,
    *,
    allow_legacy_two_part: bool = False,
) -> tuple[int, int, int] | None:
    """Return comparable stable-version parts, or ``None``."""

    if not isinstance(value, str):
        return None
    text = value.removeprefix("v")
    pieces = text.split(".")
    if allow_legacy_two_part and len(pieces) == 2:
        pieces.append("0")
    if len(pieces) != 3:
        return None
    tag = "v" + ".".join(pieces)
    match = PUBLISHED_GIT_TAG_RE.fullmatch(tag)
    if match is None:
        return None
    return tuple(int(piece) for piece in match.groups())


def release_floor_from_identity(
    identity: ReleaseIdentity,
    *,
    recorded_at: float,
) -> dict[str, object]:
    """Create the version-aware durable floor written by current clients."""

    if not release_identity_is_authoritative(identity):
        raise ReleaseVerificationError(
            "cannot create a floor from a non-published release"
        )
    return {
        "schema": 2,
        "sequence": identity.sequence,
        "version": identity.version,
        "trust": identity.trust,
        "tree_sha256": identity.tree_sha256,
        "recorded_at": recorded_at,
    }


def validate_release_floor(value: object) -> dict[str, object]:
    """Validate old sequence-only floors and current version-aware floors."""

    if not isinstance(value, dict):
        raise ReleaseVerificationError("release floor must be an object")
    schema = value.get("schema")
    expected = (
        {"schema", "sequence", "tree_sha256", "recorded_at"}
        if schema == 1
        else {
            "schema",
            "sequence",
            "version",
            "trust",
            "tree_sha256",
            "recorded_at",
        }
    )
    sequence = value.get("sequence")
    tree_sha256 = value.get("tree_sha256")
    recorded_at = value.get("recorded_at")
    if (
        schema not in {1, 2}
        or set(value) != expected
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence <= 0
        or not isinstance(tree_sha256, str)
        or len(tree_sha256) != 64
        or any(character not in "0123456789abcdef" for character in tree_sha256)
        or not isinstance(recorded_at, (int, float))
        or isinstance(recorded_at, bool)
        or not math.isfinite(float(recorded_at))
        or float(recorded_at) <= 0
    ):
        raise ReleaseVerificationError("release floor is invalid")
    if schema == 2:
        version = value.get("version")
        trust = value.get("trust")
        if (
            not isinstance(version, str)
            or not version
            or len(version) > 64
            or "\x00" in version
            or trust not in {PUBLISHED_GIT_TRUST, LEGACY_PUBLISHED_TRUST}
        ):
            raise ReleaseVerificationError("release floor is invalid")
        if trust == PUBLISHED_GIT_TRUST:
            parts = stable_release_version_parts(version)
            if parts is None:
                raise ReleaseVerificationError("Git release floor version is invalid")
            expected_sequence = (
                parts[0] * 1_000_000
                + parts[1] * 1_000
                + parts[2]
                + 1
            )
            if sequence != expected_sequence:
                raise ReleaseVerificationError(
                    "Git release floor sequence does not match its version"
                )
    return dict(value)


def compare_release_floors(
    candidate: object,
    current: object,
) -> int:
    """Compare two floors without mixing unrelated sequence namespaces.

    Returns a negative value when ``candidate`` is older, zero when it is the
    same immutable release, and a positive value when it is newer.
    """

    candidate_floor = validate_release_floor(candidate)
    current_floor = validate_release_floor(current)
    candidate_version = (
        stable_release_version_parts(
            candidate_floor.get("version"),
            allow_legacy_two_part=True,
        )
        if candidate_floor["schema"] == 2
        else None
    )
    current_version = (
        stable_release_version_parts(
            current_floor.get("version"),
            allow_legacy_two_part=True,
        )
        if current_floor["schema"] == 2
        else None
    )
    if candidate_version is not None and current_version is not None:
        order = (candidate_version > current_version) - (
            candidate_version < current_version
        )
    else:
        candidate_trust = candidate_floor.get("trust")
        current_trust = current_floor.get("trust")
        comparable_legacy_sequence = (
            candidate_trust in {None, LEGACY_PUBLISHED_TRUST}
            and current_trust in {None, LEGACY_PUBLISHED_TRUST}
        )
        if not comparable_legacy_sequence:
            raise ReleaseVerificationError(
                "legacy release floor has no comparable published version"
            )
        candidate_sequence = int(candidate_floor["sequence"])
        current_sequence = int(current_floor["sequence"])
        order = (candidate_sequence > current_sequence) - (
            candidate_sequence < current_sequence
        )
    if (
        order == 0
        and candidate_floor["tree_sha256"] != current_floor["tree_sha256"]
    ):
        raise ReleaseVerificationError(
            "release version or sequence was reused for different immutable content"
        )
    return order


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
    version: str | None = None,
    sequence: int | None = None,
    runtime_image: str = "",
    canonical_modes: dict[str, int] | None = None,
) -> FetchedRelease:
    """Create a deterministic archive and verify declared release metadata."""

    source = source.resolve(strict=True)
    tree_digest, files = hash_tree(
        source,
        excluded_prefixes=TREE_EXCLUDED_PREFIXES,
        excluded_names=TREE_EXCLUDED_NAMES,
    )
    if canonical_modes is not None:
        if set(canonical_modes) != set(files):
            raise ReleaseVerificationError(
                "published Git inventory does not match the materialized tree"
            )
        for rel, mode in canonical_modes.items():
            if mode not in {0o644, 0o755}:
                raise ReleaseVerificationError(
                    f"published Git file has an invalid mode: {rel}"
                )
            files[rel]["mode"] = mode
        tree = hashlib.sha256()
        for rel, metadata in sorted(files.items()):
            encoded = rel.encode("utf-8")
            tree.update(len(encoded).to_bytes(8, "big"))
            tree.update(encoded)
            tree.update(int(metadata["mode"]).to_bytes(4, "big"))
            tree.update(int(metadata["size"]).to_bytes(8, "big"))
            tree.update(bytes.fromhex(str(metadata["sha256"])))
        tree_digest = tree.hexdigest()
    declared_version, declared_sequence, declared_tree = _manifest_metadata(source)
    if declared_tree and declared_tree != tree_digest:
        raise ReleaseVerificationError(
            f"publisher tree digest mismatch: expected {declared_tree}, got {tree_digest}"
        )
    if version is not None and declared_version and declared_version != version:
        raise ReleaseVerificationError(
            "publisher version metadata does not match the resolved release"
        )
    if (
        sequence is not None
        and declared_sequence
        and declared_sequence != sequence
    ):
        raise ReleaseVerificationError(
            "publisher sequence metadata does not match the resolved release"
        )
    selected_version = version or declared_version or revision[:12] or tree_digest[:12]
    selected_sequence = declared_sequence if sequence is None else sequence
    if (
        not isinstance(selected_sequence, int)
        or isinstance(selected_sequence, bool)
        or selected_sequence < 0
    ):
        raise ReleaseVerificationError("release sequence is invalid")
    if runtime_image and not runtime_image_is_pinned(runtime_image):
        raise ReleaseVerificationError(
            "release runtime image is not an immutable digest reference"
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
    identity = ReleaseIdentity.from_dict(
        {
            "version": selected_version,
            "revision": revision or tree_digest,
            "sequence": selected_sequence,
            "tree_sha256": tree_digest,
            "artifact_sha256": artifact_digest,
            "source": source_label,
            "trust": trust,
        }
    )
    return FetchedRelease(
        ReleaseManifest(identity, files, runtime_image=runtime_image),
        destination,
    )


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


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "GIT_ALLOW_PROTOCOL",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PROXY_COMMAND",
        "GIT_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_NO_VERIFY",
        "GIT_TEMPLATE_DIR",
        "GIT_WORK_TREE",
        "SSH_ASKPASS",
    ):
        environment.pop(key, None)
    for key in tuple(environment):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "7",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": os.devnull,
            "GIT_CONFIG_KEY_2": "core.autocrlf",
            "GIT_CONFIG_VALUE_2": "false",
            "GIT_CONFIG_KEY_3": "core.eol",
            "GIT_CONFIG_VALUE_3": "lf",
            "GIT_CONFIG_KEY_4": "http.sslVerify",
            "GIT_CONFIG_VALUE_4": "true",
            "GIT_CONFIG_KEY_5": "protocol.allow",
            "GIT_CONFIG_VALUE_5": "never",
            "GIT_CONFIG_KEY_6": "protocol.https.allow",
            "GIT_CONFIG_VALUE_6": "always",
        }
    )
    return environment


def _run_git(
    command: list[str],
    *,
    timeout: int = 45,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_git_environment(),
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseVerificationError("Git release resolution timed out") from exc
    except UnicodeError as exc:
        raise ReleaseVerificationError(
            "Git returned non-UTF-8 release metadata"
        ) from exc
    except OSError as exc:
        raise ReleaseVerificationError(f"could not run Git: {exc}") from exc


def _published_tag_version(tag: str) -> tuple[tuple[int, int, int], str, int]:
    match = PUBLISHED_GIT_TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseVerificationError(f"invalid published Git tag: {tag!r}")
    parts = tuple(int(value) for value in match.groups())
    version = ".".join(str(value) for value in parts)
    sequence = parts[0] * 1_000_000 + parts[1] * 1_000 + parts[2] + 1
    return parts, version, sequence


def resolve_latest_published_git_release(git_url: str) -> PublishedGitRelease:
    """Resolve the highest stable SemVer tag advertised by the remote.

    No branch, timestamp, GitHub Release object, prerelease, or older-tag
    fallback participates in this decision.
    """

    if shutil.which("git") is None:
        raise ReleaseVerificationError(
            "Git is required to resolve published Silicon releases"
        )
    query = _run_git(["git", "ls-remote", "--tags", git_url])
    if query.returncode != 0:
        raise ReleaseVerificationError(
            f"could not list published releases for {git_url}: "
            f"{query.stderr.strip() or 'Git exited unsuccessfully'}"
        )
    if len(query.stdout.encode("utf-8")) > MAX_PUBLISHED_GIT_METADATA_BYTES:
        raise ReleaseVerificationError(
            "Git returned too much published-release metadata"
        )
    references: dict[str, str] = {}
    for raw_line in query.stdout.splitlines():
        if len(references) >= MAX_PUBLISHED_GIT_REFS:
            raise ReleaseVerificationError(
                "Git returned too many published-release references"
            )
        fields = raw_line.split()
        if len(fields) != 2:
            raise ReleaseVerificationError(
                "Git returned malformed published-release metadata"
            )
        object_id, reference = fields
        object_id = object_id.lower()
        if (
            len(object_id) != 40
            or any(char not in "0123456789abcdef" for char in object_id)
            or not reference.startswith("refs/tags/")
        ):
            raise ReleaseVerificationError(
                "Git returned invalid published-release metadata"
            )
        previous = references.setdefault(reference, object_id)
        if previous != object_id:
            raise ReleaseVerificationError(
                f"Git advertised conflicting objects for {reference}"
            )
    for reference in references:
        if reference.endswith("^{}") and reference[:-3] not in references:
            raise ReleaseVerificationError(
                f"Git advertised an orphan peeled tag: {reference}"
            )

    candidates: list[tuple[tuple[int, int, int], str, str, int, str, str]] = []
    for reference, tag_object in references.items():
        if reference.endswith("^{}"):
            continue
        tag = reference.removeprefix("refs/tags/")
        match = PUBLISHED_GIT_TAG_RE.fullmatch(tag)
        if match is None:
            continue
        parts, version, sequence = _published_tag_version(tag)
        revision = references.get(f"{reference}^{{}}", tag_object)
        candidates.append(
            (parts, tag, version, sequence, tag_object, revision)
        )
    if not candidates:
        raise ReleaseVerificationError(
            "the Stemcell repository has no published stable vMAJOR.MINOR.PATCH tag"
        )
    _parts, tag, version, sequence, tag_object, revision = max(
        candidates,
        key=lambda item: item[0],
    )
    return PublishedGitRelease(
        tag=tag,
        version=version,
        sequence=sequence,
        tag_object=tag_object,
        revision=revision,
    )


def _published_stemcell_metadata(
    checkout: Path,
    release: PublishedGitRelease,
) -> str:
    path = checkout / "silicon.info"
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseVerificationError(
            "published Stemcell release has no silicon.info"
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > 64 * 1024
    ):
        raise ReleaseVerificationError(
            "published Stemcell silicon.info is not a bounded regular file"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(
            "published Stemcell silicon.info is invalid"
        ) from exc
    if not isinstance(value, dict) or value.get("version") != release.version:
        raise ReleaseVerificationError(
            "published Stemcell version does not match its Git tag"
        )
    runtime_image = value.get("runtime_image")
    if not runtime_image_is_pinned(runtime_image):
        raise ReleaseVerificationError(
            "published Stemcell has no immutable runtime_image digest"
        )
    return str(runtime_image)


def _verify_checkout_matches_git_tree(
    checkout: Path,
) -> dict[str, int]:
    """Verify every materialized byte against the fetched Git index.

    Global/system Git configuration is disabled before checkout. This second
    check also catches repository attributes that transform content or modes,
    and rejects links, submodules, special files, undeclared files, and
    oversized trees.
    """

    inventory = _run_git(
        ["git", "-C", str(checkout), "ls-files", "--stage", "-z"]
    )
    if inventory.returncode:
        raise ReleaseVerificationError(
            "could not inventory the published Git tree"
        )
    raw_inventory = inventory.stdout
    if len(raw_inventory.encode("utf-8")) > MAX_PUBLISHED_GIT_METADATA_BYTES:
        raise ReleaseVerificationError(
            "published Git tree inventory is too large"
        )

    indexed: dict[str, tuple[str, int]] = {}
    declared_total = 0
    for raw_entry in raw_inventory.split("\x00"):
        if not raw_entry:
            continue
        if len(indexed) >= MAX_RELEASE_FILES:
            raise ReleaseVerificationError(
                "published Git tree contains too many files"
            )
        try:
            metadata, rel = raw_entry.split("\t", 1)
            mode_text, object_id, stage = metadata.split()
        except ValueError as exc:
            raise ReleaseVerificationError(
                "Git returned malformed tree inventory"
            ) from exc
        if (
            stage != "0"
            or mode_text not in {"100644", "100755"}
            or len(object_id) != 40
            or any(character not in "0123456789abcdef" for character in object_id)
            or "\r" in rel
            or "\n" in rel
            or rel == RELEASE_MANIFEST
        ):
            raise ReleaseVerificationError(
                f"published Git tree has an unsupported entry: {rel!r}"
            )
        try:
            validate_relative_path(rel)
        except (TypeError, ValueError, UnsafePathError) as exc:
            raise ReleaseVerificationError(
                f"published Git tree has an unsafe path: {rel!r}"
            ) from exc
        if rel in indexed:
            raise ReleaseVerificationError(
                f"published Git tree repeats a path: {rel}"
            )
        path = checkout / rel
        try:
            file_metadata = path.lstat()
        except OSError as exc:
            raise ReleaseVerificationError(
                f"published Git file is unavailable: {rel}"
            ) from exc
        if path.is_symlink() or not stat.S_ISREG(file_metadata.st_mode):
            raise ReleaseVerificationError(
                f"published Git tree entry is not a regular file: {rel}"
            )
        if file_metadata.st_size > MAX_RELEASE_FILE_BYTES:
            raise ReleaseVerificationError(
                f"published Git file is too large: {rel}"
            )
        declared_total += file_metadata.st_size
        if declared_total > MAX_RELEASE_TREE_BYTES:
            raise ReleaseVerificationError(
                "published Git tree exceeds the unpacked size limit"
            )
        indexed[rel] = (
            object_id,
            0o755 if mode_text == "100755" else 0o644,
        )
    if not indexed:
        raise ReleaseVerificationError("published Git tree contains no files")

    try:
        _tree_digest, actual_files = hash_tree(
            checkout,
            excluded_prefixes=TREE_EXCLUDED_PREFIXES,
            excluded_names=TREE_EXCLUDED_NAMES,
        )
    except (OSError, UnsafePathError) as exc:
        raise ReleaseVerificationError(
            "materialized checkout contains an unsafe file"
        ) from exc
    if set(actual_files) != set(indexed):
        raise ReleaseVerificationError(
            "materialized checkout contains files outside the published Git tree"
        )
    object_hashes = _run_git(
        [
            "git",
            "-C",
            str(checkout),
            "hash-object",
            "--no-filters",
            "--stdin-paths",
        ],
        input_text="".join(f"{rel}\n" for rel in indexed),
    )
    actual_hashes = object_hashes.stdout.splitlines()
    if object_hashes.returncode or len(actual_hashes) != len(indexed):
        raise ReleaseVerificationError(
            "could not verify materialized files against the published Git tree"
        )
    for (rel, (expected_hash, _mode)), actual_hash in zip(
        indexed.items(),
        actual_hashes,
    ):
        if actual_hash != expected_hash:
            raise ReleaseVerificationError(
                f"materialized Git file differs from its published blob: {rel}"
            )
    return {rel: mode for rel, (_object_id, mode) in indexed.items()}


def fetch_git_release(
    git_url: str,
    cache_staging: Path,
) -> FetchedRelease:
    """Fetch exactly the highest published stable tag and seal its artifact."""

    published = resolve_latest_published_git_release(git_url)
    reference = f"refs/tags/{published.tag}"
    checkout = Path(tempfile.mkdtemp(prefix="silicon-release-checkout-"))
    try:
        commands = [
            ["git", "init", "-q", "--template=", str(checkout)],
            ["git", "-C", str(checkout), "remote", "add", "origin", git_url],
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "-q",
                "--depth",
                "1",
                "origin",
                reference,
            ],
        ]
        for command in commands:
            result = _run_git(command)
            if result.returncode:
                raise ReleaseVerificationError(
                    f"failed to fetch published release {published.tag}: "
                    f"{result.stderr.strip() or 'Git exited unsuccessfully'}"
                )
        fetched_object = _run_git(
            ["git", "-C", str(checkout), "rev-parse", "FETCH_HEAD"]
        )
        if (
            fetched_object.returncode
            or fetched_object.stdout.strip().lower() != published.tag_object
        ):
            raise ReleaseVerificationError(
                "published Git tag moved while it was being fetched"
            )
        fetched_revision = _run_git(
            ["git", "-C", str(checkout), "rev-parse", "FETCH_HEAD^{commit}"]
        )
        if (
            fetched_revision.returncode
            or fetched_revision.stdout.strip().lower() != published.revision
        ):
            raise ReleaseVerificationError(
                "published Git tag did not resolve to its advertised commit"
            )
        checkout_result = _run_git(
            [
                "git",
                "-C",
                str(checkout),
                "checkout",
                "-q",
                "--detach",
                published.revision,
            ]
        )
        if checkout_result.returncode:
            raise ReleaseVerificationError(
                f"failed to check out published release {published.tag}: "
                f"{checkout_result.stderr.strip() or 'Git exited unsuccessfully'}"
            )
        checked_out = _run_git(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"]
        )
        if (
            checked_out.returncode
            or checked_out.stdout.strip().lower() != published.revision
        ):
            raise ReleaseVerificationError(
                "checked-out Stemcell does not match the published commit"
            )
        runtime_image = _published_stemcell_metadata(checkout, published)
        canonical_modes = _verify_checkout_matches_git_tree(checkout)
        shutil.rmtree(checkout / ".git", ignore_errors=True)
        cache_staging.mkdir(parents=True, exist_ok=True)
        artifact = cache_staging / f"{published.revision}.tar"
        source_label = (
            f"git+{git_url}@{reference}#{published.tag_object}"
        )
        return create_artifact(
            checkout,
            artifact,
            revision=published.revision,
            source_label=source_label,
            trust=PUBLISHED_GIT_TRUST,
            version=published.version,
            sequence=published.sequence,
            runtime_image=runtime_image,
            canonical_modes=canonical_modes,
        )
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
