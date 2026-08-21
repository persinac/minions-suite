"""LiteLLM-powered agent with tool-use loop.

Unified agent runner: handles both job orchestration agents and code review
agents through the same `run_agent()` / `_agent_loop_generic()` pipeline.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import litellm

from ..config import Config
from ..core.models import Agent, AgentRole, Job, Task, _now
from ..project_registry import ProjectConfig, ServiceTarget
from ..providers.git import GitProviderProtocol
from .caching import apply_cache_control, extract_cache_tokens
from .prompt import build_agent_prompt, build_prompt
from .tools.definitions import REVIEW_TOOL_DEFINITIONS, ToolExecutor, get_tools_for_role

logger = logging.getLogger(__name__)

# How many times an agent that stopped without delivering its required output is
# asked again before the run is allowed to end without it.
#
# Two, not more. The point is to catch a model that simply forgot the call, not
# to argue with one that has decided not to make it -- and every extra round is
# a full turn of a metered agent. Measured behaviour on the 17.7% that stopped
# short was prose in place of the call, which one reminder addresses.
MAX_FINAL_NUDGES = 2

_FINAL_CALL_NUDGE = (
    "You have not submitted a verdict. Your review is not recorded until you "
    "call `submit_review` with an explicit verdict — `approve` or "
    "`request_changes` — and a summary.\n\n"
    "Prose in your reply is NOT a verdict: nothing reads it. A missing verdict "
    "fails closed, so staying silent is read as an objection and sends the "
    "author into a revision round with no findings to address.\n\n"
    "Call `submit_review` now with the conclusion you already reached. If you "
    "found nothing worth blocking, that is `approve`."
)


def _hard_stop_instruction(agent_role: str | None) -> str:
    """What "wrap up now" means for THIS role.

    The single engineer-shaped message used to go to reviewers too, telling one
    to "commit and push what you have RIGHT NOW, then create the merge request"
    -- work a reviewer must not do and has no tools for. It never mentioned
    submit_review, so the one call that ends a review well was the one the
    hard-stop did not ask for.
    """
    role = str(agent_role or "").strip().lower()
    if role.endswith("code_reviewer"):
        return (
            "You may ONLY call: submit_review, post_inline_comment, report_review_complete. "
            "Submit your verdict RIGHT NOW with what you have already read — an incomplete "
            "review with a verdict is worth more than a thorough one with none, because a "
            "missing verdict fails closed and sends the author into a revision round with "
            "nothing to fix."
        )
    return (
        "You may ONLY call: create_branch, commit, push, create_pr, report_pr, "
        "complete_subtask, fail_subtask, update_task_status, send_heartbeat. "
        "Commit and push what you have RIGHT NOW, then create the merge request."
    )


def _owes_final_call(agent_role: str | None, verdict: str | None) -> bool:
    """Whether this agent stopped without delivering the output it exists for.

    Only reviewers today. An engineer's equivalent is report_pr, and the same
    silent-stop shape strands its branch -- but that failure has a different
    cause (the turn ceiling) and a different fix, so it is deliberately not
    lumped in here.

    The role arrives as a str from several call sites and has been seen both
    bare ("code_reviewer") and enum-stringified, so match on the suffix rather
    than on equality.
    """
    if verdict:
        return False
    role = str(agent_role or "").strip().lower()
    return role.endswith("code_reviewer")


def _log_read_stats(agent, tool_executor) -> None:
    """Report how much of this agent's input was content it already had.

    Emitted at completion beside cost and turns so the three can be correlated
    over a run of jobs. Purely observational for now: the fix (serving a stub
    for a repeat read) is a behaviour change and should be argued from these
    numbers rather than from the assumption that produced them.

    Guarded on the method rather than the type — reviewers use a different
    executor that has no filesystem tools at all.
    """
    stats_fn = getattr(tool_executor, "read_stats", None)
    if not callable(stats_fn):
        return
    try:
        stats = stats_fn()
    except Exception:
        logger.debug("Could not collect read stats for agent %s", agent.id, exc_info=True)
        return

    if not stats.get("rereads"):
        return

    logger.info(
        "Agent %s read accounting: %d file(s), %d read(s), %d re-read(s) costing >=%d chars re-sent; worst: %s",
        agent.id,
        stats["files_read"],
        stats["total_reads"],
        stats["rereads"],
        stats["reread_chars"],
        ", ".join(f"{k}x{n}" for k, n in stats["worst"]) or "-",
    )


async def run_agent(
    job: Job,
    task: Task,
    project: ProjectConfig | None = None,
    service: ServiceTarget | None = None,
    config: Config | None = None,
    db=None,
    tool_executor=None,
    context: str | None = None,
    provider: GitProviderProtocol | None = None,
    mr_info: dict | None = None,
    agent: Agent | None = None,
    knowledge_context: str | None = None,
) -> Agent:
    """Execute an agent for a job task using the LiteLLM tool-use loop.

    For CODE_REVIEWER role: pass provider and mr_info to set up the review
    tool executor and build a review-specific prompt. After the loop, captures
    verdict and comments_posted from the result.

    For all other roles: uses build_agent_prompt() and get_tools_for_role().

    If agent is provided, reuse it instead of creating a new one.
    """
    if config is None:
        config = Config.from_env()

    model = config.model
    if project and project.model:
        model = project.model

    if agent is None:
        agent = Agent(
            job_id=job.id,
            role=task.agent_role,
            task_id=task.id,
            model=model,
        )
        if db:
            agent = await db.create_agent(agent)
    elif agent.model:
        # An agent handed to us already carries a resolved model — dev.py builds
        # it with resolve_model(), which applies the difficulty tier from the
        # classifier and any per-role override.
        #
        # Without this the caller's choice was silently discarded: `model` stayed
        # at config.model, so every agent ran on the global default while its DB
        # row recorded the tier it was SUPPOSED to use. The classifier has been
        # classifying, persisting a decision, and changing nothing since it was
        # added — job 17d10bdc was rated `easy` and made 62 calls to
        # claude-opus-5 against 2 to claude-haiku-4-5.
        #
        # It also means recorded cost was wrong in the cheap direction:
        # completion_cost() prices against agent.model (haiku) while the tokens
        # were actually billed at opus rates, ~5x higher. Any spend figure taken
        # from those rows understates reality.
        model = agent.model

    log_dir = Path(config.agent_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"job-{job.id}-task-{task.id}-{agent.id}.log"
    agent.log_file = str(log_path)

    try:
        is_reviewer = task.agent_role == AgentRole.CODE_REVIEWER

        if is_reviewer and provider and mr_info:
            # Build review-specific prompt and tool executor
            changed_files = mr_info.get("changed_files", [])
            system_prompt = build_prompt(task, project, changed_files, knowledge_context=knowledge_context)
            if context:
                system_prompt += f"\n\n---\n\n{context}"
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
            system_prompt = build_agent_prompt(job, task, project, service, context, knowledge_context=knowledge_context)
            memory_enabled = config.memory_enabled if config else False
            tools = get_tools_for_role(task.agent_role, memory_enabled=memory_enabled)
            user_message = None

        # Use role-specific timeout if available
        from ..core.timeout_config import TimeoutConfig

        timeout_cfg = TimeoutConfig()
        role_cfg = timeout_cfg.roles.get(task.agent_role)
        timeout = role_cfg.task_timeout_seconds if role_cfg else config.agent_timeout

        # Refuse a model LiteLLM cannot price while ceilings are configured.
        # completion_cost() returns 0 for an unknown model, so the limits below
        # would never fire while the config still claims they are on — worse than
        # having no limits, because nothing looks wrong. Checked here rather than
        # at config load because the model is resolved per agent (difficulty
        # tier, reviewer/engineer override, per-project pin).
        from ..classifier import assert_priceable

        assert_priceable(model, config, role=str(task.agent_role) if task and task.agent_role else "agent")

        # Turns and dollars are both bounded. Turns alone were not a cost
        # control: at 100 turns a single engineer billed $20.57 by turn 64 with
        # room to spend half as much again. The cost limit is the real ceiling;
        # max_turns just keeps a cheap-but-looping agent from spinning forever.
        max_turns = config.agent_max_turns
        cost_limit = config.agent_cost_limit_usd

        # Mark agent as running so the engine knows it's alive
        agent.status = "running"
        if db:
            await db.update_agent(agent.id, status="running")

        if config.use_langgraph_agent:
            result = await _run_via_subgraph(
                model=model,
                system_prompt=system_prompt,
                tools=tools,
                tool_executor=tool_executor,
                timeout=timeout,
                log_path=log_path,
                user_message=user_message,
                max_turns=max_turns,
                cost_limit=cost_limit,
                db=db,
                job=job,
                task=task,
                agent=agent,
            )
        else:
            result = await _agent_loop_generic(
                model=model,
                system_prompt=system_prompt,
                tools=tools,
                tool_executor=tool_executor,
                timeout=timeout,
                log_path=log_path,
                user_message=user_message,
                max_turns=max_turns,
                cost_limit=cost_limit,
                db=db,
                job_id=job.id,
                agent_id=agent.id if agent else "",
                agent_role=str(task.agent_role) if task.agent_role else "",
            )

        agent.input_tokens = result["input_tokens"]
        agent.output_tokens = result["output_tokens"]
        # Without these two the columns stay 0 forever — which is exactly why
        # nobody noticed caching had never been enabled: update_agent has
        # always persisted them, and nothing ever populated them.
        agent.cache_read_tokens = result.get("cache_read_tokens", 0)
        agent.cache_creation_tokens = result.get("cache_creation_tokens", 0)
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
            agent.id,
            task.agent_role,
            agent.cost_usd,
            agent.num_turns,
        )

        # Re-read accounting, logged next to cost so the two can be correlated
        # across runs. Measurement only — nothing changes behaviour on it yet.
        _log_read_stats(agent, tool_executor)

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


async def _run_via_subgraph(
    model: str,
    system_prompt: str,
    tools: list,
    tool_executor,
    timeout: int,
    log_path,
    user_message: str | None,
    max_turns: int,
    cost_limit: float,
    db,
    job: Job,
    task: Task,
    agent: Agent,
) -> dict:
    """Execute the agent loop via LangGraph subgraph."""
    from .graph import build_agent_subgraph

    subgraph = build_agent_subgraph()

    state = {
        "job_id": job.id,
        "task_id": task.id,
        "agent_id": agent.id,
        "agent_role": task.agent_role,
        "model": model,
        "system_prompt": system_prompt,
        "tools": tools,
        "tool_executor": tool_executor,
        "timeout": timeout,
        "max_turns": max_turns,
        "cost_limit": cost_limit,
        "log_path": str(log_path),
        "user_message": user_message,
        "db": db,
        "result": None,
        "error": None,
    }

    output = await subgraph.ainvoke(state)

    if output.get("error"):
        raise RuntimeError(output["error"])

    return output["result"]


async def _agent_loop_generic(
    model: str,
    system_prompt: str,
    tools: list,
    tool_executor,
    timeout: int,
    log_path: Path,
    user_message: str | None = None,
    max_turns: int = 30,
    cost_limit: float = 0.0,
    db=None,
    job_id: str | None = None,
    agent_id: str | None = None,
    agent_role: str | None = None,
) -> dict:
    """Generic tool-use loop for all agents (job orchestration + code review).

    Captures verdict/summary/comments_posted from submit_review calls when
    present (for CODE_REVIEWER agents).
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": user_message or "Please proceed with the task described in your context."})

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_cost = 0.0
    num_turns = 0
    verdict = None
    summary = ""
    final_nudges = 0
    start_time = time.time()
    wind_down_phase = 0  # 0=normal, 1=wind-down (80%), 2=hard-stop (90%)
    wind_down_turn = int(max_turns * 0.8)
    hard_stop_turn = int(max_turns * 0.9)

    # Tools the agent is always allowed to call during hard-stop phase.
    # Everything else gets blocked so the agent is forced to ship.
    #
    # NB: the hard-stop MESSAGE below names only the engineer half of this set.
    # A reviewer that reaches hard-stop is told to "commit and push", which it
    # cannot do and must not try -- see _hard_stop_message.
    _WRAP_UP_TOOLS = frozenset(
        {
            "create_branch",
            "commit",
            "push",
            "create_pr",
            "report_pr",
            "complete_subtask",
            "fail_subtask",
            "update_task_status",
            "send_heartbeat",
            "submit_review",
            "report_review_complete",
        }
    )

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"=== Agent Log ===\nModel: {model}\nStarted: {_now()}\n\n")

        while num_turns < max_turns:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.warning("Agent timeout after %ds", elapsed)
                break

            # Hard spend ceiling. The wind-down below tries to land the work
            # gracefully well before this, so reaching it means the agent
            # ignored two escalating warnings — stop paying either way.
            if cost_limit > 0 and total_cost >= cost_limit:
                logger.error(
                    "Agent %s (role=%s) hit its $%.2f cost limit at turn %d ($%.4f spent) — stopping",
                    agent_id,
                    agent_role,
                    cost_limit,
                    num_turns,
                    total_cost,
                )
                log.write(f"[SYSTEM] Cost limit ${cost_limit:.2f} reached at turn {num_turns} (${total_cost:.4f})\n")
                break

            # --- Wind-down escalation ---
            turns_pct_reached = num_turns >= hard_stop_turn
            time_pct_reached = elapsed >= timeout * 0.9
            cost_pct_reached = cost_limit > 0 and total_cost >= cost_limit * 0.9
            if wind_down_phase < 2 and (turns_pct_reached or time_pct_reached or cost_pct_reached):
                wind_down_phase = 2
                remaining_turns = max_turns - num_turns
                remaining_time = int(timeout - elapsed)
                hard_msg = (
                    f"🛑 HARD STOP: You have only {remaining_turns} turns and ~{remaining_time}s left. "
                    "Non-essential tool calls will be BLOCKED from now on. " + _hard_stop_instruction(agent_role)
                )
                messages.append({"role": "user", "content": hard_msg})
                log.write(f"[SYSTEM] Hard-stop injected at turn {num_turns}\n")
            elif wind_down_phase < 1:
                turns_near = num_turns >= wind_down_turn
                time_near = elapsed >= timeout * 0.8
                cost_near = cost_limit > 0 and total_cost >= cost_limit * 0.8
                if turns_near or time_near or cost_near:
                    wind_down_phase = 1
                    remaining_turns = max_turns - num_turns
                    remaining_time = int(timeout - elapsed)
                    wind_down_msg = (
                        f"⚠️ WIND DOWN: You have {remaining_turns} turns and ~{remaining_time}s remaining. "
                        "Wrap up your current work now: commit what you have, push the branch, and create "
                        "a merge request for review. Any remaining subtasks will be continued after the "
                        "review cycle. Do NOT start new subtasks — focus on shipping what's done."
                    )
                    messages.append({"role": "user", "content": wind_down_msg})
                    log.write(f"[SYSTEM] Wind-down injected at turn {num_turns}\n")
            elif wind_down_phase == 1 and num_turns % 3 == 0:
                # Repeat wind-down every 3 turns so it doesn't get buried
                remaining_turns = max_turns - num_turns
                messages.append(
                    {
                        "role": "user",
                        "content": f"⚠️ REMINDER: {remaining_turns} turns left. Stop fixing issues and ship the MR now.",
                    }
                )
                log.write(f"[SYSTEM] Wind-down reminder at turn {num_turns}\n")

            num_turns += 1
            log.write(f"\n--- Turn {num_turns} ---\n")

            max_retries = 5
            for attempt in range(max_retries):
                try:
                    # Cache the stable prefix. An agentic loop re-sends
                    # turns 1..N-1 on turn N, which was 94% of the one
                    # measured job's cost. No-ops on models that do not
                    # support it, so a MODEL_ENGINEER pointing at another
                    # vendor is unaffected.
                    cached_messages = apply_cache_control(messages, model)
                    response = await litellm.acompletion(
                        model=model,
                        messages=cached_messages,
                        tools=tools,
                        max_tokens=8192,
                        timeout=120,
                        metadata={
                            "trace_name": agent_role or "agent",
                            "session_id": job_id or "",
                            "trace_user_id": agent_id or "",
                            "tags": ["minion-suite"],
                        },
                    )
                    break
                except (litellm.exceptions.RateLimitError, litellm.exceptions.InternalServerError) as e:
                    if attempt == max_retries - 1:
                        log.write(f"LLM API error after {max_retries} retries: {e}\n")
                        raise
                    wait = min(2**attempt * 5, 60)
                    log.write(f"Retryable API error (attempt {attempt + 1}/{max_retries}), waiting {wait}s...\n")
                    logger.warning(
                        "Retryable API error on turn %d, retrying in %ds (attempt %d/%d): %s",
                        num_turns,
                        wait,
                        attempt + 1,
                        max_retries,
                        type(e).__name__,
                    )
                    await asyncio.sleep(wait)
                except Exception as e:
                    log.write(f"LLM call failed: {e}\n")
                    raise

            usage = response.usage
            if usage:
                total_input += usage.prompt_tokens or 0
                total_output += usage.completion_tokens or 0
                turn_read, turn_created = extract_cache_tokens(response)
                total_cache_read += turn_read
                total_cache_creation += turn_created

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
                # "Stopped talking" is not the same as "delivered". A reviewer
                # that writes its review as prose and never calls submit_review
                # used to end right here, and the run was recorded as a success
                # with verdict=NULL -- 22 of 124 reviewer runs, 17.7%.
                #
                # That is not a cosmetic gap. aggregate_verdicts fails closed on
                # a missing verdict, so the silence is read as an objection and
                # the task is sent back for a revision round nobody asked for:
                # on job e180f866 all three rounds came from empty verdicts, with
                # zero comments and zero reviews on the PR. A spurious round
                # costs more than the fan-out cap saves.
                #
                # The prompt already says "You MUST call submit_review"
                # (prompts/agents/code_reviewer.md:17), so more prompt text is
                # not the fix -- asking again, once, is.
                if _owes_final_call(agent_role, verdict) and final_nudges < MAX_FINAL_NUDGES:
                    final_nudges += 1
                    messages.append({"role": "user", "content": _FINAL_CALL_NUDGE})
                    log.write(f"[SYSTEM] No verdict submitted; nudged ({final_nudges}/{MAX_FINAL_NUDGES})\n")
                    logger.warning(
                        "Reviewer agent %s stopped without a verdict — nudging (%d/%d)",
                        agent_id or "?",
                        final_nudges,
                        MAX_FINAL_NUDGES,
                    )
                    continue

                if _owes_final_call(agent_role, verdict):
                    # Bounded, so it can still end without one. Say so loudly:
                    # a silent NULL verdict is what made this invisible for the
                    # 124 runs before it.
                    logger.error(
                        "Reviewer agent %s finished with NO verdict after %d nudge(s) — the gate will fail closed",
                        agent_id or "?",
                        final_nudges,
                    )
                    log.write("[SYSTEM] Finished with no verdict after nudging\n")

                log.write("Agent finished (no more tool calls).\n")
                break

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                log.write(f"Tool: {fn_name}({json.dumps(fn_args)[:200]})\n")

                # Hard-stop phase: block non-essential tools
                tc_start = time.time()
                tc_error = None
                if wind_down_phase >= 2 and fn_name not in _WRAP_UP_TOOLS:
                    result = json.dumps(
                        {
                            "error": f"BLOCKED: '{fn_name}' is not allowed during hard-stop phase. "
                            "You must wrap up now — use create_branch, commit, push, create_pr, report_pr only."
                        }
                    )
                    tc_error = "blocked_hard_stop"
                    log.write(f"[BLOCKED] {fn_name} blocked during hard-stop\n")
                elif tool_executor:
                    result = await tool_executor.execute(fn_name, fn_args)
                else:
                    result = json.dumps({"error": "No tool executor configured"})

                tc_duration = (time.time() - tc_start) * 1000

                # Extract error from result if present
                if tc_error is None:
                    try:
                        parsed = json.loads(result)
                        if isinstance(parsed, dict) and "error" in parsed:
                            tc_error = str(parsed["error"])[:200]
                    except json.JSONDecodeError, TypeError:
                        pass

                # Record tool call to DB for dashboard visibility
                if db and job_id:
                    try:
                        await db.record_tool_call(
                            tool_name=fn_name,
                            params=fn_args,
                            result=result[:1000] if result else None,
                            error=tc_error,
                            duration_ms=tc_duration,
                            job_id=job_id,
                        )
                    except Exception:
                        pass

                log.write(f"Result: {result[:500]}\n")

                # Capture verdict from submit_review (CODE_REVIEWER)
                if fn_name == "submit_review":
                    verdict = fn_args.get("verdict")
                # ...and from report_review_complete, which is the OTHER tool a
                # reviewer can end on. Only the fan-out path is restricted to
                # REVIEW_TOOL_DEFINITIONS; a reviewer whose provider failed to
                # build falls through to CODE_REVIEWER_TOOL_DEFINITIONS and has
                # both. Its verdict was dropped on the floor here, which both
                # lost a real answer and would now make the nudge below badger an
                # agent that had already reported. Spelling differs between the
                # two tools ("approved" vs "approve"); aggregate_verdicts runs
                # everything through normalise_verdict, so either is fine.
                elif fn_name == "report_review_complete" and not verdict:
                    verdict = fn_args.get("verdict")
                    summary = fn_args.get("body", "")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

        log.write(f"\n=== Done ===\nTurns: {num_turns}\nInput tokens: {total_input}\nOutput tokens: {total_output}\nCost: ${total_cost:.4f}\n")

    comments_posted = 0
    if tool_executor and hasattr(tool_executor, "comments_posted"):
        comments_posted = tool_executor.comments_posted

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": total_cache_read,
        "cache_creation_tokens": total_cache_creation,
        "cost_usd": total_cost,
        "num_turns": num_turns,
        "verdict": verdict,
        "summary": summary,
        "comments_posted": comments_posted,
    }
