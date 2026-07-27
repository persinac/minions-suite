"""Working checkouts for in-process agents.

K8s dispatch clones each repo into a per-Job emptyDir via an init container
(``providers/k8s.py``). The in-process path has no equivalent: ``job_engine``
resolves a ``working_dir`` from the registry and hands it to the tool executor,
which then expects a repo to already be sitting there. Nothing ever put one
there, so ``read_file`` and every shell command failed against a path that did
not exist.

Two things are set up here:

* **A git credential helper** that reads ``GH_TOKEN`` from the environment at
  invocation time. ``providers/github_app.ensure_token`` refreshes that variable
  in-process every poll, so clones, fetches and agent-initiated pushes all pick
  up a current App token without one ever being written to disk. This is
  deliberate — embedding a token in a remote URL persists it in plaintext in
  ``.git/config``, where anything running ``git remote -v`` can read it, and a
  GitHub App token additionally expires after an hour, so a persisted one is
  both a leak and a time bomb.
* **A commit identity**, without which ``git commit`` aborts with "Please tell
  me who you are" — the agent would do all its work and fail at the last step.

Known limitation: checkouts are keyed by ``repo_path`` from the registry, so two
jobs targeting the same repo share one working tree on the PVC. A CLEAN tree is
returned to the default branch, because it has nothing to lose and leaving it
parked on the previous job's branch is its own hazard: a branch cut from there
inherits commits the current job did not make, which auto_merge would then land
under an unrelated ticket. Concurrent jobs against a single repo still need
per-job worktrees; that arrives with K8s dispatch, which gives each Job its own
emptyDir.

A DIRTY tree is only reset when the caller passes ``reset_dirty``. The
distinction that matters is *live work* versus *orphaned dirt*, and this module
cannot tell them apart — it sees uncommitted files and nothing else. Refusing
unconditionally (the original behaviour) is safe for the first case and wedges
the repo forever in the second: a job that dies mid-edit leaves files behind, no
later job will clear them, and every subsequent job on that repo inherits them
into its diff. Only the engine knows whether an agent is actually running, so
the engine makes the call and this function honours it.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Reads GH_TOKEN at call time rather than baking a value in. Stored in
# ~/.gitconfig, which holds only this snippet — never the token itself.
_CREDENTIAL_HELPER = '!f() { echo username=x-access-token; echo "password=${GH_TOKEN}"; }; f'

_BOT_NAME = "minion-suite"
_BOT_EMAIL = "minion-suite@users.noreply.github.com"

_git_configured = False


async def _run_git(*args: str, cwd: str | None = None, timeout: int = 600) -> tuple[int, str]:
    """Run a git command, returning (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"git {args[0]} timed out after {timeout}s"

    return proc.returncode or 0, (stdout or b"").decode("utf-8", errors="replace").strip()


async def configure_git() -> None:
    """Install the credential helper and commit identity. Idempotent per process."""
    global _git_configured
    if _git_configured:
        return

    settings = [
        ("credential.https://github.com.helper", _CREDENTIAL_HELPER),
        ("user.name", _BOT_NAME),
        ("user.email", _BOT_EMAIL),
        # Agents create branches and push them; without this, `git push` on a
        # fresh branch errors out asking for an explicit upstream.
        ("push.default", "current"),
        # The PVC is shared and pods run as uid 1000; a checkout written by an
        # earlier pod would otherwise trip "detected dubious ownership".
        ("safe.directory", "*"),
    ]

    for key, value in settings:
        code, out = await _run_git("config", "--global", key, value, timeout=30)
        if code != 0:
            logger.warning("git config --global %s failed (%s): %s", key, code, out)
            return

    _git_configured = True
    logger.info("git configured: credential helper reads GH_TOKEN, commits authored by %s", _BOT_NAME)


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


async def ensure_checkout(
    clone_url: str,
    dest: str,
    default_branch: str = "main",
    reset_dirty: bool = False,
) -> bool:
    """Make sure `dest` holds a usable checkout of `clone_url`.

    Clones when absent. When present, fetches, then returns a CLEAN tree to
    `default_branch`. A DIRTY tree is left alone unless `reset_dirty` is set,
    which the caller does only once it has established that no agent is running
    and the uncommitted files are therefore orphaned — see the module docstring.
    Returns True when the checkout is usable.
    """
    if not clone_url:
        logger.warning("No clone_url configured for %s — agents will run against a bare directory", dest)
        return False

    await configure_git()
    path = Path(dest)

    if _is_git_repo(path):
        code, out = await _run_git("fetch", "origin", "--prune", cwd=dest)
        if code != 0:
            logger.warning("git fetch failed in %s (%s): %s", dest, code, out)
            return True  # An existing checkout is still usable offline.

        _, branch = await _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)
        _, dirty = await _run_git("status", "--porcelain", cwd=dest)
        if dirty and not reset_dirty:
            # Somebody may be mid-work here. Resetting would delete it.
            logger.warning(
                "Checkout %s has %d uncommitted file(s) on branch %s from a previous job — not resetting",
                dest,
                len(dirty.splitlines()),
                branch,
            )
            return True

        if dirty:
            # Orphaned dirt: the caller established that nothing is running, so
            # there is no work in progress to protect. `reset --hard` below
            # handles tracked modifications, but NOT untracked files — a dead
            # engineer that created new source files would otherwise leave them
            # for the next job to pick up in its diff. `-d` for directories,
            # deliberately no `-x`: ignored paths are build artefacts like
            # .venv/ and node_modules/, and re-installing them every job is a
            # cost with no correctness benefit.
            logger.warning(
                "Checkout %s: discarding %d uncommitted file(s) left on branch %s by a job that is no longer running",
                dest,
                len(dirty.splitlines()),
                branch,
            )
            code, out = await _run_git("clean", "-fd", cwd=dest)
            if code != 0:
                logger.warning("Could not clean untracked files in %s: %s", dest, out)

        if branch != default_branch:
            # Tree with nothing left to lose — either it was clean, or it was
            # dirty and the caller told us the owner is dead. Return it to base.
            #
            # The docstring's warning is about destroying uncommitted work; by
            # here there is none. Leaving it parked on the last job's branch is
            # its own hazard: job 2e9cd9e3 started with management-api still on
            # feat-job-7c2f5e39-management-api carrying that job's commit, so a
            # branch cut from there inherits work the current job did not do and
            # its ticket does not describe — which auto_merge would then land.
            #
            # It also confuses the agent. Finding its changes apparently already
            # made, it has nothing to commit, and the git steps go strange from
            # there.
            code, out = await _run_git("checkout", "--force", default_branch, cwd=dest)
            if code != 0:
                logger.warning("Could not return %s to %s: %s", dest, default_branch, out)
                return True
            code, out = await _run_git("reset", "--hard", f"origin/{default_branch}", cwd=dest)
            if code != 0:
                logger.warning("Could not reset %s to origin/%s: %s", dest, default_branch, out)
                return True
            logger.info("Checkout %s returned from stale branch %s to %s", dest, branch, default_branch)
        else:
            # Already on base — still fast-forward, or the agent starts from a
            # stale main and its diff includes commits already merged upstream.
            await _run_git("reset", "--hard", f"origin/{default_branch}", cwd=dest)
            logger.info("Checkout %s up to date on branch %s", dest, branch)
        return True

    # A non-repo directory means a previous clone died partway. Clear it rather
    # than letting git fail on a non-empty target.
    if path.exists() and any(path.iterdir()):
        logger.warning("Replacing non-repo directory at %s", dest)
        shutil.rmtree(path, ignore_errors=True)

    path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Cloning %s -> %s (branch %s)", clone_url, dest, default_branch)
    code, out = await _run_git("clone", "--branch", default_branch, clone_url, dest)
    if code != 0:
        # The token is never in the URL, so `out` is safe to log as-is.
        logger.error("Clone of %s failed (%s): %s", clone_url, code, out)
        return False

    logger.info("Cloned %s", dest)
    return True
