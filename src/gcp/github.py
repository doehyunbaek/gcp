from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import __version__
from .config import DEFAULT_HOST, normalize_host


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class NotFound(GitHubError):
    pass


class Unauthorized(GitHubError):
    pass


def api_base(hostname: str) -> str:
    hostname = normalize_host(hostname)
    if hostname == DEFAULT_HOST:
        return "https://api.github.com"
    return f"https://{hostname}/api/v3"


def parse_repo(repo: str) -> tuple[str, str]:
    repo = (repo or "").strip().strip("/")
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must be in owner/repo format")
    return parts[0], parts[1]


class GitHubClient:
    def __init__(self, token: str, hostname: str = DEFAULT_HOST, timeout: int = 30):
        if not token:
            raise ValueError("GitHub token is required")
        self.token = token
        self.hostname = normalize_host(hostname)
        self.base_url = api_base(self.hostname)
        self.timeout = timeout

    def _request(self, method: str, route: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + route
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": f"gcp/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            message = _extract_error_message(raw) or e.reason or f"HTTP {e.code}"
            if e.code == 401:
                raise Unauthorized(message, status=e.code) from e
            if e.code == 404:
                raise NotFound(message, status=e.code) from e
            raise GitHubError(message, status=e.code) from e
        except URLError as e:
            raise GitHubError(f"could not reach GitHub: {e.reason}") from e

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise GitHubError(f"invalid JSON response from GitHub: {e}") from e

    def get_current_user(self) -> str:
        data = self._request("GET", "/user")
        login = data.get("login")
        if not login:
            raise GitHubError("GitHub did not return a username for this token")
        return str(login)

    def get_repository(self, repo: str) -> Dict[str, Any]:
        owner, name = parse_repo(repo)
        return self._request("GET", f"/repos/{quote(owner)}/{quote(name)}")

    def get_contents(self, repo: str, path: str, *, ref: Optional[str] = None) -> Any:
        owner, name = parse_repo(repo)
        path = quote(path.strip("/"), safe="/")
        route = f"/repos/{quote(owner)}/{quote(name)}/contents/{path}"
        if ref:
            route += "?" + urlencode({"ref": ref})
        return self._request("GET", route)

    def download_file(self, repo: str, path: str, *, ref: Optional[str] = None) -> tuple[bytes, Dict[str, Any]]:
        data = self.get_contents(repo, path, ref=ref)
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubError(f"{path} is not a file in {repo}")
        if data.get("encoding") != "base64" or "content" not in data:
            raise GitHubError(f"{path} is too large or cannot be decoded via the GitHub contents API")
        encoded = str(data["content"]).replace("\n", "")
        try:
            return base64.b64decode(encoded, validate=True), data
        except ValueError as e:
            raise GitHubError(f"could not decode {path}: {e}") from e

    def put_file(
        self,
        repo: str,
        path: str,
        content: bytes,
        *,
        message: str,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        owner, name = parse_repo(repo)
        normalized_path = path.strip("/")
        route_path = quote(normalized_path, safe="/")
        route = f"/repos/{quote(owner)}/{quote(name)}/contents/{route_path}"

        sha = None
        try:
            existing = self.get_contents(repo, normalized_path, ref=branch)
            if isinstance(existing, list) or existing.get("type") != "file":
                raise GitHubError(f"{path} already exists and is not a file")
            sha = existing.get("sha")
        except NotFound:
            sha = None

        body: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
        }
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha

        return self._request("PUT", route, body)


def _extract_error_message(raw: str) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    message = data.get("message", "") if isinstance(data, dict) else ""
    errors = data.get("errors", []) if isinstance(data, dict) else []
    if errors:
        details = []
        for item in errors:
            if isinstance(item, dict):
                details.append(item.get("message") or item.get("code") or str(item))
            else:
                details.append(str(item))
        return f"{message}: {', '.join(details)}" if message else ", ".join(details)
    return str(message)
