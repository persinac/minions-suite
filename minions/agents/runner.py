"""LiteLLM-powered agent with tool-use loop.

Unified agent runner: handles both job orchestration agents and code review
agents through the same `run_agent()` / `_agent_loop_generic()` pipeline.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import litellm

from ..config import Config
from ..core.models import Agent, AgentRole, Job, Task, _now
from ..project_registry import ProjectConfig, ServiceTarget
from ..providers.git import GitProviderProtocol
from .prompt import build_agent_prompt, build_prompt
from .tools.definitions import REVIEW_TOOL_DEFINITIONS, ToolExecutor, get_tools_for_role

logger = logging.getLogger(__name__)


async def run_agent(
    job: Job,
    task: Task,
    project: Optional[ProjectConfig] = None,
    service: Optional[ServiceTarget] = None,
    config: Optional[Config] = None,
    db=None,
    tool_executor=None,
    context: Optional[str] = None,
    provider: Optional[GitProviderProtocol] = None,
    mr_info: Optional[dict] = None,
) -> Agent:
    """Execute an agent for a job task using the LiteLLM tool-use loop.

    For CODE_REVIEWER role: pass provider and mr_info to set up the review
    tool executor and build a review-specific prompt. After the loop, captures
    verdict and comments_posted from the result.

    For all other roles: uses build_agent_prompt() and get_tools_for_role().
    """
    if config is None:
        config = Config.from_env()

    model = config.model
    if project and project.model:
        model = project.model

    agent = Agent(
        job_id=job.id,
        role=task.agent_role,
        task_id=task.id,
        model=model,
    )

    log_dir = Path(config.agent_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"job-{job.id}-task-{task.id}-{agent.id}.log"
    agent.log_file = str(log_path)

    if db:
        agent = await db.create_agent(agent)

    try:
        is_reviewer = task.agent_role == AgentRole.CODE_REVIEWER

        if is_reviewer and provider and mr_info:
            # Build review-specific prompt and tool executor
            changed_files = mr_info.get("changed_files", [])
            system_prompt = build_prompt(task, project, changed_files)
            tools = REVIEW_TOOL_DEFINITIONS
            executor = ToolExecutor(
                provider=provider,
                project_id=mr_info.get("project_id", project.project_id if project else ""),
                mr_id=task.mr_id or "",
                repo_path=project.repo_path if project else "",
            )
            tool_executor = executor
            user_message = "Please review the merge request described in your context. Start by reading the diff."
        else:
            system_prompt = build_agent_prompt(job, task, project, service, context)
            tools = get_tools_for_role(task.agent_role)
            user_message = None

        # Use role-specific timeout if available
        from ..core.timeout_config import TimeoutConfig
        timeout_cfg = TimeoutConfig()
        role_cfg = timeout_cfg.roles.get(task.agent_role)
        timeout = role_cfg.task_timeout_seconds if role_cfg else config.agent_timeout

        result = await _agent_loop_generic(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            tool_executor=tool_executor,
            timeout=timeout,
            log_path=log_path,
            user_message=user_message,
        )

        agent.input_tokens = result["input_tokens"]
        agent.output_tokens = result["output_tokens"]
        agent.cost_usd = result["cost_usd"]
        agent.num_turns = result["num_turns"]
        agent.status = "done"
        agent.finished_at = _now()

        # Capture review results if this was a reviewer agent
        if is_reviewer:
            agent._review_verdict = result.get("verdict")
            agent._review_summary = result.get("summary", "")
            agent._review_comments_posted = result.get("comments_posted", 0)

        logger.info(
            "Agent %s (role=%s) done: cost=$%.4f, turns=%d",
            agent.id, task.agent_role, agent.cost_usd, agent.num_turns,
        )

    except Exception as e:
        agent.status = "failed"
        agent.error = str(e)[:500]
        agent.finished_at = _now()
        logger.error("Agent %s (role=%s) failed: %s", agent.id, task.agent_role, e, exc_info=True)

    if db:
        await db.update_agent(
            agent.id,
            status=agent.status,
            finished_at=agent.finished_at,
            input_tokens=agent.input_tokens,
            output_tokens=agent.output_tokens,
            cache_read_tokens=agent.cache_read_tokens,
            cache_creation_tokens=agent.cache_creation_tokens,
            cost_usd=agent.cost_usd,
            num_turns=agent.num_turns,
            error=agent.error,
        )

    return agent


async def _agent_loop_generic(
    model: str,
    system_prompt: str,
    tools: list,
    tool_executor,
    timeout: int,
    log_path: Path,
    user_message: Optional[str] = None,
) -> dict:
    """Generic tool-use loop for all agents (job orchestration + code review).

    Captures verdict/summary/comments_posted from submit_review calls when
    present (for CODE_REVIEWER agents).
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": user_message or "Please proceed with the task described in your context."})

    total_input = 0
    total_output = 0
    total_cost = 0.0
    num_turns = 0
    verdict = None
    summary = ""
    max_turns = 30
    start_time = time.time()

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"=== Agent Log ===\nModel: {model}\nStarted: {_now()}\n\n")

        while num_turns < max_turns:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.warning("Agent timeout after %ds", elapsed)
                break

            num_turns += 1
            log.write(f"\n--- Turn {num_turns} ---\n")

            try:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    tools=tools,
                    max_tokens=8192,
                    timeout=120,
                )
            except Exception as e:
                log.write(f"LLM call failed: {e}\n")
                raise

            usage = response.usage
            if usage:
                total_input += usage.prompt_tokens or 0
                total_output += usage.completion_tokens or 0

            try:
                turn_cost = litellm.completion_cost(completion_response=response)
                total_cost += turn_cost
            except Exception:
                pass

            choice = response.choices[0]
            message = choice.message

            if message.content:
                log.write(f"Assistant: {message.content[:500]}\n")

            messages.append(message.model_dump(exclude_none=True))

            if choice.finish_reason == "stop" or not message.tool_calls:
                log.write("Agent finished (no more tool calls).\n")
                break

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                log.write(f"Tool: {fn_name}({json.dumps(fn_args)[:200]})\n")

                if tool_executor:
                    result = await tool_executor.execute(fn_name, fn_args)
                else:
                    result = json.dumps({"error": "No tool executor configured"})

                log.write(f"Result: {result[:500]}\n")

                # Capture verdict from submit_review (CODE_REVIEWER)
                if fn_name == "submit_review":
                    verdict = fn_args.get("verdict")
                    summary = fn_args.get("body", "")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

        log.write(f"\n=== Done ===\nTurns: {num_turns}\nInput tokens: {total_input}\nOutput tokens: {total_output}\nCost: ${total_cost:.4f}\n")

    comments_posted = 0
    if tool_executor and hasattr(tool_executor, "comments_posted"):
        comments_posted = tool_executor.comments_posted

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": total_cost,
        "num_turns": num_turns,
        "verdict": verdict,
        "summary": summary,
        "comments_posted": comments_posted,
    }
