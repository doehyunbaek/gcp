from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .config import (
    DEFAULT_BRANCH,
    DEFAULT_HOST,
    ConfigError,
    account_candidates,
    config_path,
    env_token,
    get_user,
    hosts,
    load_config,
    normalize_host,
    normalize_remote_dir,
    remove_account,
    save_config,
    set_account,
    users_for_host,
)
from .github import GitHubClient, GitHubError, NotFound, parse_repo
from .oauth import OAuthError, login_with_browser

GITHUB_CONTENTS_MAX_FILE_SIZE = 100 * 1024 * 1024
GITHUB_CONTENTS_MAX_REQUEST_SIZE = 50 * 1000 * 1000

HELP = """GitHub-backed file sync.

Usage:
  uvx gcp setting [login|logout|status|repo|branch] [options]
  uvx gcp FILE_OR_DIR [options]
  uvx gcp LOCAL_FILE_OR_DIR :REMOTE_PATH [options]
  uvx gcp :REMOTE_PATH LOCAL_FILE [options]
  uvx gcp status

Commands:
  setting          Configure GitHub auth and the target repository
  status           Show current auth/repository settings

Remote paths:
  :path                 Path inside the configured GitHub repository
  repo:path             Path inside a repo under the configured owner
  owner/repo:path       Path inside an explicit GitHub repository
  github:path, gh:path  Aliases for :path

Examples:
  uvx gcp README.md
  uvx gcp README.md :README.md
  uvx gcp :README.md README.md

Run `uvx gcp <command> --help` for command options.
"""


class UserError(RuntimeError):
    pass


@dataclass
class SyncContext:
    host: str
    user: str
    token: str
    repo: str
    branch: str
    remote_dir: str
    token_source: str
    account: dict[str, Any]


@dataclass(frozen=True)
class CopyTarget:
    kind: str
    path: str
    repo: str = ""


@dataclass(frozen=True)
class UploadCandidate:
    local_path: Path
    remote_path: str
    size: int
    sha: str
    mtime_ns: int = 0
    ctime_ns: int = 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
    except (UserError, ConfigError, GitHubError, OAuthError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def run(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if argv[0] in {"-V", "--version", "version"}:
        print(__version__)
        return 0

    command = argv[0]
    if command == "setting":
        return run_setting(argv[1:])
    if command == "status":
        return run_status(argv[1:])

    return run_copy(argv)


def run_copy(argv: list[str]) -> int:
    parser = command_parser(prog="gcp", description="Copy files between local disk and a configured GitHub repository.")
    parser.add_argument("source", help="Source path. Use :path, repo:path, or owner/repo:path for GitHub")
    parser.add_argument("destination", nargs="?", help="Destination path. Use :path, repo:path, or owner/repo:path for GitHub")
    add_context_flags(parser)
    parser.add_argument("-m", "--message", help="Commit message for local-to-GitHub copies")
    parser.add_argument("-f", "--force", action="store_true", help="Overwrite local changes without prompting")
    args = parser.parse_args(argv)

    source, destination = infer_copy_args(args.source, args.destination)
    src = parse_copy_target(source)
    dst = parse_copy_target(destination)
    if src.kind == dst.kind:
        raise UserError("copy must be between a local path and a GitHub path")

    ctx = resolve_context(args)
    client = GitHubClient(ctx.token, ctx.host)

    if src.kind == "local" and dst.kind == "github":
        local_path = Path(src.path).expanduser().resolve(strict=False)
        if not local_path.exists():
            raise UserError(f"local path does not exist: {local_path}")
        remote_repo = resolve_remote_repo(ctx.repo, dst.repo)
        remote_path = build_remote_path(dst.path, None, ctx.remote_dir)
        if local_path.is_dir():
            uploaded, unchanged = upload_directory(client, remote_repo, ctx.branch, local_path, remote_path, args.message)
            print(f"✓ Synced {uploaded} file(s) from {local_path} to {remote_repo}:{remote_path} on {ctx.branch}" + (f" ({unchanged} unchanged)" if unchanged else ""))
            return 0
        if not local_path.is_file():
            raise UserError(f"local path is not a file or directory: {local_path}")
        changed, short_sha = upload_file_if_changed(client, remote_repo, ctx.branch, local_path, remote_path, args.message)
        if not changed:
            print(f"✓ No changes for {remote_repo}:{remote_path} on {ctx.branch}")
            return 0
        suffix = f" @ {short_sha}" if short_sha else ""
        print(f"✓ Copied {local_path} to {remote_repo}:{remote_path} on {ctx.branch}{suffix}")
        return 0

    remote_repo = resolve_remote_repo(ctx.repo, src.repo)
    remote_path = build_remote_path(src.path, None, ctx.remote_dir)
    content, _metadata = client.download_file(remote_repo, remote_path, ref=ctx.branch)
    output_path = output_path_for_remote(dst.path, remote_path)
    if output_path.exists() and output_path.read_bytes() != content and not args.force:
        if can_prompt():
            if not confirm(f"Overwrite local file {output_path}?", default=False):
                raise UserError("local file was not overwritten")
        else:
            raise UserError(f"local file exists and differs: {output_path}; pass --force to overwrite")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    print(f"✓ Copied {remote_repo}:{remote_path} on {ctx.branch} to {output_path}")
    return 0


def run_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gcp status", description="Show current gcp settings.")
    parser.add_argument("--show-token", action="store_true", help="Show whether a stored token exists (never prints token values)")
    args = parser.parse_args(argv)
    print_status(load_config(), show_token=args.show_token)
    return 0


def run_setting(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gcp setting", description="Configure GitHub auth and sync settings.")
    subparsers = parser.add_subparsers(dest="action")

    login = subparsers.add_parser("login", help="Log in to a GitHub account", add_help=False)
    add_long_help(login)
    login.add_argument("-h", "--hostname", help="GitHub hostname (default: github.com)")
    login.add_argument("-r", "--repo", help="Repository in owner/repo format")
    login.add_argument("-b", "--branch", help="Branch to sync with")
    login.add_argument("-d", "--remote-dir", default=None, help="Optional repository directory for synced files")
    login.add_argument("--web", "-w", action="store_true", help="Login with a web browser")
    login.add_argument("--clipboard", "-c", action="store_true", help="Copy one-time OAuth device code to clipboard")
    login.add_argument("--with-token", action="store_true", help="Read a GitHub token from standard input")
    login.add_argument("--use-env-token", action="store_true", help="Use GCP_TOKEN, GH_TOKEN, or GITHUB_TOKEN instead of storing a token")

    logout = subparsers.add_parser("logout", help="Log out of a GitHub account", add_help=False)
    add_long_help(logout)
    logout.add_argument("-h", "--hostname", help="GitHub hostname")
    logout.add_argument("-u", "--user", help="GitHub username")

    status = subparsers.add_parser("status", help="Show current settings")
    status.add_argument("--show-token", action="store_true", help="Show whether a stored token exists")

    repo = subparsers.add_parser("repo", help="Change repository/branch for the active account", add_help=False)
    add_long_help(repo)
    repo.add_argument("-h", "--hostname", help="GitHub hostname")
    repo.add_argument("-u", "--user", help="GitHub username")
    repo.add_argument("-r", "--repo", help="Repository in owner/repo format")
    repo.add_argument("-b", "--branch", help="Branch to sync with")
    repo.add_argument("-d", "--remote-dir", default=None, help="Optional repository directory for synced files")

    branch = subparsers.add_parser("branch", help="Change branch for the active account", add_help=False)
    add_long_help(branch)
    branch.add_argument("branch", nargs="?", help="Branch to sync with")
    branch.add_argument("-h", "--hostname", help="GitHub hostname")
    branch.add_argument("-u", "--user", help="GitHub username")
    branch.add_argument("-b", "--branch-name", dest="branch_name", help="Branch to sync with")

    args = parser.parse_args(argv)

    if args.action == "login":
        return setting_login(args)
    if args.action == "logout":
        return setting_logout(args)
    if args.action == "status":
        print_status(load_config(), show_token=args.show_token)
        return 0
    if args.action == "repo":
        return setting_repo(args)
    if args.action == "branch":
        return setting_branch(args)

    if can_prompt():
        return interactive_setting()

    print_status(load_config(), show_token=False)
    return 0


def command_parser(*args: Any, **kwargs: Any) -> argparse.ArgumentParser:
    kwargs.setdefault("add_help", False)
    parser = argparse.ArgumentParser(*args, **kwargs)
    add_long_help(parser)
    return parser


def add_long_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--help", action="help", help="show this help message and exit")


def add_context_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--hostname", dest="hostname", help="GitHub hostname")
    parser.add_argument("-u", "--user", help="GitHub username")
    parser.add_argument("-r", "--repo", help="Repository in owner/repo format")
    parser.add_argument("-b", "--branch", help="Branch to sync with")


def setting_login(args: argparse.Namespace) -> int:
    config = load_config()
    host = normalize_host(args.hostname or prompt_for_hostname())
    token, token_source, username = read_login_token(args, host)

    client = GitHubClient(token, host)
    if not username:
        username = client.get_current_user()

    repo = args.repo or prompt_text("Repository to sync with (owner/repo)", required=True)
    parse_repo(repo)
    repo_data = client.get_repository(repo)
    default_branch = str(repo_data.get("default_branch") or DEFAULT_BRANCH)

    branch = args.branch or prompt_text("Branch", default=default_branch, required=True)
    remote_dir_default = ""
    remote_dir = args.remote_dir if args.remote_dir is not None else prompt_text(
        "Repository directory for synced files", default=remote_dir_default, required=False
    )
    remote_dir = normalize_remote_dir(remote_dir)

    set_account(
        config,
        host,
        username,
        token=token if token_source == "config" else None,
        repo=repo,
        branch=branch,
        remote_dir=remote_dir,
        token_source=token_source,
    )
    save_config(config)

    print(f"✓ Logged in to {host} account {username}")
    print(f"✓ Repository set to {repo} on {branch}" + (f" under {remote_dir}/" if remote_dir else ""))
    return 0


def setting_logout(args: argparse.Namespace) -> int:
    config = load_config()
    if not list(hosts(config)):
        raise UserError("not logged in to any hosts")

    hostname = normalize_host(args.hostname) if args.hostname else ""
    username = args.user or ""

    if hostname and hostname not in set(hosts(config)):
        raise UserError(f"not logged in to {hostname}")
    if hostname and username and username not in set(users_for_host(config, hostname)):
        raise UserError(f"not logged in to {hostname} account {username}")

    candidates = account_candidates(config, hostname, username)
    if not candidates:
        raise UserError("no accounts matched that criteria")
    if len(candidates) == 1:
        host, user = candidates[0]
    elif not can_prompt():
        raise UserError("unable to determine which account to log out of; specify --hostname and --user")
    else:
        labels = [f"{user} ({host})" for host, user in candidates]
        selected = select("What account do you want to log out of?", labels)
        host, user = candidates[selected]

    pre_active = get_active_user(config, host)
    _remaining_host, switched_user = remove_account(config, host, user)
    save_config(config)

    print(f"✓ Logged out of {host} account {user}")
    if pre_active == user and switched_user:
        print(f"✓ Switched active account for {host} to {switched_user}")
    return 0


def setting_repo(args: argparse.Namespace) -> int:
    config = load_config()
    ctx = resolve_context(args, require_repo=False, allow_env_without_account=False)

    repo = args.repo or ctx.account.get("repo") or prompt_text("Repository to sync with (owner/repo)", required=True)
    parse_repo(repo)
    client = GitHubClient(ctx.token, ctx.host)
    repo_data = client.get_repository(repo)
    default_branch = str(repo_data.get("default_branch") or DEFAULT_BRANCH)

    branch = args.branch or ctx.account.get("branch") or prompt_text("Branch", default=default_branch, required=True)
    remote_dir_default = ctx.account.get("remote_dir", "")
    remote_dir = args.remote_dir if args.remote_dir is not None else prompt_text(
        "Repository directory for synced files", default=remote_dir_default, required=False
    )
    remote_dir = normalize_remote_dir(remote_dir)

    set_account(
        config,
        ctx.host,
        ctx.user,
        token=ctx.account.get("token"),
        repo=repo,
        branch=branch,
        remote_dir=remote_dir,
        token_source=ctx.account.get("token_source", ctx.token_source),
    )
    save_config(config)
    print(f"✓ Repository set to {repo} on {branch}" + (f" under {remote_dir}/" if remote_dir else ""))
    return 0


def setting_branch(args: argparse.Namespace) -> int:
    config = load_config()
    requested_host = getattr(args, "hostname", None)
    host = normalize_host(requested_host or config.get("current_host") or DEFAULT_HOST)
    host_cfg = config.get("hosts", {}).get(host, {})
    requested_user = getattr(args, "user", None)
    user = requested_user or host_cfg.get("current_user") or ""
    account = get_user(config, host, user) if user else None

    if account is None:
        hint = "run `uvx gcp setting` first"
        if requested_host:
            raise UserError(f"not logged in to {host}; {hint}")
        raise UserError(f"not logged in; {hint}")

    branch = getattr(args, "branch_name", None) or getattr(args, "branch", None)
    branch = branch or prompt_text("Branch", default=str(account.get("branch") or DEFAULT_BRANCH), required=True)
    if not str(branch).strip():
        raise UserError("branch is required")

    set_account(
        config,
        host,
        str(user),
        token=account.get("token"),
        repo=str(account.get("repo") or ""),
        branch=str(branch).strip(),
        remote_dir=str(account.get("remote_dir") or ""),
        token_source=str(account.get("token_source") or ("config" if account.get("token") else "env")),
    )
    save_config(config)
    print(f"✓ Branch set to {str(branch).strip()}")
    return 0


def interactive_setting() -> int:
    print("GitHub-backed file sync settings\n")
    choices = [
        "Log in to a GitHub account",
        "Log out of a GitHub account",
        "Show current status",
        "Change repository/branch",
        "Change branch only",
        "Exit",
    ]
    selected = select("What would you like to do?", choices)
    if selected == 0:
        args = argparse.Namespace(hostname=None, repo=None, branch=None, remote_dir=None, web=False, clipboard=False, with_token=False, use_env_token=False)
        return setting_login(args)
    if selected == 1:
        args = argparse.Namespace(hostname=None, user=None)
        return setting_logout(args)
    if selected == 2:
        print_status(load_config(), show_token=False)
        return 0
    if selected == 3:
        args = argparse.Namespace(hostname=None, user=None, repo=None, branch=None, remote_dir=None)
        return setting_repo(args)
    if selected == 4:
        args = argparse.Namespace(hostname=None, user=None, branch=None, branch_name=None)
        return setting_branch(args)
    return 0


def read_login_token(args: argparse.Namespace, host: str) -> tuple[str, str, str]:
    env_name, env_value = env_token()

    explicit_modes = [bool(args.web), bool(args.with_token), bool(args.use_env_token)]
    if sum(explicit_modes) > 1:
        raise UserError("specify only one of --web, --with-token, or --use-env-token")

    if args.web:
        token, username = login_with_browser(
            host,
            interactive=can_prompt(),
            copy_to_clipboard=bool(args.clipboard),
        )
        print("✓ Authentication complete.", file=sys.stderr)
        return token, "config", username

    if args.with_token:
        token = sys.stdin.read().strip()
        if not token:
            raise UserError("no token found on standard input")
        return token, "config", ""
    if args.use_env_token:
        if not env_value:
            raise UserError("no token found in GCP_TOKEN, GH_TOKEN, or GITHUB_TOKEN")
        return env_value, "env", ""

    if can_prompt():
        choices = ["Login with a web browser", "Paste an authentication token"]
        if env_value:
            choices.append(f"Use token from ${env_name} without storing it")
        selected = select("How would you like to authenticate gcp?", choices)
        if selected == 0:
            token, username = login_with_browser(host, interactive=True, copy_to_clipboard=bool(args.clipboard))
            print("✓ Authentication complete.", file=sys.stderr)
            return token, "config", username
        if selected == 2 and env_value:
            return env_value, "env", ""
        token = getpass.getpass("Paste GitHub token: ").strip()
        if not token:
            raise UserError("token is required")
        return token, "config", ""

    if env_value:
        return env_value, "env", ""
    raise UserError("no token available; run interactively, pass --web, pass --with-token, or set GCP_TOKEN/GH_TOKEN/GITHUB_TOKEN")


def resolve_context(
    args: argparse.Namespace,
    *,
    require_repo: bool = True,
    allow_env_without_account: bool = True,
) -> SyncContext:
    config = load_config()
    requested_host = getattr(args, "hostname", None)
    host = normalize_host(requested_host or config.get("current_host") or DEFAULT_HOST)
    host_cfg = config.get("hosts", {}).get(host, {})
    requested_user = getattr(args, "user", None)
    user = requested_user or host_cfg.get("current_user") or ""
    account = get_user(config, host, user) if user else None

    env_name, env_value = env_token()
    if account is None:
        repo_arg = getattr(args, "repo", None)
        if allow_env_without_account and env_value and repo_arg:
            branch_arg = getattr(args, "branch", None) or DEFAULT_BRANCH
            return SyncContext(host, env_name or "env", env_value, repo_arg, branch_arg, "", "env", {})
        hint = "run `uvx gcp setting` first"
        if requested_host:
            raise UserError(f"not logged in to {host}; {hint}")
        raise UserError(f"not logged in; {hint}")

    token = str(account.get("token") or "")
    token_source = str(account.get("token_source") or ("config" if token else "env"))
    if not token:
        if env_value:
            token = env_value
            token_source = "env"
        else:
            raise UserError("no token available; set GCP_TOKEN/GH_TOKEN/GITHUB_TOKEN or run `uvx gcp setting login --with-token`")

    repo = getattr(args, "repo", None) or account.get("repo") or ""
    if require_repo and not repo:
        raise UserError("no repository configured; run `uvx gcp setting repo --repo owner/repo`")
    if repo:
        parse_repo(str(repo))

    branch = getattr(args, "branch", None) or account.get("branch") or DEFAULT_BRANCH
    remote_dir = account.get("remote_dir") or ""
    return SyncContext(host, user, token, str(repo), str(branch), str(remote_dir), token_source, account)


def get_active_user(config: dict[str, Any], host: str) -> str:
    host_cfg = config.get("hosts", {}).get(normalize_host(host), {})
    return str(host_cfg.get("current_user") or "")


def print_status(config: dict[str, Any], *, show_token: bool) -> None:
    print(f"Config: {config_path()}")
    env_name, env_value = env_token()
    if env_value:
        print(f"Environment token: ${env_name}")

    configured_hosts = list(hosts(config))
    if not configured_hosts:
        print("Not logged in. Run `uvx gcp setting` to configure GitHub access.")
        return

    current_host = normalize_host(config.get("current_host") or DEFAULT_HOST)
    for host in configured_hosts:
        host_cfg = config.get("hosts", {}).get(host, {})
        current_user = host_cfg.get("current_user")
        host_marker = "*" if host == current_host else " "
        print(f"{host_marker} {host}")
        for user in users_for_host(config, host):
            account = get_user(config, host, user) or {}
            user_marker = "*" if user == current_user else " "
            repo = account.get("repo") or "(no repo)"
            branch = account.get("branch") or DEFAULT_BRANCH
            remote_dir = account.get("remote_dir") or ""
            token_source = account.get("token_source") or ("config" if account.get("token") else "env")
            line = f"  {user_marker} {user}: {repo} [{branch}]"
            if remote_dir:
                line += f" dir={remote_dir}/"
            line += f" token={token_source}"
            if show_token:
                line += " stored=yes" if account.get("token") else " stored=no"
            print(line)


def prompt_for_hostname() -> str:
    if not can_prompt():
        return DEFAULT_HOST
    selected = select("Where do you use GitHub?", ["GitHub.com", "Other"])
    if selected == 0:
        return DEFAULT_HOST
    return prompt_text("GitHub hostname", required=True)


def can_prompt() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def select(prompt: str, choices: list[str], default_index: int = 0) -> int:
    if not choices:
        raise UserError("no choices available")
    print(prompt)
    for i, choice in enumerate(choices, start=1):
        marker = " (default)" if i - 1 == default_index else ""
        print(f"  {i}) {choice}{marker}")
    while True:
        answer = input("Select: ").strip()
        if answer == "":
            return default_index
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(choices):
                return index
        print(f"Enter a number from 1 to {len(choices)}.")


def prompt_text(prompt: str, *, default: str = "", required: bool = True) -> str:
    if not can_prompt():
        if default or not required:
            return default
        raise UserError(f"{prompt} is required")
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default or not required:
            return default
        print("A value is required.")


def confirm(prompt: str, *, default: bool) -> bool:
    yes = "Y" if default else "y"
    no = "n" if default else "N"
    while True:
        answer = input(f"{prompt} [{yes}/{no}] ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def iter_local_files(directory: Path) -> list[Path]:
    files = [path for path in directory.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.relative_to(directory).as_posix())
    if not files:
        raise UserError(f"local directory contains no files: {directory}")
    return files


def upload_directory(
    client: GitHubClient,
    remote_repo: str,
    branch: str,
    local_dir: Path,
    remote_dir: str,
    message: Optional[str],
) -> tuple[int, int]:
    if isinstance(client, GitHubClient):
        if shutil.which("git") is None:
            raise UserError("directory uploads require git to push files efficiently; install git and try again")
        client.ensure_branch(remote_repo, branch)
        return _upload_directory_with_git(
            _git_remote_url(client.hostname, remote_repo),
            client.token,
            branch,
            local_dir,
            remote_dir,
            message,
        )

    # Test/fake-client fallback, and a non-git implementation kept for custom clients.
    return _upload_directory_with_api(client, remote_repo, branch, local_dir, remote_dir, message)


def _upload_directory_with_git(
    remote_url: str,
    token: str,
    branch: str,
    local_dir: Path,
    remote_dir: str,
    message: Optional[str],
) -> tuple[int, int]:
    env = _git_env(token)
    return _upload_directory_with_git_remote(remote_url, branch, local_dir, remote_dir, message, env=env)


def _upload_directory_with_git_remote(
    remote_url: str,
    branch: str,
    local_dir: Path,
    remote_dir: str,
    message: Optional[str],
    *,
    env: Optional[dict[str, str]] = None,
) -> tuple[int, int]:
    candidates = _prepare_directory_uploads(local_dir, remote_dir, hash_files=False, contents_api_limits=False)
    total_size = sum(candidate.size for candidate in candidates)
    show_progress = sys.stderr.isatty() and (len(candidates) >= 50 or total_size >= 50 * 1024 * 1024)
    git_env = _git_env_with_identity(env or os.environ.copy())
    cache_path = _git_upload_cache_path(remote_url, branch, local_dir, remote_dir)
    cache = _load_git_hash_cache(cache_path)

    with tempfile.TemporaryDirectory(prefix="gcp-git-") as tmp:
        repo_dir = Path(tmp)
        _progress(show_progress, f"preparing git push for {len(candidates)} file(s), {_format_size(total_size)}")
        _run_git(["init", "--quiet"], repo_dir, git_env)
        _run_git(["remote", "add", "origin", remote_url], repo_dir, git_env)

        _progress(show_progress, f"fetching {branch} tree")
        _run_git(
            ["fetch", "--depth=1", "--filter=blob:none", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            repo_dir,
            git_env,
            stream=show_progress,
        )
        head_sha = _run_git(["rev-parse", f"refs/remotes/origin/{branch}"], repo_dir, git_env).strip()
        if not head_sha:
            raise GitHubError(f"could not determine head sha for branch {branch}")
        _run_git(["read-tree", head_sha], repo_dir, git_env)

        remote_shas = _git_index_file_shas(repo_dir, git_env, remote_dir)
        _raise_for_git_path_conflicts(remote_shas, candidates)

        cached, to_hash = _git_split_cached_hashes(candidates, remote_shas, cache)
        if to_hash:
            _progress(show_progress, f"hashing {len(to_hash)} changed/unknown file(s) into git object database")
            hashed = _git_hash_uploads(repo_dir, git_env, to_hash)
        else:
            _progress(show_progress, f"reusing cached hashes for {len(cached)} file(s)")
            hashed = []
        candidates_by_path = {candidate.remote_path: candidate for candidate in [*cached, *hashed]}
        candidates = [candidates_by_path[candidate.remote_path] for candidate in candidates]

        changed = [candidate for candidate in candidates if remote_shas.get(candidate.remote_path) != candidate.sha]
        unchanged = len(candidates) - len(changed)
        if not changed:
            _save_git_hash_cache(cache_path, remote_url, branch, local_dir, remote_dir, candidates)
            _progress(show_progress, "no changed files to push")
            return 0, unchanged

        _progress(show_progress, f"packing {len(changed)} changed file(s) into one commit")
        _git_update_index(repo_dir, git_env, changed)
        tree_sha = _run_git(["write-tree"], repo_dir, git_env).strip()
        if not tree_sha:
            raise GitHubError("git did not return a tree sha")
        commit_sha = _run_git(
            ["commit-tree", tree_sha, "-p", head_sha, "-m", message or f"gcp: sync {remote_dir or local_dir.name}"],
            repo_dir,
            git_env,
        ).strip()
        if not commit_sha:
            raise GitHubError("git did not return a commit sha")

        _progress(show_progress, "pushing one commit")
        _run_git(["push", "--progress", "origin", f"{commit_sha}:refs/heads/{branch}"], repo_dir, git_env, stream=show_progress)
        _save_git_hash_cache(cache_path, remote_url, branch, local_dir, remote_dir, candidates)
        return len(changed), unchanged


def _upload_directory_with_api(
    client: GitHubClient,
    remote_repo: str,
    branch: str,
    local_dir: Path,
    remote_dir: str,
    message: Optional[str],
) -> tuple[int, int]:
    candidates = _prepare_directory_uploads(local_dir, remote_dir, hash_files=True, contents_api_limits=True)
    client.ensure_branch(remote_repo, branch)

    ref = client.get_ref(remote_repo, f"heads/{branch}")
    head_sha = str(ref.get("object", {}).get("sha") or "")
    if not head_sha:
        raise GitHubError(f"could not determine head sha for {remote_repo}:{branch}")

    head_commit = client.get_commit(remote_repo, head_sha)
    base_tree_sha = str(head_commit.get("tree", {}).get("sha") or "")
    if not base_tree_sha:
        raise GitHubError(f"could not determine tree sha for {remote_repo}:{branch}")

    remote_shas = _remote_file_shas(client, remote_repo, base_tree_sha, remote_dir)
    tree_entries: list[dict[str, Any]] = []
    unchanged = 0

    for candidate in candidates:
        if remote_shas.get(candidate.remote_path) == candidate.sha:
            unchanged += 1
            continue
        try:
            blob = client.create_blob(remote_repo, candidate.local_path.read_bytes())
        except GitHubError as e:
            raise GitHubError(
                f"failed to upload {candidate.local_path} ({_format_size(candidate.size)}) to {candidate.remote_path}: {e}",
                status=e.status,
            ) from e
        blob_sha = str(blob.get("sha") or "")
        if not blob_sha:
            raise GitHubError(f"GitHub did not return a blob sha for {candidate.remote_path}")
        tree_entries.append({"path": candidate.remote_path, "mode": "100644", "type": "blob", "sha": blob_sha})

    if not tree_entries:
        return 0, unchanged

    try:
        tree = client.create_tree(remote_repo, tree_entries, base_tree=base_tree_sha)
        tree_sha = str(tree.get("sha") or "")
        if not tree_sha:
            raise GitHubError("GitHub did not return a tree sha")
        commit = client.create_commit(
            remote_repo,
            message or f"gcp: sync {remote_dir or local_dir.name}",
            tree_sha,
            [head_sha],
        )
        commit_sha = str(commit.get("sha") or "")
        if not commit_sha:
            raise GitHubError("GitHub did not return a commit sha")
        client.update_ref(remote_repo, f"heads/{branch}", commit_sha)
    except GitHubError as e:
        raise GitHubError(f"failed to commit directory upload to {remote_repo}:{remote_dir}: {e}", status=e.status) from e

    return len(tree_entries), unchanged


def _prepare_directory_uploads(
    local_dir: Path,
    remote_dir: str,
    *,
    hash_files: bool,
    contents_api_limits: bool,
) -> list[UploadCandidate]:
    candidates = []
    for path in iter_local_files(local_dir):
        rel = path.relative_to(local_dir).as_posix()
        remote_path = build_remote_path(rel, f"{remote_dir}/{rel}", "")
        size = _validate_upload_size(path) if contents_api_limits else _validate_git_upload_size(path)
        stat = path.stat()
        sha = _git_blob_sha_for_file(path, size) if hash_files else ""
        candidates.append(UploadCandidate(path, remote_path, size, sha, int(stat.st_mtime_ns), int(stat.st_ctime_ns)))
    return candidates


def _git_remote_url(hostname: str, repo: str) -> str:
    owner, name = parse_repo(repo)
    return f"https://{normalize_host(hostname)}/{owner}/{name}.git"


def _git_env(token: str) -> dict[str, str]:
    env = os.environ.copy()
    encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
        }
    )
    return env


def _git_env_with_identity(env: dict[str, str]) -> dict[str, str]:
    env = env.copy()
    env.setdefault("GIT_AUTHOR_NAME", "gcp")
    env.setdefault("GIT_AUTHOR_EMAIL", "gcp@localhost")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def _run_git(
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    input_data: Optional[str] = None,
    stream: bool = False,
) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=env,
            input=input_data,
            text=True,
            stdout=None if stream else subprocess.PIPE,
            stderr=None if stream else subprocess.PIPE,
            check=False,
        )
    except OSError as e:
        raise GitHubError(f"could not run git: {e}") from e
    if result.returncode != 0:
        detail = "" if stream else (result.stderr or result.stdout or "").strip()
        command = "git " + " ".join(args)
        raise GitHubError(f"{command} failed" + (f": {detail}" if detail else ""))
    return result.stdout or ""


def _progress(enabled: bool, message: str) -> None:
    if enabled:
        print(f"gcp: {message}...", file=sys.stderr)


def _gcp_cache_dir() -> Path:
    override = os.environ.get("GCP_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "gcp"


def _git_upload_cache_path(remote_url: str, branch: str, local_dir: Path, remote_dir: str) -> Path:
    key_data = json.dumps(
        {
            "remote_url": remote_url,
            "branch": branch,
            "local_dir": str(local_dir.resolve(strict=False)),
            "remote_dir": normalize_remote_dir(remote_dir),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _gcp_cache_dir() / "upload-hashes" / f"{hashlib.sha256(key_data).hexdigest()}.json"


def _load_git_hash_cache(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"files": {}}
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("files"), dict):
        return {"files": {}}
    return data


def _save_git_hash_cache(
    path: Path,
    remote_url: str,
    branch: str,
    local_dir: Path,
    remote_dir: str,
    candidates: list[UploadCandidate],
) -> None:
    files = {}
    for candidate in candidates:
        if not candidate.sha:
            continue
        files[candidate.remote_path] = {
            "size": candidate.size,
            "mtime_ns": candidate.mtime_ns,
            "ctime_ns": candidate.ctime_ns,
            "sha": candidate.sha,
        }
    data = {
        "version": 1,
        "remote_url": remote_url,
        "branch": branch,
        "local_dir": str(local_dir.resolve(strict=False)),
        "remote_dir": normalize_remote_dir(remote_dir),
        "files": files,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.write("\n")
        tmp_path.replace(path)
    except OSError:
        # Cache failures should not make a sync fail.
        pass


def _cache_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _git_split_cached_hashes(
    candidates: list[UploadCandidate],
    remote_shas: dict[str, str],
    cache: dict[str, Any],
) -> tuple[list[UploadCandidate], list[UploadCandidate]]:
    files = cache.get("files", {}) if isinstance(cache, dict) else {}
    cached = []
    to_hash = []
    for candidate in candidates:
        entry = files.get(candidate.remote_path) if isinstance(files, dict) else None
        sha = str(entry.get("sha") or "") if isinstance(entry, dict) else ""
        if (
            sha
            and _cache_int(entry.get("size")) == candidate.size
            and _cache_int(entry.get("mtime_ns")) == candidate.mtime_ns
            and _cache_int(entry.get("ctime_ns")) == candidate.ctime_ns
            and remote_shas.get(candidate.remote_path) == sha
        ):
            cached.append(UploadCandidate(candidate.local_path, candidate.remote_path, candidate.size, sha, candidate.mtime_ns, candidate.ctime_ns))
        else:
            to_hash.append(candidate)
    return cached, to_hash


def _git_hash_uploads(repo_dir: Path, env: dict[str, str], candidates: list[UploadCandidate]) -> list[UploadCandidate]:
    if not candidates:
        return []

    if all("\n" not in str(candidate.local_path) and "\r" not in str(candidate.local_path) for candidate in candidates):
        input_data = "".join(f"{candidate.local_path}\n" for candidate in candidates)
        output = _run_git(["hash-object", "-w", "--no-filters", "--stdin-paths"], repo_dir, env, input_data=input_data)
        shas = [line.strip() for line in output.splitlines() if line.strip()]
        if len(shas) != len(candidates):
            raise GitHubError(f"git returned {len(shas)} blob sha(s) for {len(candidates)} file(s)")
        return [
            UploadCandidate(candidate.local_path, candidate.remote_path, candidate.size, sha, candidate.mtime_ns, candidate.ctime_ns)
            for candidate, sha in zip(candidates, shas)
        ]

    hashed = []
    for candidate in candidates:
        sha = _run_git(["hash-object", "-w", "--no-filters", "--", str(candidate.local_path)], repo_dir, env).strip()
        if not sha:
            raise GitHubError(f"git did not return a blob sha for {candidate.local_path}")
        hashed.append(UploadCandidate(candidate.local_path, candidate.remote_path, candidate.size, sha, candidate.mtime_ns, candidate.ctime_ns))
    return hashed


def _git_index_file_shas(repo_dir: Path, env: dict[str, str], remote_dir: str) -> dict[str, str]:
    prefix = normalize_remote_dir(remote_dir)
    args = ["ls-files", "-s", "-z"]
    if prefix:
        args.extend(["--", f":(literal){prefix}"])
    output = _run_git(args, repo_dir, env)
    shas: dict[str, str] = {}
    for record in output.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        if prefix and path == prefix:
            raise GitHubError(f"{prefix} already exists and is not a directory")
        if prefix and not path.startswith(f"{prefix}/"):
            continue
        parts = metadata.split()
        if len(parts) >= 2:
            shas[path] = parts[1]
    return shas


def _raise_for_git_path_conflicts(remote_shas: dict[str, str], candidates: list[UploadCandidate]) -> None:
    remote_files = set(remote_shas)
    remote_dirs = set()
    for path in remote_files:
        parts = path.split("/")
        for index in range(1, len(parts)):
            remote_dirs.add("/".join(parts[:index]))

    for candidate in candidates:
        parts = candidate.remote_path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in remote_files:
                raise GitHubError(f"{parent} already exists and is not a directory")
        if candidate.remote_path in remote_dirs:
            raise GitHubError(f"{candidate.remote_path} already exists and is a directory")


def _git_update_index(repo_dir: Path, env: dict[str, str], candidates: list[UploadCandidate]) -> None:
    if not candidates:
        return
    input_data = "".join(f"100644 {candidate.sha}\t{candidate.remote_path}\0" for candidate in candidates)
    _run_git(["update-index", "-z", "--index-info"], repo_dir, env, input_data=input_data)


def _remote_file_shas(client: GitHubClient, remote_repo: str, base_tree_sha: str, remote_dir: str) -> dict[str, str]:
    prefix = normalize_remote_dir(remote_dir)
    tree_sha = base_tree_sha
    if prefix:
        entry = _find_tree_entry(client, remote_repo, base_tree_sha, prefix)
        if entry is None:
            return {}
        if entry.get("type") != "tree":
            raise GitHubError(f"{prefix} already exists and is not a directory")
        tree_sha = str(entry.get("sha") or "")
        if not tree_sha:
            raise GitHubError(f"could not determine tree sha for {prefix}")

    tree = client.get_tree(remote_repo, tree_sha, recursive=True)
    if tree.get("truncated"):
        raise GitHubError(f"remote tree for {remote_repo}:{prefix or '/'} is too large to compare safely")

    shas = {}
    for entry in tree.get("tree", []):
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = str(entry.get("path") or "").strip("/")
        sha = str(entry.get("sha") or "")
        if not path or not sha:
            continue
        full_path = f"{prefix}/{path}" if prefix else path
        shas[full_path] = sha
    return shas


def _find_tree_entry(client: GitHubClient, remote_repo: str, tree_sha: str, path: str) -> Optional[dict[str, Any]]:
    current_tree_sha = tree_sha
    parts = [part for part in normalize_remote_dir(path).split("/") if part]
    for index, part in enumerate(parts):
        tree = client.get_tree(remote_repo, current_tree_sha)
        entries = tree.get("tree", [])
        entry = next((item for item in entries if isinstance(item, dict) and item.get("path") == part), None)
        if entry is None:
            return None
        if index == len(parts) - 1:
            return entry
        if entry.get("type") != "tree":
            raise GitHubError(f"{'/'.join(parts[: index + 1])} already exists and is not a directory")
        current_tree_sha = str(entry.get("sha") or "")
        if not current_tree_sha:
            raise GitHubError(f"could not determine tree sha for {'/'.join(parts[: index + 1])}")
    return None


def upload_file_if_changed(
    client: GitHubClient,
    remote_repo: str,
    branch: str,
    local_path: Path,
    remote_path: str,
    message: Optional[str],
) -> tuple[bool, str]:
    local_size = _validate_upload_size(local_path)
    local_sha = _git_blob_sha_for_file(local_path, local_size)
    try:
        remote_metadata = client.get_contents(remote_repo, remote_path, ref=branch)
        if not isinstance(remote_metadata, dict) or remote_metadata.get("type") != "file":
            raise GitHubError(f"{remote_path} already exists and is not a file")
        if str(remote_metadata.get("sha") or "") == local_sha:
            return False, ""
    except NotFound:
        pass

    local_content = local_path.read_bytes()
    try:
        result = client.put_file(
            remote_repo,
            remote_path,
            local_content,
            message=message or f"gcp: sync {remote_path}",
            branch=branch,
        )
    except GitHubError as e:
        raise GitHubError(f"failed to upload {local_path} ({_format_size(local_size)}) to {remote_path}: {e}", status=e.status) from e
    commit = result.get("commit", {}) if isinstance(result, dict) else {}
    return True, str(commit.get("sha", ""))[:7]


def _validate_upload_size(path: Path) -> int:
    local_size = path.stat().st_size
    encoded_size = _base64_encoded_size(local_size)
    if local_size > GITHUB_CONTENTS_MAX_FILE_SIZE:
        raise UserError(
            f"file is too large for GitHub contents API upload: {path} "
            f"({_format_size(local_size)}); GitHub contents API file limit is {_format_size(GITHUB_CONTENTS_MAX_FILE_SIZE)}"
        )
    if encoded_size > GITHUB_CONTENTS_MAX_REQUEST_SIZE:
        raise UserError(
            f"file is too large for GitHub contents API upload: {path} "
            f"({_format_size(local_size)}, {_format_size(encoded_size)} after base64 encoding); "
            f"request limit is about {_format_size(GITHUB_CONTENTS_MAX_REQUEST_SIZE)}"
        )
    return local_size


def _validate_git_upload_size(path: Path) -> int:
    local_size = path.stat().st_size
    if local_size > GITHUB_CONTENTS_MAX_FILE_SIZE:
        raise UserError(
            f"file is too large for GitHub upload: {path} "
            f"({_format_size(local_size)}); GitHub file limit is {_format_size(GITHUB_CONTENTS_MAX_FILE_SIZE)}"
        )
    return local_size


def _git_blob_sha_for_file(path: Path, size: int) -> str:
    sha = hashlib.sha1(f"blob {size}\0".encode("utf-8"))
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _base64_encoded_size(size: int) -> int:
    return ((size + 2) // 3) * 4


def _format_size(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size} B"
            return f"{value:.1f} {unit}"
        value /= 1024


def build_remote_path(file_name: str, remote_path: Optional[str], remote_dir: str = "") -> str:
    if remote_path:
        rel = remote_path
    else:
        rel = str(file_name)
        if os.path.isabs(rel):
            rel = Path(rel).name
    rel = rel.replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.strip("/")
    parts = [part for part in rel.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise UserError("file path cannot be empty or contain '..'")

    prefix = normalize_remote_dir(remote_dir)
    return "/".join([prefix] + parts) if prefix else "/".join(parts)


def infer_copy_args(source: str, destination: Optional[str]) -> tuple[str, str]:
    if destination is not None:
        return source, destination
    target = parse_copy_target(source)
    if target.kind == "github":
        remote_path = default_remote_path_for_remote_arg(target.path)
        remote_source = f"{target.repo}:{remote_path}" if target.repo else f":{remote_path}"
        return remote_source, default_local_path_for_remote_arg(target.path)
    return source, f":{default_remote_path_for_local(target.path)}"


def default_remote_path_for_remote_arg(path: str) -> str:
    expanded = Path(path).expanduser()
    if path.startswith("~/") or expanded.is_absolute():
        return expanded.as_posix().lstrip("/")
    return path


def default_local_path_for_remote_arg(path: str) -> str:
    if path.startswith("~/"):
        return str(Path(path).expanduser())
    home_remote_prefix = Path.home().as_posix().lstrip("/")
    if path == home_remote_prefix or path.startswith(f"{home_remote_prefix}/"):
        return f"/{path}"
    return path


def default_remote_path_for_local(path: str) -> str:
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return expanded.as_posix().lstrip("/")
    return path.replace("\\", "/")


def parse_copy_target(value: str) -> CopyTarget:
    if value.startswith("github:"):
        return CopyTarget("github", value[len("github:") :], "")
    if value.startswith("gh:"):
        return CopyTarget("github", value[len("gh:") :], "")

    colon_index = value.find(":")
    if colon_index >= 0:
        repo_part = value[:colon_index]
        path = value[colon_index + 1 :]
        if repo_part in {"", "github", "gh"}:
            repo_part = ""
        return CopyTarget("github", path, repo_part)

    return CopyTarget("local", value)


def resolve_remote_repo(default_repo: str, repo_part: str) -> str:
    if not repo_part:
        return default_repo
    if "/" in repo_part:
        parse_repo(repo_part)
        return repo_part
    owner, _repo = parse_repo(default_repo)
    return f"{owner}/{repo_part}"


def output_path_for_remote(destination: str, remote_path: str) -> Path:
    path = Path(destination).expanduser()
    if path.exists() and path.is_dir():
        path = path / Path(remote_path).name
    return path.resolve(strict=False)


def output_path_for(file_name: str, output: Optional[str]) -> Path:
    if output:
        path = Path(output).expanduser()
        if path.exists() and path.is_dir():
            path = path / Path(file_name).name
        return path.resolve(strict=False)
    return Path(file_name).expanduser().resolve(strict=False)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
