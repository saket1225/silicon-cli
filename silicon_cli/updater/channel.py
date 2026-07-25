"""One authenticated release channel shared by bootstrap and updates."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from ..config import SILICON_RELEASE_MANIFEST_URL, STEMCELL_GIT_URL
from .cache import ReleaseCache
from .release import (
    FetchedRelease,
    HttpManifestReleaseSource,
    fetch_git_release,
)
from .signatures import verify_ed25519_signatures


class ReleaseChannelError(RuntimeError):
    """The configured release channel did not yield an acceptable release."""


def fetch_latest_release(
    cache: ReleaseCache,
    *,
    allow_unsigned_git: bool = False,
    info: Callable[[str], None] | None = None,
    warn: Callable[[str], None] | None = None,
) -> FetchedRelease:
    """Fetch, authenticate, and cache the latest immutable Stemcell release.

    Signed Glass metadata is the only default.  Git is deliberately reachable
    only through the explicit compatibility switch and is still pinned to the
    exact resolved commit plus locally verified artifact/tree hashes.
    """

    say = info or (lambda _message: None)
    caution = warn or (lambda _message: None)
    staging = Path(tempfile.mkdtemp(prefix="silicon-release-fetch-"))
    try:
        if allow_unsigned_git:
            caution(
                "Unsigned Git release mode was explicitly enabled. Commit and "
                "SHA-256 pinning protect integrity after resolution, but do not "
                "authenticate the publisher."
            )
            say("Resolving the exact Git revision once...")
            fetched = fetch_git_release(STEMCELL_GIT_URL, staging)
        else:
            say("Downloading and authenticating the signed Silicon release...")
            fetched = HttpManifestReleaseSource(
                SILICON_RELEASE_MANIFEST_URL,
                verifier=verify_ed25519_signatures,
            ).fetch(staging)
        cached = cache.store(fetched)
        if not allow_unsigned_git and (
            cached.manifest.identity.trust != "signed-ed25519"
            or cached.manifest.identity.source != "glass"
        ):
            raise ReleaseChannelError(
                "the signed release channel returned an unexpected trust identity"
            )
        return cached
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = ["ReleaseChannelError", "fetch_latest_release"]
