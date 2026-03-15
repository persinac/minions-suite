"""Shared data models for agent memory — zero framework deps beyond Pydantic."""

from pydantic import BaseModel


class MemoryNode(BaseModel):
    """A persistent knowledge node in the L3 knowledge graph."""

    id: str
    content: str
    title: str | None = None
    tags: list[str] = []
    created_at: str
    embedding: list[float] | None = None
    attributes: dict = {}
    source_job_id: str | None = None
    source_agent_role: str | None = None
    project: str
    access_count: int = 0
    links: list[str] = []  # entity names this note links to


class Fact(BaseModel):
    """A short-lived fact in the L2 tuplespace."""

    category: str
    key: str
    value: str
    tags: list[str] = []
    agent_role: str | None = None
    job_id: str | None = None
    project: str
    timestamp: float


class Entity(BaseModel):
    """A named entity in the knowledge graph (file, module, concept)."""

    id: str
    name: str
    entity_type: str | None = None
    project: str
