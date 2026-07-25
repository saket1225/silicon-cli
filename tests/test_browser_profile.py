from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from silicon_cli import cli, sync


class BrowserProfileCopyTests(unittest.TestCase):
    def test_cli_help_never_advertises_tokens_in_browser_profile_commands(self):
        with mock.patch("sys.stdout") as stdout:
            cli.cmd_help()

        rendered = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("silicon browser-profile setup", rendered)
        self.assertNotIn("browser-profile setup [token]", rendered)
        self.assertIn("silicon browser-profile finish <session_id>", rendered)
        self.assertNotIn("browser-profile finish <token>", rendered)

    def test_browserbase_setup_and_finish_use_actual_provider_name(self):
        responses = [
            (
                200,
                {
                    "provider": "browserbase",
                    "session_id": "session-1",
                    "viewer_url": "https://viewer.example/session-1",
                    "before_profile_ids": [],
                },
            ),
            (
                200,
                {
                    "profile": {"id": "profile-1", "name": "Team Browser"},
                    "assigned": 19,
                },
            ),
        ]

        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(sync, "REGISTRY_DIR", Path(temporary) / ".silicon"),
                mock.patch.object(sync, "_post_json", side_effect=responses),
                mock.patch.object(sync.ui, "interactive", return_value=True),
                mock.patch.object(sync.webbrowser, "open"),
                mock.patch("builtins.input", return_value=""),
                mock.patch("sys.stdout") as stdout,
            ):
                sync.browser_profile_setup("setup-token")

        rendered = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("Browserbase setup session started.", rendered)
        self.assertIn("Saved Browserbase profile 'Team Browser'", rendered)
        self.assertNotIn("Steel", rendered)
        self.assertNotIn("setup-token", rendered)

    def test_deferred_finish_uses_private_session_file_without_printing_token(self):
        start = {
            "provider": "steel",
            "session_id": "session-deferred",
            "viewer_url": "https://viewer.example/session-deferred",
            "before_profile_ids": ["profile-before"],
        }
        finish = {
            "profile": {"id": "profile-2", "name": "Deferred Browser"},
            "assigned": 2,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".silicon"
            with mock.patch.object(sync, "REGISTRY_DIR", root):
                with (
                    mock.patch.object(sync, "_post_json", return_value=(200, start)),
                    mock.patch.object(sync.ui, "interactive", return_value=False),
                    mock.patch.object(sync.webbrowser, "open"),
                    mock.patch("sys.stdout") as stdout,
                ):
                    sync.browser_profile_setup("secret-setup-token")

                rendered = "".join(call.args[0] for call in stdout.write.call_args_list)
                self.assertNotIn("secret-setup-token", rendered)
                self.assertIn(
                    "silicon browser-profile finish session-deferred",
                    rendered,
                )
                pending = sync._browser_profile_session_path("session-deferred")
                self.assertTrue(pending.is_file())
                self.assertEqual(pending.stat().st_mode & 0o777, 0o600)

                with mock.patch.object(
                    sync,
                    "_post_json",
                    return_value=(200, finish),
                ) as post:
                    sync.browser_profile_finish("session-deferred", None)

                self.assertEqual(
                    post.call_args.args[1],
                    {
                        "token": "secret-setup-token",
                        "session_id": "session-deferred",
                        "before_profile_ids": ["profile-before"],
                    },
                )
                self.assertFalse(pending.exists())

    def test_standalone_finish_uses_provider_neutral_fallback(self):
        response = {
            "profile": {"id": "profile-1", "name": "Team Browser"},
            "assigned": 1,
        }
        with (
            mock.patch.object(sync, "_post_json", return_value=(200, response)),
            mock.patch("sys.stdout") as stdout,
        ):
            sync.browser_profile_finish("setup-token", "session-1")

        rendered = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("Saved browser profile 'Team Browser'", rendered)
        self.assertNotIn("Steel", rendered)


if __name__ == "__main__":
    unittest.main()
