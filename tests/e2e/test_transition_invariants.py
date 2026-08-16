"""Properties the state machine must have, computed rather than enumerated.

The scenario tests next door prove the paths someone thought to write down. That
leaves the opposite risk uncovered: an edge nobody thought about. A transition to
a status that does not exist, a state no job can reach, a state no job can leave
-- none of these break any existing test, because no existing test goes there.
They surface in production as a job that stops advancing and no error anywhere.

So these tests derive their expectations from the maps themselves. Adding an edge
to JOB_TRANSITIONS does not require adding a test here; if the new edge breaks a
property, the property fails on its own.

A note on what "reachable" means: reachability is computed over the transition
maps alone. That an edge exists does not mean any code drives it -- the maps
describe what is *permitted*, and permission is what these tests check.
"""

import pytest

from minions.core.models import JobStatus, TaskStatus
from minions.core.state_transitions import (
    JOB_TRANSITIONS,
    ROLE_RESTRICTED_TASK_TRANSITIONS,
    ROLE_TASK_TRANSITIONS,
    SUBTASK_TRANSITIONS,
    TASK_PRECONDITIONS,
    TASK_TRANSITIONS,
)

ALL_MAPS = {
    "job": JOB_TRANSITIONS,
    "task": TASK_TRANSITIONS,
    "subtask": SUBTASK_TRANSITIONS,
    **{f"task[{role}]": m for role, m in ROLE_TASK_TRANSITIONS.items()},
}

JOB_START = "spec_received"
TASK_START = "pending"
SUBTASK_START = "pending"

STARTS = {
    "job": JOB_START,
    "task": TASK_START,
    "subtask": SUBTASK_START,
    **{f"task[{role}]": TASK_START for role in ROLE_TASK_TRANSITIONS},
}


def _reachable_from(transitions: dict[str, set[str]], start: str) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        for nxt in transitions.get(stack.pop(), set()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _can_reach_terminal(transitions: dict[str, set[str]], state: str) -> bool:
    terminals = {s for s, outs in transitions.items() if not outs}
    return bool(_reachable_from(transitions, state) & terminals)


@pytest.mark.parametrize("name,transitions", sorted(ALL_MAPS.items()))
class TestEveryMapIsWellFormed:
    def test_every_target_state_is_itself_a_known_state(self, name, transitions):
        """A target missing from the map's own keys is a dead end or a typo.

        `validate_*_transition` looks the target up with `.get(state, set())`, so
        a misspelled target does not raise -- it silently becomes a state with no
        way out, and the job parks there permanently.
        """
        known = set(transitions)
        for source, targets in transitions.items():
            unknown = sorted(targets - known)
            assert not unknown, f"{name}: {source} -> {unknown} names states absent from the map"

    def test_no_state_transitions_to_itself(self, name, transitions):
        """A self-loop lets a retry re-enter the same state forever without progress."""
        loops = sorted(s for s, targets in transitions.items() if s in targets)
        assert not loops, f"{name}: self-transitions on {loops}"

    def test_every_state_is_reachable_from_the_start(self, name, transitions):
        """An unreachable state is dead vocabulary that still passes validation."""
        orphans = sorted(set(transitions) - _reachable_from(transitions, STARTS[name]))
        assert not orphans, f"{name}: {orphans} cannot be reached from {STARTS[name]!r}"

    def test_every_state_can_still_reach_a_terminal(self, name, transitions):
        """No trap states: from anywhere, some path must end.

        A cycle with no exit is the worst failure this machine can have, because
        nothing errors -- the job simply advances forever between two states.
        """
        trapped = sorted(s for s in transitions if not _can_reach_terminal(transitions, s))
        assert not trapped, f"{name}: {trapped} can never reach a terminal state"

    def test_at_least_one_terminal_state_exists(self, name, transitions):
        assert any(not outs for outs in transitions.values()), f"{name}: no terminal state"


class TestStatusVocabulary:
    """The maps and the enums must agree, in both directions."""

    def test_job_map_uses_only_real_job_statuses(self):
        valid = {s.value for s in JobStatus}
        used = set(JOB_TRANSITIONS) | {t for outs in JOB_TRANSITIONS.values() for t in outs}
        assert not used - valid, f"JOB_TRANSITIONS names non-JobStatus values: {sorted(used - valid)}"

    def test_every_job_status_appears_in_the_map(self):
        """A status the enum defines but the map omits can be written to the database
        and then never legally left -- validation reads `.get(status, set())`."""
        valid = {s.value for s in JobStatus}
        assert not valid - set(JOB_TRANSITIONS), f"JobStatus values missing from JOB_TRANSITIONS: {sorted(valid - set(JOB_TRANSITIONS))}"

    def test_task_maps_use_only_real_task_statuses(self):
        valid = {s.value for s in TaskStatus}
        for name, m in {"default": TASK_TRANSITIONS, **ROLE_TASK_TRANSITIONS}.items():
            used = set(m) | {t for outs in m.values() for t in outs}
            assert not used - valid, f"task map {name!r} names non-TaskStatus values: {sorted(used - valid)}"


class TestRoleOverridesAreConsistent:
    def test_a_role_map_never_permits_what_the_default_forbids(self):
        """Role maps exist to narrow the default, not to widen it.

        `database_engineer` skips the PR cycle -- fewer edges, not different ones.
        An edge present only in a role map is a second, divergent state machine
        that nothing else in the system knows about.
        """
        for role, m in ROLE_TASK_TRANSITIONS.items():
            for source, targets in m.items():
                extra = sorted(targets - TASK_TRANSITIONS.get(source, set()))
                assert not extra, f"role {role!r} permits {source} -> {extra}, which the default map forbids"

    def test_role_restrictions_name_edges_that_exist(self):
        """A restriction on an impossible edge is never consulted and misleads the reader."""
        for (source, target), roles in ROLE_RESTRICTED_TASK_TRANSITIONS.items():
            in_default = target in TASK_TRANSITIONS.get(source, set())
            in_role_map = any(target in m.get(source, set()) for m in ROLE_TASK_TRANSITIONS.values())
            assert in_default or in_role_map, f"restriction on {source} -> {target} (roles {sorted(roles)}) but no map allows it"

    def test_restricted_edges_name_at_least_one_role(self):
        for edge, roles in ROLE_RESTRICTED_TASK_TRANSITIONS.items():
            assert roles, f"{edge} is restricted to nobody, which forbids it for everyone"


class TestPreconditions:
    def test_preconditions_apply_to_states_that_exist(self):
        valid = {s.value for s in TaskStatus}
        unknown = sorted(set(TASK_PRECONDITIONS) - valid)
        assert not unknown, f"TASK_PRECONDITIONS gates unknown statuses: {unknown}"

    def test_preconditions_apply_to_reachable_states(self):
        """A precondition on an unreachable state protects nothing."""
        reachable = _reachable_from(TASK_TRANSITIONS, TASK_START)
        unreachable = sorted(set(TASK_PRECONDITIONS) - reachable)
        assert not unreachable, f"TASK_PRECONDITIONS gates unreachable statuses: {unreachable}"

    def test_required_fields_are_real_task_fields(self):
        from minions.core.models import Task

        fields = set(Task.model_fields)
        for status, required in TASK_PRECONDITIONS.items():
            missing = sorted(required - fields)
            assert not missing, f"precondition for {status!r} requires non-existent Task fields: {missing}"


def test_the_invariants_can_fail():
    """Proof the helpers detect what they claim to.

    Every property above is a `not` assertion over a computed set, which is the
    shape that passes silently when the computation is wrong. A synthetic broken
    machine keeps that honest.
    """
    trap = {"a": {"b"}, "b": {"a"}}  # cycle, no terminal
    assert not _can_reach_terminal(trap, "a")

    orphan = {"start": {"mid"}, "mid": set(), "island": {"mid"}}
    assert "island" not in _reachable_from(orphan, "start")

    healthy = {"start": {"end"}, "end": set()}
    assert _can_reach_terminal(healthy, "start")
    assert _reachable_from(healthy, "start") == {"start", "end"}
