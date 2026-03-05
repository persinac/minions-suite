"""Database protocol for job orchestration."""

from typing import List, Optional, Protocol, runtime_checkable

from ..core.models import (
    Agent,
    AgentRole,
    Job,
    JobStatus,
    Message,
    Subtask,
    Task,
    TaskStatus,
)


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

    async def create_job(self, spec: str, external_id: Optional[str] = None) -> Job: ...

    async def create_review_job(self, project: str, mr_url: str, mr_id: str, model: Optional[str] = None) -> tuple[Job, Task]: ...

    async def get_job(self, job_id: str) -> Optional[Job]: ...

    async def get_all_jobs(self) -> List[Job]: ...

    async def get_active_jobs(self) -> List[Job]: ...

    async def get_job_by_external_id(self, external_id: str) -> Optional[Job]: ...

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
