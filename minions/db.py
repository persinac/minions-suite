"""Database protocol and SQLite implementation for job orchestration."""

import json
import logging
from typing import List, Optional, Protocol, runtime_checkable

from .models import (
    Agent,
    AgentRole,
    Job,
    JobStatus,
    Message,
    Subtask,
    SubtaskStatus,
    Task,
    TaskStatus,
    _now,
)
from .state_transitions import (
    InvalidTransitionError,
    validate_job_transition,
    validate_subtask_transition,
    validate_task_preconditions,
    validate_task_transition,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AbstractDatabase(Protocol):
    """Database interface for job orchestration."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    # -- Agents --

    async def create_agent(self, agent: Agent) -> Agent: ...

    async def update_agent(self, agent_id: str, **kwargs) -> None: ...

    async def get_agents_for_job(self, job_id: str) -> List[Agent]: ...

    async def get_agent(self, agent_id: str) -> Optional[Agent]: ...

    # -- Stats --

    async def get_cost_summary(self, project: Optional[str] = None, days: int = 30) -> dict: ...

    # -- Jobs --

    async def create_job(self, spec: str, trello_card_id: Optional[str] = None) -> Job: ...

    async def create_review_job(self, project: str, mr_url: str, mr_id: str, model: Optional[str] = None) -> tuple[Job, Task]: ...

    async def get_job(self, job_id: str) -> Optional[Job]: ...

    async def get_all_jobs(self) -> List[Job]: ...

    async def get_active_jobs(self) -> List[Job]: ...

    async def get_job_by_card(self, card_id: str) -> Optional[Job]: ...

    async def update_job_spec(self, job_id: str, spec: str) -> None: ...

    async def update_job_status(self, job_id: str, status: JobStatus, error: Optional[str] = None) -> None: ...

    async def get_job_usage(self, job_id: str) -> dict: ...

    # -- Tasks --

    async def create_task(self, task: Task) -> Task: ...

    async def get_task(self, task_id: str) -> Optional[Task]: ...

    async def get_tasks(self, job_id: str) -> List[Task]: ...

    async def update_task(self, task_id: str, **kwargs) -> Optional[Task]: ...

    async def get_tasks_by_status(self, job_id: str, status: TaskStatus) -> List[Task]: ...

    # -- Subtasks --

    async def create_subtask(self, subtask: Subtask) -> Subtask: ...

    async def create_subtasks_batch(self, subtasks: list[Subtask]) -> list[Subtask]: ...

    async def get_subtask(self, subtask_id: str) -> Subtask | None: ...

    async def get_subtasks(self, task_id: str) -> list[Subtask]: ...

    async def update_subtask(self, subtask_id: str, **kwargs) -> Subtask | None: ...

    async def get_running_subtasks_all(self) -> list: ...

    # -- Messages --

    async def send_message(self, msg: Message) -> Message: ...

    async def get_messages(self, job_id: str, role: Optional[AgentRole] = None) -> List[Message]: ...

    # -- Events / Audit --

    async def record_event(self, job_id: Optional[str], event_type: str, source: Optional[str] = None, detail: Optional[str] = None) -> None: ...

    async def get_events(self, job_id: str) -> List[dict]: ...

    async def record_tool_call(
        self,
        tool_name: str,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
        job_id: Optional[str] = None,
    ) -> None: ...

    async def get_tool_calls(self, job_id: str) -> List[dict]: ...

    async def get_job_timeline(self, job_id: str) -> List[dict]: ...

    async def record_state_transition(
        self,
        entity_type: str,
        entity_id: str,
        from_status: str,
        to_status: str,
        approved: bool,
        rejection_reason: str | None = None,
        job_id: str | None = None,
    ) -> None: ...

    # -- Heartbeats --

    async def upsert_heartbeat(
        self,
        agent_id: str,
        agent_role: str,
        job_id: Optional[str] = None,
        task_id: Optional[str] = None,
        subtask_id: Optional[str] = None,
        status: str = "active",
    ) -> None: ...

    async def get_stale_heartbeats(self, threshold_seconds: int) -> List[dict]: ...

    async def has_recent_heartbeat(self, task_id: str, threshold_seconds: int) -> bool: ...

    async def delete_heartbeat(self, agent_id: str) -> None: ...

    # -- Recovery --

    async def get_running_agents(self) -> List[Agent]: ...

    async def get_agent_for_task(self, task_id: str) -> Optional[Agent]: ...

    async def clear_all_heartbeats(self) -> None: ...


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    review_id TEXT,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'starting',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    num_turns INTEGER DEFAULT 0,
    log_file TEXT,
    error TEXT,
    job_id TEXT,
    role TEXT,
    task_id TEXT,
    k8s_job_name TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'spec_received',
    job_type TEXT NOT NULL DEFAULT 'development',
    mr_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    trello_card_id TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    branch_name TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    review_status TEXT,
    deploy_status TEXT,
    revision_count INTEGER DEFAULT 0,
    attempt INTEGER DEFAULT 1,
    max_attempts INTEGER DEFAULT 3,
    error TEXT,
    mr_url TEXT,
    mr_id TEXT,
    verdict TEXT,
    comments_posted INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subtasks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    sequence_num INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id, sequence_num);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    from_role TEXT NOT NULL,
    to_role TEXT,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    tool_name TEXT NOT NULL,
    params TEXT,
    result TEXT,
    error TEXT,
    duration_ms REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_job ON tool_calls(job_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_name);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    event_type TEXT NOT NULL,
    source TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    task_id TEXT,
    subtask_id TEXT,
    agent_id TEXT,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    approved INTEGER NOT NULL,
    rejection_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_job ON state_transitions(job_id, created_at);

CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_tasks_mr ON tasks(mr_url);
"""


class SQLiteDatabase:
    """Async SQLite database for local development."""

    def __init__(self, db_path: str):
        import aiosqlite

        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        import aiosqlite

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("SQLite connected: %s", self.db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ===================================================================
    # Agents
    # ===================================================================

    async def create_agent(self, agent: Agent) -> Agent:
        await self._db.execute(
            """INSERT INTO agents (id, review_id, model, status, started_at, log_file,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
               cost_usd, num_turns, error, job_id, role, task_id, k8s_job_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent.id, agent.review_id, agent.model, agent.status, agent.started_at,
                agent.log_file, agent.input_tokens, agent.output_tokens,
                agent.cache_read_tokens, agent.cache_creation_tokens,
                agent.cost_usd, agent.num_turns, agent.error,
                agent.job_id, agent.role, agent.task_id, agent.k8s_job_name,
            ),
        )
        await self._db.commit()
        return agent

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [agent_id]
        await self._db.execute(f"UPDATE agents SET {sets} WHERE id = ?", values)
        await self._db.commit()

    async def get_agent(self, agent_id: str) -> Optional[Agent]:
        cursor = await self._db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_agent(row)

    async def get_agents_for_job(self, job_id: str) -> List[Agent]:
        cursor = await self._db.execute(
            "SELECT * FROM agents WHERE job_id = ? ORDER BY started_at",
            (job_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_agent(r) for r in rows]

    # ===================================================================
    # Stats
    # ===================================================================

    async def get_cost_summary(self, project: Optional[str] = None, days: int = 30) -> dict:
        base_query = """
            SELECT
                COUNT(DISTINCT j.id) as total_reviews,
                COALESCE(SUM(a.cost_usd), 0) as total_cost,
                COALESCE(SUM(a.input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(a.output_tokens), 0) as total_output_tokens,
                COALESCE(AVG(a.cost_usd), 0) as avg_cost_per_review
            FROM jobs j
            LEFT JOIN agents a ON a.job_id = j.id
            WHERE j.job_type = 'review'
            AND j.created_at >= datetime('now', ?)
        """
        params: list = [f"-{days} days"]
        if project:
            base_query += " AND EXISTS (SELECT 1 FROM tasks t WHERE t.job_id = j.id AND t.service = ?)"
            params.append(project)

        cursor = await self._db.execute(base_query, params)
        row = await cursor.fetchone()
        return {
            "total_reviews": row["total_reviews"],
            "total_cost_usd": round(row["total_cost"], 4),
            "total_input_tokens": row["total_input_tokens"],
            "total_output_tokens": row["total_output_tokens"],
            "avg_cost_per_review": round(row["avg_cost_per_review"], 4),
            "period_days": days,
        }

    # ===================================================================
    # Jobs
    # ===================================================================

    async def create_job(self, spec: str, trello_card_id: Optional[str] = None) -> Job:
        job = Job(spec=spec, trello_card_id=trello_card_id)
        await self._db.execute(
            "INSERT INTO jobs (id, spec, status, job_type, mr_url, created_at, updated_at, trello_card_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job.id, job.spec, job.status, job.job_type, job.mr_url, job.created_at, job.updated_at, job.trello_card_id),
        )
        await self._db.commit()
        logger.info("Created job %s", job.id)
        await self.record_event(job.id, "job_created", "db", f"status={job.status}")
        return job

    async def create_review_job(self, project: str, mr_url: str, mr_id: str, model: Optional[str] = None) -> tuple[Job, Task]:
        """Create a review-type job with a single CODE_REVIEWER task atomically."""
        from .models import AgentRole

        job = Job(spec=mr_url, status=JobStatus.TASKS_CREATED, job_type="review", mr_url=mr_url)
        await self._db.execute(
            "INSERT INTO jobs (id, spec, status, job_type, mr_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job.id, job.spec, job.status, job.job_type, job.mr_url, job.created_at, job.updated_at),
        )

        task = Task(
            job_id=job.id,
            title=f"Review {mr_url}",
            description=f"Code review for MR {mr_id}",
            service=project,
            agent_role=AgentRole.CODE_REVIEWER,
            mr_url=mr_url,
            mr_id=mr_id,
        )
        await self._db.execute(
            """INSERT INTO tasks (id, job_id, title, description, service, agent_role, status,
               branch_name, pr_number, pr_url, review_status, deploy_status, revision_count,
               attempt, max_attempts, error, mr_url, mr_id, verdict, comments_posted, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id, task.job_id, task.title, task.description, task.service,
                task.agent_role, task.status, task.branch_name, task.pr_number,
                task.pr_url, task.review_status, task.deploy_status,
                task.revision_count, task.attempt, task.max_attempts, task.error,
                task.mr_url, task.mr_id, task.verdict, task.comments_posted,
                task.created_at, task.updated_at,
            ),
        )
        await self._db.commit()
        logger.info("Created review job %s with task %s for %s", job.id, task.id, mr_url)
        await self.record_event(job.id, "job_created", "db", f"type=review mr_url={mr_url}")
        return job, task

    async def get_job(self, job_id: str) -> Optional[Job]:
        cursor = await self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_job(row)

    async def get_all_jobs(self) -> List[Job]:
        cursor = await self._db.execute("SELECT * FROM jobs ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows]

    async def get_active_jobs(self) -> List[Job]:
        terminal = (JobStatus.DONE, JobStatus.FAILED, JobStatus.NO_WORK_NEEDED)
        cursor = await self._db.execute("SELECT * FROM jobs WHERE status NOT IN (?, ?, ?)", terminal)
        rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows]

    async def get_job_by_card(self, card_id: str) -> Optional[Job]:
        cursor = await self._db.execute("SELECT * FROM jobs WHERE trello_card_id = ?", (card_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_job(row)

    async def update_job_spec(self, job_id: str, spec: str) -> None:
        await self._db.execute(
            "UPDATE jobs SET spec = ?, updated_at = ? WHERE id = ?",
            (spec, _now(), job_id),
        )
        await self._db.commit()
        logger.info("Updated spec for job %s (%d chars)", job_id, len(spec))
        await self.record_event(job_id, "spec_refined", "db", f"spec_length={len(spec)}")

    async def update_job_status(self, job_id: str, status: JobStatus, error: Optional[str] = None) -> None:
        job = await self.get_job(job_id)
        if job:
            try:
                validate_job_transition(job_id, job.status, status)
                await self.record_state_transition("job", job_id, job.status, status, True, job_id=job_id)
            except InvalidTransitionError as e:
                await self.record_state_transition("job", job_id, job.status, status, False, str(e), job_id=job_id)
                raise

        await self._db.execute(
            "UPDATE jobs SET status = ?, updated_at = ?, error = COALESCE(?, error) WHERE id = ?",
            (status, _now(), error, job_id),
        )
        await self._db.commit()
        logger.info("Job %s -> %s", job_id, status)
        detail = f"status={status}"
        if error:
            detail += f" error={error[:200]}"
        await self.record_event(job_id, "job_status_changed", "db", detail)

    async def get_job_usage(self, job_id: str) -> dict:
        cursor = await self._db.execute(
            """SELECT
                COUNT(*) as agent_count,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) as total_cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) as total_cache_creation_tokens,
                COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
                COALESCE(SUM(num_turns), 0) as total_turns
            FROM agents WHERE job_id = ?""",
            (job_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}

    # ===================================================================
    # Tasks
    # ===================================================================

    async def create_task(self, task: Task) -> Task:
        await self._db.execute(
            """INSERT INTO tasks (id, job_id, title, description, service, agent_role, status,
               branch_name, pr_number, pr_url, review_status, deploy_status, revision_count,
               attempt, max_attempts, error, mr_url, mr_id, verdict, comments_posted, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id, task.job_id, task.title, task.description, task.service,
                task.agent_role, task.status, task.branch_name, task.pr_number,
                task.pr_url, task.review_status, task.deploy_status,
                task.revision_count, task.attempt, task.max_attempts, task.error,
                task.mr_url, task.mr_id, task.verdict, task.comments_posted,
                task.created_at, task.updated_at,
            ),
        )
        await self._db.commit()
        logger.info("Created task %s: %s", task.id, task.title)
        await self.record_event(task.job_id, "task_created", "db", f"task={task.id} title={task.title} service={task.service}")
        return task

    async def get_task(self, task_id: str) -> Optional[Task]:
        cursor = await self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_task(row)

    async def get_tasks(self, job_id: str) -> List[Task]:
        cursor = await self._db.execute("SELECT * FROM tasks WHERE job_id = ? ORDER BY created_at", (job_id,))
        rows = await cursor.fetchall()
        return [_row_to_task(r) for r in rows]

    async def update_task(self, task_id: str, **kwargs) -> Optional[Task]:
        requesting_role = kwargs.pop("agent_role", None)

        if "status" in kwargs:
            current = await self.get_task(task_id)
            if current:
                job_id = current.job_id
                role_for_validation = requesting_role or current.agent_role
                try:
                    validate_task_transition(task_id, current.status, kwargs["status"], agent_role=role_for_validation)
                    merged = current.model_dump()
                    merged.update(kwargs)
                    validate_task_preconditions(task_id, kwargs["status"], merged, agent_role=current.agent_role)
                    await self.record_state_transition("task", task_id, current.status, kwargs["status"], True, job_id=job_id)
                except InvalidTransitionError as e:
                    await self.record_state_transition("task", task_id, current.status, kwargs["status"], False, str(e), job_id=job_id)
                    raise

        kwargs["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [task_id]
        await self._db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
        await self._db.commit()
        task = await self.get_task(task_id)
        if task and "status" in kwargs:
            await self.record_event(task.job_id, "task_status_changed", "db", f"task={task_id} status={kwargs['status']}")
        return task

    async def get_tasks_by_status(self, job_id: str, status: TaskStatus) -> List[Task]:
        cursor = await self._db.execute("SELECT * FROM tasks WHERE job_id = ? AND status = ?", (job_id, status))
        rows = await cursor.fetchall()
        return [_row_to_task(r) for r in rows]

    # ===================================================================
    # Subtasks
    # ===================================================================

    async def create_subtask(self, subtask: Subtask) -> Subtask:
        await self._db.execute(
            """INSERT INTO subtasks (id, task_id, sequence_num, description, status, started_at, completed_at, result, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                subtask.id, subtask.task_id, subtask.sequence_num, subtask.description,
                subtask.status, subtask.started_at, subtask.completed_at,
                json.dumps(subtask.result) if subtask.result else None,
                subtask.error, subtask.created_at,
            ),
        )
        await self._db.commit()
        logger.info("Created subtask %s for task %s", subtask.id, subtask.task_id)
        task = await self.get_task(subtask.task_id)
        if task:
            await self.record_event(task.job_id, "subtask_created", "db", f"subtask={subtask.id} task={subtask.task_id} seq={subtask.sequence_num}")
        return subtask

    async def create_subtasks_batch(self, subtasks: list[Subtask]) -> list[Subtask]:
        results = []
        for s in subtasks:
            results.append(await self.create_subtask(s))
        return results

    async def get_subtask(self, subtask_id: str) -> Optional[Subtask]:
        cursor = await self._db.execute("SELECT * FROM subtasks WHERE id = ?", (subtask_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_subtask(row)

    async def get_subtasks(self, task_id: str) -> list[Subtask]:
        cursor = await self._db.execute("SELECT * FROM subtasks WHERE task_id = ? ORDER BY sequence_num", (task_id,))
        rows = await cursor.fetchall()
        return [_row_to_subtask(r) for r in rows]

    async def update_subtask(self, subtask_id: str, **kwargs) -> Optional[Subtask]:
        if "status" in kwargs:
            current = await self.get_subtask(subtask_id)
            if current:
                task = await self.get_task(current.task_id)
                job_id = task.job_id if task else None
                try:
                    validate_subtask_transition(subtask_id, current.status, kwargs["status"])
                    await self.record_state_transition("subtask", subtask_id, current.status, kwargs["status"], True, job_id=job_id)
                except InvalidTransitionError as e:
                    await self.record_state_transition("subtask", subtask_id, current.status, kwargs["status"], False, str(e), job_id=job_id)
                    raise

        if "result" in kwargs and kwargs["result"] is not None:
            kwargs["result"] = json.dumps(kwargs["result"])

        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [subtask_id]
        await self._db.execute(f"UPDATE subtasks SET {sets} WHERE id = ?", vals)
        await self._db.commit()
        subtask = await self.get_subtask(subtask_id)
        if subtask and "status" in kwargs:
            task = await self.get_task(subtask.task_id)
            if task:
                await self.record_event(task.job_id, "subtask_status_changed", "db", f"subtask={subtask_id} status={kwargs['status']}")
        return subtask

    async def get_running_subtasks_all(self) -> list:
        cursor = await self._db.execute("SELECT * FROM subtasks WHERE status = 'running'")
        rows = await cursor.fetchall()
        return [_row_to_subtask(r) for r in rows]

    # ===================================================================
    # Messages
    # ===================================================================

    async def send_message(self, msg: Message) -> Message:
        await self._db.execute(
            "INSERT INTO messages (id, job_id, from_role, to_role, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (msg.id, msg.job_id, msg.from_role, msg.to_role, msg.content, msg.created_at),
        )
        await self._db.commit()
        return msg

    async def get_messages(self, job_id: str, role: Optional[AgentRole] = None) -> List[Message]:
        if role:
            cursor = await self._db.execute(
                "SELECT * FROM messages WHERE job_id = ? AND (to_role = ? OR to_role IS NULL) ORDER BY created_at",
                (job_id, role),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM messages WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            )
        rows = await cursor.fetchall()
        return [_row_to_message(r) for r in rows]

    # ===================================================================
    # Events / Audit
    # ===================================================================

    async def record_event(self, job_id: Optional[str], event_type: str, source: Optional[str] = None, detail: Optional[str] = None) -> None:
        try:
            await self._db.execute(
                "INSERT INTO events (job_id, event_type, source, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, event_type, source, detail, _now()),
            )
            await self._db.commit()
        except Exception:
            logger.debug("Failed to record event %s", event_type, exc_info=True)

    async def get_events(self, job_id: str) -> List[dict]:
        cursor = await self._db.execute("SELECT * FROM events WHERE job_id = ? ORDER BY created_at", (job_id,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def record_tool_call(
        self,
        tool_name: str,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
        job_id: Optional[str] = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO tool_calls (job_id, tool_name, params, result, error, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, tool_name, json.dumps(params) if params else None, result, error, duration_ms, _now()),
        )
        await self._db.commit()

    async def get_tool_calls(self, job_id: str) -> List[dict]:
        cursor = await self._db.execute("SELECT * FROM tool_calls WHERE job_id = ? ORDER BY created_at", (job_id,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_job_timeline(self, job_id: str) -> List[dict]:
        events = await self.get_events(job_id)
        tool_calls = await self.get_tool_calls(job_id)

        timeline = []
        for e in events:
            timeline.append(
                {"type": "event", "timestamp": e["created_at"], "event_type": e["event_type"], "source": e.get("source"), "detail": e.get("detail")}
            )
        for tc in tool_calls:
            detail = f"params={tc['params']}" if tc["params"] else ""
            if tc.get("error"):
                detail += f" error={tc['error']}"
            if tc.get("duration_ms") is not None:
                detail += f" duration={tc['duration_ms']:.0f}ms"
            timeline.append(
                {
                    "type": "tool_call",
                    "timestamp": tc["created_at"],
                    "event_type": f"tool:{tc['tool_name']}",
                    "source": "mcp",
                    "detail": detail.strip(),
                }
            )

        timeline.sort(key=lambda x: x["timestamp"])
        return timeline

    async def record_state_transition(
        self,
        entity_type: str,
        entity_id: str,
        from_status: str,
        to_status: str,
        approved: bool,
        rejection_reason: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> None:
        try:
            task_id = entity_id if entity_type == "task" else None
            subtask_id = entity_id if entity_type == "subtask" else None
            effective_job_id = entity_id if entity_type == "job" else job_id

            await self._db.execute(
                """INSERT INTO state_transitions (job_id, task_id, subtask_id, from_status, to_status, approved, rejection_reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (effective_job_id, task_id, subtask_id, from_status, to_status, 1 if approved else 0, rejection_reason, _now()),
            )
            await self._db.commit()
        except Exception:
            logger.debug("Failed to record state_transition %s %s->%s", entity_type, from_status, to_status, exc_info=True)

    # ===================================================================
    # Heartbeats (no-op for SQLite — table only lives in Postgres)
    # ===================================================================

    async def upsert_heartbeat(self, agent_id: str, agent_role: str, job_id: Optional[str] = None,
                               task_id: Optional[str] = None, subtask_id: Optional[str] = None, status: str = "active") -> None:
        pass

    async def get_stale_heartbeats(self, threshold_seconds: int) -> List[dict]:
        return []

    async def has_recent_heartbeat(self, task_id: str, threshold_seconds: int) -> bool:
        return False

    async def delete_heartbeat(self, agent_id: str) -> None:
        pass

    async def clear_all_heartbeats(self) -> None:
        pass

    # ===================================================================
    # Recovery
    # ===================================================================

    async def get_running_agents(self) -> List[Agent]:
        cursor = await self._db.execute(
            "SELECT * FROM agents WHERE status IN ('starting', 'running') ORDER BY started_at"
        )
        rows = await cursor.fetchall()
        return [_row_to_agent(r) for r in rows]

    async def get_agent_for_task(self, task_id: str) -> Optional[Agent]:
        cursor = await self._db.execute(
            "SELECT * FROM agents WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_agent(row)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_agent(row) -> Agent:
    return Agent(
        id=row["id"],
        review_id=row["review_id"],
        model=row["model"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        input_tokens=row["input_tokens"] or 0,
        output_tokens=row["output_tokens"] or 0,
        cache_read_tokens=row["cache_read_tokens"] or 0,
        cache_creation_tokens=row["cache_creation_tokens"] or 0,
        cost_usd=row["cost_usd"] or 0.0,
        num_turns=row["num_turns"] or 0,
        log_file=row["log_file"],
        error=row["error"],
        job_id=row["job_id"],
        role=row["role"],
        task_id=row["task_id"],
        k8s_job_name=row["k8s_job_name"],
    )


def _row_to_job(row) -> Job:
    return Job(
        id=row["id"],
        spec=row["spec"],
        status=JobStatus(row["status"]),
        job_type=row["job_type"] if row["job_type"] else "development",
        mr_url=row["mr_url"],
        error=row["error"],
        trello_card_id=row["trello_card_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_task(row) -> Task:
    return Task(
        id=row["id"],
        job_id=row["job_id"],
        title=row["title"],
        description=row["description"],
        service=row["service"],
        agent_role=row["agent_role"],
        status=TaskStatus(row["status"]),
        branch_name=row["branch_name"],
        pr_number=row["pr_number"],
        pr_url=row["pr_url"],
        review_status=row["review_status"],
        deploy_status=row["deploy_status"],
        revision_count=row["revision_count"] or 0,
        attempt=row["attempt"] or 1,
        max_attempts=row["max_attempts"] or 3,
        error=row["error"],
        mr_url=row["mr_url"],
        mr_id=row["mr_id"],
        verdict=row["verdict"],
        comments_posted=row["comments_posted"] or 0,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_subtask(row) -> Subtask:
    d = dict(row)
    if d.get("result") and isinstance(d["result"], str):
        d["result"] = json.loads(d["result"])
    return Subtask(
        id=d["id"],
        task_id=d["task_id"],
        sequence_num=d["sequence_num"],
        description=d["description"],
        status=SubtaskStatus(d["status"]),
        started_at=d.get("started_at"),
        completed_at=d.get("completed_at"),
        result=d.get("result"),
        error=d.get("error"),
        created_at=d["created_at"],
    )


def _row_to_message(row) -> Message:
    return Message(
        id=row["id"],
        job_id=row["job_id"],
        from_role=row["from_role"],
        to_role=row["to_role"],
        content=row["content"],
        created_at=row["created_at"],
    )
