"""Trusted, CLI-owned Silicon update engine.

The running Stemcell and the downloaded candidate are deliberately outside the
update trust boundary.  Only code shipped in ``silicon-cli`` plans, journals,
activates, resumes, and rolls back an update.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import (
        EngineHooks,
        TransactionalUpdater,
        UpdateCancelled,
        UpdateConflict,
        UpdateError,
    )
    from .release import ReleaseIdentity, ReleaseManifest

__all__ = [
    "EngineHooks",
    "ReleaseIdentity",
    "ReleaseManifest",
    "TransactionalUpdater",
    "UpdateCancelled",
    "UpdateConflict",
    "UpdateError",
]

_LAZY_EXPORTS = {
    "EngineHooks": (".engine", "EngineHooks"),
    "TransactionalUpdater": (".engine", "TransactionalUpdater"),
    "UpdateCancelled": (".engine", "UpdateCancelled"),
    "UpdateConflict": (".engine", "UpdateConflict"),
    "UpdateError": (".engine", "UpdateError"),
    "ReleaseIdentity": (".release", "ReleaseIdentity"),
    "ReleaseManifest": (".release", "ReleaseManifest"),
}


def __getattr__(name: str):
    """Keep lightweight helpers from importing the complete update engine."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
