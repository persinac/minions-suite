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
jobs targeting the same repo share one working tree on the PVC. This function
therefore never resets an existing checkout — doing so would delete the
uncommitted work of a job already running there. It fetches instead and reports
what it found, leaving the branch state to the agent. Concurrent jobs against a
single repo still need per-job worktrees; that arrives with K8s dispatch, which
gives each Job its own emptyDir.
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


async def ensure_checkout(clone_url: str, dest: str, default_branch: str = "main") -> bool:
    """Make sure `dest` holds a usable checkout of `clone_url`.

    Clones when absent. When present, fetches but does NOT reset — see the
    module docstring. Returns True when the checkout is usable.
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
        if dirty:
            logger.warning(
                "Checkout %s has %d uncommitted file(s) on branch %s from a previous job — not resetting",
                dest,
                len(dirty.splitlines()),
                branch,
            )
        else:
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
