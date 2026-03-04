# Full Pub/Sub: Evolving Beyond Polling

## Status: Proposed Enhancement

## Context

The job engine currently polls the database every 5 seconds to discover state changes. This works well but introduces up to 5s latency between a state change (written by the arbiter) and the engine reacting. Full pub/sub would make the engine event-driven.

## Current: Poll-Only

```
Arbiter writes DB
  ↓
  (up to 5s delay)
  ↓
Job Engine polls DB → _advance(job)
```

**Strengths:**
- Crash recovery is trivial — restart and poll
- `_advance()` is idempotent — safe to run repeatedly
- No ordering concerns, no duplicate event handling
- Engine doesn't need to know *what changed*, just *what is*

**Weaknesses:**
- Up to 5s latency on every state transition
- Wasted DB queries when nothing has changed
- Doesn't scale well if poll interval needs to decrease

## Proposed: Hybrid (Pub/Sub + Reconciliation Poll)

```
Arbiter writes DB
  → publishes event to NATS: "state.changed.{entity_type}.{entity_id}"
  → Job Engine subscribes, reacts immediately (fast path)

Job Engine also polls DB every 30-60s (safety net)
  → catches anything missed due to NATS blip, engine restart, etc.
```

### NATS Event Schema

```json
{
  "entity_type": "job|task|subtask",
  "entity_id": "abc123",
  "job_id": "job-456",
  "from_status": "in_review",
  "to_status": "merged",
  "timestamp": "2026-03-01T12:00:00Z"
}
```

### Suggested NATS Subjects

| Subject | Purpose |
|---------|---------|
| `state.changed.job.{job_id}` | Job status transitions |
| `state.changed.task.{task_id}` | Task status transitions |
| `state.changed.subtask.{subtask_id}` | Subtask status transitions |

Use JetStream with durable consumers so the engine doesn't miss events during restarts.

## Implementation Notes

### What changes in the arbiter
Minimal. After each successful `_apply_*_transition()`, publish the event:

```python
# In _apply_task_transition, after DB write succeeds:
await self.nats_client.publish(
    f"state.changed.task.{task_id}",
    {"entity_type": "task", "entity_id": task_id, "job_id": job_id,
     "from_status": current_status, "to_status": to_status, "timestamp": _now()}
)
```

### What changes in the job engine
Add a JetStream durable subscription alongside the existing poll loop:

```python
async def start(self):
    # Subscribe to state change events (fast path)
    await self._nats_client.subscribe("state.changed.>", self._on_state_changed)

    # Reconciliation poll (safety net, longer interval)
    while self._running:
        await self._poll()
        await asyncio.sleep(60)  # relaxed from 5s to 60s

async def _on_state_changed(self, msg):
    payload = json.loads(msg.data)
    job_id = payload["job_id"]
    job = await self.db.get_job(job_id)
    if job:
        await self._advance(job)
```

### Idempotency
`_advance()` is already idempotent — it checks current state before acting. Processing the same event twice (once from NATS, once from poll) is safe. No deduplication logic needed.

### JetStream consumer config
- **Durable name:** `job-engine`
- **Ack policy:** Explicit (ack after `_advance()` completes)
- **Deliver policy:** Last per subject (on restart, only need latest state per entity)
- **Max deliver:** 3 (retry on processing failure, then dead-letter)

## Tradeoffs

| | Poll-Only (current) | Hybrid (proposed) |
|---|---|---|
| Latency | Up to 5s | Sub-second (fast path) |
| Crash recovery | Trivial | Trivial (poll safety net + JetStream durability) |
| Complexity | Low | Moderate (JetStream consumer management) |
| DB load | Higher (frequent polls) | Lower (poll interval relaxed to 60s) |
| Ordering | N/A (reads latest state) | N/A (`_advance()` is idempotent) |

## Migration Path

1. Add event publishing to arbiter `_apply_*` methods
2. Add JetStream subscription to job engine alongside existing poll
3. Increase poll interval from 5s to 30-60s
4. Monitor for a release cycle to confirm no missed events
5. Optionally remove poll entirely (or keep as 5-minute heartbeat)
