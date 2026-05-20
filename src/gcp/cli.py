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
from .github import GitHubClient, GitHubError, NotFound, git_blob_sha, parse_repo
from .oauth import OAuthError, login_with_browser

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
    uploaded = 0
    unchanged = 0
    for path in iter_local_files(local_dir):
        rel = path.relative_to(local_dir).as_posix()
        remote_path = build_remote_path(rel, f"{remote_dir}/{rel}", "")
        changed, _short_sha = upload_file_if_changed(client, remote_repo, branch, path, remote_path, message)
        if changed:
            uploaded += 1
        else:
            unchanged += 1
    return uploaded, unchanged


def upload_file_if_changed(
    client: GitHubClient,
    remote_repo: str,
    branch: str,
    local_path: Path,
    remote_path: str,
    message: Optional[str],
) -> tuple[bool, str]:
    local_content = local_path.read_bytes()
    local_sha = git_blob_sha(local_content)
    try:
        remote_metadata = client.get_contents(remote_repo, remote_path, ref=branch)
        if not isinstance(remote_metadata, dict) or remote_metadata.get("type") != "file":
            raise GitHubError(f"{remote_path} already exists and is not a file")
        if str(remote_metadata.get("sha") or "") == local_sha:
            return False, ""
    except NotFound:
        pass

    result = client.put_file(
        remote_repo,
        remote_path,
        local_content,
        message=message or f"gcp: sync {remote_path}",
        branch=branch,
    )
    commit = result.get("commit", {}) if isinstance(result, dict) else {}
    return True, str(commit.get("sha", ""))[:7]


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
