"""Jev classification. Fakes use the SDK's own models, so a shape change breaks these."""

import json

import pytest
from typesafe_sdk import ScoreAnswer, SystemOneResponse, Usage

from minions.classifier import EASY, HARD, MEDIUM, classify_difficulty, resolve_model
from minions.classifier_jev import (
    BLAST_RADIUS_CRITERIA,
    CLARITY_CRITERIA,
    CONSEQUENCE_CRITERIA,
    EFFORT_CRITERIA,
    INPUT_USD_PER_MTOK,
    jev_cost_usd,
    levels_to_difficulty,
    score_difficulty_jev,
)
from minions.config import Config

RICE_JSON = '{"reach": 1, "impact": 0.5, "confidence": 1.0, "effort": 1.0, "reason": "mirrored test"}'


def _answer(score: float, confidence: float, criteria: list[str]) -> ScoreAnswer:
    levels = len(criteria)
    probabilities = {i: 0.0 for i in range(levels)}
    probabilities[min(round(score), levels - 1)] = 1.0
    return ScoreAnswer(
        type="score",
        score=score,
        confidence=confidence,
        legend=dict(enumerate(criteria)),
        probabilities=probabilities,
    )


def _response(effort, clarity, blast, consequence, confidence=0.9, input_tokens=1500, output_tokens=9, confidences=None):
    per_question = confidences or {}
    scores = {"effort": effort, "clarity": clarity, "blast_radius": blast, "consequence": consequence}
    criteria = {
        "effort": EFFORT_CRITERIA,
        "clarity": CLARITY_CRITERIA,
        "blast_radius": BLAST_RADIUS_CRITERIA,
        "consequence": CONSEQUENCE_CRITERIA,
    }
    return SystemOneResponse(
        model="jev-1.13.0",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        answers={name: _answer(score, per_question.get(name, confidence), criteria[name]) for name, score in scores.items()},
    )


class _FakeClient:
    """Counts calls, so a test cannot pass by never reaching the client at all."""

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = 0
        self.state = None
        self.questions = None

    async def system_one(self, state, questions, **kwargs):
        self.calls += 1
        self.state = state
        self.questions = questions
        if self._exc is not None:
            raise self._exc
        return self._response

    async def close(self):
        return None


def _config(**overrides):
    config = Config.from_env()
    config.classifier_enabled = True
    config.classifier_backend = "litellm"
    config.classifier_min_confidence = 0.5
    config.typesafe_api_key = "test-key-not-a-real-credential"
    config.classifier_max_chars = 6000
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _fake_litellm(monkeypatch, content=RICE_JSON):
    class _Msg:
        def __init__(self):
            self.content = content

    class _Choice:
        def __init__(self):
            self.message = _Msg()

    class _Resp:
        def __init__(self):
            self.choices = [_Choice()]

    async def _fake(**kwargs):
        return _Resp()

    monkeypatch.setattr("minions.classifier.litellm.acompletion", _fake)
    monkeypatch.setattr("minions.classifier.litellm.completion_cost", lambda **kwargs: 0.0)


class TestLevelsToDifficulty:
    @pytest.mark.parametrize(
        "label,effort,clarity,expected",
        [
            ("one-liner, step-by-step", 0.0, 2.0, EASY),
            ("large refactor", 3.0, 2.0, HARD),
            ("mid effort but approach unknown", 1.5, 0.0, HARD),
            ("mid effort, well specified", 1.5, 2.0, MEDIUM),
            ("small effort, approach unspecified", 0.75, 1.0, MEDIUM),
        ],
    )
    def test_tiers(self, label, effort, clarity, expected):
        difficulty, _ = levels_to_difficulty(effort=effort, clarity=clarity, blast_radius=0.0, consequence=0.0)
        assert difficulty == expected, label

    def test_hard_effort_boundary_is_reachable_from_both_sides(self):
        """2.0/3 = 0.667 sits just under HARD_EFFORT; 2.1/3 = 0.700 just over."""
        below, _ = levels_to_difficulty(effort=2.0, clarity=2.0, blast_radius=0.0, consequence=0.0)
        above, _ = levels_to_difficulty(effort=2.1, clarity=2.0, blast_radius=0.0, consequence=0.0)

        assert below == MEDIUM
        assert above == HARD

    def test_vagueness_raises_difficulty_at_equal_effort(self):
        clear, _ = levels_to_difficulty(effort=1.5, clarity=2.0, blast_radius=0.0, consequence=0.0)
        vague, _ = levels_to_difficulty(effort=1.5, clarity=0.0, blast_radius=0.0, consequence=0.0)

        assert clear == MEDIUM
        assert vague == HARD

    def test_stakes_guard_needs_both_factors_high(self):
        wide_and_costly, detail = levels_to_difficulty(effort=0.0, clarity=2.0, blast_radius=2.0, consequence=2.0)
        wide_only, _ = levels_to_difficulty(effort=0.0, clarity=2.0, blast_radius=2.0, consequence=0.0)
        costly_only, _ = levels_to_difficulty(effort=0.0, clarity=2.0, blast_radius=0.0, consequence=2.0)

        assert wide_and_costly == MEDIUM
        assert detail["stakes_guard"] is True
        assert wide_only == EASY, "blast radius alone must not lift the tier"
        assert costly_only == EASY, "consequence alone must not lift the tier"

    def test_scores_are_normalized_across_differing_scale_lengths(self):
        _, detail = levels_to_difficulty(effort=3.0, clarity=2.0, blast_radius=2.0, consequence=2.0)

        assert detail["effort_n"] == 1.0
        assert detail["clarity_n"] == 1.0

    def test_out_of_range_scores_are_clamped(self):
        difficulty, detail = levels_to_difficulty(effort=99.0, clarity=-5.0, blast_radius=0.0, consequence=0.0)

        assert difficulty == HARD
        assert detail["effort_n"] == 1.0
        assert detail["clarity_n"] == 0.0


class TestCost:
    def test_one_million_input_tokens_costs_the_documented_rate(self):
        assert jev_cost_usd(Usage(input_tokens=1_000_000, output_tokens=0)) == pytest.approx(INPUT_USD_PER_MTOK)

    def test_output_tokens_are_free(self):
        cheap = jev_cost_usd(Usage(input_tokens=1000, output_tokens=0))
        chatty = jev_cost_usd(Usage(input_tokens=1000, output_tokens=500_000))

        assert cheap == chatty

    @pytest.mark.parametrize("usage", [None, Usage(input_tokens=None, output_tokens=None)])
    def test_missing_usage_is_zero_not_an_error(self, usage):
        assert jev_cost_usd(usage) == 0.0


class TestConfidenceGate:
    async def test_low_confidence_returns_unclassified(self):
        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0, confidence=0.4))

        difficulty, reason, telemetry = await score_difficulty_jev("a spec", _config(), client=client)

        assert client.calls == 1, "the gate must apply to a real answer, not a skipped call"
        assert difficulty is None
        assert telemetry["gated"] is True
        assert "below floor" in reason

    async def test_a_gated_answer_routes_to_the_medium_tier_not_the_opus_default(self):
        config = _config()
        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0, confidence=0.4))

        difficulty, _, _ = await score_difficulty_jev("a spec", config, client=client)

        assert resolve_model(config, difficulty) == config.model_medium
        assert resolve_model(config, difficulty) != config.model

    async def test_the_weakest_question_is_named(self):
        response = _response(0.0, 2.0, 0.0, 0.0, confidence=0.95, confidences={"clarity": 0.10})
        client = _FakeClient(response)

        _, reason, telemetry = await score_difficulty_jev("a spec", _config(), client=client)

        assert telemetry["min_confidence_question"] == "clarity"
        assert "clarity" in reason

    async def test_high_confidence_is_acted_on(self):
        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0, confidence=0.95))

        difficulty, _, telemetry = await score_difficulty_jev("a spec", _config(), client=client)

        assert difficulty == EASY
        assert telemetry["gated"] is False


class TestFailOpen:
    async def test_a_raising_client_returns_unclassified(self):
        client = _FakeClient(exc=RuntimeError("rate limited"))

        difficulty, reason, _ = await score_difficulty_jev("a spec", _config(), client=client)

        assert client.calls == 1
        assert difficulty is None
        assert "rate limited" in reason

    async def test_a_missing_key_short_circuits_before_any_call(self):
        difficulty, reason, _ = await score_difficulty_jev("a spec", _config(typesafe_api_key=""))

        assert difficulty is None
        assert "TYPESAFE_API_KEY" in reason

    async def test_an_incomplete_response_returns_unclassified(self):
        response = _response(0.0, 2.0, 0.0, 0.0)
        del response.answers["clarity"]
        client = _FakeClient(response)

        difficulty, reason, _ = await score_difficulty_jev("a spec", _config(), client=client)

        assert difficulty is None
        assert "incomplete" in reason

    async def test_the_spec_is_truncated_to_the_configured_budget(self):
        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0))

        await score_difficulty_jev("x" * 9000, _config(classifier_max_chars=120), client=client)

        assert len(client.state) == 120

    async def test_all_four_questions_go_in_one_request(self):
        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0))

        await score_difficulty_jev("a spec", _config(), client=client)

        assert client.calls == 1
        assert set(client.questions) == {"effort", "clarity", "blast_radius", "consequence"}


class TestBackendSwitch:
    async def test_litellm_backend_never_constructs_a_typesafe_client(self, monkeypatch):
        built = []

        def _spy(**kwargs):
            built.append(kwargs)
            raise AssertionError("the litellm backend must not reach TypeSafe")

        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", _spy)
        _fake_litellm(monkeypatch)

        difficulty, _ = await classify_difficulty("a spec", _config(classifier_backend="litellm"))

        assert difficulty == EASY
        assert built == []

    async def test_disabled_classifier_short_circuits_every_backend(self):
        config = _config(classifier_backend="shadow", classifier_enabled=False)

        assert await classify_difficulty("a spec", config) == (None, "classifier disabled")

    async def test_an_unknown_backend_falls_back_to_litellm(self, monkeypatch):
        _fake_litellm(monkeypatch)

        difficulty, _ = await classify_difficulty("a spec", _config(classifier_backend="nonsense"))

        assert difficulty == EASY


class _FakeDb:
    def __init__(self):
        self.events = []

    async def record_event(self, job_id, event_type, source, detail):
        self.events.append({"job_id": job_id, "event_type": event_type, "source": source, "detail": detail})


class TestShadowMode:
    async def test_a_raising_jev_leaves_the_litellm_verdict_intact(self, monkeypatch):
        """gather(return_exceptions=True) swallows the raise, so count the call too."""
        client = _FakeClient(exc=RuntimeError("jev is down"))
        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", lambda **kwargs: client)
        _fake_litellm(monkeypatch)
        db = _FakeDb()

        difficulty, reason = await classify_difficulty("a spec", _config(classifier_backend="shadow"), db=db, job_id="job-1")

        assert client.calls == 1, "vacuous pass: the jev path never ran"
        assert difficulty == EASY
        assert "E/C" in reason, "the returned reason must be the litellm one"

    async def test_the_comparison_is_recorded_as_an_event(self, monkeypatch):
        client = _FakeClient(_response(3.0, 0.0, 0.0, 0.0, confidence=0.95))
        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", lambda **kwargs: client)
        _fake_litellm(monkeypatch)
        db = _FakeDb()

        difficulty, _ = await classify_difficulty("a spec", _config(classifier_backend="shadow"), db=db, job_id="job-1")

        assert difficulty == EASY, "the litellm verdict is what ships"
        assert len(db.events) == 1
        event = db.events[0]
        assert event["event_type"] == "difficulty_shadow"
        assert event["job_id"] == "job-1"

        record = json.loads(event["detail"])
        assert record["difficulty"] == HARD, "jev disagreed, and the disagreement is what we want logged"
        assert record["litellm_difficulty"] == EASY
        assert record["agreed"] is False
        assert record["cost_usd"] > 0
        assert set(record["scores"]) == {"effort", "clarity", "blast_radius", "consequence"}

    async def test_agreement_is_flagged_when_both_agree(self, monkeypatch):
        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0, confidence=0.95))
        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", lambda **kwargs: client)
        _fake_litellm(monkeypatch)
        db = _FakeDb()

        await classify_difficulty("a spec", _config(classifier_backend="shadow"), db=db, job_id="job-1")

        record = json.loads(db.events[0]["detail"])
        assert record["difficulty"] == EASY
        assert record["agreed"] is True

    async def test_a_jev_failure_is_still_recorded(self, monkeypatch):
        client = _FakeClient(exc=RuntimeError("jev is down"))
        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", lambda **kwargs: client)
        _fake_litellm(monkeypatch)
        db = _FakeDb()

        await classify_difficulty("a spec", _config(classifier_backend="shadow"), db=db, job_id="job-1")

        record = json.loads(db.events[0]["detail"])
        assert record["difficulty"] is None
        assert "jev is down" in record["reason"]

    async def test_a_failing_event_write_does_not_break_the_verdict(self, monkeypatch):
        class _BrokenDb:
            async def record_event(self, *args, **kwargs):
                raise RuntimeError("postgres is unreachable")

        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0, confidence=0.95))
        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", lambda **kwargs: client)
        _fake_litellm(monkeypatch)

        difficulty, _ = await classify_difficulty("a spec", _config(classifier_backend="shadow"), db=_BrokenDb(), job_id="job-1")

        assert difficulty == EASY

    async def test_shadow_without_a_db_still_returns_the_verdict(self, monkeypatch):
        client = _FakeClient(_response(0.0, 2.0, 0.0, 0.0, confidence=0.95))
        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", lambda **kwargs: client)
        _fake_litellm(monkeypatch)

        difficulty, _ = await classify_difficulty("a spec", _config(classifier_backend="shadow"))

        assert difficulty == EASY


class TestTypesafeBackend:
    async def test_the_typesafe_backend_returns_jevs_verdict(self, monkeypatch):
        client = _FakeClient(_response(3.0, 0.0, 0.0, 0.0, confidence=0.95))
        monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", lambda **kwargs: client)

        difficulty, reason = await classify_difficulty("a spec", _config(classifier_backend="typesafe"))

        assert client.calls == 1
        assert difficulty == HARD
        assert reason.startswith("jev ")
