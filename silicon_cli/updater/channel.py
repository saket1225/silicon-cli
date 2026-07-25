"""One Git-published release channel shared by bootstrap and updates."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from ..config import STEMCELL_GIT_URL
from .cache import ReleaseCache
from .release import (
    FetchedRelease,
    PUBLISHED_GIT_TRUST,
    ReleaseVerificationError,
    fetch_git_release,
    release_identity_is_authoritative,
)


class ReleaseChannelError(RuntimeError):
    """The configured release channel did not yield an acceptable release."""


def fetch_latest_release(
    cache: ReleaseCache,
    *,
    info: Callable[[str], None] | None = None,
) -> FetchedRelease:
    """Resolve, verify, and cache the latest immutable Stemcell release.

    The highest stable SemVer tag advertised by the canonical Git repository
    is the sole source of truth. It is resolved once, pinned to its exact tag
    object and commit, and locally sealed with verified artifact/tree hashes.
    """

    say = info or (lambda _message: None)
    staging = Path(tempfile.mkdtemp(prefix="silicon-release-fetch-"))
    try:
        say("Resolving the latest published Stemcell Git tag...")
        try:
            fetched = fetch_git_release(STEMCELL_GIT_URL, staging)
            cached = cache.store(fetched)
        except ReleaseVerificationError as exc:
            raise ReleaseChannelError(str(exc)) from exc
        if (
            cached.manifest.identity.trust != PUBLISHED_GIT_TRUST
            or not release_identity_is_authoritative(
                cached.manifest.identity
            )
        ):
            raise ReleaseChannelError(
                "the Git release channel returned an unexpected identity"
            )
        return cached
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = ["ReleaseChannelError", "fetch_latest_release"]
