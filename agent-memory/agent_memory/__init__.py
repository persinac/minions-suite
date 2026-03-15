"""agent-memory — Framework-grade tiered memory system for AI agents."""

from .protocols import EmbeddingProvider, MemoryStoreBackend, TupleSpaceBackend
from .tags import normalize_tags
from .types import Entity, Fact, MemoryNode

__all__ = [
    "EmbeddingProvider",
    "Entity",
    "Fact",
    "MemoryNode",
    "MemoryStore",
    "MemoryStoreBackend",
    "TupleSpace",
    "TupleSpaceBackend",
    "build_file_context",
    "build_knowledge_context",
    "get_file_backlinks",
    "get_relevant_memories",
    "normalize_tags",
]


def __getattr__(name: str):
    """Lazy imports for modules that may not be created yet."""
    if name == "TupleSpace":
        from .tuplespace import TupleSpace

        return TupleSpace
    if name == "MemoryStore":
        from .store import MemoryStore

        return MemoryStore
    if name == "get_relevant_memories":
        from .retrieval import get_relevant_memories

        return get_relevant_memories
    if name == "get_file_backlinks":
        from .retrieval import get_file_backlinks

        return get_file_backlinks
    if name == "build_knowledge_context":
        from .context import build_knowledge_context

        return build_knowledge_context
    if name == "build_file_context":
        from .context import build_file_context

        return build_file_context
    raise AttributeError(f"module 'agent_memory' has no attribute {name!r}")
