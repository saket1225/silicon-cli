from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import runtime_contract
from silicon_cli.updater.release import (
    PUBLISHED_GIT_TRUST,
    PublishedGitRelease,
    ReleaseVerificationError,
    _git_environment,
    _published_stemcell_metadata,
    fetch_git_release,
    release_identity_is_authoritative,
    resolve_latest_published_git_release,
    safe_extract,
)

RUNTIME_IMAGE = (
    "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "a" * 64
)


class GitReleaseSourceTests(unittest.TestCase):
    @staticmethod
    def _result(stdout: str = "", stderr: str = "", returncode: int = 0):
        return SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        )

    @staticmethod
    def _local_repository_git_environment() -> dict[str, str]:
        """Extend the production HTTPS-only policy for local test fixtures."""

        environment = _git_environment()
        environment["GIT_CONFIG_COUNT"] = "8"
        environment["GIT_CONFIG_KEY_7"] = "protocol.file.allow"
        environment["GIT_CONFIG_VALUE_7"] = "always"
        return environment

    def test_resolver_uses_numeric_highest_stable_three_part_tag(self):
        output = "\n".join(
            [
                f"{'1' * 40}\trefs/tags/v1.9.0",
                f"{'2' * 40}\trefs/tags/v1.10.0",
                f"{'3' * 40}\trefs/tags/v2.0",
                f"{'4' * 40}\trefs/tags/v9.0.0-rc1",
                f"{'5' * 40}\trefs/tags/not-a-release",
            ]
        )
        with (
            mock.patch("silicon_cli.updater.release.shutil.which", return_value="git"),
            mock.patch(
                "silicon_cli.updater.release._run_git",
                return_value=self._result(output),
            ),
        ):
            release = resolve_latest_published_git_release(
                "https://github.com/teamofsilicons/silicon-stemcell.git"
            )

        self.assertEqual(release.tag, "v1.10.0")
        self.assertEqual(release.version, "1.10.0")
        self.assertEqual(release.sequence, 1_010_001)
        self.assertEqual(release.tag_object, "2" * 40)
        self.assertEqual(release.revision, "2" * 40)

    def test_git_environment_ignores_ambient_redirects_and_helpers(self):
        hostile = {
            "GIT_CONFIG_PARAMETERS": "'url.https://evil.invalid/.insteadOf' 'https://github.com/'",
            "GIT_DIR": "/tmp/attacker.git",
            "GIT_EXEC_PATH": "/tmp/attacker-bin",
            "GIT_SSL_NO_VERIFY": "1",
            "GIT_TEMPLATE_DIR": "/tmp/attacker-template",
            "GIT_CONFIG_KEY_99": "url.https://evil.invalid/.insteadOf",
            "GIT_CONFIG_VALUE_99": "https://github.com/",
            "GIT_ASKPASS": "/tmp/attacker-askpass",
        }
        with mock.patch.dict(os.environ, hostile, clear=False):
            environment = _git_environment()

        for key in hostile:
            self.assertNotIn(key, environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "7")
        self.assertEqual(environment["GIT_CONFIG_KEY_4"], "http.sslVerify")
        self.assertEqual(environment["GIT_CONFIG_VALUE_4"], "true")
        self.assertEqual(environment["GIT_CONFIG_KEY_5"], "protocol.allow")
        self.assertEqual(environment["GIT_CONFIG_VALUE_5"], "never")
        self.assertEqual(
            environment["GIT_CONFIG_KEY_6"],
            "protocol.https.allow",
        )
        self.assertEqual(environment["GIT_CONFIG_VALUE_6"], "always")

    def test_resolver_peels_annotated_tag(self):
        output = "\n".join(
            [
                f"{'a' * 40}\trefs/tags/v2.0.0",
                f"{'b' * 40}\trefs/tags/v2.0.0^{{}}",
            ]
        )
        with (
            mock.patch("silicon_cli.updater.release.shutil.which", return_value="git"),
            mock.patch(
                "silicon_cli.updater.release._run_git",
                return_value=self._result(output),
            ),
        ):
            release = resolve_latest_published_git_release(
                "https://github.com/teamofsilicons/silicon-stemcell.git"
            )

        self.assertEqual(release.tag_object, "a" * 40)
        self.assertEqual(release.revision, "b" * 40)

    def test_resolver_rejects_orphan_peeled_tag_instead_of_falling_back(self):
        output = "\n".join(
            [
                f"{'a' * 40}\trefs/tags/v1.0.0",
                f"{'b' * 40}\trefs/tags/v2.0.0^{{}}",
            ]
        )
        with (
            mock.patch("silicon_cli.updater.release.shutil.which", return_value="git"),
            mock.patch(
                "silicon_cli.updater.release._run_git",
                return_value=self._result(output),
            ),
            self.assertRaisesRegex(
                ReleaseVerificationError,
                "orphan peeled tag",
            ),
        ):
            resolve_latest_published_git_release(
                "https://github.com/teamofsilicons/silicon-stemcell.git"
            )

    def test_resolver_rejects_conflicting_duplicate_reference(self):
        output = "\n".join(
            [
                f"{'a' * 40}\trefs/tags/v2.0.0",
                f"{'b' * 40}\trefs/tags/v2.0.0",
            ]
        )
        with (
            mock.patch("silicon_cli.updater.release.shutil.which", return_value="git"),
            mock.patch(
                "silicon_cli.updater.release._run_git",
                return_value=self._result(output),
            ),
            self.assertRaisesRegex(
                ReleaseVerificationError,
                "conflicting objects",
            ),
        ):
            resolve_latest_published_git_release(
                "https://github.com/teamofsilicons/silicon-stemcell.git"
            )

    def test_resolver_never_falls_back_without_a_stable_tag(self):
        output = "\n".join(
            [
                f"{'a' * 40}\trefs/tags/v2.0.0-rc1",
                f"{'b' * 40}\trefs/tags/v2.0",
            ]
        )
        with (
            mock.patch("silicon_cli.updater.release.shutil.which", return_value="git"),
            mock.patch(
                "silicon_cli.updater.release._run_git",
                return_value=self._result(output),
            ),
            self.assertRaisesRegex(
                ReleaseVerificationError,
                "no published stable",
            ),
        ):
            resolve_latest_published_git_release(
                "https://github.com/teamofsilicons/silicon-stemcell.git"
            )

    @staticmethod
    def _git(cwd: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _repository(
        self,
        root: Path,
        *,
        version: str = "2.0.0",
        runtime_image: str = RUNTIME_IMAGE,
        annotated: bool = True,
        ident_transform: bool = False,
        symbolic_link: bool = False,
    ) -> tuple[Path, Path, str]:
        work = root / "work"
        remote = root / "remote.git"
        work.mkdir()
        self._git(work, "init", "-q")
        self._git(work, "config", "user.name", "Silicon Tests")
        self._git(work, "config", "user.email", "tests@example.invalid")
        (work / "main.py").write_text("print('published')\n", encoding="utf-8")
        (work / ".silicon-data-root-v1").write_text("1\n", encoding="utf-8")
        (work / "requirements.lock").write_text("", encoding="utf-8")
        if ident_transform:
            (work / ".gitattributes").write_text(
                "transformed.txt ident\n",
                encoding="utf-8",
            )
            (work / "transformed.txt").write_text(
                "$Id$\n",
                encoding="utf-8",
            )
        if symbolic_link:
            (work / "linked-main.py").symlink_to("main.py")
        (work / "silicon.info").write_text(
            json.dumps(
                {
                    "version": version,
                    "runtime_image": runtime_image,
                    "runtime_contract": (
                        runtime_contract.release_contract_metadata()
                    ),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self._git(work, "add", ".")
        self._git(work, "commit", "-q", "-m", "published")
        tag = f"v{version}"
        if annotated:
            self._git(work, "tag", "-a", tag, "-m", tag)
        else:
            self._git(work, "tag", tag)
        revision = self._git(work, "rev-parse", "HEAD")
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(work), str(remote)],
            check=True,
        )
        self._git(work, "remote", "add", "published", str(remote))
        return work, remote, revision

    def test_fetch_pins_annotated_tag_commit_and_verifies_artifact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _work, remote, revision = self._repository(root)
            with mock.patch(
                "silicon_cli.updater.release._git_environment",
                side_effect=self._local_repository_git_environment,
            ):
                fetched = fetch_git_release(str(remote), root / "staging")

            self.assertEqual(fetched.manifest.identity.version, "2.0.0")
            self.assertEqual(fetched.manifest.identity.revision, revision)
            self.assertEqual(
                fetched.manifest.identity.trust,
                PUBLISHED_GIT_TRUST,
            )
            self.assertEqual(fetched.manifest.runtime_image, RUNTIME_IMAGE)
            self.assertEqual(
                fetched.manifest.runtime_contract,
                runtime_contract.release_contract_metadata(),
            )
            extracted = root / "extracted"
            safe_extract(fetched.artifact, extracted, fetched.manifest)
            self.assertEqual(
                json.loads((extracted / "silicon.info").read_text())["version"],
                "2.0.0",
            )

    def test_fetch_rejects_tag_move_between_resolution_and_fetch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, remote, _revision = self._repository(
                root,
                annotated=False,
            )
            with mock.patch(
                "silicon_cli.updater.release._git_environment",
                side_effect=self._local_repository_git_environment,
            ):
                resolved = resolve_latest_published_git_release(str(remote))
            (work / "main.py").write_text("print('moved')\n", encoding="utf-8")
            self._git(work, "add", "main.py")
            self._git(work, "commit", "-q", "-m", "move tag")
            self._git(work, "tag", "-f", "v2.0.0")
            self._git(
                work,
                "push",
                "-q",
                "--force",
                "published",
                "refs/tags/v2.0.0",
            )

            with (
                mock.patch(
                    "silicon_cli.updater.release._git_environment",
                    side_effect=self._local_repository_git_environment,
                ),
                mock.patch(
                    "silicon_cli.updater.release."
                    "resolve_latest_published_git_release",
                    return_value=resolved,
                ),
                self.assertRaisesRegex(
                    ReleaseVerificationError,
                    "moved while",
                ),
            ):
                fetch_git_release(str(remote), root / "staging")

    def test_fetch_rejects_checkout_content_transformed_by_attributes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _work, remote, _revision = self._repository(
                root,
                ident_transform=True,
            )
            with (
                mock.patch(
                    "silicon_cli.updater.release._git_environment",
                    side_effect=self._local_repository_git_environment,
                ),
                self.assertRaisesRegex(
                    ReleaseVerificationError,
                    "differs from its published blob",
                ),
            ):
                fetch_git_release(str(remote), root / "staging")

    def test_fetch_rejects_symbolic_links_from_published_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _work, remote, _revision = self._repository(
                root,
                symbolic_link=True,
            )
            with (
                mock.patch(
                    "silicon_cli.updater.release._git_environment",
                    side_effect=self._local_repository_git_environment,
                ),
                self.assertRaisesRegex(
                    ReleaseVerificationError,
                    "unsupported entry",
                ),
            ):
                fetch_git_release(str(remote), root / "staging")

    def test_tagged_metadata_requires_matching_version_and_pinned_runtime(self):
        release = PublishedGitRelease(
            tag="v2.0.0",
            version="2.0.0",
            sequence=2_000_001,
            tag_object="a" * 40,
            revision="b" * 40,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            info = root / "silicon.info"
            info.write_text(
                json.dumps(
                    {
                        "version": "2.0.1",
                        "runtime_image": RUNTIME_IMAGE,
                        "runtime_contract": (
                            runtime_contract.release_contract_metadata()
                        ),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ReleaseVerificationError,
                "does not match",
            ):
                _published_stemcell_metadata(root, release)

            info.write_text(
                json.dumps(
                    {
                        "version": "2.0.0",
                        "runtime_image": "ghcr.io/example/runtime:latest",
                        "runtime_contract": (
                            runtime_contract.release_contract_metadata()
                        ),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ReleaseVerificationError,
                "immutable runtime_image",
            ):
                _published_stemcell_metadata(root, release)

            info.write_text(
                json.dumps(
                    {
                        "version": "2.0.0",
                        "runtime_image": RUNTIME_IMAGE,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ReleaseVerificationError,
                "before image download",
            ):
                _published_stemcell_metadata(root, release)

    def test_only_canonical_git_tag_identity_raises_release_floor(self):
        fetched = PublishedGitRelease(
            tag="v2.0.0",
            version="2.0.0",
            sequence=2_000_001,
            tag_object="a" * 40,
            revision="b" * 40,
        )
        from silicon_cli.updater.release import ReleaseIdentity

        identity = ReleaseIdentity(
            version=fetched.version,
            revision=fetched.revision,
            sequence=fetched.sequence,
            tree_sha256="c" * 64,
            artifact_sha256="d" * 64,
            source=(
                "git+https://github.com/teamofsilicons/"
                f"silicon-stemcell.git@refs/tags/{fetched.tag}"
                f"#{fetched.tag_object}"
            ),
            trust=PUBLISHED_GIT_TRUST,
        )
        self.assertTrue(release_identity_is_authoritative(identity))
        self.assertFalse(
            release_identity_is_authoritative(
                type(identity)(
                    **{
                        **identity.to_dict(),
                        "sequence": identity.sequence + 1,
                    }
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
