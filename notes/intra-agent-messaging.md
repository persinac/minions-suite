# Intra-Agent Messaging: Agent-to-Agent Communication

## Status: Partially Implemented

## Context

Agents are `claude -p` subprocesses — they run, call tools, and exit. They don't have event loops listening for incoming messages. We want agents to be able to queue tasks or responses for other agents without requiring real-time delivery.

## What Exists Today

### Database-backed messages (`send_message` / `get_messages`)
- **Model:** `Message(id, job_id, from_role, to_role, content, created_at)` — `to_role=None` means broadcast
- **Tools:** `send_message` (available to spec_analyst, arbiter), `get_messages`
- **Usage:** Code reviewer auto-sends feedback to the engineer on review completion (`server.py:334-341`)
- **Delivery:** Messages are injected into agent prompt context at launch time (`job_engine.py:816-822`)
- **Limitation:** Agent 2 only sees messages when (re)launched — no real-time delivery

### NATS pub/sub subjects (defined but not fully wired)
- `agents.{job_id}.messages.{to_role}` — targeted message to a role
- `agents.{job_id}.messages.broadcast` — broadcast to all agents in a job
- **Publisher:** `nats_publisher.publish_agent_message()` exists
- **Subscriber:** No agent subscribes to these during execution

## Proposed Approaches

### Option A: Poll-Based (Recommended — Fits One-Shot Model)

Add a `check_messages` tool that agents call between subtasks. This is the hybrid approach — DB persistence for durability, agent-initiated polling for delivery.

```
Agent 1 calls send_message(job_id, from="code_reviewer", to="backend_engineer", content="...")
  → persisted to messages table
  → optionally published to NATS for dashboard/logging

Agent 2 calls check_messages(job_id, my_role) between subtasks
  → reads from messages table
  → returns new messages since last check
```

**Changes needed:**
1. Expose `check_messages` tool to all agent roles (currently limited to spec_analyst/arbiter)
2. Add a `last_read_at` or sequence number to track what Agent 2 has already seen
3. Prompt agents to call `check_messages` between subtasks (system prompt instruction)
4. Optionally: return message count in heartbeat response so agents know to check

**Prompt addition for agents:**
```
Between subtasks, call check_messages() to see if other agents have sent you
feedback or coordination requests. Act on any messages before starting your
next subtask.
```

### Option B: NATS Inbox with Tool Drain

Use JetStream durable consumers as per-agent mailboxes. Messages queue until the agent drains them.

```
Agent 1 publishes to agents.{job_id}.messages.backend_engineer
  → JetStream durable consumer holds the message

Agent 2 calls check_inbox() tool
  → drains JetStream consumer
  → returns queued messages
  → acks after agent processes them
```

**Advantages over Option A:**
- Messages survive DB being slow/unavailable
- Natural ordering guarantees from JetStream
- Consumer tracks read position automatically (no `last_read_at` needed)

**Additional complexity:**
- Need per-role durable consumers per job
- Consumer lifecycle management (create on agent launch, clean up on job completion)
- Dual-write to both DB (audit) and NATS (delivery)

### Option C: Long-Lived Agents with Event Loops (Not Recommended)

Agents become persistent processes with NATS subscriptions, reacting to messages as they arrive.

**Why not:**
- Fights the `claude -p` one-shot model
- Requires rearchitecting agent lifecycle
- Significantly more complex process management
- Overkill for the coordination patterns we need

## Recommendation

**Option A** gets 80% of the value for 20% of the work. The building blocks are already in place:
- `send_message` and `get_messages` exist
- The `messages` table exists
- The NATS subjects are defined
- The job engine already injects messages into agent prompts

The main gaps are:
1. Tool availability — expose messaging tools to all roles
2. Agent prompting — instruct agents to poll between subtasks
3. Read tracking — simple `last_read_at` timestamp per agent per job

Option B is the natural evolution if message volume or latency becomes an issue, but the DB-backed approach is simpler and sufficient for current agent coordination needs.

## Message Flow Examples

### Code Review Feedback Loop
```
1. Job Engine launches backend_engineer (attempt 1)
2. Backend engineer writes code, opens PR
3. Job Engine launches code_reviewer
4. Code reviewer finds issues:
   → send_message(to="backend_engineer", content="SQL injection in query builder")
   → report_review_complete(verdict="request_changes")
5. Arbiter transitions task back to in_progress
6. Job Engine relaunches backend_engineer (attempt 2)
   → messages injected into prompt context
7. Backend engineer sees feedback, fixes issues
```

### Spec Analyst → Engineers Coordination
```
1. Spec analyst refines spec, notices ambiguity in DB schema
   → send_message(to="database_engineer", content="Use UUID PKs, not auto-increment")
   → send_message(broadcast, content="Auth uses JWT, not sessions")
2. When engineers launch, they see these messages in context
```

### Future: Live Coordination Between Running Agents
```
1. Backend engineer is running, hits a question about API contract
   → send_message(to="frontend_engineer", content="Should /users return nested addresses?")
2. Frontend engineer (also running) calls check_messages()
   → sees the question
   → send_message(to="backend_engineer", content="Yes, nested. Max depth 2.")
3. Backend engineer calls check_messages()
   → sees the answer, continues implementation
```
