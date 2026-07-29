"""A fresh checkout has no node_modules, so `npm test` cannot be the first command.

The image had no node at all, so every TypeScript service in projects.yaml was
unworkable: an agent would clone, edit, and then fail at the verification step
with "command not found", burning an attempt to discover the image could not
build its own target. The label gate hid it -- those tickets were never picked
up, so the gap showed up as work that silently never started rather than as a
failure anyone could see.

Node in the image is half the fix. The other half is telling the agent to
install dependencies before running tests, which is what install_command does.

The guard at the bottom is the part that matters over time: it asserts against
the real k8s projects.yaml, so adding a TypeScript repo without an install step
fails the suite instead of failing in production eight hours later.
"""

from pathlib import Path

import yaml

from minions.project_registry import build_registry

K8S_PROJECTS = Path(__file__).resolve().parents[2] / "k8s" / "base" / "config" / "projects.yaml"


def _registry(tmp_path, services: dict):
    """Written with yaml.dump rather than an indented heredoc: the nesting here
    is four levels deep and a hand-indented fixture silently produces a config
    with no services at all, which fails as a KeyError far from the cause."""
    cfg = tmp_path / "projects.yaml"
    cfg.write_text(yaml.safe_dump({"projects": {"demo": {"project_id": "acme/demo", "services": services}}}))
    return build_registry(str(cfg))


class TestItIsParsed:
    def test_install_command_is_read_from_config(self, tmp_path):
        reg = _registry(tmp_path, {"web": {"language": "typescript", "install_command": "npm ci", "test_command": "npm test"}})

        assert reg["demo"].services["web"].install_command == "npm ci"

    def test_it_defaults_to_empty(self, tmp_path):
        """Most Python repos need no install step; absence must not break them."""
        reg = _registry(tmp_path, {"api": {"language": "python", "test_command": "pytest"}})

        assert reg["demo"].services["api"].install_command == ""


class TestItReachesThePrompt:
    """A config field the agent never sees is not a fix."""

    def _service(self, tmp_path, install: str):
        reg = _registry(tmp_path, {"web": {"language": "typescript", "install_command": install, "test_command": "npm test"}})
        return reg["demo"].services["web"]

    def _render(self, service):
        from minions.agents.prompt import build_agent_prompt
        from minions.core.models import AgentRole, Job, Task

        job = Job(spec="tweak a button")
        task = Task(
            job_id="j1",
            title="Tweak a button",
            service="web",
            agent_role=AgentRole.FRONTEND_ENGINEER,
        )
        return build_agent_prompt(job=job, task=task, service=service, project=None)

    def test_the_install_command_is_rendered(self, tmp_path):
        text = self._render(self._service(tmp_path, "npm ci"))

        assert "npm ci" in text

    def test_it_is_rendered_before_the_test_command(self, tmp_path):
        """Order in the prompt is the order the agent has to execute in."""
        text = self._render(self._service(tmp_path, "npm ci"))

        assert text.index("npm ci") < text.index("npm test")

    def test_an_empty_install_command_is_omitted(self, tmp_path):
        """An empty backticked line reads as a command to run."""
        text = self._render(self._service(tmp_path, ""))

        assert "Install command" not in text


class TestTheRealConfigStaysHonest:
    """Guards against the two bugs actually found on 2026-07-29."""

    def _typescript_services(self):
        data = yaml.safe_load(K8S_PROJECTS.read_text())
        found = {}
        for project in (data.get("projects") or {}).values():
            for name, svc in (project.get("services") or {}).items():
                if (svc or {}).get("language") == "typescript":
                    found[name] = svc
        return found

    def test_there_are_typescript_services_to_check(self):
        """Otherwise the assertions below pass vacuously forever."""
        assert len(self._typescript_services()) >= 5

    def test_every_typescript_service_installs_before_it_tests(self):
        missing = [n for n, s in self._typescript_services().items() if not s.get("install_command")]

        assert missing == [], f"TypeScript services with no install_command: {missing}"

    def test_no_typescript_service_still_claims_a_bare_npm_test(self):
        """`npm test` was declared on all five repos; three have no `test`
        script, so it could only ever fail with "Missing script: test". Any
        future `npm test` has to be verified against that repo's package.json,
        which is exactly the check this failure should prompt."""
        verified_to_have_a_test_script = {"management-dashboard", "ui-integration-tests"}

        unverified = [
            name
            for name, svc in self._typescript_services().items()
            if svc.get("test_command") == "npm test" and name not in verified_to_have_a_test_script
        ]

        assert unverified == [], f"`npm test` declared without a verified test script: {unverified}"
