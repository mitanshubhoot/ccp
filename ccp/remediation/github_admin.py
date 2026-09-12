"""The only GitHub calls that change anything, behind a seam so tests can record them. Nothing
here deletes a repository, a branch, a file, or an organization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class PlannedCall:
    method: str
    path: str
    body: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "path": self.path, "body": self.body}


def plan_remove_collaborator(org: str, repo: str, login: str) -> PlannedCall:
    return PlannedCall("DELETE", f"/repos/{org}/{repo}/collaborators/{login}")


def plan_add_collaborator(org: str, repo: str, login: str, permission: str) -> PlannedCall:
    return PlannedCall(
        "PUT", f"/repos/{org}/{repo}/collaborators/{login}", {"permission": permission}
    )


def plan_remove_org_member(org: str, login: str) -> PlannedCall:
    return PlannedCall("DELETE", f"/orgs/{org}/members/{login}")


class GitHubAdmin(Protocol):
    def collaborator_permission(self, org: str, repo: str, login: str) -> str | None: ...

    def remove_collaborator(self, org: str, repo: str, login: str) -> dict[str, Any]: ...

    def add_collaborator(
        self, org: str, repo: str, login: str, permission: str
    ) -> dict[str, Any]: ...


class LiveGitHubAdmin:
    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def collaborator_permission(self, org: str, repo: str, login: str) -> str | None:
        response = self._client.get(f"/repos/{org}/{repo}/collaborators/{login}/permission")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        return str(data.get("role_name") or data.get("permission") or "") or None

    def remove_collaborator(self, org: str, repo: str, login: str) -> dict[str, Any]:
        response = self._client.delete(f"/repos/{org}/{repo}/collaborators/{login}")
        response.raise_for_status()
        return {"status_code": response.status_code}

    def add_collaborator(self, org: str, repo: str, login: str, permission: str) -> dict[str, Any]:
        response = self._client.put(
            f"/repos/{org}/{repo}/collaborators/{login}", json={"permission": permission}
        )
        response.raise_for_status()
        return {"status_code": response.status_code}


@dataclass
class RecordingGitHubAdmin:
    """Test double: records every call, never talks to GitHub."""

    permissions: dict[tuple[str, str, str], str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def collaborator_permission(self, org: str, repo: str, login: str) -> str | None:
        self.calls.append({"method": "GET", "org": org, "repo": repo, "login": login})
        return self.permissions.get((org, repo, login))

    def remove_collaborator(self, org: str, repo: str, login: str) -> dict[str, Any]:
        self.calls.append({"method": "DELETE", "org": org, "repo": repo, "login": login})
        self.permissions.pop((org, repo, login), None)
        return {"status_code": 204}

    def add_collaborator(self, org: str, repo: str, login: str, permission: str) -> dict[str, Any]:
        self.calls.append(
            {"method": "PUT", "org": org, "repo": repo, "login": login, "permission": permission}
        )
        self.permissions[(org, repo, login)] = permission
        return {"status_code": 201}

    @property
    def deletes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "DELETE"]
