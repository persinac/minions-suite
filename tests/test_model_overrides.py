"""Engineer model override, and the guard that keeps the ceilings real.

The engineer was 79% of the one measured job's cost ($20.57 of $26.22), and its
workload is input-dominated — 3.85M in against 53k out — so a cheaper input rate
compounds across every turn. That makes it the lever worth pointing at another
vendor, ahead of the reviewers.

The guard exists because completion_cost() returns 0.0 for a model LiteLLM does
not know. An unpriceable model does not make agents free; it makes the $8 agent
ceiling and the $25 job ceiling silently inert while the config still says they
are on. Provider prefixes decide this: moonshot/kimi-k2.6 is priced,
openai/kimi-k2.6 routes fine and is not.
"""

import pytest

from minions.classifier import EASY, HARD, MEDIUM, UnpriceableModelError, assert_priceable, is_priceable, resolve_model
from minions.config import Config


class TestEngineerOverride:
    def test_unset_follows_the_difficulty_tier(self):
        """Behaviour must not change until someone sets it deliberately."""
        config = Config.from_env()
        config.model_engineer = ""

        for difficulty, expected in ((EASY, config.model_easy), (MEDIUM, config.model_medium), (HARD, config.model_hard)):
            assert resolve_model(config, difficulty, is_engineer=True) == expected

    def test_set_overrides_every_tier(self):
        """Unlike the reviewer default, this is not capped by the tier — the
        point is to move the engineer wholesale to another vendor."""
        config = Config.from_env()
        config.model_engineer = "moonshot/kimi-k2.6"

        for difficulty in (EASY, MEDIUM, HARD):
            assert resolve_model(config, difficulty, is_engineer=True) == "moonshot/kimi-k2.6"

    def test_a_pinned_project_model_still_wins(self):
        config = Config.from_env()
        config.model_engineer = "moonshot/kimi-k2.6"

        assert resolve_model(config, HARD, project_model="pinned", is_engineer=True) == "pinned"

    def test_reviewers_are_unaffected(self):
        """The two overrides must not bleed into each other."""
        config = Config.from_env()
        config.model_engineer = "moonshot/kimi-k2.6"

        assert resolve_model(config, HARD, is_reviewer=True) == config.model_reviewer

    def test_other_roles_are_unaffected(self):
        """Spec analyst, arbiter and deploy monitor keep the tier."""
        config = Config.from_env()
        config.model_engineer = "moonshot/kimi-k2.6"

        assert resolve_model(config, HARD) == config.model_hard

    def test_the_engineer_call_site_passes_the_flag(self):
        """An override nothing reads is dead config."""
        import inspect

        from minions.engine import dev

        assert "is_engineer=True" in inspect.getsource(dev.run_engineer)


class TestPriceability:
    @pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "moonshot/kimi-k2.6"])
    def test_known_models_are_priceable(self, model):
        assert is_priceable(model) is True

    @pytest.mark.parametrize("model", ["openai/kimi-k2.6", "totally-made-up-model", ""])
    def test_unknown_models_are_not(self, model):
        assert is_priceable(model) is False

    def test_the_provider_prefix_decides_it(self):
        """The exact trap: same model, two prefixes, only one is priced."""
        assert is_priceable("moonshot/kimi-k2.6")
        assert not is_priceable("openai/kimi-k2.6")


class TestGuard:
    @staticmethod
    def _config(agent_limit=8.0, job_limit=25.0):
        config = Config.from_env()
        config.agent_cost_limit_usd = agent_limit
        config.job_cost_limit_usd = job_limit
        return config

    def test_refuses_an_unpriceable_model_when_ceilings_are_on(self):
        with pytest.raises(UnpriceableModelError) as exc:
            assert_priceable("openai/kimi-k2.6", self._config(), role="backend_engineer")

        message = str(exc.value)
        assert "backend_engineer" in message
        assert "moonshot/kimi-k2.6" in message, "the error should name the working alternative"

    def test_allows_a_priceable_model(self):
        # The assertion is that it does not raise.
        assert_priceable("moonshot/kimi-k2.6", self._config())

    def test_allows_anything_when_both_ceilings_are_off(self):
        """The guard protects the ceilings. With none set there is nothing to
        protect, and a model LiteLLM has not catalogued yet should not be blocked."""
        assert_priceable("brand-new-model", self._config(agent_limit=0, job_limit=0))

    def test_one_ceiling_still_counts(self):
        with pytest.raises(UnpriceableModelError):
            assert_priceable("brand-new-model", self._config(agent_limit=0, job_limit=25.0))
        with pytest.raises(UnpriceableModelError):
            assert_priceable("brand-new-model", self._config(agent_limit=8.0, job_limit=0))

    def test_the_agent_loop_enforces_it(self):
        """A guard that exists but is never called protects nothing."""
        import inspect

        from minions.agents.runner import run_agent

        assert "assert_priceable" in inspect.getsource(run_agent)


class TestCostArithmetic:
    """Pins the comparison the override was chosen on, against LiteLLM's own rates."""

    def test_kimi_is_cheaper_than_the_hard_tier_on_input(self):
        import litellm

        kimi = litellm.model_cost.get("moonshot/kimi-k2.6", {})
        opus = litellm.model_cost.get("claude-opus-5", {})
        if not kimi or not opus:
            pytest.skip("models absent from the cost map")

        assert kimi["input_cost_per_token"] < opus["input_cost_per_token"]

    def test_the_measured_workload_is_input_dominated(self):
        """3.85M in / 53k out — why input rate is the lever that matters."""
        import litellm

        opus = litellm.model_cost["claude-opus-5"]
        input_cost = 3_848_087 * opus["input_cost_per_token"]
        output_cost = 53_267 * opus["output_cost_per_token"]

        assert input_cost / (input_cost + output_cost) > 0.9
