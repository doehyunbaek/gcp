import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from gcp.cli import UserError, build_remote_path, output_path_for, run
from gcp.config import load_config, save_config, set_account


@contextmanager
def isolated_config_dir():
    old_dir = os.environ.get("GCP_CONFIG_DIR")
    old_config = os.environ.get("GCP_CONFIG")
    with tempfile.TemporaryDirectory() as directory:
        os.environ["GCP_CONFIG_DIR"] = directory
        os.environ.pop("GCP_CONFIG", None)
        try:
            yield Path(directory)
        finally:
            if old_dir is None:
                os.environ.pop("GCP_CONFIG_DIR", None)
            else:
                os.environ["GCP_CONFIG_DIR"] = old_dir
            if old_config is None:
                os.environ.pop("GCP_CONFIG", None)
            else:
                os.environ["GCP_CONFIG"] = old_config


class PathTests(unittest.TestCase):
    def test_build_remote_path_defaults_to_file_name(self):
        self.assertEqual(build_remote_path("notes/today.md", None, ""), "notes/today.md")

    def test_build_remote_path_uses_basename_for_absolute_paths(self):
        self.assertEqual(build_remote_path("/tmp/today.md", None, "docs"), "docs/today.md")

    def test_build_remote_path_prefers_remote_path(self):
        self.assertEqual(build_remote_path("local.md", "remote.md", "docs"), "docs/remote.md")

    def test_build_remote_path_rejects_parent_segments(self):
        with self.assertRaises(UserError):
            build_remote_path("../secret", None, "")

    def test_output_path_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(output_path_for("notes/today.md", directory), Path(directory) / "today.md")


class ConfigTests(unittest.TestCase):
    def test_save_and_load_account(self):
        with isolated_config_dir():
            config = load_config()
            set_account(
                config,
                "GitHub.com",
                "octocat",
                token="ghp_secret",
                repo="octo/repo",
                branch="main",
                remote_dir="docs",
                token_source="config",
            )
            save_config(config)

            loaded = load_config()
            account = loaded["hosts"]["github.com"]["users"]["octocat"]
            self.assertEqual(loaded["current_host"], "github.com")
            self.assertEqual(account["repo"], "octo/repo")
            self.assertEqual(account["remote_dir"], "docs")
            self.assertEqual(account["token"], "ghp_secret")


class CliTests(unittest.TestCase):
    def test_status_without_config(self):
        with isolated_config_dir(), patch.dict(os.environ, {"GCP_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""}, clear=False):
            output = StringIO()
            with redirect_stdout(output):
                code = run(["status"])
            self.assertEqual(code, 0)
            self.assertIn("Not logged in", output.getvalue())

    def test_root_help(self):
        output = StringIO()
        with redirect_stdout(output):
            code = run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("uvx gcp push FILE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
