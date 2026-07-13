import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from http.client import IncompleteRead
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from gitcp.cli import UserError, _upload_directory_with_git_remote, build_remote_path, default_remote_path_for_local, infer_copy_args, iter_local_files, output_path_for, parse_copy_target, resolve_remote_repo, run, upload_directory, upload_file_if_changed
from gitcp.config import load_config, save_config, set_account
from gitcp.github import GitHubClient, GitHubError, NotFound, git_blob_sha


@contextmanager
def isolated_config_dir():
    old_dir = os.environ.get("GITCP_CONFIG_DIR")
    old_config = os.environ.get("GITCP_CONFIG")
    with tempfile.TemporaryDirectory() as directory:
        os.environ["GITCP_CONFIG_DIR"] = directory
        os.environ.pop("GITCP_CONFIG", None)
        try:
            yield Path(directory)
        finally:
            if old_dir is None:
                os.environ.pop("GITCP_CONFIG_DIR", None)
            else:
                os.environ["GITCP_CONFIG_DIR"] = old_dir
            if old_config is None:
                os.environ.pop("GITCP_CONFIG", None)
            else:
                os.environ["GITCP_CONFIG"] = old_config


def run_git_command(args, *, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(args)}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result.stdout


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
                self.blobs = []
                self.trees = []
                self.commits = []
                self.ref_updates = []

            def ensure_branch(self, repo, branch):
                self.branch = (repo, branch)

            def get_ref(self, repo, ref):
                return {"object": {"sha": "head123"}}

            def get_commit(self, repo, sha):
                return {"tree": {"sha": "base123"}}

            def get_tree(self, repo, sha, *, recursive=False):
                return {"tree": []}

            def create_blob(self, repo, content):
                sha = f"blob{len(self.blobs) + 1}"
                self.blobs.append((repo, content))
                return {"sha": sha}

            def create_tree(self, repo, tree, *, base_tree=None):
                self.trees.append((repo, tree, base_tree))
                return {"sha": "tree123"}

            def create_commit(self, repo, message, tree, parents):
                self.commits.append((repo, message, tree, parents))
                return {"sha": "commit123"}

            def update_ref(self, repo, ref, sha, *, force=False):
                self.ref_updates.append((repo, ref, sha, force))
                return {}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            (root / "a.txt").write_text("a")
            (root / "sub" / "b.txt").write_text("b")
            client = FakeClient()

            uploaded, unchanged = upload_directory(client, "octo/repo", "main", root, "logs", None)

            self.assertEqual((uploaded, unchanged), (2, 0))
            self.assertEqual([item[1] for item in client.blobs], [b"a", b"b"])
            self.assertEqual([entry["path"] for entry in client.trees[0][1]], ["logs/a.txt", "logs/sub/b.txt"])
            self.assertEqual(client.trees[0][2], "base123")
            self.assertEqual(client.commits, [("octo/repo", "gitcp: sync logs", "tree123", ["head123"])])
            self.assertEqual(client.ref_updates, [("octo/repo", "heads/main", "commit123", False)])

    @unittest.skipIf(shutil.which("git") is None, "git is required")
    def test_git_directory_upload_pushes_one_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare = root / "remote.git"
            seed = root / "seed"
            local = root / "local"
            clone = root / "clone"

            run_git_command(["git", "init", "--bare", str(bare)])
            run_git_command(["git", "init", str(seed)])
            run_git_command(["git", "config", "user.name", "test"], cwd=seed)
            run_git_command(["git", "config", "user.email", "test@example.com"], cwd=seed)
            (seed / "README.md").write_text("root\n")
            run_git_command(["git", "add", "."], cwd=seed)
            run_git_command(["git", "commit", "-m", "init"], cwd=seed)
            run_git_command(["git", "branch", "-M", "main"], cwd=seed)
            run_git_command(["git", "remote", "add", "origin", str(bare)], cwd=seed)
            run_git_command(["git", "push", "origin", "main"], cwd=seed)

            (local / "sub").mkdir(parents=True)
            (local / "a.txt").write_text("a")
            (local / "sub" / "b.txt").write_text("b")
            env = os.environ.copy()
            env.update({"GIT_AUTHOR_NAME": "gitcp", "GIT_AUTHOR_EMAIL": "gitcp@example.com", "GIT_COMMITTER_NAME": "gitcp", "GIT_COMMITTER_EMAIL": "gitcp@example.com"})

            with patch.dict(os.environ, {"GITCP_CACHE_DIR": str(root / "cache")}):
                uploaded, unchanged = _upload_directory_with_git_remote(str(bare), "main", local, "logs", None, env=env)

                self.assertEqual((uploaded, unchanged), (2, 0))
                run_git_command(["git", "clone", "--quiet", "--branch", "main", str(bare), str(clone)])
                self.assertEqual((clone / "README.md").read_text(), "root\n")
                self.assertEqual((clone / "logs" / "a.txt").read_text(), "a")
                self.assertEqual((clone / "logs" / "sub" / "b.txt").read_text(), "b")
                commits = run_git_command(["git", "log", "--oneline"], cwd=clone).splitlines()
                self.assertEqual(len(commits), 2)

                with patch("gitcp.cli._git_hash_uploads", side_effect=AssertionError("cached sync should not hash file contents")):
                    uploaded, unchanged = _upload_directory_with_git_remote(str(bare), "main", local, "logs", None, env=env)
                self.assertEqual((uploaded, unchanged), (0, 2))

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

    def test_upload_file_reports_large_file_path_and_size(self):
        class FakeClient:
            def get_contents(self, repo, path, *, ref=None):
                raise AssertionError("oversized file should fail before API calls")

            def put_file(self, repo, path, content, *, message, branch=None):
                raise AssertionError("oversized file should fail before upload")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.bin"
            with path.open("wb") as f:
                f.truncate(101 * 1024 * 1024)

            with self.assertRaisesRegex(UserError, r"large\.bin.*101\.0 MiB.*100\.0 MiB"):
                upload_file_if_changed(FakeClient(), "octo/repo", "main", path, "large.bin", None)

    def test_upload_file_reports_base64_request_size_limit(self):
        class FakeClient:
            def get_contents(self, repo, path, *, ref=None):
                raise AssertionError("oversized request should fail before API calls")

            def put_file(self, repo, path, content, *, message, branch=None):
                raise AssertionError("oversized request should fail before upload")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "limit.bin"
            with path.open("wb") as f:
                f.truncate(47 * 1024 * 1024)

            with self.assertRaisesRegex(UserError, r"limit\.bin.*47\.0 MiB.*62\.7 MiB after base64.*47\.7 MiB"):
                upload_file_if_changed(FakeClient(), "octo/repo", "main", path, "limit.bin", None)

    def test_upload_file_adds_path_and_size_to_github_errors(self):
        class FakeClient:
            def get_contents(self, repo, path, *, ref=None):
                raise NotFound("missing")

            def put_file(self, repo, path, content, *, message, branch=None):
                raise GitHubError("Sorry, the file is too large to be processed.")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            path.write_bytes(b"content")

            with self.assertRaisesRegex(GitHubError, r"data\.bin.*7 B.*Sorry"):
                upload_file_if_changed(FakeClient(), "octo/repo", "main", path, "data.bin", None)

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

    def test_request_retries_incomplete_get_response(self):
        class FakeResponse:
            def __init__(self, payload=None, error=None):
                self.payload = payload
                self.error = error

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                if self.error:
                    raise self.error
                return self.payload

        responses = [
            FakeResponse(error=IncompleteRead(b'{"partial"', 10)),
            FakeResponse(payload=b'{"ok": true}'),
        ]

        with patch("gitcp.github.urlopen", side_effect=responses) as urlopen_mock:
            data = GitHubClient("token")._request("GET", "/user")

        self.assertEqual(data, {"ok": True})
        self.assertEqual(urlopen_mock.call_count, 2)


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
        with isolated_config_dir(), patch.dict(os.environ, {"GITCP_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""}, clear=False):
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
        self.assertIn("uvx gitcp LOCAL_FILE_OR_DIR :REMOTE_PATH", output.getvalue())


if __name__ == "__main__":
    unittest.main()
