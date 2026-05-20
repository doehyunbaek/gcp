import base64
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from gcp.cli import UserError, build_remote_path, default_remote_path_for_local, infer_copy_args, iter_local_files, output_path_for, parse_copy_target, resolve_remote_repo, run, upload_directory, upload_file_if_changed
from gcp.config import load_config, save_config, set_account
from gcp.github import GitHubClient, NotFound, git_blob_sha


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

    def test_parse_copy_target_github(self):
        self.assertEqual(parse_copy_target("github:README.md").kind, "github")
        self.assertEqual(parse_copy_target("github:README.md").path, "README.md")
        self.assertEqual(parse_copy_target("gh:README.md").path, "README.md")
        self.assertEqual(parse_copy_target(":README.md").path, "README.md")
        self.assertEqual(parse_copy_target(":README.md").repo, "")

    def test_parse_copy_target_repo_prefix(self):
        target = parse_copy_target("privatee:README.md")
        self.assertEqual(target.kind, "github")
        self.assertEqual(target.repo, "privatee")
        self.assertEqual(target.path, "README.md")

    def test_parse_copy_target_full_repo_prefix(self):
        target = parse_copy_target("doehyunbaekk/privatee:README.md")
        self.assertEqual(target.kind, "github")
        self.assertEqual(target.repo, "doehyunbaekk/privatee")
        self.assertEqual(target.path, "README.md")

    def test_parse_copy_target_local(self):
        target = parse_copy_target("README.md")
        self.assertEqual(target.kind, "local")
        self.assertEqual(target.path, "README.md")

    def test_resolve_remote_repo(self):
        self.assertEqual(resolve_remote_repo("doehyunbaek/private", ""), "doehyunbaek/private")
        self.assertEqual(resolve_remote_repo("doehyunbaek/private", "privatee"), "doehyunbaek/privatee")
        self.assertEqual(resolve_remote_repo("doehyunbaek/private", "doehyunbaekk/privatee"), "doehyunbaekk/privatee")

    def test_default_remote_path_for_home_file(self):
        local = str(Path.home() / ".pi" / "agent" / "multicodex.json")
        home_remote = Path.home().as_posix().lstrip("/")
        self.assertEqual(default_remote_path_for_local(local), f"{home_remote}/.pi/agent/multicodex.json")

    def test_infer_one_arg_push(self):
        self.assertEqual(infer_copy_args("README.md", None), ("README.md", ":README.md"))

    def test_infer_one_arg_push_absolute_home_path(self):
        local = str(Path.home() / ".pi" / "agent" / "multicodex.json")
        home_remote = Path.home().as_posix().lstrip("/")
        self.assertEqual(infer_copy_args(local, None), (local, f":{home_remote}/.pi/agent/multicodex.json"))

    def test_infer_one_arg_github_source_downloads_to_same_path(self):
        self.assertEqual(infer_copy_args(":README.md", None), (":README.md", "README.md"))

    def test_infer_one_arg_github_home_source_downloads_to_home_path(self):
        source, destination = infer_copy_args(":~/.pi/agent/multicodex.json", None)
        home_remote = Path.home().as_posix().lstrip("/")
        self.assertEqual(source, f":{home_remote}/.pi/agent/multicodex.json")
        self.assertEqual(destination, str(Path.home() / ".pi" / "agent" / "multicodex.json"))

    def test_iter_local_files_rejects_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UserError):
                iter_local_files(Path(directory))

    def test_upload_directory_preserves_relative_paths(self):
        class FakeClient:
            def __init__(self):
                self.uploads = []

            def get_contents(self, repo, path, *, ref=None):
                raise NotFound("missing")

            def put_file(self, repo, path, content, *, message, branch=None):
                self.uploads.append((repo, path, content, message, branch))
                return {"commit": {"sha": "abcdef123"}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            (root / "a.txt").write_text("a")
            (root / "sub" / "b.txt").write_text("b")
            client = FakeClient()

            uploaded, unchanged = upload_directory(client, "octo/repo", "main", root, "logs", None)

            self.assertEqual((uploaded, unchanged), (2, 0))
            self.assertEqual([item[1] for item in client.uploads], ["logs/a.txt", "logs/sub/b.txt"])

    def test_upload_file_skips_unchanged_file_by_git_blob_sha(self):
        class FakeClient:
            def get_contents(self, repo, path, *, ref=None):
                return {"type": "file", "sha": git_blob_sha(b"same")}

            def put_file(self, repo, path, content, *, message, branch=None):
                raise AssertionError("unchanged file should not be uploaded")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "same.txt"
            path.write_bytes(b"same")

            changed, short_sha = upload_file_if_changed(FakeClient(), "octo/repo", "main", path, "same.txt", None)

            self.assertFalse(changed)
            self.assertEqual(short_sha, "")

    def test_download_file_falls_back_to_git_blob_api(self):
        content = b"larger than contents inline payload"
        sha = "abc123"
        encoded = base64.b64encode(content).decode("ascii")
        client = GitHubClient("token")
        requested_routes = []

        def fake_request(method, route, body=None):
            requested_routes.append(route)
            if route == "/repos/octo/repo/contents/big.log?ref=main":
                return {"type": "file", "sha": sha, "encoding": "none"}
            if route == f"/repos/octo/repo/git/blobs/{sha}":
                return {"encoding": "base64", "content": encoded}
            raise AssertionError(f"unexpected route: {route}")

        client._request = fake_request

        downloaded, metadata = client.download_file("octo/repo", "big.log", ref="main")

        self.assertEqual(downloaded, content)
        self.assertEqual(metadata["sha"], sha)
        self.assertEqual(requested_routes, ["/repos/octo/repo/contents/big.log?ref=main", f"/repos/octo/repo/git/blobs/{sha}"])


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

    def test_setting_branch_updates_active_account(self):
        with isolated_config_dir():
            config = load_config()
            set_account(
                config,
                "github.com",
                "octocat",
                token="ghp_secret",
                repo="octo/repo",
                branch="main",
                remote_dir="docs",
                token_source="config",
            )
            save_config(config)

            output = StringIO()
            with redirect_stdout(output):
                code = run(["setting", "branch", "feature"])

            self.assertEqual(code, 0)
            account = load_config()["hosts"]["github.com"]["users"]["octocat"]
            self.assertEqual(account["branch"], "feature")
            self.assertEqual(account["repo"], "octo/repo")
            self.assertEqual(account["remote_dir"], "docs")
            self.assertIn("Branch set to feature", output.getvalue())

    def test_setting_branch_accepts_flag(self):
        with isolated_config_dir():
            config = load_config()
            set_account(
                config,
                "github.com",
                "octocat",
                token=None,
                repo="octo/repo",
                branch="main",
                remote_dir="",
                token_source="env",
            )
            save_config(config)

            output = StringIO()
            with redirect_stdout(output):
                code = run(["setting", "branch", "--branch", "develop"])

            self.assertEqual(code, 0)
            account = load_config()["hosts"]["github.com"]["users"]["octocat"]
            self.assertEqual(account["branch"], "develop")

    def test_root_help(self):
        output = StringIO()
        with redirect_stdout(output):
            code = run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("uvx gcp LOCAL_FILE_OR_DIR :REMOTE_PATH", output.getvalue())


if __name__ == "__main__":
    unittest.main()
