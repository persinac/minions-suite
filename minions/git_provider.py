"""Git provider abstraction — GitLab, GitHub, Bitbucket.

Each provider implements the same protocol for fetching MR/PR data and
posting review comments. The agent's tools dispatch through this layer
so tool definitions are provider-agnostic.
"""

import logging
import subprocess
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


@dataclass
class PRInfo:
    """Normalized merge/pull request metadata."""

    id: str  # MR IID (GitLab) or PR number (GitHub)
    url: str
    title: str
    description: str
    author: str
    branch: str
    target_branch: str
    state: str  # open, merged, closed


@dataclass
class InlineComment:
    """A comment on a specific file and line."""

    file_path: str
    line: int
    body: str
    side: str = "RIGHT"  # LEFT = old code, RIGHT = new code


@runtime_checkable
class GitProviderProtocol(Protocol):
    """Interface that all git providers must implement."""

    async def get_pr(self, project_id: str, mr_id: str) -> PRInfo: ...

    async def get_diff(self, project_id: str, mr_id: str) -> str: ...

    async def get_changed_files(self, project_id: str, mr_id: str) -> list[str]: ...

    async def get_comments(self, project_id: str, mr_id: str) -> list[dict]: ...

    async def post_inline_comment(self, project_id: str, mr_id: str, comment: InlineComment) -> dict: ...

    async def submit_review(self, project_id: str, mr_id: str, verdict: str, body: str) -> dict: ...


class GitLabProvider:
    """GitLab MR operations via REST API v4."""

    def __init__(self, gitlab_url: str, token: str):
        self.base_url = gitlab_url.rstrip("/")
        self.token = token
        self._headers = {"PRIVATE-TOKEN": token}

    def _api(self, path: str) -> str:
        return f"{self.base_url}/api/v4{path}"

    def _encode_project(self, project_id: str) -> str:
        return project_id.replace("/", "%2F")

    async def get_pr(self, project_id: str, mr_id: str) -> PRInfo:
        encoded = self._encode_project(project_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}"),
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return PRInfo(
                id=str(data["iid"]),
                url=data["web_url"],
                title=data["title"],
                description=data.get("description", ""),
                author=data["author"]["username"],
                branch=data["source_branch"],
                target_branch=data["target_branch"],
                state=data["state"],
            )

    async def get_diff(self, project_id: str, mr_id: str) -> str:
        encoded = self._encode_project(project_id)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}/diffs"),
                headers=self._headers,
                params={"per_page": 100},
            )
            resp.raise_for_status()
            diffs = resp.json()

            parts = []
            for d in diffs:
                header = f"--- a/{d['old_path']}\n+++ b/{d['new_path']}"
                parts.append(f"{header}\n{d.get('diff', '')}")
            return "\n".join(parts)

    async def get_changed_files(self, project_id: str, mr_id: str) -> list[str]:
        encoded = self._encode_project(project_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}/diffs"),
                headers=self._headers,
                params={"per_page": 100},
            )
            resp.raise_for_status()
            return [d["new_path"] for d in resp.json()]

    async def get_comments(self, project_id: str, mr_id: str) -> list[dict]:
        encoded = self._encode_project(project_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}/notes"),
                headers=self._headers,
                params={"per_page": 100, "sort": "asc"},
            )
            resp.raise_for_status()
            return [
                {
                    "author": n["author"]["username"],
                    "body": n["body"],
                    "created_at": n["created_at"],
                    "system": n.get("system", False),
                }
                for n in resp.json()
                if not n.get("system", False)
            ]

    async def post_inline_comment(self, project_id: str, mr_id: str, comment: InlineComment) -> dict:
        encoded = self._encode_project(project_id)
        # GitLab uses "discussions" for inline MR comments
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "body": comment.body,
                "position": {
                    "base_sha": "",  # Will be filled by _get_versions
                    "start_sha": "",
                    "head_sha": "",
                    "position_type": "text",
                    "new_path": comment.file_path,
                    "new_line": comment.line,
                },
            }

            # Get MR versions to fill SHA fields
            versions_resp = await client.get(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}/versions"),
                headers=self._headers,
            )
            versions_resp.raise_for_status()
            versions = versions_resp.json()
            if versions:
                latest = versions[0]
                payload["position"]["base_sha"] = latest["base_commit_sha"]
                payload["position"]["start_sha"] = latest["start_commit_sha"]
                payload["position"]["head_sha"] = latest["head_commit_sha"]

            resp = await client.post(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}/discussions"),
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def submit_review(self, project_id: str, mr_id: str, verdict: str, body: str) -> dict:
        encoded = self._encode_project(project_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Post summary as a note
            resp = await client.post(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}/notes"),
                headers=self._headers,
                json={"body": body},
            )
            resp.raise_for_status()

            # Approve if verdict is approve
            result = {"note_id": resp.json()["id"], "verdict": verdict}
            if verdict == "approve":
                approve_resp = await client.post(
                    self._api(f"/projects/{encoded}/merge_requests/{mr_id}/approve"),
                    headers=self._headers,
                )
                if approve_resp.status_code == 200:
                    result["approved"] = True
                else:
                    logger.warning("GitLab approve returned %d: %s", approve_resp.status_code, approve_resp.text[:200])
                    result["approved"] = False

            return result


class GitHubProvider:
    """GitHub PR operations via gh CLI."""

    def __init__(self, token: str = ""):
        self.token = token

    def _run_gh(self, args: list[str], timeout: int = 30) -> str:
        """Run a gh CLI command and return stdout."""
        env = None
        if self.token:
            import os

            env = {**os.environ, "GH_TOKEN": self.token}
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr[:200]}")
        return result.stdout.strip()

    async def get_pr(self, project_id: str, mr_id: str) -> PRInfo:
        import json

        raw = self._run_gh([
            "pr", "view", mr_id,
            "--repo", project_id,
            "--json", "number,url,title,body,author,headRefName,baseRefName,state",
        ])
        data = json.loads(raw)
        return PRInfo(
            id=str(data["number"]),
            url=data["url"],
            title=data["title"],
            description=data.get("body", ""),
            author=data["author"]["login"],
            branch=data["headRefName"],
            target_branch=data["baseRefName"],
            state=data["state"].lower(),
        )

    async def get_diff(self, project_id: str, mr_id: str) -> str:
        return self._run_gh(["pr", "diff", mr_id, "--repo", project_id], timeout=60)

    async def get_changed_files(self, project_id: str, mr_id: str) -> list[str]:
        import json

        raw = self._run_gh([
            "pr", "view", mr_id,
            "--repo", project_id,
            "--json", "files",
        ])
        data = json.loads(raw)
        return [f["path"] for f in data.get("files", [])]

    async def get_comments(self, project_id: str, mr_id: str) -> list[dict]:
        import json

        raw = self._run_gh([
            "pr", "view", mr_id,
            "--repo", project_id,
            "--json", "comments",
        ])
        data = json.loads(raw)
        return [
            {
                "author": c["author"]["login"],
                "body": c["body"],
                "created_at": c["createdAt"],
            }
            for c in data.get("comments", [])
        ]

    async def post_inline_comment(self, project_id: str, mr_id: str, comment: InlineComment) -> dict:
        # GitHub doesn't support inline PR comments via gh CLI easily.
        # Fall back to a regular comment with file/line reference.
        body = f"**{comment.file_path}:{comment.line}**\n\n{comment.body}"
        self._run_gh([
            "pr", "comment", mr_id,
            "--repo", project_id,
            "--body", body,
        ])
        return {"posted": True, "inline": False}

    async def submit_review(self, project_id: str, mr_id: str, verdict: str, body: str) -> dict:
        if verdict == "approve":
            self._run_gh([
                "pr", "review", mr_id,
                "--repo", project_id,
                "--approve",
                "--body", body,
            ])
        else:
            self._run_gh([
                "pr", "review", mr_id,
                "--repo", project_id,
                "--request-changes",
                "--body", body,
            ])
        return {"verdict": verdict, "posted": True}


def create_provider(provider_type: str, **kwargs) -> GitProviderProtocol:
    """Factory function to create a git provider instance."""
    if provider_type == "gitlab":
        gitlab_url = kwargs.get("gitlab_url", "")
        token = kwargs.get("token", "")
        if not gitlab_url:
            raise ValueError("gitlab_url is required for GitLab provider")
        if not token:
            raise ValueError("gitlab_token is required for GitLab provider")
        return GitLabProvider(gitlab_url, token)

    if provider_type == "github":
        token = kwargs.get("token", "")
        return GitHubProvider(token)

    raise ValueError(f"Unsupported git provider: {provider_type}")
