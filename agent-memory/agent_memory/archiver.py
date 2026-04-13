"""L2→L3 archival — archive tuplespace facts to the knowledge graph on job completion."""

import logging
import re
import time
import uuid

from .store import MemoryStore
from .tags import normalize_tags
from .tuplespace import TupleSpace
from .types import MemoryNode

logger = logging.getLogger(__name__)

# Simple pattern to extract entity references (file paths, module names, etc.)
_ENTITY_PATTERN = re.compile(r"(?:[\w-]+/)*[\w-]+\.[\w]+|[\w]+-[\w]+(?:-[\w]+)*")


class MemoryArchiver:
    """Archives L2 facts to L3 knowledge graph on job completion."""

    def __init__(self, embedding_provider=None):
        self._embedding_provider = embedding_provider

    async def archive_job(
        self,
        tuplespace: TupleSpace,
        store: MemoryStore,
        job_id: str,
        project: str,
    ) -> int:
        """Fast path: read L2 facts for the job, create L3 nodes + edges.

        Returns the count of archived facts.
        """
        # Read all facts for this job
        facts = await tuplespace.rd(tags=[], limit=10000)
        job_facts = [f for f in facts if f.job_id == job_id]

        if not job_facts:
            logger.debug("No facts to archive for job %s", job_id)
            return 0

        # Sort by timestamp for temporal ordering
        job_facts.sort(key=lambda f: f.timestamp)

        # Create L3 nodes
        node_ids: list[str] = []
        for fact in job_facts:
            node_id = uuid.uuid4().hex[:12]
            embedding = None
            if self._embedding_provider:
                try:
                    embedding = await self._embedding_provider.embed(fact.value)
                except Exception as e:
                    logger.warning("Failed to embed fact: %s", e)

            node = MemoryNode(
                id=node_id,
                content=fact.value,
                title=f"{fact.category}:{fact.key}",
                tags=normalize_tags(fact.tags + [fact.category]),
                embedding=embedding,
                attributes={"original_key": fact.key, "original_category": fact.category},
                source_job_id=fact.job_id,
                source_agent_role=fact.agent_role,
                project=project,
                created_at=str(fact.timestamp),
            )
            await store.create_node(node)
            node_ids.append(node_id)

            # Extract and link entities
            entities = _extract_entities(fact.value)
            for entity_name in entities:
                await store.ensure_entity(entity_name, project)
                await store.create_link(node_id, entity_name, "mentions")

        # Create temporal edges (FOLLOWS)
        for i in range(1, len(node_ids)):
            await store.create_link(node_ids[i - 1], node_ids[i], "FOLLOWS", confidence=1.0, reasoning="temporal ordering within job")

        # Clean up archived facts from L2
        for fact in job_facts:
            await tuplespace.in_(category=fact.category, key_pattern=fact.key)

        logger.info("Archived %d facts for job %s to L3", len(job_facts), job_id)
        return len(job_facts)

    async def schedule_causal_inference(
        self,
        store: MemoryStore,
        node_ids: list[str],
        project: str,
    ) -> str | None:
        """Submit an Anthropic Message Batches request for causal link inference.

        Returns a batch ID or None if batching is unavailable.
        """
        if not node_ids:
            return None

        try:
            import anthropic

            client = anthropic.Anthropic()

            # Build the batch request
            nodes = []
            for nid in node_ids:
                node = await store.get_node(nid)
                if node:
                    nodes.append({"id": node.id, "content": node.content, "tags": node.tags})

            if len(nodes) < 2:
                return None

            nodes_text = "\n".join(f"- [{n['id']}] {n['content']} (tags: {', '.join(n['tags'])})" for n in nodes)
            prompt = f"""Analyze these knowledge nodes from a software project and identify causal relationships.
For each pair where one event likely caused or influenced another, output a JSON line:
{{"from": "<id>", "to": "<id>", "confidence": 0.0-1.0, "reasoning": "<why>"}}

Nodes:
{nodes_text}

Output only JSON lines, no other text."""

            requests = [
                {
                    "custom_id": f"causal-{project}-{int(time.time())}",
                    "params": {
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 2000,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                }
            ]

            batch = client.messages.batches.create(requests=requests)
            logger.info("Submitted causal inference batch %s for project %s", batch.id, project)
            return batch.id

        except ImportError:
            logger.warning("anthropic package not installed — causal inference unavailable")
            return None
        except Exception as e:
            logger.warning("Failed to submit causal inference batch: %s", e)
            return None

    async def process_causal_batch(
        self,
        store: MemoryStore,
        batch_id: str,
    ) -> int:
        """Parse completed batch results and create causal edges.

        Returns the count of causal edges created.
        """
        import json

        try:
            import anthropic

            client = anthropic.Anthropic()
            batch = client.messages.batches.retrieve(batch_id)

            if batch.processing_status != "ended":
                logger.debug("Batch %s still processing", batch_id)
                return 0

            edges_created = 0
            for result in client.messages.batches.results(batch_id):
                if result.result.type != "succeeded":
                    continue
                content = result.result.message.content[0].text
                for line in content.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        edge = json.loads(line)
                        await store.create_link(
                            from_id=edge["from"],
                            to_entity=edge["to"],
                            link_type="CAUSED_BY",
                            confidence=float(edge.get("confidence", 0.5)),
                            reasoning=edge.get("reasoning"),
                        )
                        edges_created += 1
                    except (json.JSONDecodeError, KeyError):
                        continue

            logger.info("Processed causal batch %s: %d edges created", batch_id, edges_created)
            return edges_created

        except ImportError:
            logger.warning("anthropic package not installed")
            return 0
        except Exception as e:
            logger.warning("Failed to process causal batch %s: %s", batch_id, e)
            return 0


def _extract_entities(text: str) -> list[str]:
    """Extract entity references from text (file paths, module names)."""
    matches = _ENTITY_PATTERN.findall(text)
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen and len(m) > 3:
            seen.add(m)
            result.append(m)
    return result[:10]  # Cap at 10 entities per fact
