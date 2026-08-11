from __future__ import annotations

import os
import unittest

from silicon_cli import stemcell, sync
from silicon_cli.cli import _parse_pull_args


class PullArgsTests(unittest.TestCase):
    def test_token_and_defaults(self):
        token, opts = _parse_pull_args(["sct_live_abc"])
        self.assertEqual(token, "sct_live_abc")
        self.assertFalse(opts.assume_yes)
        self.assertIsNone(opts.brain)
        self.assertIsNone(opts.backup)

    def test_all_flags(self):
        token, opts = _parse_pull_args(
            ["sct_live_x", "--yes", "--brain", "both", "--no-backup", "--name", "sales"]
        )
        self.assertEqual(token, "sct_live_x")
        self.assertTrue(opts.assume_yes)
        self.assertEqual(opts.brain, "both")
        self.assertFalse(opts.backup)
        self.assertEqual(opts.name, "sales")

    def test_brain_both_expands_order(self):
        _t, opts = _parse_pull_args(["tok", "--brain", "both"])
        kw = opts.setup_config_kwargs()
        self.assertEqual(kw["brain"], "claude")
        self.assertEqual(kw["brain_order"], ["claude", "codex"])

    def test_brain_order_explicit(self):
        _t, opts = _parse_pull_args(["tok", "--brain", "codex", "--brain-order", "codex,claude"])
        kw = opts.setup_config_kwargs()
        self.assertEqual(kw["brain"], "codex")
        self.assertEqual(kw["brain_order"], ["codex", "claude"])

    def test_runtime_sets_env(self):
        old = os.environ.get("SILICON_RUNTIME")
        try:
            _parse_pull_args(["tok", "--runtime", "local"])
            self.assertEqual(os.environ.get("SILICON_RUNTIME"), "local")
        finally:
            if old is None:
                os.environ.pop("SILICON_RUNTIME", None)
            else:
                os.environ["SILICON_RUNTIME"] = old

    def test_pull_defaults_to_local_even_if_docker_was_initialized(self):
        journal = type("Journal", (), {"value": {"runtime": ""}})()
        old_runtime = os.environ.pop("SILICON_RUNTIME", None)
        old_legacy = os.environ.pop("SILICON_RUNTIME_DOCKER", None)
        try:
            self.assertFalse(sync._pull_runtime_requested(journal))
        finally:
            if old_runtime is not None:
                os.environ["SILICON_RUNTIME"] = old_runtime
            if old_legacy is not None:
                os.environ["SILICON_RUNTIME_DOCKER"] = old_legacy

    def test_want_backups_logic(self):
        self.assertFalse(sync._want_backups(sync.PullOpts(backup=False)))
        self.assertTrue(sync._want_backups(sync.PullOpts(backup=True)))
        self.assertTrue(sync._want_backups(sync.PullOpts(assume_yes=True)))

    def test_choose_setup_config_honors_override(self):
        # Explicit brain skips detection entirely (tools may not be installed yet).
        cfg = stemcell.choose_setup_config("", brain="codex", brain_order=["codex", "claude"])
        self.assertEqual(cfg["brain"], "codex")
        self.assertEqual(cfg["brain_order"], ["codex", "claude"])
        self.assertEqual(cfg["workers"]["terminal"], ["codex", "claude"])


if __name__ == "__main__":
    unittest.main()
