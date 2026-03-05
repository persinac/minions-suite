"""PostgreSQL implementation of the job orchestration database.

Uses psycopg3 with an async connection pool. All tables live under
``minions.*`` schema.
"""

import json
import logging
from datetime import datetime
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from ..core.models import (
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
from ..core.state_transitions import (
    InvalidTransitionError,
    validate_job_transition,
    validate_subtask_transition,
    validate_task_preconditions,
    validate_task_transition,
)

logger = logging.getLogger(__name__)

JOB_SCHEMA = "minions"


def _ts(value) -> Optional[str]:
    """Convert a Postgres TIMESTAMPTZ to an ISO string, or pass through if already a string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class PostgresDatabase:
    """Async PostgreSQL database for production use."""

    def __init__(self, postgres_url: str, pool_min: int = 2, pool_max: int = 10):
        self._url = postgres_url
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: Optional[AsyncConnectionPool] = None

    async def connect(self) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=self._url,
            min_size=self._pool_min,
            max_size=self._pool_max,
            kwargs={"row_factory": dict_row},
        )
        await self._pool.open()
        logger.info("PostgresDatabase pool opened (min=%d, max=%d)", self._pool_min, self._pool_max)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PostgresDatabase pool closed")

    # ===================================================================
    # Agents
    # ===================================================================

    async def create_agent(self, agent: Agent) -> Agent:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.agents
                    (id, job_id, role, task_id, status, started_at, finished_at,
                     error, log_file, input_tokens, output_tokens, cache_read_tokens,
                     cache_creation_tokens, cost_usd, num_turns, model, k8s_job_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    agent.id, agent.job_id, agent.role, agent.task_id,
                    agent.status, agent.started_at, agent.finished_at,
                    agent.error, agent.log_file, agent.input_tokens, agent.output_tokens,
                    agent.cache_read_tokens, agent.cache_creation_tokens,
                    agent.cost_usd, agent.num_turns, agent.model, agent.k8s_job_name,
                ),
            )
        return agent

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [agent_id]
        async with self._pool.connection() as conn:
            await conn.execute(f"UPDATE {JOB_SCHEMA}.agents SET {sets} WHERE id = %s", values)

    async def get_agent(self, agent_id: str) -> Optional[Agent]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.agents WHERE id = %s", (agent_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return _dict_to_job_agent(row)

    async def get_agents_for_job(self, job_id: str) -> List[Agent]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {JOB_SCHEMA}.agents WHERE job_id = %s ORDER BY started_at",
                (job_id,),
            )
            rows = await cur.fetchall()
            return [_dict_to_job_agent(r) for r in rows]

    # ===================================================================
    # Stats
    # ===================================================================

    async def get_cost_summary(self, project: Optional[str] = None, days: int = 30) -> dict:
        query = f"""
            SELECT
                COUNT(DISTINCT j.id) as total_reviews,
                COALESCE(SUM(a.cost_usd), 0) as total_cost,
                COALESCE(SUM(a.input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(a.output_tokens), 0) as total_output_tokens,
                COALESCE(AVG(a.cost_usd), 0) as avg_cost_per_review
            FROM {JOB_SCHEMA}.jobs j
            LEFT JOIN {JOB_SCHEMA}.agents a ON a.job_id = j.id
            WHERE j.job_type = 'review'
            AND j.created_at >= NOW() - INTERVAL '%s days'
        """
        params = [days]
        if project:
            query += f" AND EXISTS (SELECT 1 FROM {JOB_SCHEMA}.tasks t WHERE t.job_id = j.id AND t.service = %s)"
            params.append(project)

        async with self._pool.connection() as conn:
            cur = await conn.execute(query, params)
            row = await cur.fetchone()
            return {
                "total_reviews": row["total_reviews"],
                "total_cost_usd": round(float(row["total_cost"]), 4),
                "total_input_tokens": row["total_input_tokens"],
                "total_output_tokens": row["total_output_tokens"],
                "avg_cost_per_review": round(float(row["avg_cost_per_review"]), 4),
                "period_days": days,
            }

    # ===================================================================
    # Jobs
    # ===================================================================

    async def create_job(self, spec: str, external_id: Optional[str] = None) -> Job:
        job = Job(spec=spec, external_id=external_id)
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.jobs (id, spec, status, job_type, mr_url, created_at, updated_at, external_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (job.id, job.spec, job.status, job.job_type, job.mr_url, job.created_at, job.updated_at, job.external_id),
            )
        logger.info("Created job %s", job.id)
        await self.record_event(job.id, "job_created", "db", f"status={job.status}")
        return job

    async def create_review_job(self, project: str, mr_url: str, mr_id: str, model: Optional[str] = None) -> tuple[Job, Task]:
        """Create a review-type job with a single CODE_REVIEWER task atomically."""
        job = Job(spec=mr_url, status=JobStatus.TASKS_CREATED, job_type="review", mr_url=mr_url)
        task = Task(
            job_id=job.id,
            title=f"Review {mr_url}",
            description=f"Code review for MR {mr_id}",
            service=project,
            agent_role=AgentRole.CODE_REVIEWER,
            mr_url=mr_url,
            mr_id=mr_id,
        )
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.jobs (id, spec, status, job_type, mr_url, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (job.id, job.spec, job.status, job.job_type, job.mr_url, job.created_at, job.updated_at),
            )
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.tasks
                    (id, job_id, title, description, service, agent_role, status,
                     branch_name, pr_number, pr_url, review_status, deploy_status,
                     revision_count, attempt, max_attempts, error,
                     mr_url, mr_id, verdict, comments_posted, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    task.id, task.job_id, task.title, task.description, task.service,
                    task.agent_role, task.status, task.branch_name, task.pr_number,
                    task.pr_url, task.review_status, task.deploy_status,
                    task.revision_count, task.attempt, task.max_attempts, task.error,
                    task.mr_url, task.mr_id, task.verdict, task.comments_posted,
                    task.created_at, task.updated_at,
                ),
            )
        logger.info("Created review job %s with task %s for %s", job.id, task.id, mr_url)
        await self.record_event(job.id, "job_created", "db", f"type=review mr_url={mr_url}")
        return job, task

    async def get_job(self, job_id: str) -> Optional[Job]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.jobs WHERE id = %s", (job_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return _dict_to_job(row)

    async def get_all_jobs(self) -> List[Job]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.jobs ORDER BY created_at DESC")
            rows = await cur.fetchall()
            return [_dict_to_job(r) for r in rows]

    async def get_active_jobs(self) -> List[Job]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {JOB_SCHEMA}.jobs WHERE status NOT IN (%s, %s, %s)",
                (JobStatus.DONE, JobStatus.FAILED, JobStatus.NO_WORK_NEEDED),
            )
            rows = await cur.fetchall()
            return [_dict_to_job(r) for r in rows]

    async def get_job_by_external_id(self, external_id: str) -> Optional[Job]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.jobs WHERE external_id = %s", (external_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return _dict_to_job(row)

    async def update_job_spec(self, job_id: str, spec: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE {JOB_SCHEMA}.jobs SET spec = %s, updated_at = %s WHERE id = %s",
                (spec, _now(), job_id),
            )
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

        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE {JOB_SCHEMA}.jobs SET status = %s, updated_at = %s, error = COALESCE(%s, error) WHERE id = %s",
                (status, _now(), error, job_id),
            )
        logger.info("Job %s -> %s", job_id, status)
        detail = f"status={status}"
        if error:
            detail += f" error={error[:200]}"
        await self.record_event(job_id, "job_status_changed", "db", detail)

    async def get_job_usage(self, job_id: str) -> dict:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT
                    COUNT(*) as agent_count,
                    COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as total_cache_read_tokens,
                    COALESCE(SUM(cache_creation_tokens), 0) as total_cache_creation_tokens,
                    COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
                    COALESCE(SUM(num_turns), 0) as total_turns
                FROM {JOB_SCHEMA}.agents WHERE job_id = %s""",
                (job_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else {}

    # ===================================================================
    # Tasks
    # ===================================================================

    async def create_task(self, task: Task) -> Task:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.tasks
                    (id, job_id, title, description, service, agent_role, status,
                     branch_name, pr_number, pr_url, review_status, deploy_status,
                     revision_count, attempt, max_attempts, error,
                     mr_url, mr_id, verdict, comments_posted, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    task.id, task.job_id, task.title, task.description, task.service,
                    task.agent_role, task.status, task.branch_name, task.pr_number,
                    task.pr_url, task.review_status, task.deploy_status,
                    task.revision_count, task.attempt, task.max_attempts,
                    task.error, task.mr_url, task.mr_id, task.verdict, task.comments_posted,
                    task.created_at, task.updated_at,
                ),
            )
        logger.info("Created task %s: %s", task.id, task.title)
        await self.record_event(task.job_id, "task_created", "db", f"task={task.id} title={task.title} service={task.service}")
        return task

    async def get_task(self, task_id: str) -> Optional[Task]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.tasks WHERE id = %s", (task_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return _dict_to_task(row)

    async def get_tasks(self, job_id: str) -> List[Task]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.tasks WHERE job_id = %s ORDER BY created_at", (job_id,))
            rows = await cur.fetchall()
            return [_dict_to_task(r) for r in rows]

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
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        vals = list(kwargs.values()) + [task_id]
        async with self._pool.connection() as conn:
            await conn.execute(f"UPDATE {JOB_SCHEMA}.tasks SET {sets} WHERE id = %s", vals)
        task = await self.get_task(task_id)
        if task and "status" in kwargs:
            await self.record_event(task.job_id, "task_status_changed", "db", f"task={task_id} status={kwargs['status']}")
        return task

    async def get_tasks_by_status(self, job_id: str, status: TaskStatus) -> List[Task]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {JOB_SCHEMA}.tasks WHERE job_id = %s AND status = %s",
                (job_id, status),
            )
            rows = await cur.fetchall()
            return [_dict_to_task(r) for r in rows]

    # ===================================================================
    # Subtasks
    # ===================================================================

    async def create_subtask(self, subtask: Subtask) -> Subtask:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.subtasks
                    (id, task_id, sequence_num, description, status, started_at, completed_at, result, error, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    subtask.id, subtask.task_id, subtask.sequence_num, subtask.description,
                    subtask.status, subtask.started_at, subtask.completed_at,
                    Json(subtask.result) if subtask.result else None,
                    subtask.error, subtask.created_at,
                ),
            )
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
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.subtasks WHERE id = %s", (subtask_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return _dict_to_subtask(row)

    async def get_subtasks(self, task_id: str) -> list[Subtask]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.subtasks WHERE task_id = %s ORDER BY sequence_num", (task_id,))
            rows = await cur.fetchall()
            return [_dict_to_subtask(r) for r in rows]

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
            kwargs["result"] = Json(kwargs["result"])

        sets = ", ".join(f"{k} = %s" for k in kwargs)
        vals = list(kwargs.values()) + [subtask_id]
        async with self._pool.connection() as conn:
            await conn.execute(f"UPDATE {JOB_SCHEMA}.subtasks SET {sets} WHERE id = %s", vals)
        subtask = await self.get_subtask(subtask_id)
        if subtask and "status" in kwargs:
            task = await self.get_task(subtask.task_id)
            if task:
                await self.record_event(task.job_id, "subtask_status_changed", "db", f"subtask={subtask_id} status={kwargs['status']}")
        return subtask

    async def get_running_subtasks_all(self) -> list:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {JOB_SCHEMA}.subtasks WHERE status = 'running'")
            rows = await cur.fetchall()
            return [_dict_to_subtask(r) for r in rows]

    # ===================================================================
    # Messages
    # ===================================================================

    async def send_message(self, msg: Message) -> Message:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.messages (id, job_id, from_role, to_role, content, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                (msg.id, msg.job_id, msg.from_role, msg.to_role, msg.content, msg.created_at),
            )
        return msg

    async def get_messages(self, job_id: str, role: Optional[AgentRole] = None) -> List[Message]:
        async with self._pool.connection() as conn:
            if role:
                cur = await conn.execute(
                    f"SELECT * FROM {JOB_SCHEMA}.messages WHERE job_id = %s AND (to_role = %s OR to_role IS NULL) ORDER BY created_at",
                    (job_id, role),
                )
            else:
                cur = await conn.execute(
                    f"SELECT * FROM {JOB_SCHEMA}.messages WHERE job_id = %s ORDER BY created_at",
                    (job_id,),
                )
            rows = await cur.fetchall()
            return [_dict_to_message(r) for r in rows]

    # ===================================================================
    # Events / Audit
    # ===================================================================

    async def record_event(self, job_id: Optional[str], event_type: str, source: Optional[str] = None, detail: Optional[str] = None) -> None:
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"""INSERT INTO {JOB_SCHEMA}.events (job_id, event_type, source, detail, created_at)
                        VALUES (%s, %s, %s, %s, %s)""",
                    (job_id, event_type, source, detail, _now()),
                )
        except Exception:
            logger.debug("Failed to record event %s", event_type, exc_info=True)

    async def get_events(self, job_id: str) -> List[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {JOB_SCHEMA}.events WHERE job_id = %s ORDER BY created_at",
                (job_id,),
            )
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("created_at"), datetime):
                    d["created_at"] = d["created_at"].isoformat()
                result.append(d)
            return result

    async def record_tool_call(
        self,
        tool_name: str,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
        job_id: Optional[str] = None,
    ) -> None:
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"""INSERT INTO {JOB_SCHEMA}.tool_calls (job_id, tool_name, params, result, error, duration_ms, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (job_id, tool_name, Json(params) if params else None, result, error, duration_ms, _now()),
                )
        except Exception:
            logger.debug("Failed to record tool_call %s", tool_name, exc_info=True)

    async def get_tool_calls(self, job_id: str) -> List[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {JOB_SCHEMA}.tool_calls WHERE job_id = %s ORDER BY created_at",
                (job_id,),
            )
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("created_at"), datetime):
                    d["created_at"] = d["created_at"].isoformat()
                if d.get("params") is not None and not isinstance(d["params"], str):
                    d["params"] = json.dumps(d["params"], default=str)
                result.append(d)
            return result

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
        task_id = entity_id if entity_type == "task" else None
        subtask_id = entity_id if entity_type == "subtask" else None
        effective_job_id = entity_id if entity_type == "job" else job_id

        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"""INSERT INTO {JOB_SCHEMA}.state_transitions
                        (job_id, task_id, subtask_id, from_status, to_status, approved, rejection_reason, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (effective_job_id, task_id, subtask_id, from_status, to_status, approved, rejection_reason, _now()),
                )
        except Exception:
            logger.debug("Failed to record state_transition %s %s->%s", entity_type, from_status, to_status, exc_info=True)

    # ===================================================================
    # Heartbeats
    # ===================================================================

    async def upsert_heartbeat(
        self,
        agent_id: str,
        agent_role: str,
        job_id: Optional[str] = None,
        task_id: Optional[str] = None,
        subtask_id: Optional[str] = None,
        status: str = "active",
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {JOB_SCHEMA}.heartbeats (agent_id, agent_role, job_id, current_task_id, current_subtask_id, status, last_seen)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (agent_id) DO UPDATE SET
                        agent_role = EXCLUDED.agent_role,
                        job_id = EXCLUDED.job_id,
                        current_task_id = EXCLUDED.current_task_id,
                        current_subtask_id = EXCLUDED.current_subtask_id,
                        status = EXCLUDED.status,
                        last_seen = NOW()""",
                (agent_id, agent_role, job_id, task_id, subtask_id, status),
            )

    async def get_stale_heartbeats(self, threshold_seconds: int) -> List[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT * FROM {JOB_SCHEMA}.heartbeats
                    WHERE EXTRACT(EPOCH FROM NOW() - last_seen) > %s
                    AND status != 'lost'""",
                (threshold_seconds,),
            )
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                for k in ("last_seen", "created_at"):
                    if isinstance(d.get(k), datetime):
                        d[k] = d[k].isoformat()
                result.append(d)
            return result

    async def has_recent_heartbeat(self, task_id: str, threshold_seconds: int) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT 1 FROM {JOB_SCHEMA}.heartbeats
                    WHERE current_task_id = %s
                    AND EXTRACT(EPOCH FROM NOW() - last_seen) <= %s
                    AND status != 'lost'
                    LIMIT 1""",
                (task_id, threshold_seconds),
            )
            row = await cur.fetchone()
            return row is not None

    async def delete_heartbeat(self, agent_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(f"DELETE FROM {JOB_SCHEMA}.heartbeats WHERE agent_id = %s", (agent_id,))

    async def clear_all_heartbeats(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(f"TRUNCATE {JOB_SCHEMA}.heartbeats")

    # ===================================================================
    # Recovery
    # ===================================================================

    async def get_running_agents(self) -> List[Agent]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {JOB_SCHEMA}.agents WHERE status IN ('starting', 'running') ORDER BY started_at"
            )
            rows = await cur.fetchall()
            return [_dict_to_job_agent(r) for r in rows]

    async def get_agent_for_task(self, task_id: str) -> Optional[Agent]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {JOB_SCHEMA}.agents WHERE task_id = %s ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return _dict_to_job_agent(row)


# ---------------------------------------------------------------------------
# Dict helpers
# ---------------------------------------------------------------------------


def _dict_to_job_agent(d: dict) -> Agent:
    return Agent(
        id=d["id"],
        review_id=None,
        model=d.get("model", ""),
        status=d["status"],
        started_at=_ts(d["started_at"]) or "",
        finished_at=_ts(d.get("finished_at")),
        input_tokens=d.get("input_tokens", 0),
        output_tokens=d.get("output_tokens", 0),
        cache_read_tokens=d.get("cache_read_tokens", 0),
        cache_creation_tokens=d.get("cache_creation_tokens", 0),
        cost_usd=d.get("cost_usd", 0.0),
        num_turns=d.get("num_turns", 0),
        log_file=d.get("log_file"),
        error=d.get("error"),
        job_id=d.get("job_id"),
        role=d.get("role"),
        task_id=d.get("task_id"),
        k8s_job_name=d.get("k8s_job_name"),
    )


def _dict_to_job(d: dict) -> Job:
    return Job(
        id=d["id"],
        spec=d["spec"],
        status=JobStatus(d["status"]),
        job_type=d.get("job_type") or "development",
        mr_url=d.get("mr_url"),
        error=d.get("error"),
        external_id=d.get("external_id"),
        created_at=_ts(d["created_at"]) or "",
        updated_at=_ts(d["updated_at"]) or "",
    )


def _dict_to_task(d: dict) -> Task:
    return Task(
        id=d["id"],
        job_id=d["job_id"],
        title=d["title"],
        description=d.get("description", ""),
        service=d["service"],
        agent_role=d["agent_role"],
        status=TaskStatus(d["status"]),
        branch_name=d.get("branch_name"),
        pr_number=d.get("pr_number"),
        pr_url=d.get("pr_url"),
        review_status=d.get("review_status"),
        deploy_status=d.get("deploy_status"),
        revision_count=d.get("revision_count", 0),
        attempt=d.get("attempt", 1),
        max_attempts=d.get("max_attempts", 3),
        error=d.get("error"),
        mr_url=d.get("mr_url"),
        mr_id=d.get("mr_id"),
        verdict=d.get("verdict"),
        comments_posted=d.get("comments_posted", 0),
        created_at=_ts(d["created_at"]) or "",
        updated_at=_ts(d["updated_at"]) or "",
    )


def _dict_to_subtask(d: dict) -> Subtask:
    result_val = d.get("result")
    if result_val is not None and isinstance(result_val, str):
        result_val = json.loads(result_val)
    return Subtask(
        id=d["id"],
        task_id=d["task_id"],
        sequence_num=d["sequence_num"],
        description=d["description"],
        status=SubtaskStatus(d["status"]),
        started_at=_ts(d.get("started_at")),
        completed_at=_ts(d.get("completed_at")),
        result=result_val,
        error=d.get("error"),
        created_at=_ts(d["created_at"]) or "",
    )


def _dict_to_message(d: dict) -> Message:
    return Message(
        id=d["id"],
        job_id=d["job_id"],
        from_role=d["from_role"],
        to_role=d.get("to_role"),
        content=d["content"],
        created_at=_ts(d["created_at"]) or "",
    )
