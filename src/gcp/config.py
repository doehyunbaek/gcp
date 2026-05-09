from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

APP_NAME = "gcp"
DEFAULT_HOST = "github.com"
DEFAULT_BRANCH = "main"
CONFIG_VERSION = 1
CONFIG_ENV = "GCP_CONFIG"
CONFIG_DIR_ENV = "GCP_CONFIG_DIR"
TOKEN_ENV_VARS = ("GCP_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


class ConfigError(RuntimeError):
    """Raised when the local gcp configuration cannot be read or written."""


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_NAME


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.json"


def default_config() -> Dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "current_host": DEFAULT_HOST,
        "hosts": {},
    }


def load_config() -> Dict[str, Any]:
    path = config_path()
    if not path.exists():
        return default_config()

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid config file {path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"could not read config file {path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"invalid config file {path}: expected a JSON object")

    data.setdefault("version", CONFIG_VERSION)
    data.setdefault("current_host", DEFAULT_HOST)
    data.setdefault("hosts", {})
    if not isinstance(data["hosts"], dict):
        raise ConfigError(f"invalid config file {path}: hosts must be a JSON object")
    return data


def save_config(config: Dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(stat.S_IRWXU)
    except OSError:
        # Best effort; some filesystems do not support chmod.
        pass

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, sort_keys=True)
            f.write("\n")
        try:
            tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        tmp_path.replace(path)
    except OSError as e:
        raise ConfigError(f"could not write config file {path}: {e}") from e


def normalize_host(hostname: Optional[str]) -> str:
    hostname = (hostname or DEFAULT_HOST).strip().lower()
    if hostname.startswith("https://"):
        hostname = hostname[len("https://") :]
    elif hostname.startswith("http://"):
        hostname = hostname[len("http://") :]
    return hostname.strip("/") or DEFAULT_HOST


def get_host(config: Dict[str, Any], hostname: str) -> Dict[str, Any]:
    hosts = config.setdefault("hosts", {})
    return hosts.setdefault(normalize_host(hostname), {"current_user": "", "users": {}})


def get_user(config: Dict[str, Any], hostname: str, username: str) -> Optional[Dict[str, Any]]:
    host_cfg = config.get("hosts", {}).get(normalize_host(hostname), {})
    user_cfg = host_cfg.get("users", {}).get(username)
    return user_cfg if isinstance(user_cfg, dict) else None


def hosts(config: Dict[str, Any]) -> Iterable[str]:
    return sorted(config.get("hosts", {}).keys())


def users_for_host(config: Dict[str, Any], hostname: str) -> Iterable[str]:
    host_cfg = config.get("hosts", {}).get(normalize_host(hostname), {})
    users = host_cfg.get("users", {})
    return sorted(users.keys()) if isinstance(users, dict) else []


def env_token() -> Tuple[Optional[str], Optional[str]]:
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return name, value.strip()
    return None, None


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "…" + token[-4:]


def account_candidates(config: Dict[str, Any], hostname: str = "", username: str = "") -> list[tuple[str, str]]:
    wanted_host = normalize_host(hostname) if hostname else ""
    candidates: list[tuple[str, str]] = []
    for host in hosts(config):
        if wanted_host and host != wanted_host:
            continue
        for user in users_for_host(config, host):
            if username and user != username:
                continue
            candidates.append((host, user))
    return candidates


def remove_account(config: Dict[str, Any], hostname: str, username: str) -> Tuple[Optional[str], Optional[str]]:
    hostname = normalize_host(hostname)
    host_cfg = config.get("hosts", {}).get(hostname)
    if not host_cfg:
        return None, None

    users = host_cfg.get("users", {})
    if username in users:
        del users[username]

    switched_user: Optional[str] = None
    if host_cfg.get("current_user") == username:
        remaining_users = sorted(users.keys())
        switched_user = remaining_users[0] if remaining_users else None
        host_cfg["current_user"] = switched_user or ""

    if not users:
        del config["hosts"][hostname]
        if config.get("current_host") == hostname:
            remaining_hosts = sorted(config.get("hosts", {}).keys())
            config["current_host"] = remaining_hosts[0] if remaining_hosts else DEFAULT_HOST
            return None, None

    return hostname, switched_user


def set_account(
    config: Dict[str, Any],
    hostname: str,
    username: str,
    *,
    token: Optional[str],
    repo: str,
    branch: str,
    remote_dir: str,
    token_source: str,
) -> None:
    hostname = normalize_host(hostname)
    host_cfg = get_host(config, hostname)
    users = host_cfg.setdefault("users", {})

    account = users.setdefault(username, {})
    account.update(
        {
            "repo": repo,
            "branch": branch,
            "remote_dir": normalize_remote_dir(remote_dir),
            "token_source": token_source,
        }
    )
    if token is not None:
        account["token"] = token
    elif token_source == "env":
        account.pop("token", None)

    host_cfg["current_user"] = username
    config["current_host"] = hostname


def normalize_remote_dir(remote_dir: Optional[str]) -> str:
    if not remote_dir:
        return ""
    value = str(remote_dir).strip().replace("\\", "/")
    value = value.strip("/")
    if value == ".":
        return ""
    parts = [p for p in value.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ConfigError("remote directory cannot contain '..'")
    return "/".join(parts)
