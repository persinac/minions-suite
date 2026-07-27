"""A project must not get a pinned model nobody asked for.

build_registry defaulted `model` to "claude-opus-5" when projects.yaml omitted
it. ProjectConfig.model documents empty as "use the default", so that hardcoded
fallback made the documented behaviour unreachable — every project carried a pin.

It matters because project_model is passed to resolve_model at exactly ONE call
site, the reviewer fan-out, where it returns BEFORE the reviewer default or the
difficulty tier is consulted:

    if project_model:
        return project_model      # <- wins outright
    ...
    if is_reviewer:
        return config.model_reviewer

So that one default, not anything anyone configured, is why every reviewer ran on
Opus while model_reviewer=claude-sonnet-5 sat unused and the classifier's verdict
was discarded. Job 33c89d9b was classified EASY, ran its engineer on the herder
for $0.00, and still spent $4.76 of Opus reviewers out of a $4.81 total.

An explicit per-project pin still wins — that is a deliberate override and stays
supported. The fix is only that ABSENCE stops meaning Opus.
"""

import textwrap

import pytest

from minions.classifier import EASY, HARD, MEDIUM, resolve_model
from minions.config import Config
from minions.project_registry import build_registry


def _registry(tmp_path, body: str):
    path = tmp_path / "projects.yaml"
    path.write_text(textwrap.dedent(body))
    return build_registry(str(path))


BARE = """
    defaults:
      git_provider: github
    projects:
      svc:
        project_id: org/svc
        repo_path: /repos/svc
"""


class TestAbsenceMeansUnset:
    def test_no_model_anywhere_leaves_it_empty(self, tmp_path):
        assert _registry(tmp_path, BARE)["svc"].model == ""

    def test_the_shipped_config_pins_nothing(self):
        """The real file, not a fixture — this is the one that was costing money."""
        registry = build_registry("k8s/base/config/projects.yaml")

        pinned = {name: p.model for name, p in registry.items() if p.model}
        assert not pinned, f"unexpected project model pins: {pinned}"


class TestExplicitPinsStillWork:
    def test_a_defaults_model_still_applies(self, tmp_path):
        registry = _registry(
            tmp_path,
            """
            defaults:
              model: claude-opus-5
            projects:
              svc:
                project_id: org/svc
            """,
        )
        assert registry["svc"].model == "claude-opus-5"

    def test_a_per_project_model_beats_defaults(self, tmp_path):
        registry = _registry(
            tmp_path,
            """
            defaults:
              model: claude-sonnet-5
            projects:
              svc:
                project_id: org/svc
                model: claude-opus-5
            """,
        )
        assert registry["svc"].model == "claude-opus-5"


class TestReviewerResolution:
    """What the fix actually buys, asserted end to end."""

    @pytest.fixture
    def config(self):
        c = Config.from_env()
        c.model_easy = "claude-haiku-4-5"
        c.model_medium = "claude-sonnet-5"
        c.model_hard = "claude-opus-5"
        c.model_reviewer = "claude-sonnet-5"
        return c

    @pytest.mark.parametrize("difficulty", [EASY, MEDIUM, HARD, None])
    def test_an_unpinned_project_gets_the_reviewer_default(self, config, difficulty, tmp_path):
        project = _registry(tmp_path, BARE)["svc"]

        assert resolve_model(config, difficulty, project.model, is_reviewer=True) == "claude-sonnet-5"

    def test_a_hard_ticket_no_longer_drags_reviewers_to_opus(self, config, tmp_path):
        """The specific regression: HARD used to yield Opus via the project pin,
        not via any reviewer policy."""
        project = _registry(tmp_path, BARE)["svc"]

        assert resolve_model(config, HARD, project.model, is_reviewer=True) != "claude-opus-5"

    def test_easy_tickets_no_longer_drop_reviewers_to_haiku(self, config, tmp_path):
        """model_reviewer is a floor. Reviewers fan out ~3x and re-run every
        revision round, so varying them per ticket economises on the most
        leveraged quality decision in the system."""
        project = _registry(tmp_path, BARE)["svc"]

        assert resolve_model(config, EASY, project.model, is_reviewer=True) == "claude-sonnet-5"

    def test_a_deliberate_pin_still_overrides_everything(self, config):
        assert resolve_model(config, HARD, "moonshot/kimi-k2.6", is_reviewer=True) == "moonshot/kimi-k2.6"

    def test_non_reviewer_roles_were_never_affected(self, config, tmp_path):
        """project_model reaches only the reviewer call site, so engineers and
        orchestration always followed the tier — which is why they were already
        running Haiku while reviewers were on Opus."""
        assert resolve_model(config, EASY, is_engineer=True) == "claude-haiku-4-5"
        assert resolve_model(config, MEDIUM) == "claude-sonnet-5"
