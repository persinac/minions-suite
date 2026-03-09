# NATS in Minion Suite

Minion Suite uses [NATS](https://nats.io/) with JetStream as its internal message bus for agent coordination, state management, and real-time event delivery. NATS is **optional** — without it the system falls back to direct database writes, but multi-agent orchestration (dev jobs, arbiter, K8s dispatch) requires it.

---

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `NATS_ENABLED` | `false` | Master toggle for all NATS functionality |
| `NATS_SERVER_IP` | `nats://localhost:4222` | Server address(es), comma-separated for clusters |
| `NATS_USER` / `NATS_PASS` | — | Optional basic auth credentials |
| `JETSTREAM_STREAM` | `minions` | JetStream stream name |
| `ARBITER_ENABLED` | `false` | Route state mutations through the arbiter (requires NATS) |

The `NatsConfig` class normalises URLs automatically — bare hostnames like `nats` become `nats://nats:4222`, and schemes `nats://`, `tls://`, `ws://`, `wss://` are all supported.

---

## JetStream Stream

Initialised on server startup by `ensure_jetstream_stream()` (`connectors/nats_init.py`):

| Setting | Value |
|---|---|
| Retention | LIMITS |
| Storage | FILE |
| Max messages | 1,000,000 |
| Max age | 7 days |
| Max message size | 1 MB |
| Discard policy | OLD |
| Replicas | 1 |

**Subjects bound to the stream:**

```
agents.>      # Agent work, results, status, control, messages
system.>      # Dashboard events
reviews.>     # Review lifecycle events
jobs.>        # Job status updates
```

A **durable pull consumer** named `agent-workers` is created on `agents.work` for K8s job dispatch (ack wait 120 s, max deliver 3, max ack pending 10).

---

## Subject Taxonomy

### Review lifecycle (fire-and-forget)

```
jobs.review.requested.<project>   # New review job created
jobs.review.started.<project>     # Review picked up by engine
jobs.review.completed.<project>   # Review finished (approve / request_changes)
jobs.review.failed.<project>      # Review errored
```

### Agent work queue (JetStream durable pull)

```
agents.work                       # Work items dispatched to K8s pods
agents.results.<job_id>           # Agent publishes its result back to the engine
```

### Agent lifecycle & control

```
agents.<job_id>.status            # Status events (launched, completed, failed)
agents.<agent_id>.control         # Kill signals from the arbiter
agents.<job_id>.messages.<target> # Inter-agent messaging
```

### Arbiter coordination (request / reply)

```
arbiter.state.transition          # State transition proposals → approval / rejection
arbiter.heartbeat                 # Agent heartbeat signals
```

### System events

```
system.events                     # Real-time events streamed to the dashboard via SSE
```

---

## Key Components

### NatsClient (`connectors/nats_client.py`)

Persistent connection with auto-reconnect (2-second wait, infinite retries, graceful drain on close). Core methods:

| Method | Pattern | Purpose |
|---|---|---|
| `publish()` | Fire-and-forget | Generic publishing |
| `subscribe()` | Push | Generic message handler |
| `request()` | Request / reply | Arbiter state proposals (10 s timeout) |
| `publish_work()` | JetStream publish | Enqueue work for K8s agents |
| `subscribe_work_queue()` | JetStream durable pull | K8s pods consume work items |
| `subscribe_results()` | Push (wildcard) | Engine listens on `agents.results.>` |
| `extend_ack()` | JetStream in-progress | Long-running agents extend ack deadline |

### NatsPublisher (`connectors/nats_publisher.py`)

Lightweight ephemeral connect-publish-close wrapper used by **K8s agent pods** that don't maintain persistent connections:

- `publish_agent_message()` — inter-agent messaging
- `publish_agent_status()` — lifecycle events
- `publish_system_event()` — dashboard updates

### NatsSubscriber (`connectors/nats_subscriber.py`)

Async generator that yields `system.events` messages for the dashboard SSE endpoint. Uses a JetStream push subscription with `DeliverPolicy.NEW` and auto-reconnects with 5-second backoff.

---

## Data Flows

### Review Job

```
Webhook / CLI
  └─ db.create_review_job()
       └─ Engine polls, picks up job
            ├─ publish: jobs.review.started.<project>
            └─ Launches agent (in-process or K8s)
                 ├─ K8s: publish_work() → agents.work
                 │        pod completes → agents.results.<job_id>
                 │        Engine._on_nats_result() handles it
                 └─ In-process: direct return
            └─ publish: jobs.review.completed.<project>
```

### Dev Job (Multi-Agent Orchestration)

```
Spec submitted (MCP tool)
  └─ Spec analyst launched (K8s)
       └─ Results via agents.results.<job_id>
            └─ Arbiter creates tasks
                 └─ Engineers launched (K8s)
                      ├─ Heartbeats → arbiter.heartbeat
                      ├─ State changes → arbiter.state.transition (req/reply)
                      └─ Results via agents.results.<job_id>
                           └─ PR opened → Code review
                                └─ Deploy monitor launched
                                     └─ Results via agents.results.<job_id>
                                          └─ Job DONE
```

### Arbiter State Machine

```
MCP tool (e.g. update_task_status)
  └─ _propose_transition()
       └─ nats_client.request("arbiter.state.transition", payload)
            └─ Arbiter._handle_transition()
                 ├─ Validates transition is legal
                 ├─ Checks circuit breaker
                 ├─ Applies to DB
                 └─ Returns { approved: true/false, error: "..." }
```

### Heartbeat Monitoring

```
Agent pod
  └─ send_heartbeat() → arbiter.heartbeat
       └─ Arbiter._handle_heartbeat() → stores in DB
            └─ _check_stale_heartbeats() (periodic)
                 └─ Stale agent detected
                      └─ publish: agents.<agent_id>.control  { action: "kill" }
```

---

## Arbiter Request / Reply Protocol

State-mutating MCP tools route through the arbiter when `ARBITER_ENABLED=true` and NATS is connected. The arbiter acts as a **centralized state validator** with a circuit breaker.

**Request payload:**

```json
{
  "entity_type": "job | task | subtask",
  "entity_id": "<uuid>",
  "to_status": "<target status>",
  "job_id": "<job uuid>",
  "kwargs": {}
}
```

**Response payload:**

```json
{
  "approved": true,
  "from_status": "IN_PROGRESS",
  "to_status": "DONE"
}
```

Or on rejection:

```json
{
  "approved": false,
  "error": "Invalid transition from PENDING to DONE"
}
```

Timeout is 10 seconds. If the arbiter is unreachable the tool returns an error to the calling agent.

---

## Behaviour Without NATS

When `NATS_ENABLED=false`:

- MCP tools write directly to the database (no arbiter validation)
- Heartbeats are stored in the DB (PostgreSQL only)
- No K8s work queue — agents run in-process only
- No real-time dashboard events (SSE is unavailable)
- Review jobs still work via the engine's polling loop

---

## Docker Compose

```yaml
nats:
  image: nats:latest
  command: ["-js", "--store_dir=/data"]   # JetStream enabled, file storage
  volumes:
    - natsdata:/data
  ports:
    - "4222:4222"    # Client connections
    - "8222:8222"    # HTTP monitoring
```

The `minion-suite` service depends on `nats:service_started` and connects via the container hostname (`nats://nats:4222`).

---

## Error Handling

| Component | Strategy |
|---|---|
| NatsClient | Auto-reconnect, infinite retries, 2 s backoff, graceful drain |
| Arbiter | Circuit breaker (opens after N failures in a time window, cooldown period) |
| Dashboard subscriber | Catches exceptions, reconnects with 5 s backoff |
| K8s work queue | Max 3 delivery attempts, 120 s ack wait |
| Agent pods | Ephemeral publisher — connect, publish, close; failures are logged but non-fatal |
