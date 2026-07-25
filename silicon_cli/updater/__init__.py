"""Trusted, CLI-owned Silicon update engine.

The running Stemcell and the downloaded candidate are deliberately outside the
update trust boundary.  Only code shipped in ``silicon-cli`` plans, journals,
activates, resumes, and rolls back an update.
"""

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
