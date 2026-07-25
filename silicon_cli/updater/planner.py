"""Three-way customization planning without executing candidate code."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .io import hash_tree, regular_files
from .policy import RUNTIME_EXACT, RUNTIME_PREFIXES, is_runtime_path


@dataclass(frozen=True)
class PlanAction:
    path: str
    action: str
    reason: str


@dataclass
class UpdatePlan:
    actions: list[PlanAction] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    resolutions: dict[str, bytes | None] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.action] = counts.get(action.action, 0) + 1
        return {
            "actions": [asdict(action) for action in self.actions],
            "conflicts": list(self.conflicts),
            "counts": counts,
        }


def _inventory(root: Path, *, local: bool = False) -> dict[str, Path]:
    if not root.exists():
        return {}
    excluded_prefixes = RUNTIME_PREFIXES if local else {".git/"}
    excluded_names = RUNTIME_EXACT if local else set()
    return dict(
        regular_files(
            root,
            excluded_prefixes=excluded_prefixes,
            excluded_names=excluded_names,
        )
    )


def _bytes(path: Path | None) -> bytes | None:
    return path.read_bytes() if path is not None else None


def _mode(path: Path | None) -> int | None:
    return (path.stat().st_mode & 0o777) if path is not None else None


def _text(data: bytes | None) -> bool:
    if data is None:
        return False
    try:
        data.decode("utf-8")
        return b"\x00" not in data
    except UnicodeDecodeError:
        return False


def _three_way(local: bytes, base: bytes, upstream: bytes) -> bytes | None:
    if shutil.which("git") is None:
        return None
    temporary = Path(tempfile.mkdtemp(prefix="silicon-merge-"))
    try:
        local_path = temporary / "local"
        base_path = temporary / "base"
        upstream_path = temporary / "upstream"
        local_path.write_bytes(local)
        base_path.write_bytes(base)
        upstream_path.write_bytes(upstream)
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                str(local_path),
                str(base_path),
                str(upstream_path),
            ],
            capture_output=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def build_plan(base: Path, local: Path, upstream: Path) -> UpdatePlan:
    base_files = _inventory(base)
    local_files = _inventory(local, local=True)
    upstream_files = _inventory(upstream)
    plan = UpdatePlan()
    for rel in sorted(set(base_files) | set(local_files) | set(upstream_files)):
        if is_runtime_path(rel):
            continue
        base_data = _bytes(base_files.get(rel))
        local_data = _bytes(local_files.get(rel))
        new_data = _bytes(upstream_files.get(rel))
        if rel not in base_files:
            if rel in local_files and rel not in upstream_files:
                plan.actions.append(PlanAction(rel, "preserve-local", "local-only file"))
                plan.resolutions[rel] = local_data
            elif rel in local_files and rel in upstream_files and local_data != new_data:
                plan.conflicts.append(rel)
                plan.actions.append(
                    PlanAction(rel, "conflict", "local and upstream independently added file")
                )
            elif rel in local_files and rel in upstream_files:
                plan.actions.append(
                    PlanAction(
                        rel,
                        "already-merged",
                        "local and upstream independently added identical file",
                    )
                )
                if _mode(local_files.get(rel)) != _mode(upstream_files.get(rel)):
                    plan.resolutions[rel] = local_data
            else:
                plan.actions.append(PlanAction(rel, "add-upstream", "new upstream file"))
            continue
        local_changed = (
            local_data != base_data
            or _mode(local_files.get(rel)) != _mode(base_files.get(rel))
        )
        upstream_changed = (
            new_data != base_data
            or _mode(upstream_files.get(rel)) != _mode(base_files.get(rel))
        )
        if not local_changed:
            action = "delete-upstream" if new_data is None else "update-upstream"
            plan.actions.append(PlanAction(rel, action, "local copy matches prior upstream"))
            continue
        if not upstream_changed:
            action = "preserve-delete" if local_data is None else "preserve-local"
            plan.actions.append(PlanAction(rel, action, "only local copy changed"))
            plan.resolutions[rel] = local_data
            continue
        if local_data == new_data:
            plan.actions.append(PlanAction(rel, "already-merged", "both sides are identical"))
            if _mode(local_files.get(rel)) != _mode(upstream_files.get(rel)):
                plan.resolutions[rel] = local_data
            continue
        if local_data is None or new_data is None:
            plan.conflicts.append(rel)
            plan.actions.append(
                PlanAction(rel, "conflict", "delete conflicts with a changed file")
            )
            continue
        merged = (
            _three_way(local_data, base_data or b"", new_data)
            if _text(local_data) and _text(base_data) and _text(new_data)
            else None
        )
        if merged is None:
            plan.conflicts.append(rel)
            plan.actions.append(
                PlanAction(rel, "conflict", "overlapping or binary local/upstream changes")
            )
        else:
            plan.actions.append(PlanAction(rel, "merge", "clean three-way text merge"))
            plan.resolutions[rel] = merged
    return plan


def apply_plan(plan: UpdatePlan, candidate: Path, local: Path) -> None:
    if plan.conflicts:
        raise RuntimeError("cannot apply a plan with conflicts")
    candidate_root = candidate.resolve(strict=True)
    for rel, payload in plan.resolutions.items():
        target = candidate_root / rel
        if payload is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        source = local / rel
        if source.exists():
            os.chmod(temporary, source.stat().st_mode & 0o777)
        os.replace(temporary, target)


def seed_legacy_snapshot(source: Path, target: Path) -> None:
    """Create the first merge base using CLI code, never candidate code."""

    destination = target / ".silicon-upstream" / "base"
    temporary = destination.with_name(f".base.{os.getpid()}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True, exist_ok=True)
    try:
        for rel, path in _inventory(source).items():
            output = temporary / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        metadata = {
            "schema": 1,
            "source": str(source),
            "created_at": time.time(),
            "authority": "silicon-cli",
            "tree_sha256": hash_tree(destination)[0],
        }
        (destination.parent / "meta.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
