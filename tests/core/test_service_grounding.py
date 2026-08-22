"""Telling "the spec supports this service" from "the name merely sounded right".

Job 9a1aeba4: the spec named flashback-cns repeatedly and cited
services/game_play_router, and the arbiter still routed the task to
flashback-process — a docs repo registered ten minutes earlier. Every check in
the pipeline passed, because the wrong name was a real name.

check_grounding is the discriminator, and like missing_verdicts it is
deliberately one-sided: it flags a choice only when the spec mentions OTHER
registered services and never the chosen one. Everything else is grounded, so
acting on it can only ever catch a contradiction, never block a spec that
simply doesn't name-drop its repo.
"""

from dataclasses import dataclass, field

from minions.core.service_grounding import check_grounding, evidence_tokens, mentioned_services, mismatch_remedy


@dataclass
class _Svc:
    project_id: str = ""
    repo_path: str = ""
    clone_url: str = ""


@dataclass
class _Project:
    name: str
    project_id: str = ""
    description: str = ""
    services: dict = field(default_factory=dict)


def _registry(*names, **overrides):
    """A minimal registry: one project per service, name == service name."""
    registry = {}
    for name in names:
        svc = overrides.get(name) or _Svc(project_id=f"flippin-balls/{name}")
        registry[name] = _Project(name=name, project_id=svc.project_id, services={name: svc})
    return registry


class TestEvidenceTokens:
    def test_service_key_and_project_id_tail_are_evidence(self):
        tokens = evidence_tokens(_registry("flashback-cns"))

        assert "flashback-cns" in tokens["flashback-cns"]

    def test_clone_url_tail_is_evidence_with_git_stripped(self):
        registry = _registry("cns", cns=_Svc(clone_url="https://github.com/flippin-balls/flashback-cns.git"))
        tokens = evidence_tokens(registry)

        assert "flashback-cns" in tokens["cns"]

    def test_repo_path_basename_is_evidence(self):
        registry = _registry("cns", cns=_Svc(repo_path="/repos/flashback-cns"))

        assert "flashback-cns" in evidence_tokens(registry)["cns"]

    def test_tokens_are_casefolded(self):
        registry = _registry("flashback-android", **{"flashback-android": _Svc(project_id="flippin-balls/Flashback-Android")})

        assert "flashback-android" in evidence_tokens(registry)["flashback-android"]


class TestMentionMatching:
    def test_a_plain_mention_is_found(self):
        tokens = evidence_tokens(_registry("flashback-cns", "healthcheck"))

        assert mentioned_services("Investigated in flashback-cns.", tokens) == {"flashback-cns"}

    def test_matching_is_case_insensitive(self):
        tokens = evidence_tokens(_registry("flashback-android"))

        assert mentioned_services("see flippin-balls/Flashback-Android", tokens) == {"flashback-android"}

    def test_a_prefix_service_does_not_match_inside_a_longer_name(self):
        """`-` is a \\b boundary, so plain word-boundary matching would find
        pinball-db inside pinball-db-web — and then every pinball-db-web spec
        would "mention" both."""
        tokens = evidence_tokens(_registry("pinball-db", "pinball-db-web"))

        assert mentioned_services("update pinball-db-web only", tokens) == {"pinball-db-web"}

    def test_a_github_url_grounds_the_repo(self):
        registry = _registry("cns", cns=_Svc(clone_url="https://github.com/flippin-balls/flashback-cns.git"))
        tokens = evidence_tokens(registry)

        assert mentioned_services("PR at https://github.com/flippin-balls/flashback-cns/pull/1", tokens) == {"cns"}

    def test_empty_text_mentions_nothing(self):
        assert mentioned_services("", evidence_tokens(_registry("a", "b"))) == set()


class TestCheckGrounding:
    def test_the_misroute_this_was_built_for(self):
        """Job 9a1aeba4: spec names flashback-cns, arbiter chose flashback-process."""
        registry = _registry("flashback-cns", "flashback-process")
        spec = "The player-facing latency is in services/game_play_router. Investigated in flashback-cns."

        assert check_grounding("flashback-process", spec, registry) == ["flashback-cns"]
        assert check_grounding("flashback-cns", spec, registry) == []

    def test_a_spec_that_names_no_service_grounds_any_choice(self):
        """Most specs describe behaviour, not repos. No evidence, no flag."""
        registry = _registry("wallet-api", "storefront-api")

        assert check_grounding("wallet-api", "make the insert tokens button faster", registry) == []

    def test_a_mentioned_choice_is_grounded_even_beside_other_mentions(self):
        """Multi-service jobs legitimately name several repos."""
        registry = _registry("wallet-api", "storefront-api")
        spec = "wallet-api owns the balance; storefront-api displays it"

        assert check_grounding("storefront-api", spec, registry) == []

    def test_all_mentioned_services_are_named_not_just_one(self):
        """The refusal must not steer the arbiter toward one arbitrary pick."""
        registry = _registry("a-api", "b-api", "c-api")

        assert check_grounding("c-api", "a-api calls b-api", registry) == ["a-api", "b-api"]

    def test_a_single_service_registry_grounds_everything(self):
        """The hermetic e2e shape: one service, specs that never name it."""
        registry = _registry("api")

        assert check_grounding("api", "add an export endpoint", registry) == []

    def test_an_empty_registry_grounds_everything(self):
        assert check_grounding("anything", "mentions of whatever", {}) == []


class TestMismatchRemedy:
    def test_names_the_evidence_and_the_way_through(self):
        registry = _registry("flashback-cns", "flashback-process")
        registry["flashback-process"].description = "Cross-repo process, runbook, and design docs"

        remedy = mismatch_remedy("flashback-process", ["flashback-cns"], registry)

        assert "flashback-cns" in remedy
        assert "Cross-repo process" in remedy, "the description is what tells the arbiter WHY the choice looks wrong"
        assert "again with the same service" in remedy, "a deliberate arbiter must know the identical retry is accepted"
