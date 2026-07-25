"""Pinned Ed25519 trust anchors for Silicon release manifests.

Production public keys belong in ``PACKAGED_RELEASE_KEYS`` before publishing a
CLI wheel. Self-hosted operators can add public keys through the documented
JSON environment variable. Private keys are never accepted by this package.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
from collections.abc import Mapping
from importlib import resources

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .release import ReleaseVerificationError

KEY_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")

def _decode_public_key(key_id: str, encoded: object) -> bytes:
    if KEY_ID_RE.fullmatch(key_id) is None or not isinstance(encoded, str):
        raise ReleaseVerificationError("release trust keyring has invalid fields")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReleaseVerificationError(
            f"release public key {key_id!r} is not valid base64"
        ) from exc
    if len(raw) != 32:
        raise ReleaseVerificationError(
            f"release public key {key_id!r} must contain 32 raw bytes"
        )
    return raw


def _load_packaged_release_keys() -> dict[str, str]:
    try:
        raw = (
            resources.files("silicon_cli")
            .joinpath("release_trust_anchors.json")
            .read_text(encoding="utf-8")
        )
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(
            "packaged release trust anchors are unavailable or malformed"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "keys"}
        or value.get("schema") != 1
        or not isinstance(value.get("keys"), dict)
        or len(value["keys"]) > 32
    ):
        raise ReleaseVerificationError(
            "packaged release trust anchors have an invalid schema"
        )
    result: dict[str, str] = {}
    for key_id, encoded in value["keys"].items():
        if not isinstance(key_id, str) or not isinstance(encoded, str):
            raise ReleaseVerificationError(
                "packaged release trust anchors have invalid fields"
            )
        _decode_public_key(key_id, encoded)
        result[key_id] = encoded
    return result


# Key rotation is additive: ship the new public key before Glass begins signing
# with it, then retain the previous key until every supported CLI can update.
# Private key material remains exclusively in the Glass production credential
# secret and is never accepted by this package.
PACKAGED_RELEASE_KEYS: dict[str, str] = _load_packaged_release_keys()


def validate_packaged_release_keys(
    mapping: Mapping[str, object] | None = None,
    *,
    require_nonempty: bool = True,
) -> dict[str, bytes]:
    """Validate the public-only anchors that will ship in a built wheel."""

    encoded = dict(PACKAGED_RELEASE_KEYS if mapping is None else mapping)
    if len(encoded) > 32:
        raise ReleaseVerificationError(
            "packaged release trust anchors contain more than 32 keys"
        )
    decoded = {
        str(key_id): _decode_public_key(str(key_id), public_key)
        for key_id, public_key in encoded.items()
    }
    if require_nonempty and not decoded:
        raise ReleaseVerificationError(
            "no production release trust anchor is packaged; provision at "
            "least one Ed25519 public key before publishing the CLI"
        )
    return decoded


def trusted_release_keys(
    configured: str | None = None,
) -> dict[str, bytes]:
    """Load packaged keys plus an explicit public-only operator keyring.

    ``SILICON_RELEASE_TRUSTED_KEYS`` is a JSON object mapping a key id to the
    standard-base64 encoding of its raw 32-byte Ed25519 public key. A configured
    key may add a rotation key but cannot replace a packaged key id.
    """

    validate_packaged_release_keys(require_nonempty=False)
    encoded: dict[str, object] = dict(PACKAGED_RELEASE_KEYS)
    raw_config = (
        os.environ.get("SILICON_RELEASE_TRUSTED_KEYS", "")
        if configured is None
        else configured
    ).strip()
    if raw_config:
        try:
            value = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ReleaseVerificationError(
                "SILICON_RELEASE_TRUSTED_KEYS must be a JSON object"
            ) from exc
        if not isinstance(value, dict) or len(value) > 32:
            raise ReleaseVerificationError(
                "SILICON_RELEASE_TRUSTED_KEYS must contain at most 32 keys"
            )
        for key_id, public_key in value.items():
            key_id = str(key_id)
            if key_id in encoded and encoded[key_id] != public_key:
                raise ReleaseVerificationError(
                    f"configured key {key_id!r} conflicts with a packaged trust anchor"
                )
            encoded[key_id] = public_key
    return {
        key_id: _decode_public_key(key_id, public_key)
        for key_id, public_key in encoded.items()
    }


def verify_ed25519_signatures(
    payload: bytes,
    signatures: list[dict],
    *,
    trusted_keys: Mapping[str, bytes] | None = None,
) -> None:
    """Require at least one valid signature from the pinned release keyring.

    The signature array is outside the signed payload. Therefore malformed,
    unknown, duplicate, and invalid extras cannot veto a valid trusted
    signature; accepting any such veto would create an injection-based denial
    of service. The array itself is bounded and at least one pinned key must
    verify the canonical payload.
    """

    keys = dict(trusted_keys) if trusted_keys is not None else trusted_release_keys()
    if not keys:
        raise ReleaseVerificationError(
            "no Silicon release public key is trusted; provision "
            "SILICON_RELEASE_TRUSTED_KEYS with the published key id and raw "
            "Ed25519 public key"
        )
    if not isinstance(signatures, list) or not signatures or len(signatures) > 32:
        raise ReleaseVerificationError(
            "release manifest must contain 1 to 32 signatures"
        )
    valid = False
    for entry in signatures:
        if not isinstance(entry, dict) or set(entry) != {
            "key_id",
            "algorithm",
            "signature",
        }:
            continue
        key_id = str(entry["key_id"])
        if KEY_ID_RE.fullmatch(key_id) is None:
            continue
        if entry["algorithm"] != "ed25519":
            continue
        public = keys.get(key_id)
        if public is None:
            continue
        try:
            signature = base64.b64decode(str(entry["signature"]), validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(signature) != 64:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
        except (InvalidSignature, ValueError):
            continue
        valid = True
    if not valid:
        raise ReleaseVerificationError(
            "release manifest has no valid signature from a trusted key"
        )


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args != ["--check-packaged"]:
        print(
            "usage: python -m silicon_cli.updater.signatures --check-packaged",
            file=sys.stderr,
        )
        return 2
    try:
        keys = validate_packaged_release_keys()
    except ReleaseVerificationError as exc:
        print(f"release trust-anchor check failed: {exc}", file=sys.stderr)
        return 1
    print(
        "validated packaged release trust anchors: "
        + ", ".join(sorted(keys))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
