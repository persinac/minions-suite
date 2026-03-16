"""Prompt context builders — Obsidian-style markdown from L3 knowledge."""

import logging

from .store import MemoryStore
from .tracing import MemoryTraceEvent, TraceOp, emit
from .types import MemoryNode

logger = logging.getLogger(__name__)

# Rough chars-per-token estimate
CHARS_PER_TOKEN = 4


async def build_knowledge_context(
    store: MemoryStore,
    project: str,
    task_description: str | None = None,
    embedding: list[float] | None = None,
    max_tokens: int = 2000,
) -> str:
    """Retrieve relevant L3 memories and format as Obsidian-style markdown.

    Returns empty string when no relevant knowledge is found.
    """
    from .retrieval import get_relevant_memories

    tags = _extract_tags(task_description) if task_description else []
    nodes = await get_relevant_memories(store, project, query_embedding=embedding, tags=tags, max_tokens=max_tokens)

    if not nodes:
        emit(MemoryTraceEvent(op=TraceOp.CONTEXT_KNOWLEDGE, project=project, tier="context", details={"chars": 0, "node_count": 0}))
        return ""

    result = _format_knowledge_section(nodes, max_tokens)
    emit(
        MemoryTraceEvent(
            op=TraceOp.CONTEXT_KNOWLEDGE,
            project=project,
            tier="context",
            details={"chars": len(result), "node_count": len(nodes), "extracted_tags": tags, "node_ids": [n.id for n in nodes[:10]]},
        )
    )
    return result


async def build_file_context(
    store: MemoryStore,
    project: str,
    file_paths: list[str],
    max_tokens: int = 2000,
) -> str:
    """Retrieve memories linked to changed files and format as markdown.

    Returns empty string when no file-linked memories exist.
    """
    from .retrieval import get_file_backlinks

    nodes = await get_file_backlinks(store, project, file_paths, max_tokens=max_tokens)

    if not nodes:
        emit(
            MemoryTraceEvent(
                op=TraceOp.CONTEXT_FILE, project=project, tier="context", details={"chars": 0, "node_count": 0, "file_count": len(file_paths)}
            )
        )
        return ""

    result = _format_file_section(nodes, file_paths, max_tokens)
    emit(
        MemoryTraceEvent(
            op=TraceOp.CONTEXT_FILE,
            project=project,
            tier="context",
            details={"chars": len(result), "node_count": len(nodes), "file_count": len(file_paths), "files": file_paths[:10]},
        )
    )
    return result


def _format_knowledge_section(nodes: list[MemoryNode], max_tokens: int) -> str:
    """Format nodes as an Obsidian-style 'Prior Knowledge' section."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    lines = ["## Prior Knowledge", "", "Relevant knowledge from prior work on this project:", ""]
    used = sum(len(ln) for ln in lines)

    for node in nodes:
        entry = _format_node(node)
        if used + len(entry) > max_chars:
            break
        lines.append(entry)
        used += len(entry)

    if len(lines) <= 4:
        return ""

    return "\n".join(lines)


def _format_file_section(nodes: list[MemoryNode], file_paths: list[str], max_tokens: int) -> str:
    """Format nodes as a 'File Knowledge' section grouped by file."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    lines = ["## File Knowledge", "", "Known context for files under review:", ""]
    used = sum(len(ln) for ln in lines)

    for node in nodes:
        entry = _format_node(node)
        if used + len(entry) > max_chars:
            break
        lines.append(entry)
        used += len(entry)

    if len(lines) <= 4:
        return ""

    return "\n".join(lines)


def _format_node(node: MemoryNode) -> str:
    """Format a single MemoryNode as Obsidian-style markdown."""
    parts = []

    title = node.title or node.content[:60].replace("\n", " ")
    parts.append(f"### {title}")

    if node.tags:
        tag_str = " ".join(f"#{t}" for t in node.tags)
        parts.append(f"Tags: {tag_str}")

    parts.append(node.content)

    if node.links:
        link_str = ", ".join(f"[[{lk}]]" for lk in node.links)
        parts.append(f"Links: {link_str}")

    if node.source_agent_role:
        parts.append(f"_Source: {node.source_agent_role}_")

    parts.append("")  # blank line after each node
    return "\n".join(parts)


def _extract_tags(text: str) -> list[str]:
    """Extract potential tag keywords from task description text."""
    from .tags import CONTROLLED_TAGS

    words = set(text.lower().split())
    return [w for w in words if w in CONTROLLED_TAGS]
