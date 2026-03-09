# K8s Execution Engine — Order of Operations

## 1. Engine Startup (`engine/job_engine.py:start`)

1. Set `_running = True`
2. If K8s dispatch is enabled (`config.k8s_dispatch` + launcher present):
   - Subscribe to NATS subject `agents.results.>` for result messages
   - Spawn a background `_k8s_job_watcher` polling loop (fallback)
3. Run `_startup_cleanup()` — recover orphaned agents from a prior crash
4. Enter the main `_poll()` loop

## 2. Startup Recovery (`engine/job_engine.py:_startup_cleanup`)

For each agent still marked "running" in the DB:

- **In-process agent** (no `k8s_job_name`) — mark failed as "orphaned by restart"
- **K8s agent** — query K8s API for actual Job status:
  - `succeeded`/`failed` — synthesize an `AgentResultMessage`, process it
  - `unknown` (Job gone) — mark agent failed
  - `running`/`pending` — leave alone, the watcher will handle it
- If the orphaned agent had a task with retries remaining, reset the task to `PENDING` with `attempt + 1`

## 3. Dispatch (`engine/job_engine.py:_dispatch_k8s`)

When the poll loop decides an agent needs to launch:

1. Resolve timeout from `TimeoutConfig` for the role
2. Get tool schemas via `get_tools_for_role(role)`
3. Build an `AgentWorkItem` (job/agent IDs, prompt, tools, MCP URL, model, timeout, clone URL)
4. Call `K8sJobLauncher.launch_agent()`

## 4. K8s Job Creation (`providers/k8s.py:launch_agent`)

1. Load K8s config (in-cluster or kubeconfig, once)
2. Generate a K8s-safe Job name: `agent-{role_short}-{job_id[:8]}-{agent_id[:8]}`
3. Serialize the `AgentWorkItem` to JSON
4. **Create a ConfigMap** containing `work-item.json`
5. **Create a K8s Job** with:
   - `backoff_limit=0` (no retries)
   - `ttl_seconds_after_finished` (auto-cleanup)
   - `active_deadline_seconds = timeout + 60`
   - **Init container** (`alpine/git`) — shallow-clones the repo to `/workspace/repo`
   - **Main container** (`agent_image`) — runs `python -m minions agent-worker`:
     - Reads work item from `/config/work-item.json` (ConfigMap volume)
     - Works in `/workspace/repo` (shared emptyDir volume)
     - Secrets injected via `envFrom` (K8s Secret)
   - Role-based resource limits (engineers get 2 CPU / 4Gi; lightweight roles get 1 CPU / 2Gi)
6. Update DB: agent status -> `running`, store `k8s_job_name`

## 5. Result Collection (two paths)

**Path A — NATS (primary):**
The agent-worker process publishes an `AgentResultMessage` to NATS when done. The engine's subscription callback `_on_nats_result` deserializes it and calls `_handle_agent_result`.

**Path B — K8s Job Watcher (fallback):**
Every 30 seconds, polls `list_agent_jobs()`. For any Job in `succeeded`/`failed` whose agent isn't already terminal in the DB:
- Fetch pod logs (last 50 lines)
- Synthesize an `AgentResultMessage` with zeroed-out token counts
- Call `_handle_agent_result`

## 6. Result Processing (`engine/job_engine.py:_handle_agent_result`)

1. Look up agent and job in DB; skip if already terminal or missing
2. On **success**: update agent -> `completed`, record event, publish NATS status, post Trello comment
3. On **failure**: update agent -> `failed` with error text, then:
   - If role is `SPEC_ANALYST` or `ARBITER` -> fail the entire Job
   - Otherwise -> fail the agent's task (allowing retry logic upstream)

## 7. Cleanup

- **TTL-based**: K8s auto-deletes finished Jobs after `job_ttl` seconds
- **`cleanup_old_jobs`**: runs on engine shutdown, deletes finished Jobs + ConfigMaps older than 2 hours
- **`delete_job`**: on-demand deletion of a specific Job + its ConfigMap

## 8. Engine Shutdown (`engine/job_engine.py:stop`)

1. Set `_running = False`
2. Mark in-process agents as failed (K8s agents are left alone — they run independently)
3. Cancel all background tasks
4. K8s watcher loop exits and runs `cleanup_old_jobs()` on its way out
