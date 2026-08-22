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
import hashlib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_YAML = REPO_ROOT / "k8s" / "base" / "config" / "projects.yaml"
K8S_BASE = REPO_ROOT / "k8s" / "base"

CONFIG_MAP_NAME = "minion-suite-config-files"
CHECKSUM_ANNOTATION = "minion-suite/projects-checksum"


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

    def test_every_project_has_a_description(self, projects):
        """The arbiter routes from the service list, and the description is the
        only "what is this repo for" signal that list carries. A bare name is
        how job 9a1aeba4 sent a flashback-cns code task to flashback-process
        (a docs repo). The file is regenerated from the GitHub App
        installation, and the generator lives outside this repo — so this test
        is what keeps a regeneration from silently dropping the descriptions:
        set them on the GitHub repo (`gh repo edit -d`), not only here."""
        undescribed = [name for name, project in projects.items() if not (project.get("description") or "").strip()]

        assert not undescribed, f"Projects with no description: {undescribed}"


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


class TestPolyglotRepos:
    """One repo, two toolchains.

    pinball-db holds a Python ETL in jobs/ and the Next.js site behind
    db.flashbackfleet.com in web/. A single service entry can only carry one
    `language`, one test_command and one lint_command, and it carried the ETL's
    -- so a change under web/ was handed `pytest` from a checkout root that has
    no pyproject.toml at all.

    Splitting is only safe if the two halves stay genuinely separate, which is
    what these assert.
    """

    def _by_clone_url(self, projects):
        grouped = collections.defaultdict(list)
        for project in projects.values():
            for name, service in (project.get("services") or {}).items():
                url = (service or {}).get("clone_url")
                if url:
                    grouped[url].append((name, service))
        return {url: svcs for url, svcs in grouped.items() if len(svcs) > 1}

    def test_services_sharing_a_repo_have_distinct_languages(self, projects):
        """Two entries for one repo only earn their keep if they describe
        different toolchains. Same language means the split is duplication, and
        duplication is how the wrong test command survives a fix."""
        offenders = {}
        for url, services in self._by_clone_url(projects).items():
            languages = [(svc or {}).get("language", "") for _, svc in services]
            if len(set(languages)) != len(languages):
                offenders[url] = languages

        assert not offenders, f"Services share a repo AND a language: {offenders}"

    def test_services_sharing_a_repo_do_not_share_a_checkout(self, projects):
        """Covered generally by test_repo_paths_are_unique; asserted here too
        because the tempting way to add a second toolchain is to reuse the first
        one's repo_path, and these two agents would then fight over one tree."""
        offenders = {}
        for url, services in self._by_clone_url(projects).items():
            paths = [(svc or {}).get("repo_path", "") for _, svc in services]
            if len(set(paths)) != len(paths):
                offenders[url] = paths

        assert not offenders, f"Services share a repo AND a checkout dir: {offenders}"

    def test_pinball_db_covers_both_halves(self, projects):
        """The concrete case this exists for. Fails if either half is dropped."""
        services = {name: svc for project in projects.values() for name, svc in (project.get("services") or {}).items() if "pinball-db" in name}

        languages = {name: (svc or {}).get("language") for name, svc in services.items()}

        assert "pinball-db" in services, "the Python ETL half is missing"
        assert "pinball-db-web" in services, "the Next.js half is missing — a web/ change would be handed pytest"
        assert set(languages.values()) == {"python", "typescript"}, languages

    def test_the_python_half_runs_where_its_config_lives(self, projects):
        """There is no pyproject.toml, pytest.ini or conftest.py at the repo
        root — all of it is under jobs/ — so a bare `pytest` runs with no
        project config against an uninstalled src-layout package."""
        etl = next(svc for project in projects.values() for name, svc in (project.get("services") or {}).items() if name == "pinball-db")

        assert etl["test_command"] != "pytest", "bare pytest runs from a root with no Python config"
        assert "jobs" in etl["test_command"], etl["test_command"]
        assert "jobs" in etl["lint_command"], etl["lint_command"]


def _deployments_mounting_the_config() -> dict[str, dict]:
    """Every base Deployment with a volume backed by the projects ConfigMap.

    Derived from the manifests rather than hardcoded, so a Deployment added later
    is covered the day it starts mounting the file.
    """
    found = {}
    for manifest in sorted(K8S_BASE.glob("*/deployment.yaml")):
        for doc in yaml.safe_load_all(manifest.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
                continue
            volumes = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
            mounts_config = any((v.get("configMap") or {}).get("name") == CONFIG_MAP_NAME for v in volumes if isinstance(v, dict))
            if mounts_config:
                found[str(manifest.relative_to(REPO_ROOT))] = doc
    return found


class TestConfigChecksum:
    """projects.yaml only reaches the cluster if the pod template changes with it.

    The ConfigMap has a stable name on purpose (config/kustomization.yaml explains
    the prune race that motivated it), it is mounted with subPath so the kubelet
    never refreshes the file, and build_registry reads it once at startup. A
    projects.yaml-only commit therefore changes nothing ArgoCD can roll: it reports
    Synced while the engine routes on whatever the file said when the pod started.

    8b22888 split pinball-db into its ETL and its site and sat unapplied for twelve
    days that way — the running pod had none of it, and the only visible symptom
    would have been a web/ change handed `pytest`.

    Stamping the file's sha256 into the pod template is what makes the edit roll the
    pods. These fail when the stamp and the file disagree; scripts/sync-config-checksum.sh
    fixes them.
    """

    @pytest.fixture(scope="class")
    def expected_checksum(self) -> str:
        if not PROJECTS_YAML.exists():
            pytest.skip(f"{PROJECTS_YAML} not present")
        return hashlib.sha256(PROJECTS_YAML.read_bytes()).hexdigest()

    def test_some_deployment_mounts_the_config(self):
        """Guards the other tests here: if the mount is renamed, they would all
        vacuously pass over an empty set."""
        assert _deployments_mounting_the_config(), f"No base Deployment mounts a ConfigMap named {CONFIG_MAP_NAME}"

    def test_every_mounting_deployment_stamps_the_checksum(self, expected_checksum):
        stale = {}
        for path, doc in _deployments_mounting_the_config().items():
            annotations = (((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations") or {}
            actual = annotations.get(CHECKSUM_ANNOTATION)
            if actual != expected_checksum:
                stale[path] = actual

        assert not stale, (
            f"Pod-template {CHECKSUM_ANNOTATION} does not match projects.yaml "
            f"(expected {expected_checksum[:12]}…). Run scripts/sync-config-checksum.sh "
            f"and commit the result, or the edit never reaches the cluster. Stale: {stale}"
        )
