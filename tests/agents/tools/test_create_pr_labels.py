"""The PR is the deliverable; the label is bookkeeping. Never trade the first
for the second.

`gh pr create --label` resolves labels BEFORE creating anything, and the
per-job label (minions-job-<id>) can never pre-exist. Job 3b8b8ba9's engineer
finished all its code and died right there — "could not add label:
'minions-job-3b8b8ba9' not found" — with the PR never created. Creation and
labeling are now separate steps, and every label step is best-effort.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from minions.agents.tools.mcp_executor import McpToolExecutor


def _executor():
    config = MagicMock()
    config.git_provider = "github"
    return McpToolExecutor(
        mcp_server=None,
        job_id="3b8b8ba9deadbeef",
        task_id="14e04cb5",
        agent_id="a1",
        agent_role="backend_engineer",
        working_dir="/tmp",
        config=config,
        project=None,
    )


def _gh(responses: dict[str, tuple[int, str, str]]):
    """Stub _run_gh keyed on the first two args ("pr create", "label create", …)."""
    calls: list[tuple[str, ...]] = []

    async def _run(*args):
        calls.append(args)
        return responses.get(" ".join(args[:2]), (0, "", ""))

    return _run, calls


class TestCreationSurvivesTheLabel:
    async def test_the_pr_is_created_even_when_every_label_step_fails(self):
        executor = _executor()
        run, _calls = _gh(
            {
                "pr create": (0, "https://github.com/o/r/pull/9\n", ""),
                "label create": (1, "", "HTTP 403: Resource not accessible"),
                "pr edit": (1, "", "could not add label: 'minions-job-3b8b8ba9' not found"),
            }
        )
        with patch.object(executor, "_run_gh", new=run), patch("minions.providers.github_app.refresh_env_token", new=AsyncMock()):
            out = json.loads(await executor._create_pr({"title": "t", "body": "b"}))

        assert out == {"pr_url": "https://github.com/o/r/pull/9", "created": True}

    async def test_pr_create_carries_no_label_flag(self):
        """The failure mode was gh refusing to CREATE over an unresolvable
        label — the create call itself must never mention one again."""
        executor = _executor()
        run, calls = _gh({"pr create": (0, "https://github.com/o/r/pull/9\n", "")})
        with patch.object(executor, "_run_gh", new=run), patch("minions.providers.github_app.refresh_env_token", new=AsyncMock()):
            await executor._create_pr({"title": "t", "body": "b"})

        create_call = next(c for c in calls if c[:2] == ("pr", "create"))
        assert "--label" not in create_call

    async def test_the_label_is_created_then_attached_after_the_pr_exists(self):
        executor = _executor()
        run, calls = _gh({"pr create": (0, "https://github.com/o/r/pull/9\n", "")})
        with patch.object(executor, "_run_gh", new=run), patch("minions.providers.github_app.refresh_env_token", new=AsyncMock()):
            await executor._create_pr({"title": "t", "body": "b"})

        heads = [" ".join(c[:2]) for c in calls]
        assert heads == ["pr create", "label create", "pr edit"], "create first; label bookkeeping only after"
        label_call = calls[1]
        assert "minions-job-3b8b8ba9" in label_call
        edit_call = calls[2]
        assert "--add-label" in edit_call and "minions-job-3b8b8ba9" in edit_call

    async def test_a_failed_create_is_still_an_error_and_labels_are_not_attempted(self):
        executor = _executor()
        run, calls = _gh({"pr create": (1, "", "a PR for this branch already exists")})
        with patch.object(executor, "_run_gh", new=run), patch("minions.providers.github_app.refresh_env_token", new=AsyncMock()):
            out = json.loads(await executor._create_pr({"title": "t", "body": "b"}))

        assert "gh pr create failed" in out["error"]
        assert len(calls) == 1, "no label bookkeeping for a PR that does not exist"
