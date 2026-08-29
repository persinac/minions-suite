"""RICE difficulty classification and model-tier routing.

The classifier exists to stop paying Opus rates for mechanical work — one
backend_engineer run billed $20.57. It is deliberately fail-open: anything that
goes wrong leaves the job unclassified, and unclassified jobs use the default
model. It can make a job cheaper or leave it alone, never break it.
"""

import typing

import pytest

from minions.classifier import EASY, HARD, MEDIUM, _parse, classify_difficulty, resolve_model, score_to_difficulty
from minions.config import Config


class TestScoreToDifficulty:
    @pytest.mark.parametrize(
        "label,effort,confidence,reach,impact,expected",
        [
            ("typo in a constant", 0.5, 1.0, 1, 0.25, EASY),
            ("bump a dependency", 1.0, 0.9, 2, 0.5, EASY),
            ("add a test mirroring an existing one", 1.5, 0.9, 1, 0.5, EASY),
            # 3.0/0.8 = 3.75, just over the easy ceiling of 3.0.
            ("new endpoint, clear pattern", 3.0, 0.8, 3, 1.0, MEDIUM),
            ("wallet-api new fixture pattern", 6.0, 0.6, 2, 1.0, HARD),
            ("auth refactor across services", 8.0, 0.4, 8, 3.0, HARD),
        ],
    )
    def test_real_tickets_land_in_the_right_tier(self, label, effort, confidence, reach, impact, expected):
        difficulty, _ = score_to_difficulty(effort=effort, confidence=confidence, reach=reach, impact=impact)
        assert difficulty == expected, label

    def test_low_confidence_raises_difficulty_at_equal_effort(self):
        """Confidence is the point of dividing by it — a vague ticket is harder."""
        clear, clear_score = score_to_difficulty(effort=2.0, confidence=1.0, reach=1, impact=0.5)
        vague, vague_score = score_to_difficulty(effort=2.0, confidence=0.3, reach=1, impact=0.5)

        # 2.0/1.0 = 2.0 (easy) vs 2.0/0.3 = 6.67 (medium): identical effort,
        # one tier apart on confidence alone.
        assert vague_score > clear_score
        assert clear == EASY
        assert vague == MEDIUM

    def test_stakes_guard_lifts_a_trivial_but_high_impact_change(self):
        """A one-line billing change is easy to write and expensive to get wrong."""
        difficulty, score = score_to_difficulty(effort=0.5, confidence=1.0, reach=6, impact=3.0)

        assert score <= 3.0, "effort/confidence alone would call this easy"
        assert difficulty == MEDIUM, "reach x impact must floor it above the cheapest tier"

    def test_stakes_guard_leaves_low_stakes_work_alone(self):
        difficulty, _ = score_to_difficulty(effort=0.5, confidence=1.0, reach=1, impact=0.25)
        assert difficulty == EASY

    def test_out_of_range_scores_are_clamped_not_rejected(self):
        """A model that ignores the scale must not produce a nonsense tier."""
        difficulty, score = score_to_difficulty(effort=999.0, confidence=0.0, reach=-5, impact=99.0)

        assert difficulty == HARD
        assert score == pytest.approx(10.0 / 0.2)

    def test_classic_rice_priority_would_invert_trivial_tickets(self):
        """Documents why the classic (R*I*C)/E score is not used for routing.

        A typo fix scores near the bottom on value-RICE and would be sent to the
        most expensive model precisely because it is trivial and low-impact.
        """
        reach, impact, confidence, effort = 1, 0.25, 1.0, 0.5
        classic_rice = (reach * impact * confidence) / effort

        assert classic_rice < 1.0, "value-RICE ranks a typo fix as low priority"
        assert score_to_difficulty(effort=effort, confidence=confidence, reach=reach, impact=impact)[0] == EASY


class TestResolveModel:
    def test_each_tier_selects_its_model(self):
        config = Config.from_env()

        assert resolve_model(config, EASY) == config.model_easy
        assert resolve_model(config, MEDIUM) == config.model_medium
        assert resolve_model(config, HARD) == config.model_hard

    def test_unclassified_falls_back_to_the_medium_tier(self):
        """Not to `config.model`, which is Opus.

        This asserted the opposite until 2026-08-29. Failing open sent every
        spec the classifier could not score to the most expensive model in the
        system — two unclassified backend_engineer runs on 2026-07-25 cost
        $20.57, against $0.23 for the same role on the easy tier.
        """
        config = Config.from_env()

        assert resolve_model(config, None) == config.model_medium
        assert resolve_model(config, "") == config.model_medium
        assert resolve_model(config, "bogus") == config.model_medium

    def test_unscored_is_never_the_premium_tier(self):
        """The property that matters, stated independently of which model is which.

        Pinned against config.model_hard as well: if someone later points
        model_medium at the hard tier, this still catches it.
        """
        config = Config.from_env()

        for difficulty in (None, "", "bogus"):
            assert resolve_model(config, difficulty) != config.model_hard, "an unscored ticket must not buy the premium tier"

    def test_the_global_default_is_still_the_last_resort(self):
        """model_medium is preferred, but an empty one must not resolve to ''."""

        class _NoMedium:
            model = "fallback-model"
            model_easy = "cheap"
            model_medium = ""
            model_hard = "dear"
            model_reviewer = ""
            model_engineer = ""
            model_finisher = ""

        assert resolve_model(_NoMedium(), None) == "fallback-model"

    def test_explicit_project_model_beats_the_tier(self):
        """Someone who set a model in projects.yaml chose it deliberately."""
        config = Config.from_env()

        assert resolve_model(config, EASY, project_model="my-pinned-model") == "my-pinned-model"

    def test_the_easy_tier_is_actually_cheaper_than_the_hard_tier(self):
        """Guards against a config edit that quietly makes 'easy' the costly one."""
        import litellm

        config = Config.from_env()
        easy = litellm.model_cost.get(config.model_easy, {})
        hard = litellm.model_cost.get(config.model_hard, {})

        if not easy or not hard:
            pytest.skip("models absent from the litellm cost map")

        assert easy["input_cost_per_token"] < hard["input_cost_per_token"]
        assert easy["output_cost_per_token"] < hard["output_cost_per_token"]


class TestParsing:
    def test_parses_a_clean_json_reply(self):
        factors, error = _parse('{"reach": 3, "impact": 1, "confidence": 0.8, "effort": 2, "reason": "ok"}')

        assert error == ""
        assert factors["effort"] == 2.0
        assert factors["reason"] == "ok"

    def test_strips_a_code_fence_the_prompt_asked_it_not_to_emit(self):
        raw = '```json\n{"reach": 1, "impact": 0.5, "confidence": 1.0, "effort": 1}\n```'
        factors, error = _parse(raw)

        assert error == ""
        assert factors["confidence"] == 1.0

    @pytest.mark.parametrize(
        "raw",
        [
            "I think this one is pretty hard, honestly",
            '{"difficulty": "easy"}',
            "[1, 2, 3]",
            "",
            '{"reach": "lots", "impact": 1, "confidence": 1, "effort": 1}',
        ],
    )
    def test_unusable_replies_are_reported_not_guessed(self, raw):
        factors, error = _parse(raw)

        assert factors is None
        assert error


class TestFailOpen:
    """Classification must never be able to fail a job."""

    async def test_disabled_classifier_returns_unclassified(self):
        config = Config.from_env()
        config.classifier_enabled = False

        assert await classify_difficulty("anything", config) == (None, "classifier disabled")

    async def test_empty_spec_returns_unclassified(self):
        config = Config.from_env()
        config.classifier_enabled = True

        difficulty, _ = await classify_difficulty("   ", config)
        assert difficulty is None

    async def test_an_api_error_returns_unclassified(self, monkeypatch):
        config = Config.from_env()
        config.classifier_enabled = True

        async def _boom(**kwargs):
            raise RuntimeError("rate limited")

        monkeypatch.setattr("minions.classifier.litellm.acompletion", _boom)

        difficulty, reason = await classify_difficulty("real spec text", config)

        assert difficulty is None
        assert "rate limited" in reason

    async def test_a_garbage_reply_returns_unclassified(self, monkeypatch):
        config = Config.from_env()
        config.classifier_enabled = True

        class _Msg:
            content = "no idea what you're asking"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices: typing.ClassVar = [_Choice()]

        async def _fake(**kwargs):
            return _Resp()

        monkeypatch.setattr("minions.classifier.litellm.acompletion", _fake)

        difficulty, _ = await classify_difficulty("real spec text", config)
        assert difficulty is None

    async def test_uses_the_configured_cheap_model(self, monkeypatch):
        """The classifier must not run on the expensive default."""
        config = Config.from_env()
        config.classifier_enabled = True
        seen = {}

        class _Msg:
            content = '{"reach": 1, "impact": 0.5, "confidence": 1.0, "effort": 1}'

        class _Choice:
            message = _Msg()

        class _Resp:
            choices: typing.ClassVar = [_Choice()]

        async def _fake(**kwargs):
            seen.update(kwargs)
            return _Resp()

        monkeypatch.setattr("minions.classifier.litellm.acompletion", _fake)
        await classify_difficulty("spec", config)

        assert seen["model"] == config.classifier_model
        assert seen["model"] != config.model_hard


class TestReviewerModel:
    """Reviewers fan out, so their cost multiplies where the engineer's does not."""

    def test_reviewers_default_to_sonnet_not_opus_on_hard(self):
        config = Config.from_env()

        assert resolve_model(config, HARD, is_reviewer=True) == config.model_reviewer
        assert resolve_model(config, HARD, is_reviewer=False) == config.model_hard

    def test_reviewers_hold_their_model_even_on_easy_tickets(self):
        """model_reviewer is a FLOOR, not a tier.

        This previously dropped to the cheap tier on easy tickets. Job 33c89d9b
        was classified easy and its reviewers caught an escaping fix resting on
        an undocumented parser grammar, plus a change giving `None` a third
        meaning on a widely-used lookup. Both would have merged. Difficulty
        scores how hard a ticket is to IMPLEMENT, not how hard the diff is to
        JUDGE — and a small change to a security path is easy to write and
        unforgiving to get wrong.
        """
        config = Config.from_env()

        assert resolve_model(config, EASY, is_reviewer=True) == config.model_reviewer

    def test_the_reviewer_model_is_the_same_at_every_difficulty(self):
        config = Config.from_env()

        picks = {resolve_model(config, d, is_reviewer=True) for d in (EASY, MEDIUM, HARD, None)}
        assert picks == {config.model_reviewer}

    def test_a_pinned_project_model_still_wins(self):
        config = Config.from_env()

        assert resolve_model(config, HARD, project_model="pinned", is_reviewer=True) == "pinned"

    def test_engineers_are_unaffected(self):
        config = Config.from_env()

        for difficulty in (EASY, MEDIUM, HARD):
            assert resolve_model(config, difficulty) == resolve_model(config, difficulty, is_reviewer=False)

    def test_the_reviewer_model_is_cheaper_than_the_hard_tier(self):
        import litellm

        config = Config.from_env()
        reviewer = litellm.model_cost.get(config.model_reviewer, {})
        hard = litellm.model_cost.get(config.model_hard, {})
        if not reviewer or not hard:
            pytest.skip("models absent from the litellm cost map")

        assert reviewer["output_cost_per_token"] < hard["output_cost_per_token"]
