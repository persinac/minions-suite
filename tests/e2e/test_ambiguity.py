"""Ambiguous tickets, and what the system does with the guesses they force.

A vague ticket is the normal case, not the edge case. "Show recent orders" does
not say how recent; the analyst must decide before anything downstream can be
implemented. The decision is fine. The decision being invisible is not: once
`submit_refined_spec` overwrites `job.spec`, an engineer reading the refined text
cannot tell the analyst's guess from the author's instruction, and neither can
whoever reviews the PR.

These tests cover the three things that make a guess auditable -- it must be
stated, it must survive to the agents that act on it, and the ticket that
prompted it must still exist to judge it against.
"""

from minions.core.models import JobStatus
from minions.core.spec_contract import SpecContractError, extract_assumptions, has_assumptions, validate_refined_spec
from minions.engine import dev

from .conftest import Call, turn

VAGUE_TICKET = "Show recent orders on the dashboard"

WITH_ASSUMPTIONS = """# Refined: recent orders on the dashboard

Add a panel listing recent orders.

## Assumptions
1. "recent" — unbounded in the ticket. Read as the last 30 days, matching the
   window `reports/weekly.py` already uses for the same word.
2. Ordering is newest-first, matching every other list in the dashboard.
"""

NO_ASSUMPTIONS = """# Refined: recent orders on the dashboard

Add a panel listing recent orders from the last 30 days, newest first.
"""


class TestTheContractItself:
    def test_a_spec_without_assumptions_is_refused(self):
        try:
            validate_refined_spec(NO_ASSUMPTIONS)
        except SpecContractError as e:
            assert "## Assumptions" in e.remedy
            assert "None — spec fully specified" in e.remedy, "the refusal must show the exit for a genuinely complete spec"
        else:
            raise AssertionError("expected SpecContractError")

    def test_an_empty_assumptions_section_is_refused(self):
        """An empty section is indistinguishable from not having looked."""
        try:
            validate_refined_spec("# Spec\n\nDo the thing.\n\n## Assumptions\n")
        except SpecContractError as e:
            assert "empty" in str(e).lower() or "nothing under it" in str(e)
        else:
            raise AssertionError("expected SpecContractError")

    def test_an_empty_section_followed_by_another_is_still_empty(self):
        """The section is bounded by the next heading, not by end-of-document.

        Without the bound, "is there text after the heading" answers yes because
        the *following* section supplies the text -- so an empty assumptions
        section slipped past the check whose whole purpose is empty sections.
        Whether the analyst puts assumptions last is not something to rely on.
        """
        try:
            validate_refined_spec("# Spec\n\n## Assumptions\n\n## Notes\nUnrelated prose.\n")
        except SpecContractError as e:
            assert "nothing under it" in str(e)
        else:
            raise AssertionError("an empty section followed by another section must still be refused")

    def test_a_populated_section_followed_by_another_is_accepted(self):
        spec = "# Spec\n\n## Assumptions\n1. Read 'recent' as 30 days.\n\n## Notes\nUnrelated.\n"
        validate_refined_spec(spec)
        assert extract_assumptions(spec) == "1. Read 'recent' as 30 days."
        assert "Unrelated" not in extract_assumptions(spec), "extraction must stop at the next heading"

    def test_an_explicit_none_is_accepted(self):
        validate_refined_spec("# Spec\n\nDo it.\n\n## Assumptions\nNone — spec fully specified.")

    def test_heading_level_and_case_are_not_the_point(self):
        """Refusing `### assumptions` would burn a turn teaching nothing."""
        assert has_assumptions("## Assumptions\n1. x")
        assert has_assumptions("### assumptions\n1. x")
        assert has_assumptions("# ASSUMPTIONS\n1. x")
        assert not has_assumptions("I made some assumptions about the window.")

    def test_what_is_accepted_is_what_is_read_back(self):
        """The grader reads assumptions with the same rule that accepted them.

        Two regexes would drift, and then a spec could pass submission and grade
        as having none -- a contradiction with no obvious wrong party.
        """
        assert extract_assumptions(WITH_ASSUMPTIONS).startswith("1.")
        assert "newest-first" in extract_assumptions(WITH_ASSUMPTIONS).lower()
        assert extract_assumptions(NO_ASSUMPTIONS) == ""
        # Anything has_assumptions accepts must yield readable text, and vice versa.
        for spec in (WITH_ASSUMPTIONS, NO_ASSUMPTIONS, "## Assumptions\nNone — spec fully specified."):
            assert bool(extract_assumptions(spec)) == has_assumptions(spec)


class TestTheContractInTheLoop:
    async def test_a_refused_spec_is_not_stored(self, db, e2e_engine, scripted_llm):
        scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": NO_ASSUMPTIONS})))

        job = await db.create_job(VAGUE_TICKET)
        await dev.launch_spec_analyst(e2e_engine, job)

        assert (await db.get_job(job.id)).spec == VAGUE_TICKET, "a refused spec must not overwrite the ticket"

    async def test_a_never_refined_job_advances_but_says_so(self, db, e2e_engine, scripted_llm):
        """The contract cannot deadlock a job, and the escape hatch is audited.

        `launch_spec_analyst` advances on the raw ticket when no refined spec was
        accepted, so a broken analyst degrades the job instead of wedging it.
        That is the right trade, but it means "every job carries assumptions" is
        not a guarantee -- so the exception has to be visible. Previously it was a
        log line only, and a job that guessed silently looked identical to one
        that had nothing to guess about.
        """
        scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": NO_ASSUMPTIONS})))

        job = await db.create_job(VAGUE_TICKET)
        await dev.launch_spec_analyst(e2e_engine, job)

        updated = await db.get_job(job.id)
        assert updated.status == JobStatus.SPEC_READY
        kinds = {e["event_type"] for e in await db.get_events(job.id)}
        assert "spec_refinement_skipped" in kinds, "an unrefined job must leave a trace"

    async def test_the_analyst_can_correct_itself_and_proceed(self, db, e2e_engine, scripted_llm):
        """The refusal is retryable, and retryable has to mean recoverable.

        This is the whole reason the error names what is missing instead of just
        saying no. Turn one omits the section and is refused; turn two adds it
        and lands. If this fails, the contract is not a guardrail -- it is a way
        to kill jobs.
        """
        scripted_llm.script(
            "spec_analyst",
            turn(Call("submit_refined_spec", {"spec": NO_ASSUMPTIONS})),
            turn(Call("submit_refined_spec", {"spec": WITH_ASSUMPTIONS})),
        )

        job = await db.create_job(VAGUE_TICKET)
        await dev.launch_spec_analyst(e2e_engine, job)

        updated = await db.get_job(job.id)
        assert updated.status == JobStatus.SPEC_READY
        assert "last 30 days" in updated.spec


class TestGuessesSurvive:
    async def test_assumptions_reach_the_agents_that_act_on_them(self, db, e2e_engine, scripted_llm):
        """The refined spec is appended to every downstream prompt verbatim.

        Asserted against the prompt the arbiter was actually handed, not against
        the database row -- the row being right is not the same as the text
        arriving where a decision gets made.
        """
        scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": WITH_ASSUMPTIONS})))
        scripted_llm.script(
            "arbiter",
            turn(Call("create_task", {"title": "Orders panel", "description": "Add it.", "service": "api", "agent_role": "backend_engineer"})),
            turn(Call("mark_tasks_created", {})),
        )

        job = await db.create_job(VAGUE_TICKET)
        await dev.launch_spec_analyst(e2e_engine, job)
        await dev.launch_arbiter(e2e_engine, await db.get_job(job.id))

        arbiter_prompts = [p for role, p in scripted_llm.prompts if role == "arbiter"]
        assert arbiter_prompts, "the arbiter never ran"
        assert "last 30 days" in arbiter_prompts[0], "the analyst's guess never reached the arbiter"

    async def test_the_original_ticket_survives_for_comparison(self, db, e2e_engine, scripted_llm):
        """Judging an assumption needs the ticket that prompted it.

        `submit_refined_spec` overwrites `spec`, so without `original_spec` the
        vague wording is gone at exactly the moment there is something to compare
        it to.
        """
        scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": WITH_ASSUMPTIONS})))

        job = await db.create_job(VAGUE_TICKET)
        await dev.launch_spec_analyst(e2e_engine, job)

        updated = await db.get_job(job.id)
        assert updated.original_spec == VAGUE_TICKET
        assert updated.spec != VAGUE_TICKET

    async def test_a_second_refinement_does_not_clobber_the_original(self, db, e2e_engine, scripted_llm):
        """A retry must not overwrite the human's words with the machine's."""
        scripted_llm.script(
            "spec_analyst",
            turn(Call("submit_refined_spec", {"spec": WITH_ASSUMPTIONS})),
            turn(Call("submit_refined_spec", {"spec": WITH_ASSUMPTIONS.replace("30 days", "14 days")})),
        )

        job = await db.create_job(VAGUE_TICKET)
        await dev.launch_spec_analyst(e2e_engine, job)

        updated = await db.get_job(job.id)
        assert updated.original_spec == VAGUE_TICKET, "the original must survive every later refinement"
