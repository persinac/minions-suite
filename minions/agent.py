"""LiteLLM-powered review agent with tool-use loop.

Replaces the `claude -p` subprocess pattern from mcp-minions with an
in-process agent loop that calls any LiteLLM-supported model.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import litellm

from .config import Config
from .git_provider import GitProviderProtocol
from .models import Agent, Review, ReviewVerdict, _now
from .project_registry import ProjectConfig
from .prompt import build_prompt
from .tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)


async def run_review(
    review: Review,
    project: ProjectConfig,
    provider: GitProviderProtocol,
    config: Config,
    db=None,
) -> Agent:
    """Execute a full review cycle: fetch MR, run LLM agent loop, post results.

    Returns an Agent record with cost/usage metrics.
    """
    model = project.model or config.model
    agent = Agent(review_id=review.id, model=model)

    # Ensure log directory exists
    log_dir = Path(config.agent_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"review-{review.id}-{agent.id}.log"
    agent.log_file = str(log_path)

    if db:
        agent = await db.create_agent(agent)

    try:
        # Fetch MR metadata and changed files
        pr_info = await provider.get_pr(project.project_id, review.mr_id)
        changed_files = await provider.get_changed_files(project.project_id, review.mr_id)

        # Update review with MR metadata
        review.title = pr_info.title
        review.author = pr_info.author
        review.branch = pr_info.branch

        # Build prompt
        system_prompt = build_prompt(review, project, changed_files)

        # Set up tool executor
        executor = ToolExecutor(
            provider=provider,
            project_id=project.project_id,
            mr_id=review.mr_id,
            repo_path=project.repo_path,
        )

        # Run agent loop
        result = await _agent_loop(
            model=model,
            system_prompt=system_prompt,
            executor=executor,
            config=config,
            log_path=log_path,
        )

        # Populate agent metrics
        agent.input_tokens = result["input_tokens"]
        agent.output_tokens = result["output_tokens"]
        agent.cost_usd = result["cost_usd"]
        agent.num_turns = result["num_turns"]
        agent.status = "done"
        agent.finished_at = _now()

        # Extract verdict from the final submit_review call
        review.verdict = result.get("verdict")
        review.summary = result.get("summary", "")
        review.comments_posted = executor.comments_posted

        logger.info(
            "Review %s complete: verdict=%s, comments=%d, cost=$%.4f, turns=%d",
            review.id,
            review.verdict,
            review.comments_posted,
            agent.cost_usd,
            agent.num_turns,
        )

    except Exception as e:
        agent.status = "failed"
        agent.error = str(e)[:500]
        agent.finished_at = _now()
        logger.error("Review %s failed: %s", review.id, e, exc_info=True)

    if db:
        await db.update_agent(
            agent.id,
            status=agent.status,
            finished_at=agent.finished_at,
            input_tokens=agent.input_tokens,
            output_tokens=agent.output_tokens,
            cost_usd=agent.cost_usd,
            num_turns=agent.num_turns,
            error=agent.error,
        )

    return agent


async def _agent_loop(
    model: str,
    system_prompt: str,
    executor: ToolExecutor,
    config: Config,
    log_path: Path,
) -> dict:
    """Core tool-use loop: call LLM -> execute tools -> repeat until done."""
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": "Please review the merge request described in your context. Start by reading the diff."})

    total_input = 0
    total_output = 0
    total_cost = 0.0
    num_turns = 0
    verdict = None
    summary = ""
    max_turns = 30
    start_time = time.time()

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"=== Review Agent Log ===\nModel: {model}\nStarted: {_now()}\n\n")

        while num_turns < max_turns:
            elapsed = time.time() - start_time
            if elapsed > config.agent_timeout:
                logger.warning("Agent timeout after %ds", elapsed)
                break

            num_turns += 1
            log.write(f"\n--- Turn {num_turns} ---\n")

            try:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    max_tokens=8192,
                    timeout=120,
                )
            except Exception as e:
                log.write(f"LLM call failed: {e}\n")
                raise

            # Track usage
            usage = response.usage
            if usage:
                total_input += usage.prompt_tokens or 0
                total_output += usage.completion_tokens or 0

            try:
                turn_cost = litellm.completion_cost(completion_response=response)
                total_cost += turn_cost
            except Exception:
                pass  # Cost calc may fail for some providers

            choice = response.choices[0]
            message = choice.message

            # Log assistant response
            if message.content:
                log.write(f"Assistant: {message.content[:500]}\n")

            # Add assistant message to history
            messages.append(message.model_dump(exclude_none=True))

            # Check if done
            if choice.finish_reason == "stop" or not message.tool_calls:
                log.write("Agent finished (no more tool calls).\n")
                break

            # Execute tool calls
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                log.write(f"Tool: {fn_name}({json.dumps(fn_args)[:200]})\n")

                result = await executor.execute(fn_name, fn_args)

                log.write(f"Result: {result[:500]}\n")

                # Capture verdict from submit_review
                if fn_name == "submit_review":
                    verdict = fn_args.get("verdict")
                    summary = fn_args.get("body", "")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

        log.write(f"\n=== Done ===\nTurns: {num_turns}\nInput tokens: {total_input}\nOutput tokens: {total_output}\nCost: ${total_cost:.4f}\n")

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": total_cost,
        "num_turns": num_turns,
        "verdict": verdict,
        "summary": summary,
    }
