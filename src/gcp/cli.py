from __future__ import annotations

import argparse
import getpass
import os
import sys
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
from .github import GitHubClient, GitHubError, parse_repo
from .oauth import OAuthError, login_with_browser

HELP = """GitHub-backed file sync.

Usage:
  uvx gcp setting [login|logout|status|repo] [options]
  uvx gcp push FILE [options]
  uvx gcp FILE [options]
  uvx gcp status

Commands:
  setting          Configure GitHub auth and the target repository
  push FILE        Upload a local file to GitHub
  FILE             Download a GitHub file to the local path
  status           Show current auth/repository settings

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
    if command == "push":
        return run_push(argv[1:])
    if command == "status":
        return run_status(argv[1:])

    return run_pull(argv)


def run_push(argv: list[str]) -> int:
    parser = command_parser(prog="gcp push", description="Upload a local file to GitHub.")
    parser.add_argument("file_name", help="Local file to upload")
    add_context_flags(parser)
    parser.add_argument("--remote-path", help="GitHub repository path to write to (defaults to FILE)")
    parser.add_argument("-m", "--message", help="Commit message")
    args = parser.parse_args(argv)

    local_path = Path(args.file_name).expanduser()
    if not local_path.exists():
        raise UserError(f"local file does not exist: {local_path}")
    if not local_path.is_file():
        raise UserError(f"local path is not a file: {local_path}")

    ctx = resolve_context(args)
    remote_path = build_remote_path(args.file_name, args.remote_path, ctx.remote_dir)
    message = args.message or f"gcp: sync {remote_path}"

    client = GitHubClient(ctx.token, ctx.host)
    result = client.put_file(ctx.repo, remote_path, local_path.read_bytes(), message=message, branch=ctx.branch)
    commit = result.get("commit", {}) if isinstance(result, dict) else {}
    short_sha = str(commit.get("sha", ""))[:7]
    suffix = f" @ {short_sha}" if short_sha else ""
    print(f"✓ Pushed {local_path} to {ctx.repo}:{remote_path} on {ctx.branch}{suffix}")
    return 0


def run_pull(argv: list[str]) -> int:
    parser = command_parser(prog="gcp", description="Download a GitHub file to a local path.")
    parser.add_argument("file_name", help="File path to download from GitHub and write locally")
    add_context_flags(parser)
    parser.add_argument("--remote-path", help="GitHub repository path to read from (defaults to FILE)")
    parser.add_argument("-o", "--output", help="Local output file or existing directory")
    parser.add_argument("-f", "--force", action="store_true", help="Overwrite local changes without prompting")
    args = parser.parse_args(argv)

    ctx = resolve_context(args)
    remote_path = build_remote_path(args.file_name, args.remote_path, ctx.remote_dir)

    client = GitHubClient(ctx.token, ctx.host)
    content, _metadata = client.download_file(ctx.repo, remote_path, ref=ctx.branch)

    output_path = output_path_for(args.file_name, args.output)
    if output_path.exists() and output_path.read_bytes() != content and not args.force:
        if can_prompt():
            if not confirm(f"Overwrite local file {output_path}?", default=False):
                raise UserError("local file was not overwritten")
        else:
            raise UserError(f"local file exists and differs: {output_path}; pass --force to overwrite")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    print(f"✓ Pulled {ctx.repo}:{remote_path} on {ctx.branch} to {output_path}")
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


def interactive_setting() -> int:
    print("GitHub-backed file sync settings\n")
    choices = [
        "Log in to a GitHub account",
        "Log out of a GitHub account",
        "Show current status",
        "Change repository/branch",
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


def output_path_for(file_name: str, output: Optional[str]) -> Path:
    if output:
        path = Path(output).expanduser()
        if path.exists() and path.is_dir():
            return path / Path(file_name).name
        return path
    return Path(file_name).expanduser()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
