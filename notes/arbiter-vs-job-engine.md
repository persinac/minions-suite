# Arbiter vs Job Engine: Responsibilities & Boundaries

## Status: Current Architecture

## Context

The job engine was the first implementation — a polling loop that advances jobs through a state machine. As the system grew, we wanted more granular control over state transitions with validation, circuit breaking, and anomaly detection. The arbiter was born to fill that role.

## Current Flow

```
Agent (claude -p subprocess)
  │
  │  tool call (e.g. report_review_complete)
  ▼
MCP Server (server.py / tools.py)
  │
  │  NATS request/reply → "arbiter.state.transition"
  │  payload: { entity_type, entity_id, to_status, job_id, kwargs }
  │  timeout: 10s
  ▼
Arbiter (arbiter.py)
  │
  │  1. Circuit breaker check
  │  2. Dispatch to _apply_job|task|subtask_transition()
  │  3. Direct async DB write
  │  4. DB layer validates transition against state_transitions.py
  │  5. Records audit event
  │  6. Reply: { approved: true/false, ... }
  ▼
Database (SQLite / PostgreSQL)
  │
  │  (no notification — just rows changed)
  ▼
Job Engine (job_engine.py) — polls DB every 5s
  │  _poll() → get_active_jobs() → _advance(job)
  │  Reacts to state, launches next agent(s)
```

## Responsibilities

| Component | Owns | Writes to DB |
|-----------|------|--------------|
| **Arbiter** | State transition validation, circuit breaker, heartbeat monitoring, timeout detection, anomaly remediation | Yes — agent-initiated state changes (task status, job status, subtask status) |
| **Job Engine** | Orchestration state machine, agent lifecycle (launch/cleanup), job advancement | Yes — orchestration-level changes (advancing job state, creating agents/tasks) |

## Key Design Decisions

### Arbiter writes to DB directly (not via job engine)
The arbiter applies validated transitions immediately. The job engine discovers these changes on next poll. This avoids a round-trip through the engine for every agent action.

### Job engine polls DB (not NATS events from arbiter)
Simplicity wins here. Polling is idempotent, crash-recoverable, and requires no event replay logic. See `full-pub-sub.md` for the evolution path.

### Double validation
The DB layer (`db.py`) also validates transitions using `state_transitions.py`. Even if something bypasses the arbiter (fallback mode, direct DB access), illegal transitions are rejected. Defense in depth.

### Fallback mode
When `arbiter_enabled=False` or NATS is unavailable, the MCP server tools write directly to DB. The job engine polls and advances jobs identically — it doesn't care who wrote the state.

## NATS Subjects

| Subject | Pattern | Purpose |
|---------|---------|---------|
| `arbiter.state.transition` | Request/reply (10s timeout) | Agent → Arbiter state change proposals |
| `arbiter.heartbeat` | Fire-and-forget publish | Agent liveness signals |
| `agents.{agent_id}.control` | Publish | Arbiter → Agent kill signals |
| `agents.results.>` | JetStream publish | K8s agent completion → Job Engine |
| `agents.{job_id}.status` | Publish | Agent lifecycle events for dashboard |

## Open Questions

- Should the arbiter own job-level advancement too (currently job engine's responsibility)?
- Should the arbiter publish state-change events for the job engine to subscribe to? (See `full-pub-sub.md`)
