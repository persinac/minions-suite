"""A station run must never occupy a line slot.

Intake counts `max_concurrent_jobs` against every non-terminal job. With the
cap at 1, a scout job that counted would stop ALL card intake for as long as it
ran -- a station that exists to feed the line would starve it instead. These
drive the real `_poll` of each poller against a fake DB holding one active
scout job, and assert a card is still launched.
"""

import pytest

from minions.config import Config
from minions.core.models import Job, JobStatus
from minions.core.stations import SCOUT
from minions.engine import anomaly_rules
from minions.providers.gitlab_issues import GitLabIssuesPoller
from minions.providers.trello import LIST_ONDECK, TrelloPoller


def _job(job_type: str) -> Job:
    return Job(spec="x", job_type=job_type, status=JobStatus.SCOUTING if job_type == SCOUT else JobStatus.DEV_IN_PROGRESS)


class _DB:
    def __init__(self, active: list[Job]):
        self.active = active

    async def get_active_jobs(self):
        return list(self.active)

    async def count_jobs_since(self, since):
        return 0

    async def get_tasks(self, job_id):
        return []


def _trello(active: list[Job]) -> tuple[TrelloPoller, list]:
    config = Config.from_env()
    config.max_concurrent_jobs = 1
    config.trello_min_job_interval = 0
    poller = TrelloPoller.__new__(TrelloPoller)
    poller.config = config
    poller.db = _DB(active)
    poller._active = {}
    poller._list_ids = {LIST_ONDECK: "ondeck"}
    launched: list = []

    async def _monitor_jobs():
        return None

    async def _get_cards(list_id, require_minion_label=False):
        return [{"id": "card-1", "name": "a card"}]

    async def _launch_job(card):
        launched.append(card["id"])

    poller._monitor_jobs = _monitor_jobs
    poller._get_cards = _get_cards
    poller._launch_job = _launch_job
    return poller, launched


@pytest.mark.asyncio
class TestTrelloCapacity:
    async def test_a_running_scout_does_not_block_intake(self):
        poller, launched = _trello([_job(SCOUT)])

        await poller._poll()

        assert launched == ["card-1"]

    async def test_a_running_line_job_still_blocks_intake(self):
        """The control: the filter removes stations, not the cap."""
        poller, launched = _trello([_job("development")])

        await poller._poll()

        assert launched == []


@pytest.mark.asyncio
class TestGitLabCapacity:
    async def test_a_running_scout_does_not_use_up_the_slots(self):
        config = Config.from_env()
        config.max_concurrent_jobs = 1
        poller = GitLabIssuesPoller.__new__(GitLabIssuesPoller)
        poller.config = config
        poller.db = _DB([_job(SCOUT)])
        poller._active = {}
        poller.projects = {}

        async def _monitor_jobs():
            return None

        poller._monitor_jobs = _monitor_jobs
        seen_slots: list[int] = []
        original_items = poller.projects.items

        class _Projects(dict):
            def items(self):
                seen_slots.append(1)
                return original_items()

        poller.projects = _Projects()

        await poller._poll()

        assert seen_slots == [1], "the poller returned early: the scout job was counted as a line slot"


@pytest.mark.asyncio
class TestStuckTaskRuleIgnoresStations:
    async def test_the_line_rule_never_looks_inside_a_station_job(self):
        asked: list[str] = []

        class _TaskDB(_DB):
            async def get_tasks(self, job_id):
                asked.append(job_id)
                return []

        scout = _job(SCOUT)
        line = _job("development")

        await anomaly_rules.check_stuck_tasks(_TaskDB([scout, line]))

        assert asked == [line.id]
