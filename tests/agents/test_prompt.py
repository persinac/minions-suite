"""Tests for composable prompt assembly."""

from unittest.mock import patch

from minions.agents.prompt import (
    _ROLE_TO_LANGUAGE,
    _ROLE_TO_PROMPT,
    build_agent_prompt,
    build_review_prompt,
)
from minions.core.models import AgentRole, Job, Task
from minions.project_registry import ProjectConfig, ReviewProfile, ServiceTarget


def _make_project(**overrides):
    defaults = {
        "name": "test-project",
        "project_id": "team/test",
        "git_provider": "gitlab",
    }
    defaults.update(overrides)
    return ProjectConfig(**defaults)


def _make_review_task(**overrides):
    defaults = {
        "job_id": "j1",
        "title": "Review MR",
        "service": "api",
        "agent_role": AgentRole.CODE_REVIEWER,
        "mr_url": "https://gitlab.example.com/mr/1",
        "mr_id": "1",
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_dev_task(**overrides):
    defaults = {
        "job_id": "j1",
        "title": "Implement feature",
        "description": "Build the widget",
        "service": "api",
        "agent_role": AgentRole.BACKEND_ENGINEER,
    }
    defaults.update(overrides)
    return Task(**defaults)


# -----------------------------------------------------------------------
# Review prompt
# -----------------------------------------------------------------------


class TestBuildReviewPrompt:
    @patch("minions.agents.prompt._load")
    def test_includes_base(self, mock_load):
        mock_load.return_value = "BASE CONTENT"
        project = _make_project(review_profile=ReviewProfile(roles=[], languages=[]))
        task = _make_review_task()
        result = build_review_prompt(task, project, ["foo.py"])
        assert "BASE CONTENT" in result

    @patch("minions.agents.prompt._load")
    def test_includes_review_context(self, mock_load):
        mock_load.return_value = "mixin"
        project = _make_project()
        task = _make_review_task()
        result = build_review_prompt(task, project, ["foo.py", "bar.py"])
        assert "Review Context" in result
        assert "test-project" in result
        assert "foo.py" in result

    @patch("minions.agents.prompt._load")
    def test_auto_infers_profile(self, mock_load):
        mock_load.return_value = "mixin"
        project = _make_project(review_profile=ReviewProfile())  # empty = auto-infer
        task = _make_review_task()
        build_review_prompt(task, project, ["src/api/handler.py", "db/migration.sql"])
        # Should have called _load for inferred roles/languages
        assert mock_load.call_count >= 1

    @patch("minions.agents.prompt._load")
    def test_sections_joined_with_separator(self, mock_load):
        mock_load.return_value = "SECTION"
        project = _make_project(review_profile=ReviewProfile(roles=["backend"], languages=["python"]))
        task = _make_review_task()
        result = build_review_prompt(task, project, [])
        assert "---" in result

    @patch("minions.agents.prompt._load")
    def test_ignore_paths_shown(self, mock_load):
        mock_load.return_value = "mixin"
        project = _make_project(ignore_paths=["vendor/", "dist/"])
        task = _make_review_task()
        result = build_review_prompt(task, project, [])
        assert "Ignore paths" in result
        assert "vendor/" in result

    @patch("minions.agents.prompt._load")
    def test_auto_approve_paths_shown(self, mock_load):
        mock_load.return_value = "mixin"
        project = _make_project(auto_approve_paths=["docs/"])
        task = _make_review_task()
        result = build_review_prompt(task, project, [])
        assert "Auto-approve" in result

    @patch("minions.agents.prompt._load")
    def test_many_files_truncated(self, mock_load):
        mock_load.return_value = "mixin"
        project = _make_project()
        task = _make_review_task()
        files = [f"file{i}.py" for i in range(100)]
        result = build_review_prompt(task, project, files)
        assert "more files" in result


# -----------------------------------------------------------------------
# Agent prompt
# -----------------------------------------------------------------------


class TestBuildAgentPrompt:
    @patch("minions.agents.prompt._load")
    def test_includes_task_context(self, mock_load):
        mock_load.return_value = "agent base"
        job = Job(spec="Build widget")
        task = _make_dev_task(job_id=job.id)
        result = build_agent_prompt(job, task)
        assert "Task Context" in result
        assert job.id in result
        assert task.id in result
        assert "Build widget" in result

    @patch("minions.agents.prompt._load")
    def test_includes_service_info(self, mock_load):
        mock_load.return_value = "agent base"
        job = Job(spec="test")
        task = _make_dev_task(job_id=job.id)
        service = ServiceTarget(name="api", clone_url="git@example.com:api.git", test_command="pytest")
        result = build_agent_prompt(job, task, service=service)
        assert "git@example.com:api.git" in result
        assert "pytest" in result

    @patch("minions.agents.prompt._load")
    def test_includes_additional_context(self, mock_load):
        mock_load.return_value = "agent base"
        job = Job(spec="test")
        task = _make_dev_task(job_id=job.id)
        result = build_agent_prompt(job, task, context="Previous attempt failed with error X")
        assert "Previous attempt failed" in result


class TestRoleMappings:
    def test_all_roles_have_prompt_mapping(self):
        expected_roles = {
            "spec_analyst",
            "arbiter",
            "backend_engineer",
            "frontend_engineer",
            "database_engineer",
            "code_reviewer",
            "deploy_monitor",
            "finisher",
        }
        assert set(_ROLE_TO_PROMPT.keys()) == expected_roles

    def test_every_agent_role_is_mapped(self):
        """The set above is hand-written, so it can agree with itself while
        still missing a role that exists. A role with no mapping silently falls
        back to the engineer prompt — which for the finisher would hand it the
        very workflow it was created to avoid."""
        from minions.core.models import AgentRole

        assert {r.value for r in AgentRole} <= set(_ROLE_TO_PROMPT.keys())

    def test_engineer_roles_share_prompt(self):
        assert _ROLE_TO_PROMPT["backend_engineer"] == _ROLE_TO_PROMPT["frontend_engineer"]
        assert _ROLE_TO_PROMPT["backend_engineer"] == _ROLE_TO_PROMPT["database_engineer"]

    def test_the_finisher_does_not_share_the_engineer_prompt(self):
        assert _ROLE_TO_PROMPT["finisher"] != _ROLE_TO_PROMPT["backend_engineer"]

    def test_language_mappings(self):
        assert _ROLE_TO_LANGUAGE["backend_engineer"] == "python"
        assert _ROLE_TO_LANGUAGE["frontend_engineer"] == "typescript"
        assert _ROLE_TO_LANGUAGE["database_engineer"] == "sql"
