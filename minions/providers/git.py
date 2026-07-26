"""Git provider abstraction — GitLab, GitHub, Bitbucket.

Each provider implements the same protocol for fetching MR/PR data and
posting review comments. The agent's tools dispatch through this layer
so tool definitions are provider-agnostic.
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

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

    async def merge_mr(self, project_id: str, mr_id: str) -> dict: ...

    async def add_mr_labels(self, project_id: str, mr_id: str, labels: list[str]) -> dict: ...


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
                    "base_sha": "",
                    "start_sha": "",
                    "head_sha": "",
                    "position_type": "text",
                    "old_path": comment.file_path,
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

            # If inline comment fails, fall back to a regular MR note
            if resp.status_code == 400:
                logger.warning("Inline comment failed (400), falling back to MR note: %s", resp.text[:200])
                fallback_body = f"**{comment.file_path}:{comment.line}**\n\n{comment.body}"
                fallback_resp = await client.post(
                    self._api(f"/projects/{encoded}/merge_requests/{mr_id}/notes"),
                    headers=self._headers,
                    json={"body": fallback_body},
                )
                fallback_resp.raise_for_status()
                return fallback_resp.json()

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

    async def merge_mr(self, project_id: str, mr_id: str) -> dict:
        encoded = self._encode_project(project_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.put(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}/merge"),
                headers=self._headers,
                json={"should_remove_source_branch": True},
            )
            if resp.status_code == 200:
                return {"merged": True}
            logger.warning("GitLab merge returned %d: %s", resp.status_code, resp.text[:200])
            return {"merged": False, "error": resp.text[:200]}

    async def add_mr_labels(self, project_id: str, mr_id: str, labels: list[str]) -> dict:
        encoded = self._encode_project(project_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.put(
                self._api(f"/projects/{encoded}/merge_requests/{mr_id}"),
                headers=self._headers,
                json={"add_labels": ",".join(labels)},
            )
            if resp.status_code == 200:
                return {"labels_added": labels}
            logger.warning("GitLab add_mr_labels returned %d: %s", resp.status_code, resp.text[:200])
            return {"labels_added": [], "error": resp.text[:200]}

    async def create_mr(
        self, project_id: str, source_branch: str, target_branch: str, title: str, description: str = "", labels: list[str] | None = None
    ) -> dict:
        """Create a merge request with optional labels."""
        encoded = self._encode_project(project_id)
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        if labels:
            payload["labels"] = ",".join(labels)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._api(f"/projects/{encoded}/merge_requests"),
                headers=self._headers,
                json=payload,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return {"mr_url": data["web_url"], "mr_id": str(data["iid"]), "created": True}
            logger.warning("GitLab create_mr returned %d: %s", resp.status_code, resp.text[:200])
            return {"created": False, "error": resp.text[:200]}


class GitHubProvider:
    """GitHub PR operations via gh CLI."""

    def __init__(self, token: str = ""):
        self.token = token

    def _run_gh(self, args: list[str], timeout: int = 30, stdin: str | None = None) -> str:
        """Run a gh CLI command and return stdout.

        `stdin` feeds a JSON body to `gh api --input -`. Review comment bodies
        contain newlines, quotes and backticks; passing those as argv is a
        quoting minefield, and `--field` form-encodes rather than sending JSON.
        """
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
            input=stdin,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr[:200]}")
        return result.stdout.strip()

    async def get_pr(self, project_id: str, mr_id: str) -> PRInfo:
        import json

        raw = self._run_gh(
            [
                "pr",
                "view",
                mr_id,
                "--repo",
                project_id,
                "--json",
                "number,url,title,body,author,headRefName,baseRefName,state",
            ]
        )
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

        raw = self._run_gh(
            [
                "pr",
                "view",
                mr_id,
                "--repo",
                project_id,
                "--json",
                "files",
            ]
        )
        data = json.loads(raw)
        return [f["path"] for f in data.get("files", [])]

    async def get_comments(self, project_id: str, mr_id: str) -> list[dict]:
        import json

        raw = self._run_gh(
            [
                "pr",
                "view",
                mr_id,
                "--repo",
                project_id,
                "--json",
                "comments",
            ]
        )
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
        """Post a genuinely inline review comment, anchored to path:line.

        Previously this always degraded to a top-level PR comment, on the belief
        that gh could not do inline. `gh pr comment` cannot, but `gh api` posts
        to the pulls/{n}/comments endpoint fine — it just needs the head SHA and
        a JSON body rather than form fields.

        Anchoring is the point: the reviewer personas each require exactly one
        `<new-path>:<line>` per finding so it lands on the offending line. A
        top-level blob of twenty findings is markedly less useful to whoever has
        to act on it.

        Falls back to a top-level comment when the line is not part of the diff
        (GitHub 422s those). A finding is never dropped — worst case it loses its
        anchor and says so.
        """
        try:
            sha = await self.get_pr_head_sha(project_id, mr_id)
        except RuntimeError as e:
            logger.warning("Could not resolve head SHA for %s#%s: %s", project_id, mr_id, str(e)[:120])
            return await self._post_unanchored(project_id, mr_id, comment, "head SHA unavailable")

        payload = {
            "commit_id": sha,
            "path": comment.file_path,
            "line": comment.line,
            "side": comment.side,
            "body": comment.body,
        }

        try:
            self._run_gh(
                ["api", "--method", "POST", f"/repos/{project_id}/pulls/{mr_id}/comments", "--input", "-"],
                stdin=json.dumps(payload),
            )
            return {"posted": True, "inline": True, "path": comment.file_path, "line": comment.line}
        except RuntimeError as e:
            # 422 is the common one: the line is not in the diff, so it cannot be
            # anchored. Unprocessable, not retryable — degrade rather than lose it.
            logger.info(
                "Inline anchor rejected for %s:%s on %s#%s (%s) — posting unanchored",
                comment.file_path,
                comment.line,
                project_id,
                mr_id,
                str(e)[:120],
            )
            return await self._post_unanchored(project_id, mr_id, comment, "line not in diff")

    async def _post_unanchored(self, project_id: str, mr_id: str, comment: InlineComment, reason: str) -> dict:
        """Last-resort top-level comment, keeping the location in the text."""
        body = f"**{comment.file_path}:{comment.line}**\n\n{comment.body}\n\n_(not anchored inline: {reason})_"
        self._run_gh(["pr", "comment", mr_id, "--repo", project_id, "--body", body])
        return {"posted": True, "inline": False, "reason": reason}

    async def get_required_checks(self, project_id: str, branch: str) -> list[str]:
        """Required status-check names in force on a branch, or [] if none.

        Reads /repos/{repo}/rules/branches/{branch} — the EFFECTIVE rules for the
        branch, aggregated across every ruleset that targets it.

        Deliberately not /branches/{branch}/protection/required_status_checks.
        That endpoint only reports *classic* branch protection: an org-level
        ruleset does not appear there at all, so a gate reading it sees [] on a
        fully-protected branch. Verified against wallet-api@main, where the
        classic endpoint returned "Resource not accessible by integration" while
        the rules endpoint returned the live org ruleset (id 19750440) with
        required_status_checks: ['secret-scan'].

        The classic endpoint also needs Administration:read, which this App does
        not have and does not need; the rules endpoint works with its existing
        grant. Two reasons to prefer it, either sufficient.

        An empty list means nothing is required, which callers treat as BLOCK for
        agent PRs.
        """
        try:
            raw = self._run_gh(["api", f"/repos/{project_id}/rules/branches/{branch}"])
        except RuntimeError as e:
            logger.info("Could not read branch rules for %s@%s (%s)", project_id, branch, str(e)[:100])
            return []

        try:
            rules = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Unparseable branch-rules payload for %s@%s", project_id, branch)
            return []

        if not isinstance(rules, list):
            return []

        names: list[str] = []
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            params = rule.get("parameters") or {}
            for check in params.get("required_status_checks") or []:
                context = check.get("context") if isinstance(check, dict) else None
                if context and context not in names:
                    names.append(context)

        return names

    async def get_check_runs(self, project_id: str, sha: str) -> dict[str, str]:
        """Map check-run name -> conclusion for a commit.

        Deliberately the Checks API, NOT the combined commit status. GitHub
        Actions posts check-runs, and those are not aggregated into
        /commits/{sha}/status — so a status-only gate sees a fully-gated repo as
        empty and, with empty-means-pass semantics, merges anyway.
        """
        try:
            raw = self._run_gh(["api", "--paginate", f"/repos/{project_id}/commits/{sha}/check-runs"])
        except RuntimeError as e:
            logger.warning("Could not read check-runs for %s@%s: %s", project_id, sha[:8], str(e)[:120])
            return {}

        runs: dict[str, str] = {}
        for chunk in raw.split("\n"):
            if not chunk.strip():
                continue
            try:
                payload = json.loads(chunk)
            except (json.JSONDecodeError, ValueError):
                continue
            for run in payload.get("check_runs", []):
                name = run.get("name")
                if not name:
                    continue
                # in_progress/queued runs have conclusion=None; surface the status
                # so the caller can tell "still running" from "failed".
                runs[name] = run.get("conclusion") or run.get("status") or "unknown"
        return runs

    async def get_pr_head_sha(self, project_id: str, mr_id: str) -> str:
        """Head commit of a PR — the SHA that check-runs are attached to."""
        raw = self._run_gh(["api", f"/repos/{project_id}/pulls/{mr_id}", "--jq", ".head.sha"])
        return raw.strip()

    # GitHub refuses a formal review on a PR you authored:
    #   GraphQL: Review Can not approve your own pull request (addPullRequestReview)
    # The same GitHub App opens the PR and reviews it, so this fires on every
    # minion-authored PR. It is a platform rule, not a permissions problem —
    # no token scope makes it work.
    _SELF_REVIEW_MARKERS = ("your own pull request", "can not approve your own")

    async def submit_review(self, project_id: str, mr_id: str, verdict: str, body: str) -> dict:
        flag = "--approve" if verdict == "approve" else "--request-changes"

        try:
            self._run_gh(["pr", "review", mr_id, "--repo", project_id, flag, "--body", body])
            return {"verdict": verdict, "posted": True, "as": "review"}
        except RuntimeError as e:
            if not any(marker in str(e).lower() for marker in self._SELF_REVIEW_MARKERS):
                raise

        # Fall back to a plain PR comment. The verdict is stated in the body so
        # it is not lost, just not enforced by GitHub's review machinery. Same
        # precedent as post_inline_comment, which already degrades to a regular
        # comment on GitHub. Previously this raised and the reviewer's entire
        # analysis was discarded.
        label = "APPROVED" if verdict == "approve" else "CHANGES REQUESTED"
        commented = (
            f"**Automated review — {label}**\n\n"
            f"{body}\n\n"
            "_Posted as a comment rather than a formal review: GitHub does not permit "
            "the identity that opened a pull request to review it._"
        )
        self._run_gh(["pr", "comment", mr_id, "--repo", project_id, "--body", commented])
        logger.info("Posted review for %s#%s as a comment (self-authored PR)", project_id, mr_id)
        return {"verdict": verdict, "posted": True, "as": "comment", "reason": "self-authored PR"}

    async def merge_mr(self, project_id: str, mr_id: str) -> dict:
        try:
            self._run_gh(["pr", "merge", mr_id, "--repo", project_id, "--squash", "--delete-branch"])
            return {"merged": True}
        except RuntimeError as e:
            logger.warning("GitHub merge failed: %s", e)
            return {"merged": False, "error": str(e)[:200]}

    async def add_mr_labels(self, project_id: str, mr_id: str, labels: list[str]) -> dict:
        try:
            self._run_gh(["pr", "edit", mr_id, "--repo", project_id, "--add-label", ",".join(labels)])
            return {"labels_added": labels}
        except RuntimeError as e:
            logger.warning("GitHub add_mr_labels failed: %s", e)
            return {"labels_added": [], "error": str(e)[:200]}


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
