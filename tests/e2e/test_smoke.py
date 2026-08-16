"""Harness smoke test: does a scripted model actually move a real job?

If this fails, nothing else in tests/e2e/ means anything -- the seam is wrong and
the other tests are asserting against a job nobody drove.
"""

from minions.core.models import JobStatus
from minions.engine import dev

from .conftest import Call, turn

REFINED = """# Refined

Add the thing.

## Assumptions
1. "the thing" — read as the export endpoint, the only exporter in the repo.
"""


async def test_spec_analyst_moves_the_job_to_spec_ready(db, e2e_engine, scripted_llm):
    scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": REFINED})))

    job = await db.create_job("Add the thing")
    await dev.launch_spec_analyst(e2e_engine, job)

    updated = await db.get_job(job.id)
    assert updated.status == JobStatus.SPEC_READY
    assert scripted_llm.called("spec_analyst", "submit_refined_spec")


async def test_the_refined_spec_replaces_the_raw_one(db, e2e_engine, scripted_llm):
    scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": REFINED})))

    job = await db.create_job("Add the thing")
    await dev.launch_spec_analyst(e2e_engine, job)

    updated = await db.get_job(job.id)
    assert "## Assumptions" in updated.spec
    assert updated.original_spec == "Add the thing"


async def test_the_analyst_is_offered_only_its_own_tools(db, e2e_engine, scripted_llm):
    """The tools reaching the model are the ones get_tools_for_role returns."""
    scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": REFINED})))

    job = await db.create_job("Add the thing")
    await dev.launch_spec_analyst(e2e_engine, job)

    offered = scripted_llm.tool_names["spec_analyst"]
    assert "submit_refined_spec" in offered
    assert "create_task" not in offered, "decomposition belongs to the arbiter"
