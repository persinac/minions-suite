"""The scout's first phase: cheap, deterministic signals. No model, no cost.

openspec/changes/factory-stations/design.md, "Scout". The model is expensive and
reads slowly; git is free and reads everything. So git picks WHERE to look and
the model only reads there:

- **hot spots** -- files changed most in the last 90 days, weighted by size. A
  big file that keeps needing change is where defects and rework concentrate.
- **TODO density** -- files carrying the most TODO / FIXME / HACK / XXX marks.
- **missing test oracle** -- the repo declares no `test_command`. Nothing the
  line ships there can be checked, so this outranks everything else.

Deferred, on purpose, and recorded in the signals as such so nobody mistakes an
absence for a clean result:

- lint and type output. Running a repo's `lint_command` needs its dependencies
  installed, which in the engine pod means a full `npm ci` / `uv sync` per repo
  per run. That is a sandbox question, not a signals question.
- red non-required CI checks. Needs the GitHub checks API per repo; cheap to add
  once the scout has shown its findings are worth the API calls.
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CHURN_DAYS = 90
TOP_N = 10

# Generated, vendored or lockfile paths. Churn there is noise: a lockfile
# "changes" every time a dependency moves and is never where a defect lives.
_NOISE_PARTS = ("node_modules/", "vendor/", "dist/", "build/", ".venv/", "__pycache__/", "migrations/", "managed_components/")
_NOISE_SUFFIXES = (".lock", "-lock.json", ".lockb", ".min.js", ".map", ".svg", ".png", ".jpg", ".pdf", ".snap")
_NOISE_NAMES = frozenset(
    {"package-lock.json", "uv.lock", "poetry.lock", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "dependencies.lock", "CHANGELOG.md"}
)

_MAX_FILE_BYTES = 1_000_000
_TODO_PATTERN = r"\b(TODO|FIXME|HACK|XXX)\b"


def is_noise(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in _NOISE_NAMES:
        return True
    if path.endswith(_NOISE_SUFFIXES):
        return True
    return any(part in path for part in _NOISE_PARTS)


async def _git(repo_dir: str, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo_dir,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _line_count(path: Path) -> int | None:
    """Lines in a text file, or None for a missing, huge or binary one."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


async def churn_hot_spots(repo_dir: str, days: int = CHURN_DAYS, top_n: int = TOP_N) -> list[dict]:
    """Files ranked by (commits touching them in `days`) x (current line count)."""
    code, out = await _git(repo_dir, "log", f"--since={days}.days", "--no-merges", "--name-only", "--format=")
    if code != 0:
        logger.warning("scout: git log failed in %s: %s", repo_dir, out[:200])
        return []
    commits: dict[str, int] = {}
    for line in out.splitlines():
        path = line.strip()
        if not path or is_noise(path):
            continue
        commits[path] = commits.get(path, 0) + 1

    spots = []
    root = Path(repo_dir)
    for path, count in commits.items():
        lines = _line_count(root / path)
        if not lines:
            continue  # deleted since, binary, or empty
        spots.append({"path": path, "commits": count, "lines": lines, "score": count * lines})
    spots.sort(key=lambda s: (-s["score"], s["path"]))
    return spots[:top_n]


async def todo_density(repo_dir: str, top_n: int = TOP_N) -> list[dict]:
    """Files with the most TODO / FIXME / HACK / XXX marks, tracked files only."""
    code, out = await _git(repo_dir, "grep", "-c", "-I", "-E", _TODO_PATTERN)
    # git grep exits 1 when nothing matches. That is a clean answer, not an error.
    if code not in (0, 1):
        logger.warning("scout: git grep failed in %s: %s", repo_dir, out[:200])
        return []
    counts = []
    for line in out.splitlines():
        path, _, n = line.rpartition(":")
        if not path or is_noise(path) or not n.isdigit():
            continue
        counts.append({"path": path, "todos": int(n)})
    counts.sort(key=lambda c: (-c["todos"], c["path"]))
    return counts[:top_n]


async def collect_signals(repo_dir: str, repo: str, service) -> dict:
    """Everything the model will be told about this repo before it reads a line."""
    hot_spots, todos = await asyncio.gather(churn_hot_spots(repo_dir), todo_density(repo_dir))
    return {
        "repo": repo,
        "churn_days": CHURN_DAYS,
        "hot_spots": hot_spots,
        "todos": todos,
        "missing_test_oracle": not (getattr(service, "test_command", "") or "").strip(),
        "test_command": getattr(service, "test_command", "") or "",
        "lint_command": getattr(service, "lint_command", "") or "",
        "language": getattr(service, "language", "") or "",
        "deferred": ["lint/type output (needs dependencies installed)", "red non-required CI checks"],
    }


def worth_a_model_call(signals: dict) -> bool:
    """Whether the signals point anywhere. If not, the run ends without spending."""
    return bool(signals.get("missing_test_oracle") or signals.get("hot_spots") or signals.get("todos"))


def render_signals(signals: dict, max_findings: int) -> str:
    """The signals as the prompt section the scout reads first."""
    lines = [
        "## Scout signals",
        "",
        f"Repo: `{signals['repo']}`. Measured by git before you started — read the code at these places.",
        f"You may file at most **{max_findings}** finding(s) this run.",
        "",
    ]
    if signals.get("missing_test_oracle"):
        lines += [
            "### ⚠️ No test command",
            "",
            "This repo declares no `test_command`. Nothing the line ships here can be checked.",
            "**Your first finding must be `kind=missing_test_oracle`.** Say which test framework fits the repo,",
            "the smallest first test worth writing, and the exact command that would become `test_command`.",
            "The tool refuses any other kind for this repo until that one is filed.",
            "",
        ]
    if signals.get("hot_spots"):
        lines += [f"### Hot spots (commits in {signals['churn_days']} days x lines)", ""]
        lines += [f"- `{s['path']}` — {s['commits']} commits, {s['lines']} lines" for s in signals["hot_spots"]]
        lines.append("")
    if signals.get("todos"):
        lines += ["### TODO / FIXME density", ""]
        lines += [f"- `{t['path']}` — {t['todos']} marks" for t in signals["todos"]]
        lines.append("")
    lines += [f"_Not measured this run: {', '.join(signals.get('deferred', []))}._"]
    return "\n".join(lines)
