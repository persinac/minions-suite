# Plan: Restructure `minions/` package into namespaced sub-packages

**Status:** Complete (all 8 phases done)
**Prerequisite:** Unit tests must exist before any moves begin — ✅ 372 tests

## Problem

30 `.py` files in a flat `minions/` namespace. No hierarchy signals what depends on what. `job_engine.py` alone is 1290 lines handling three distinct job lifecycles. A new contributor has to read every file to understand boundaries.

## Current layout

```
minions/                              # 30 files, 1 sub-package
├── __init__.py, __main__.py, cli.py
├── config.py, project_registry.py
├── models.py, state_transitions.py, timeout_config.py
├── db.py (916L), db_postgres.py (774L)
├── job_engine.py (1290L)
├── agent.py, agent_dispatch.py
├── mcp_tool_executor.py, tools.py
├── server.py, tool_audit_middleware.py
├── arbiter.py, anomaly_rules.py
├── prompt.py, git_provider.py
├── preflight.py, dashboard.py
├── artifact_uploader.py, k8s_launcher.py
├── trello_poller.py, gitlab_issues_poller.py
├── renovate_engine.py, renovate_classifier.py
└── connectors/
```

## Target layout

```
minions/
├── __init__.py
├── __main__.py
├── cli.py                              # Entry point
├── config.py                           # App config
├── project_registry.py                 # Multi-project YAML config
│
├── core/                               # Domain model — zero external deps
│   ├── __init__.py                     # Re-exports: models, enums, state_transitions
│   ├── models.py                       # Pydantic models, enums, _now()
│   ├── state_transitions.py            # Transition validation, InvalidTransitionError
│   └── timeout_config.py              # RoleTimeoutConfig, TimeoutConfig
│
├── db/                                 # Persistence layer
│   ├── __init__.py                     # Re-exports: AbstractDatabase, SQLiteDatabase
│   ├── abstract.py                     # AbstractDatabase protocol
│   ├── sqlite.py                       # SQLite implementation
│   └── postgres.py                     # PostgreSQL implementation
│
├── engine/                             # Job orchestration
│   ├── __init__.py                     # Re-exports: JobEngine
│   ├── job_engine.py                   # Core poll loop, state dispatch, helpers
│   ├── review.py                       # _launch_review_tasks, _run_review_in_process, _check_review_tasks
│   ├── dev.py                          # _launch_spec_analyst, _launch_arbiter, _launch_engineers, _run_engineer
│   ├── deploy.py                       # _launch_deploy_monitor, _check_deployed
│   └── arbiter.py                      # Arbiter service + anomaly_rules
│
├── agents/                             # Agent execution
│   ├── __init__.py                     # Re-exports: run_agent
│   ├── runner.py                       # run_agent(), _agent_loop_generic()
│   ├── dispatch.py                     # AgentWorkItem, serialization (K8s handoff)
│   ├── prompt.py                       # build_prompt(), build_agent_prompt(), profile inference
│   └── tools/                          # Tool schemas + executors
│       ├── __init__.py                 # Re-exports: get_tools_for_role, ToolExecutor, McpToolExecutor
│       ├── definitions.py              # OpenAI function schemas (REVIEW_, ENGINEER_, DEPLOY_, SPEC_)
│       ├── review_executor.py          # ToolExecutor (git provider tool dispatch for reviewers)
│       └── mcp_executor.py             # McpToolExecutor (MCP server routing for orchestration agents)
│
├── providers/                          # External service integrations
│   ├── __init__.py
│   ├── git.py                          # GitProviderProtocol, GitLabProvider, GitHubProvider, create_provider()
│   ├── trello.py                       # TrelloPoller
│   ├── gitlab_issues.py                # GitLabIssuesPoller
│   └── k8s.py                          # K8sJobLauncher
│
├── connectors/                         # NATS message bus (unchanged)
│   ├── __init__.py
│   ├── nats_client.py
│   ├── nats_config.py
│   ├── nats_init.py
│   ├── nats_publisher.py
│   └── nats_subscriber.py
│
├── server/                             # MCP server
│   ├── __init__.py                     # Re-exports: create_server, set_nats_client
│   ├── mcp.py                          # FastMCP tool registrations (current server.py)
│   └── middleware.py                   # ToolAuditMiddleware
│
├── renovate/                           # Renovate auto-merge (self-contained feature)
│   ├── __init__.py
│   ├── classifier.py                   # Risk classification, version parsing
│   └── engine.py                       # RenovateEngine polling loop
│
├── dashboard.py                        # Web UI (single file, stays at root)
├── preflight.py                        # Health checks (single file, stays at root)
└── artifact_uploader.py                # S3 uploads (single file, stays at root)
```

## Dependency graph (top-down)

```
cli.py
  ├── config.py
  ├── db/
  ├── server/
  ├── engine/
  │     ├── core/
  │     ├── db/
  │     ├── agents/
  │     │     ├── core/
  │     │     ├── agents/tools/
  │     │     └── providers/git
  │     ├── providers/
  │     └── connectors/
  ├── providers/
  └── connectors/

core/ ← imported by everything, imports nothing from minions
db/   ← imports core/
```

## Phase 0: Unit tests (MUST complete before any moves)

No automated tests exist. We need baseline coverage on the modules that will be split/moved so that import rewiring is verified mechanically.

### Test structure

```
tests/
├── conftest.py                     # Shared fixtures: mock db, mock config, mock MCP server
├── core/
│   ├── test_models.py              # Model construction, enum values, _now()
│   └── test_state_transitions.py   # Valid/invalid transitions, precondition checks
├── db/
│   └── test_sqlite.py              # CRUD ops against in-memory SQLite
├── agents/
│   ├── test_runner.py              # run_agent with mock LiteLLM (tool loop basics)
│   ├── test_prompt.py              # build_prompt, build_agent_prompt, profile inference
│   └── tools/
│       ├── test_definitions.py     # get_tools_for_role returns correct schemas per role
│       ├── test_review_executor.py # ToolExecutor dispatches to mock git provider
│       └── test_mcp_executor.py    # McpToolExecutor routes to MCP server, injects context
├── engine/
│   ├── test_job_engine.py          # Poll loop starts/stops, dispatches by job status
│   ├── test_review.py              # Review lifecycle handlers
│   └── test_dev.py                 # Dev lifecycle handlers (spec analyst, arbiter, engineers)
├── providers/
│   └── test_git.py                 # GitLab/GitHub provider with mocked HTTP/CLI
├── server/
│   ├── test_mcp.py                 # MCP tool registrations return expected JSON
│   └── test_middleware.py          # ToolAuditMiddleware records calls, handles errors
└── renovate/
    └── test_classifier.py          # Risk classification, version parsing, auto-merge decisions
```

### Test priorities (ordered by risk during restructure)

1. **`test_state_transitions.py`** — pure logic, no deps, easy to write, validates core invariant
2. **`test_models.py`** — model construction, enum membership
3. **`test_sqlite.py`** — CRUD against in-memory DB, validates AbstractDatabase contract
4. **`test_definitions.py`** — `get_tools_for_role` returns correct tool sets per role
5. **`test_mcp_executor.py`** — context injection, MCP routing, middleware chain
6. **`test_prompt.py`** — prompt assembly, mixin loading, auto-inference
7. **`test_mcp.py`** — MCP server tools return valid JSON, accept expected params
8. **`test_classifier.py`** — renovate risk classification (pure logic)
9. **`test_review_executor.py`** — ToolExecutor dispatches to mock provider
10. **`test_middleware.py`** — audit middleware records calls, handles exceptions
11. **`test_job_engine.py`** — poll loop, state dispatch (heavier, needs more mocking)
12. **`test_git.py`** — provider abstraction with mocked HTTP

### Test tooling

- **pytest** + **pytest-asyncio** for async tests
- **No external services** — all tests use in-memory SQLite, mock LiteLLM, mock git providers
- Add to `pyproject.toml`:
  ```toml
  [project.optional-dependencies]
  test = ["pytest>=8.0", "pytest-asyncio>=0.24"]

  [tool.pytest.ini_options]
  asyncio_mode = "auto"
  testpaths = ["tests"]
  ```
- Run: `uv run pytest` or `task test`
- Add `task test` to `Taskfile.yml`

## Phase 1: Create `core/` sub-package

Lowest risk — these files are imported everywhere but import nothing from `minions`.

### Steps

1. Create `minions/core/__init__.py` with re-exports
2. Move `models.py` → `core/models.py`
3. Move `state_transitions.py` → `core/state_transitions.py`
4. Move `timeout_config.py` → `core/timeout_config.py`
5. Update all imports project-wide (`from .models` → `from .core.models`, etc.)
6. Add re-exports in `core/__init__.py` so `from .core import Job, TaskStatus` works
7. Run `uv run pytest` — all tests pass
8. Run `uv run ruff check` — clean lint
9. Commit: `refactor: extract core/ sub-package (models, state_transitions, timeout_config)`

## Phase 2: Create `db/` sub-package

### Steps

1. Create `minions/db/__init__.py`
2. Split `db.py` → `db/abstract.py` (protocol + base) + `db/sqlite.py` (SQLiteDatabase)
3. Move `db_postgres.py` → `db/postgres.py`
4. Re-export from `db/__init__.py`: `AbstractDatabase`, `SQLiteDatabase`
5. Update all imports
6. Run tests + lint
7. Commit: `refactor: extract db/ sub-package (abstract, sqlite, postgres)`

## Phase 3: Create `agents/` and `agents/tools/` sub-packages

### Steps

1. Create `minions/agents/__init__.py`, `minions/agents/tools/__init__.py`
2. Move `agent.py` → `agents/runner.py`
3. Move `agent_dispatch.py` → `agents/dispatch.py`
4. Move `prompt.py` → `agents/prompt.py`
5. Split `tools.py`:
   - Tool schemas (definitions) → `agents/tools/definitions.py`
   - `ToolExecutor` class → `agents/tools/review_executor.py`
   - `get_tools_for_role()` → `agents/tools/definitions.py`
6. Move `mcp_tool_executor.py` → `agents/tools/mcp_executor.py`
7. Re-exports in `__init__.py` files
8. Update all imports
9. Run tests + lint
10. Commit: `refactor: extract agents/ sub-package (runner, dispatch, prompt, tools)`

## Phase 4: Create `providers/` sub-package

### Steps

1. Create `minions/providers/__init__.py`
2. Move `git_provider.py` → `providers/git.py`
3. Move `trello_poller.py` → `providers/trello.py`
4. Move `gitlab_issues_poller.py` → `providers/gitlab_issues.py`
5. Move `k8s_launcher.py` → `providers/k8s.py`
6. Update all imports
7. Run tests + lint
8. Commit: `refactor: extract providers/ sub-package (git, trello, gitlab_issues, k8s)`

## Phase 5: Create `server/` sub-package

### Steps

1. Create `minions/server/__init__.py`
2. Move `server.py` → `server/mcp.py`
3. Move `tool_audit_middleware.py` → `server/middleware.py`
4. Re-export `create_server`, `set_nats_client` from `server/__init__.py`
5. Update all imports
6. Run tests + lint
7. Commit: `refactor: extract server/ sub-package (mcp, middleware)`

## Phase 6: Create `renovate/` sub-package

### Steps

1. Create `minions/renovate/__init__.py`
2. Move `renovate_classifier.py` → `renovate/classifier.py`
3. Move `renovate_engine.py` → `renovate/engine.py`
4. Update all imports
5. Run tests + lint
6. Commit: `refactor: extract renovate/ sub-package (classifier, engine)`

## Phase 7: Split `job_engine.py` into `engine/`

This is the riskiest phase — 1290 lines split into 4-5 files.

### Steps

1. Create `minions/engine/__init__.py`
2. Extract review handlers → `engine/review.py`:
   - `_launch_review_tasks()`
   - `_run_review_in_process()`
   - `_check_review_tasks()`
3. Extract dev handlers → `engine/dev.py`:
   - `_launch_spec_analyst()`
   - `_launch_arbiter()`
   - `_launch_engineers()`
   - `_run_engineer()`
   - `_manage_dev_tasks()`
   - `_run_task_review()`
   - `_get_review_feedback()`
   - `_build_checkpoint_summary()`
4. Extract deploy handlers → `engine/deploy.py`:
   - `_launch_deploy_monitor()`
   - `_check_deployed()`
5. Move `arbiter.py` + `anomaly_rules.py` → `engine/arbiter.py` (merge or keep separate)
6. Keep in `engine/job_engine.py`: core class, `__init__`, `start/stop`, `_poll`, `_process_job`, `_spawn`, helpers
7. Handler modules receive `self` (the JobEngine instance) as first arg, or use mixin pattern
8. Update all imports
9. Run tests + lint
10. Commit: `refactor: split job_engine into engine/ sub-package (review, dev, deploy)`

### Design decision: handler pattern

Two options for splitting methods out of the 1290-line class:

**Option A: Standalone functions** — handlers become module-level async functions that receive the engine as a parameter:

```python
# engine/review.py
async def launch_review_tasks(engine: "JobEngine", job: Job):
    ...
```

**Option B: Mixin classes** — JobEngine inherits from handler mixins:

```python
# engine/review.py
class ReviewHandlerMixin:
    async def _launch_review_tasks(self, job: Job):
        ...

# engine/job_engine.py
class JobEngine(ReviewHandlerMixin, DevHandlerMixin, DeployHandlerMixin):
    ...
```

**Recommendation: Option A (standalone functions).** Mixins create implicit coupling and make the class hierarchy harder to follow. Standalone functions are explicit about their dependencies and easier to test in isolation. The engine dispatch in `_process_job` would call `await review.launch_review_tasks(self, job)`.

## Phase 8: Update CLAUDE.md and pyproject.toml

1. Update `CLAUDE.md` key modules section to reflect new paths
2. Update `pyproject.toml` entry point if needed
3. Update Dockerfile if any paths changed
4. Commit: `docs: update CLAUDE.md and config for new package structure`

## Execution rules

- **One phase per commit** — each phase is a self-contained, working state
- **Tests run between every phase** — `uv run pytest && uv run ruff check`
- **Re-exports preserve old import paths temporarily** — sub-package `__init__.py` files re-export key symbols so we don't break everything at once. Deprecation warnings can be added later.
- **Phase 0 (tests) blocks everything else** — no file moves until baseline tests exist and pass
- **Each phase merges to main** — no long-lived branch; each commit is independently deployable

## Files that stay at root level

These are standalone utilities that don't form natural groups:

| File | Reason |
|------|--------|
| `cli.py` | Entry point, imports from sub-packages |
| `config.py` | App-wide config, imported by everything |
| `project_registry.py` | YAML config loader, used across packages |
| `dashboard.py` | Self-contained web UI, single file |
| `preflight.py` | Health checks, single file |
| `artifact_uploader.py` | S3 uploads, single file |
