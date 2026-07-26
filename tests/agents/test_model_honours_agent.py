"""The agent loop must call the model the agent was assigned.

`dev.py` builds an Agent with `resolve_model(...)` — the classifier's difficulty
tier, plus any per-role or per-project override — and hands it to `run_agent`.
`run_agent` then computed `model = config.model` and used THAT for every LiteLLM
call, consulting `agent.model` only when it had to create the agent itself.

So the caller's choice was discarded. The classifier classified, persisted a
tier to the database, and changed nothing: on job 17d10bdc, rated `easy` and
recorded as `claude-haiku-4-5`, the process made 62 calls to `claude-opus-5`
against 2 to haiku.

Two consequences, and the second is worse than the waste:

* Every agent ran on the global default instead of its tier, so the whole
  cost-control story — classify cheap work, route it to a cheap model — was
  inert while appearing to work end to end.
* `completion_cost()` prices against `agent.model`. Recorded spend was computed
  at haiku rates for tokens billed at opus rates, roughly 5x. Every cost figure
  read off those rows understates what was actually spent, including the $8.18
  that tripped the per-agent ceiling.
"""

import inspect

from minions.agents.runner import run_agent


def _resolved_model(config_model, project_model, agent_model):
    """Mirror of the precedence now implemented in run_agent."""
    model = config_model
    if project_model:
        model = project_model
    if agent_model:
        model = agent_model
    return model


class TestModelPrecedence:
    def test_a_passed_agent_s_model_wins(self):
        """The regression: haiku was assigned, opus was called."""
        assert _resolved_model("claude-opus-5", None, "claude-haiku-4-5") == "claude-haiku-4-5"

    def test_the_project_override_still_applies_when_no_agent_model(self):
        assert _resolved_model("claude-opus-5", "claude-sonnet-5", None) == "claude-sonnet-5"

    def test_the_config_default_is_the_floor(self):
        assert _resolved_model("claude-opus-5", None, None) == "claude-opus-5"

    def test_an_agent_model_beats_a_project_model(self):
        """resolve_model already folds the project model in, so by the time an
        Agent carries one it is the more specific decision."""
        assert _resolved_model("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5") == "claude-haiku-4-5"

    def test_an_empty_agent_model_does_not_blank_the_selection(self):
        """A falsy model must fall back, never send an empty model string."""
        assert _resolved_model("claude-opus-5", None, "") == "claude-opus-5"


class TestWiring:
    def test_run_agent_consults_the_passed_agent(self):
        source = inspect.getsource(run_agent)

        assert "elif agent.model:" in source
        assert "model = agent.model" in source

    def test_the_fallback_chain_is_intact(self):
        """Creating an agent from scratch must still work as before."""
        source = inspect.getsource(run_agent)

        assert "model = config.model" in source
        assert "model = project.model" in source

    def test_the_assignment_happens_before_the_loop_uses_it(self):
        """Resolving agent.model after the loop starts would fix nothing.

        Deliberately anchored on the loop invocations rather than on any
        `model=model`: the Agent constructor also passes `model=model`, and it
        sits ABOVE this assignment by design — that branch only runs when no
        agent was supplied, so agent.model cannot apply there.
        """
        source = inspect.getsource(run_agent)

        assign = source.index("model = agent.model")
        for loop_call in ("_run_via_subgraph(", "_agent_loop_generic("):
            assert assign < source.index(loop_call), f"agent.model must resolve before {loop_call}"

    def test_assert_priceable_sees_the_resolved_model(self):
        """The ceilings are enforced against whatever model is actually called.
        If assert_priceable ran on the pre-resolution value it would validate a
        model the loop never uses."""
        source = inspect.getsource(run_agent)

        assert source.index("model = agent.model") < source.index("assert_priceable(model")
