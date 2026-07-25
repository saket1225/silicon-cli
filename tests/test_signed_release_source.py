from __future__ import annotations

import json
import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from silicon_cli.updater.release import (
    HttpManifestReleaseSource,
    RELEASE_MANIFEST,
    ReleaseVerificationError,
    create_artifact,
)
from silicon_cli.updater.signatures import (
    trusted_release_keys,
    validate_packaged_release_keys,
    verify_ed25519_signatures,
)
from silicon_cli.updater import signatures as signature_module


class Response:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.closed = False

    def read(self, amount=-1):
        if amount is None or amount < 0:
            result, self.payload = self.payload, b""
            return result
        result, self.payload = self.payload[:amount], self.payload[amount:]
        return result

    def close(self):
        self.closed = True


class SignedReleaseSourceTests(unittest.TestCase):
    def test_manifest_response_closes_when_read_fails(self):
        class BrokenResponse(Response):
            def read(self, amount=-1):
                raise OSError("connection reset")

        response = BrokenResponse(b"")
        source = HttpManifestReleaseSource(
            "https://glass.example/release.json",
            verifier=lambda *_args: None,
            opener=lambda *_args, **_kwargs: response,
        )
        with tempfile.TemporaryDirectory() as raw, self.assertRaisesRegex(
            ReleaseVerificationError,
            "could not read",
        ):
            source.fetch(Path(raw))

        self.assertTrue(response.closed)

    def test_verifies_envelope_before_downloading_exact_artifact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "main.py").write_text("print('signed')\n")
            (source / RELEASE_MANIFEST).write_text(
                json.dumps(
                    {
                        "identity": {
                            "version": "2.4.0",
                            "sequence": 1,
                            "tree_sha256": "",
                        }
                    }
                )
            )
            fetched = create_artifact(
                source,
                root / "release.tar",
                revision="d" * 64,
                source_label="glass",
                trust="signed-ed25519",
            )
            artifact_bytes = fetched.artifact.read_bytes()
            signed = {
                "identity": fetched.manifest.identity.to_dict(),
                "files": fetched.manifest.files,
                "artifact_url": (
                    "https://glass.example/api/v1/silicon-release/artifacts/"
                    f"{fetched.manifest.identity.tree_sha256}.tar"
                ),
                "artifact_size": len(artifact_bytes),
                "runtime_image": (
                    "ghcr.io/teamofsilicons/silicon-runtime@sha256:"
                    + "1" * 64
                ),
                "expires_at": int(time.time()) + 300,
            }
            envelope = {
                "schema": 1,
                "signed": signed,
                "signatures": [
                    {
                        "key_id": "release-2026",
                        "algorithm": "ed25519",
                        "signature": "test",
                    }
                ],
            }
            manifest_bytes = json.dumps(envelope).encode()
            verified = []
            responses = []

            def verifier(payload, signatures):
                verified.append((json.loads(payload), signatures))

            def opener(url, **_kwargs):
                if url == "https://glass.example/release.json":
                    response = Response(manifest_bytes)
                elif url == signed["artifact_url"]:
                    response = Response(artifact_bytes)
                else:
                    raise AssertionError(url)
                responses.append(response)
                return response

            result = HttpManifestReleaseSource(
                "https://glass.example/release.json",
                verifier=verifier,
                opener=opener,
            ).fetch(root / "staging")

            self.assertEqual(
                result.manifest.identity.tree_sha256,
                fetched.manifest.identity.tree_sha256,
            )
            self.assertEqual(result.manifest.runtime_image, signed["runtime_image"])
            self.assertEqual(verified[0][0], signed)
            self.assertEqual(len(responses), 2)
            self.assertTrue(all(response.closed for response in responses))

    def test_signed_manifest_rejects_unpinned_runtime_image(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "main.py").write_text("print('signed')\n")
            fetched = create_artifact(
                source,
                root / "release.tar",
                revision="d" * 64,
                source_label="glass",
                trust="signed-ed25519",
            )
            signed = {
                "identity": fetched.manifest.identity.to_dict(),
                "files": fetched.manifest.files,
                "artifact_url": (
                    "https://glass.example/api/v1/silicon-release/artifacts/"
                    f"{fetched.manifest.identity.tree_sha256}.tar"
                ),
                "artifact_size": fetched.artifact.stat().st_size,
                "runtime_image": "ghcr.io/teamofsilicons/silicon-runtime:latest",
                "expires_at": int(time.time()) + 300,
            }
            envelope = {
                "schema": 1,
                "signed": signed,
                "signatures": [
                    {
                        "key_id": "release-2026",
                        "algorithm": "ed25519",
                        "signature": "test",
                    }
                ],
            }

            def opener(url, **_kwargs):
                if url == "https://glass.example/release.json":
                    return Response(json.dumps(envelope).encode())
                if url == signed["artifact_url"]:
                    return Response(fetched.artifact.read_bytes())
                raise AssertionError(url)

            release_source = HttpManifestReleaseSource(
                "https://glass.example/release.json",
                verifier=lambda *_args: None,
                opener=opener,
            )
            with self.assertRaisesRegex(
                ReleaseVerificationError,
                "immutable runtime image",
            ):
                release_source.fetch(root / "staging")

    def test_pinned_ed25519_key_authenticates_and_allows_unknown_rotation_key(self):
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        payload = b'{"signed":"canonical"}'
        signatures = [
            {
                "key_id": "future-key",
                "algorithm": "ed25519",
                "signature": base64.b64encode(b"x" * 64).decode(),
            },
            {
                "key_id": "release-test-1",
                "algorithm": "ed25519",
                "signature": base64.b64encode(private.sign(payload)).decode(),
            },
        ]

        verify_ed25519_signatures(
            payload,
            signatures,
            trusted_keys={"release-test-1": public},
        )

    def test_unsigned_signature_array_poison_cannot_veto_valid_rotation_key(self):
        old = Ed25519PrivateKey.generate()
        new = Ed25519PrivateKey.generate()
        new_public = new.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        payload = b"same signed payload"
        entries = [
            {"injected": "malformed"},
            {
                "key_id": "release-new",
                "algorithm": "rsa",
                "signature": "not-base64",
            },
            {
                "key_id": "release-new",
                "algorithm": "ed25519",
                "signature": base64.b64encode(old.sign(payload)).decode(),
            },
            {
                "key_id": "release-new",
                "algorithm": "ed25519",
                "signature": base64.b64encode(new.sign(payload)).decode(),
            },
        ]

        verify_ed25519_signatures(
            payload,
            entries,
            trusted_keys={"release-new": new_public},
        )

    def test_signature_verification_fails_closed_without_a_pinned_key(self):
        private = Ed25519PrivateKey.generate()
        payload = b"manifest"
        signature = {
            "key_id": "unknown",
            "algorithm": "ed25519",
            "signature": base64.b64encode(private.sign(payload)).decode(),
        }
        with self.assertRaisesRegex(
            ReleaseVerificationError, "no Silicon release public key"
        ):
            verify_ed25519_signatures(payload, [signature], trusted_keys={})

    def test_operator_keyring_is_public_only_strict_json(self):
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        encoded = base64.b64encode(public).decode()
        expected = validate_packaged_release_keys(require_nonempty=False)
        expected["release-self-hosted-1"] = public
        self.assertEqual(
            trusted_release_keys(
                json.dumps({"release-self-hosted-1": encoded})
            ),
            expected,
        )
        with self.assertRaises(ReleaseVerificationError):
            trusted_release_keys('{"bad key id":"AAAA"}')

    def test_packaged_anchors_support_additive_rotation_without_replacement(self):
        old = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        current = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        future = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        packaged = {
            "release-old": base64.b64encode(old).decode(),
            "release-current": base64.b64encode(current).decode(),
        }
        self.assertEqual(
            validate_packaged_release_keys(packaged),
            {"release-old": old, "release-current": current},
        )
        with mock.patch.object(
            signature_module, "PACKAGED_RELEASE_KEYS", packaged
        ):
            keys = trusted_release_keys(
                json.dumps(
                    {"release-future": base64.b64encode(future).decode()}
                )
            )
            self.assertEqual(
                keys,
                {
                    "release-old": old,
                    "release-current": current,
                    "release-future": future,
                },
            )
            with self.assertRaisesRegex(
                ReleaseVerificationError, "conflicts with a packaged"
            ):
                trusted_release_keys(
                    json.dumps(
                        {
                            "release-current": base64.b64encode(
                                future
                            ).decode()
                        }
                    )
                )

    def test_clean_wheel_fails_packaged_anchor_release_gate(self):
        with mock.patch.object(
            signature_module, "PACKAGED_RELEASE_KEYS", {}
        ):
            with self.assertRaisesRegex(
                ReleaseVerificationError, "no production release trust anchor"
            ):
                validate_packaged_release_keys()
            self.assertEqual(signature_module.main(["--check-packaged"]), 1)

    def test_rejects_manifest_when_trusted_verifier_rejects_signature(self):
        envelope = {
            "schema": 1,
            "signed": {},
            "signatures": [{"key_id": "bad"}],
        }

        def reject(_payload, _signatures):
            raise ReleaseVerificationError("bad signature")

        with tempfile.TemporaryDirectory() as raw:
            source = HttpManifestReleaseSource(
                "https://glass.example/release.json",
                verifier=reject,
                opener=lambda *_args, **_kwargs: Response(
                    json.dumps(envelope).encode()
                ),
            )
            with self.assertRaises(ReleaseVerificationError):
                source.fetch(Path(raw))


if __name__ == "__main__":
    unittest.main()
