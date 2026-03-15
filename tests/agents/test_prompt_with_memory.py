"""Tests for prompt integration with memory knowledge context."""

from minions.agents.prompt import build_agent_prompt
from minions.core.models import Job, Task


def _make_job():
    return Job(spec="Build auth module")


def _make_task(role="backend_engineer"):
    return Task(
        job_id="job-1",
        title="Implement JWT auth",
        description="Add JWT auth to the API",
        service="api",
        agent_role=role,
    )


def test_prior_knowledge_section_included():
    """When knowledge_context is provided, prompt includes Prior Knowledge."""
    job = _make_job()
    task = _make_task()
    knowledge = "## Prior Knowledge\n\nAuth uses JWT with RS256 signing."
    prompt = build_agent_prompt(job, task, knowledge_context=knowledge)
    assert "Prior Knowledge" in prompt
    assert "RS256 signing" in prompt


def test_no_knowledge_section_when_none():
    """When knowledge_context is None, no extra section appears."""
    job = _make_job()
    task = _make_task()
    prompt = build_agent_prompt(job, task, knowledge_context=None)
    assert "Prior Knowledge" not in prompt


def test_no_knowledge_section_when_empty():
    """When knowledge_context is empty string, no extra section appears."""
    job = _make_job()
    task = _make_task()
    prompt = build_agent_prompt(job, task, knowledge_context="")
    assert "Prior Knowledge" not in prompt


def test_knowledge_between_task_and_additional_context():
    """Knowledge context appears between task context and additional context."""
    job = _make_job()
    task = _make_task()
    knowledge = "## Prior Knowledge\n\nRelevant info."
    additional = "## Retry Context\n\nPrior error: timeout"
    prompt = build_agent_prompt(job, task, context=additional, knowledge_context=knowledge)
    knowledge_pos = prompt.index("Prior Knowledge")
    context_pos = prompt.index("Retry Context")
    assert knowledge_pos < context_pos
