"""Station settings must load in production, not only in default.

[production.engine] REPLACES [default.engine] -- release 0.8.70 set a key in
default alone, deployed green, and the pod never saw it (#102). A station whose
pin and budget live only in default would, in production, have no model pin and
a ZERO budget, and would silently never run.

So this loads the real settings.toml through the real loader in a fresh
interpreter per environment -- the same path a pod takes -- rather than parsing
the file text.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import json
from minions.config import Config
c = Config.load()
print(json.dumps({"role_models": c.role_models, "station_budgets": c.station_budgets, "total": c.station_total_daily_usd}))
"""


def _load(env: str) -> dict:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("STATION_")}
    environ["ENV_FOR_DYNACONF"] = env
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=environ, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("env", ["development", "production"])
def test_every_environment_pins_and_budgets_the_scout(env):
    loaded = _load(env)

    assert loaded["role_models"].get("scout"), f"{env}: scout has no pinned model"
    budget = loaded["station_budgets"].get("scout") or {}
    assert budget.get("daily_usd", 0) > 0, f"{env}: scout has a zero budget and would never run"
    assert budget.get("max_runs_per_day", 0) > 0, f"{env}: scout may run zero times a day"


def test_production_and_default_agree():
    """The two copies exist only because dynaconf cannot merge them. They must
    say the same thing, or production quietly runs a different factory."""
    assert _load("production") == _load("development")


def test_the_agreed_ceiling_holds():
    """$3/day across all stations, agreed 2026-10-03. Per-station caps that sum
    past it would let the total cap, not the station caps, do all the work."""
    loaded = _load("production")
    assert loaded["total"] <= 3.0
    assert loaded["station_budgets"]["scout"]["daily_usd"] <= loaded["total"]
