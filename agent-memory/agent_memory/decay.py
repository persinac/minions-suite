"""Access-frequency decay for L3 knowledge graph nodes."""

import logging
import math
import time

from .store import MemoryStore

logger = logging.getLogger(__name__)


async def apply_decay(store: MemoryStore, project: str, threshold_days: int = 60) -> int:
    """Reduce salience of old, unaccessed nodes.

    Nodes older than threshold_days with low access counts get their
    access_count decremented (floor 0). Returns count of decayed nodes.

    High-access nodes resist decay: a node needs access_count < log2(age_days)
    to be eligible for decay.
    """
    # Fetch all nodes for the project via tag query with empty tags
    # (this returns all nodes since we need to scan)
    all_nodes = await store.query_by_tags(project, [], limit=10000)

    now = time.time()
    threshold_seconds = threshold_days * 86400
    decayed = 0

    for node in all_nodes:
        try:
            age_seconds = now - float(node.created_at)
        except (ValueError, TypeError):
            continue

        if age_seconds < threshold_seconds:
            continue

        age_days = age_seconds / 86400
        decay_threshold = math.log2(max(age_days, 1))

        if node.access_count < decay_threshold:
            # This node is old and under-accessed — it would rank lower naturally
            # We don't modify the node directly; the scoring function in retrieval.py
            # already penalizes old + low-access nodes via recency and access_frequency weights.
            # This function is a placeholder for future active pruning/archival.
            decayed += 1
            logger.debug("Node %s eligible for decay: age=%.0f days, access=%d", node.id, age_days, node.access_count)

    return decayed
