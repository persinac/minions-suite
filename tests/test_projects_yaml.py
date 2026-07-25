"""Validate the deployed projects.yaml against what the engine assumes of it.

JobEngine._resolve_service looks a service up by name across *every* registered
project and returns the first match, so service names must be globally unique.
Nothing enforced that. The generated file gave all 33 projects a service called
`app`, so every job resolved to whichever project sorted first — a wallet-api
job checked out Flashback-Android, with write, commit and push tools pointed at
the wrong repository.

The failure was invisible from the outside: the job advanced through its states
normally. These tests fail loudly instead.
"""

import collections
from pathlib import Path

import pytest
import yaml

PROJECTS_YAML = Path(__file__).resolve().parents[1] / "k8s" / "base" / "config" / "projects.yaml"


@pytest.fixture(scope="module")
def projects() -> dict:
    if not PROJECTS_YAML.exists():
        pytest.skip(f"{PROJECTS_YAML} not present")
    data = yaml.safe_load(PROJECTS_YAML.read_text(encoding="utf-8")) or {}
    return data.get("projects") or {}


class TestServiceNames:
    def test_service_names_are_globally_unique(self, projects):
        """The invariant _resolve_service depends on and never checks."""
        counts = collections.Counter(name for project in projects.values() for name in (project.get("services") or {}))
        duplicates = {name: n for name, n in counts.items() if n > 1}

        assert not duplicates, (
            f"Service names must be unique across all projects — _resolve_service "
            f"matches by name alone and returns the first hit. Duplicated: {duplicates}"
        )

    def test_every_project_defines_at_least_one_service(self, projects):
        """A project with no services can never be resolved, so no job can target it."""
        empty = [name for name, project in projects.items() if not (project.get("services") or {})]

        assert not empty, f"Projects with no services: {empty}"


class TestServiceTargets:
    def test_every_service_has_a_clone_url(self, projects):
        """Without one, ensure_checkout bails and the agent runs against an empty dir."""
        missing = [
            f"{project_name}/{service_name}"
            for project_name, project in projects.items()
            for service_name, service in (project.get("services") or {}).items()
            if not (service or {}).get("clone_url")
        ]

        assert not missing, f"Services with no clone_url: {missing}"

    def test_every_service_has_a_repo_path(self, projects):
        missing = [
            f"{project_name}/{service_name}"
            for project_name, project in projects.items()
            for service_name, service in (project.get("services") or {}).items()
            if not (service or {}).get("repo_path")
        ]

        assert not missing, f"Services with no repo_path: {missing}"

    def test_repo_paths_are_unique(self, projects):
        """Two services sharing a checkout directory would clobber each other."""
        counts = collections.Counter(
            service["repo_path"]
            for project in projects.values()
            for service in (project.get("services") or {}).values()
            if (service or {}).get("repo_path")
        )
        duplicates = {path: n for path, n in counts.items() if n > 1}

        assert not duplicates, f"repo_path collisions: {duplicates}"

    def test_clone_urls_are_https_not_ssh(self, projects):
        """Auth flows through the GH_TOKEN credential helper; no SSH key is mounted."""
        ssh = [
            f"{project_name}/{service_name}"
            for project_name, project in projects.items()
            for service_name, service in (project.get("services") or {}).items()
            if str((service or {}).get("clone_url", "")).startswith(("git@", "ssh://"))
        ]

        assert not ssh, f"SSH clone URLs will fail — no key is mounted: {ssh}"

    def test_no_credentials_are_embedded_in_clone_urls(self, projects):
        """A token in a remote URL persists in .git/config in plaintext."""
        leaky = [
            f"{project_name}/{service_name}"
            for project_name, project in projects.items()
            for service_name, service in (project.get("services") or {}).items()
            if "@" in str((service or {}).get("clone_url", "")).split("://")[-1].split("/")[0]
        ]

        assert not leaky, f"Clone URLs with embedded credentials: {leaky}"
