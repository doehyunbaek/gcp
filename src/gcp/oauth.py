from __future__ import annotations

import json
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import DEFAULT_HOST, normalize_host
from .github import GitHubClient

# Same OAuth app used by GitHub CLI. The upstream source notes that this secret
# is safe to embed in version control: ~/cli/internal/authflow/flow.go.
OAUTH_CLIENT_ID = "178c6fc778ccc68e1d6a"
OAUTH_CLIENT_SECRET = "34ddeff2b558a23d38fba8a6de74f086ede1cc0b"
DEFAULT_SCOPES = ("repo", "read:org", "gist")


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class OAuthError(RuntimeError):
    pass


def oauth_base(hostname: str) -> str:
    hostname = normalize_host(hostname)
    if hostname == DEFAULT_HOST:
        return "https://github.com"
    return f"https://{hostname}"


def login_with_browser(
    hostname: str,
    *,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    interactive: bool = True,
    copy_to_clipboard: bool = False,
    open_browser: bool = True,
    timeout: int = 900,
) -> tuple[str, str]:
    """Run a GitHub OAuth device-code browser flow and return token/login."""
    hostname = normalize_host(hostname)
    device = request_device_code(hostname, scopes=scopes)

    if copy_to_clipboard and copy_text(device.user_code):
        print(f"! One-time code ({device.user_code}) copied to clipboard", file=sys.stderr)
    else:
        print(f"! First copy your one-time code: {device.user_code}", file=sys.stderr)

    if open_browser:
        if interactive:
            print(f"Press Enter to open {device.verification_uri} in your browser... ", end="", file=sys.stderr)
            try:
                input()
            except EOFError:
                pass
        else:
            print(f"Open this URL to continue in your web browser: {device.verification_uri}", file=sys.stderr)

        try:
            opened = webbrowser.open(device.verification_uri)
        except Exception as e:  # pragma: no cover - platform/browser dependent
            opened = False
            print(f"! Failed opening a web browser: {e}", file=sys.stderr)
        if not opened:
            print("  Please open the URL manually in your browser", file=sys.stderr)

    token = poll_for_access_token(hostname, device, timeout=timeout)
    username = GitHubClient(token, hostname).get_current_user()
    return token, username


def request_device_code(hostname: str, *, scopes: tuple[str, ...] = DEFAULT_SCOPES) -> DeviceCode:
    data = post_oauth_json(
        hostname,
        "/login/device/code",
        {
            "client_id": OAUTH_CLIENT_ID,
            "scope": " ".join(scopes),
        },
    )
    try:
        return DeviceCode(
            device_code=str(data["device_code"]),
            user_code=str(data["user_code"]),
            verification_uri=str(data.get("verification_uri") or data.get("verification_url")),
            expires_in=int(data.get("expires_in", 900)),
            interval=int(data.get("interval", 5)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise OAuthError(f"invalid device-code response from GitHub: {data}") from e


def poll_for_access_token(hostname: str, device: DeviceCode, *, timeout: int = 900) -> str:
    deadline = time.monotonic() + min(timeout, device.expires_in)
    interval = max(device.interval, 1)

    while time.monotonic() < deadline:
        time.sleep(interval)
        data = post_oauth_json(
            hostname,
            "/login/oauth/access_token",
            {
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "device_code": device.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )

        token = data.get("access_token")
        if token:
            return str(token)

        error = str(data.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise OAuthError("the one-time code expired; run `uvx gcp setting login --web` again")
        if error == "access_denied":
            raise OAuthError("browser authentication was cancelled")
        description = data.get("error_description") or error or data
        raise OAuthError(f"browser authentication failed: {description}")

    raise OAuthError("timed out waiting for browser authentication")


def post_oauth_json(hostname: str, path: str, fields: dict[str, Any]) -> dict[str, Any]:
    url = oauth_base(hostname) + path
    body = urlencode(fields).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "gcp/oauth",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise OAuthError(_extract_oauth_error(raw) or f"GitHub OAuth HTTP {e.code}") from e
    except URLError as e:
        raise OAuthError(f"could not reach GitHub OAuth endpoint: {e.reason}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OAuthError(f"invalid JSON response from GitHub OAuth endpoint: {e}") from e
    if not isinstance(data, dict):
        raise OAuthError("invalid response from GitHub OAuth endpoint")
    return data


def copy_text(text: str) -> bool:
    commands = []
    if sys.platform == "darwin":
        commands.append(["pbcopy"])
    elif sys.platform.startswith("win"):
        commands.append(["clip"])
    else:
        commands.extend([["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]])

    for command in commands:
        try:
            subprocess.run(command, input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def _extract_oauth_error(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if isinstance(data, dict):
        return str(data.get("error_description") or data.get("error") or "")
    return ""
