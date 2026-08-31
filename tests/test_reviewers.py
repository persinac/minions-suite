"""Which specialists a PR wakes.

Conditionality is the cost control: each reviewer is a full agent run, so a
Python-only test PR should wake three, not five. Over-firing costs money;
under-firing loses the lens that would have caught the bug.

Every specialist is gated now, `api` and `backend-architecture` included.
That's the fix for job 3945783f (2026-08-31): on a pure-C firmware diff, `api`
correctly self-abstained but still cost a full run, and `backend-architecture`
had no abstention path and rubber-stamped a diff its checklist didn't apply to.
`backend-architecture` stays the broad default — it fires on everything except
a diff that's 100% frontend/UI files — but it's a gate, not a given.

The DBA and API triggers are both content-based as well as path-based. A
migration is obvious from its path; a lock-taking ALTER TABLE inside ordinary
application code, or a FastAPI route defined in main.py, is not — and those
are exactly the cases each specialist exists to catch.
"""

import pytest

from minions.reviewers import (
    API,
    BACKEND_ARCHITECTURE,
    DBA,
    FRONTEND,
    PYTHONISTA,
    infer_specialists,
    skipped_specialists,
)


class TestApiGating:
    def test_fires_on_api_path(self):
        assert API in infer_specialists(["app/api/routes/users.py"])

    def test_fires_on_proto_files(self):
        assert API in infer_specialists(["proto/user.proto"])

    def test_fires_on_openapi_spec(self):
        assert API in infer_specialists(["docs/openapi.yaml"])

    def test_fires_on_fastapi_decorator_outside_an_api_path(self):
        diff = "+@app.get('/health')\n+def health():\n+    return 'ok'\n"
        assert API in infer_specialists(["app/main.py"], diff)

    def test_fires_on_router_include(self):
        diff = "+app.include_router(users_router)\n"
        assert API in infer_specialists(["app/main.py"], diff)

    def test_silent_without_signal(self):
        diff = "+def helper():\n+    return 1\n"
        assert API not in infer_specialists(["app/service.py"], diff)

    def test_silent_on_docs_only(self):
        assert API not in infer_specialists(["README.md", "docs/guide.md"])

    def test_silent_on_firmware_c(self):
        """Job 3945783f: api ran on an ESP-IDF C diff and correctly returned
        N/A, but that was still a wasted agent run — it should never have
        been invoked at all."""
        assert API not in infer_specialists(["src/atecc_hex_utils.c"])

    def test_no_diff_falls_back_to_path(self):
        assert API not in infer_specialists(["app/main.py"])
        assert API in infer_specialists(["app/api/main.py"])


class TestBackendGating:
    """The broad default: fires on anything that isn't 100% frontend/UI."""

    @pytest.mark.parametrize(
        "files",
        [
            ["app/main.py"],
            ["README.md"],
            ["src/atecc_hex_utils.c"],
            ["Makefile"],
            ["db/migrations/1.sql"],
        ],
    )
    def test_fires_on_non_frontend(self, files):
        assert BACKEND_ARCHITECTURE in infer_specialists(files)

    def test_silent_on_pure_frontend(self):
        assert BACKEND_ARCHITECTURE not in infer_specialists(["src/App.tsx"])

    def test_one_non_frontend_file_is_enough_to_wake_it(self):
        """'Almost everything' means the bar for exclusion is 100% frontend,
        not majority frontend."""
        assert BACKEND_ARCHITECTURE in infer_specialists(["src/App.tsx", "app/api.py"])

    def test_empty_diff_wakes_nobody(self):
        assert infer_specialists([]) == []


class TestPythonista:
    def test_fires_on_python(self):
        assert PYTHONISTA in infer_specialists(["app/crud/play_transaction.py"])

    def test_silent_without_python(self):
        assert PYTHONISTA not in infer_specialists(["src/App.tsx", "README.md"])


class TestFrontend:
    @pytest.mark.parametrize("path", ["src/App.tsx", "src/App.jsx", "src/util.ts", "src/util.js"])
    def test_fires_on_component_extensions(self, path):
        assert FRONTEND in infer_specialists([path])

    def test_type_declarations_do_not_count(self):
        """.d.ts has no component logic — waking a reviewer on it is pure cost."""
        assert FRONTEND not in infer_specialists(["types/api.d.ts"])

    def test_a_real_ts_file_alongside_a_declaration_still_fires(self):
        assert FRONTEND in infer_specialists(["types/api.d.ts", "src/App.tsx"])


class TestDbaPathSignals:
    @pytest.mark.parametrize(
        "path",
        [
            "database/pgsql/migrations/20260725_add_col.sql",
            "app/migrations/0003_auto.py",
            "alembic/versions/abc123_add_index.py",
            "db/migrate/20260101_create.rb",
        ],
    )
    def test_fires_on_migration_paths(self, path):
        assert DBA in infer_specialists([path])

    def test_silent_on_ordinary_code(self):
        assert DBA not in infer_specialists(["app/routes/health.py"], diff="+def health():\n+    return 'ok'\n")


class TestDbaContentSignals:
    """The reason this is not path-only."""

    @pytest.mark.parametrize(
        "line",
        [
            "+    op.add_column('users', sa.Column('email', sa.String()))",
            "+    rows = session.query(Play).filter(Play.user_id == uid).all()",
            "+    cursor.execute('SELECT id FROM plays WHERE user_id = %s', (uid,))",
            "+    qs = Play.objects.filter(user_id=uid)",
            "+    await db.execute(stmt)",
            "+    conn.exec_driver_sql('ALTER TABLE plays ADD COLUMN x int')",
        ],
    )
    def test_orm_and_sql_in_application_code_wakes_the_dba(self, line):
        assert DBA in infer_specialists(["app/service.py"], diff=line)

    def test_bare_alter_table_wakes_the_dba(self):
        diff = "+ALTER TABLE plays ADD COLUMN promo boolean NOT NULL DEFAULT false;"

        assert DBA in infer_specialists(["scripts/fix.txt"], diff=diff)

    def test_only_added_lines_count(self):
        """A PR that DELETES the last raw query should not wake a DBA."""
        diff = "-    cursor.execute('SELECT * FROM plays')\n+    rows = repo.list_plays()\n"

        assert DBA not in infer_specialists(["app/service.py"], diff=diff)

    def test_diff_header_lines_are_not_content(self):
        """`+++ b/migrations.py` is a header, not an added line."""
        diff = "--- a/app/readme.md\n+++ b/app/readme.md\n+Some prose about updating things.\n"

        assert DBA not in infer_specialists(["app/readme.md"], diff=diff)

    def test_the_word_update_alone_does_not_fire(self):
        """Substring matching on UPDATE would fire on 'updated_at' everywhere."""
        diff = "+    record.updated_at = now()\n+    # update the cache\n"

        assert DBA not in infer_specialists(["app/service.py"], diff=diff)

    def test_no_diff_falls_back_to_paths(self):
        assert DBA not in infer_specialists(["app/service.py"])
        assert DBA in infer_specialists(["app/migrations/0001.py"])


class TestRealPrs:
    def test_the_wallet_api_pr(self):
        """4 Python test files, no migrations, no frontend, no API surface —
        despite the project being called 'wallet-api', nothing in this diff
        is a route or contract."""
        files = [
            "tests/conftest.py",
            "tests/test_play_transaction_crud.py",
            "tests/fixtures/db.py",
            "pyproject.toml",
        ]
        diff = "+    rows = session.query(PlayTransaction).filter_by(user_id=uid).all()\n"

        selected = infer_specialists(files, diff)

        assert set(selected) == {BACKEND_ARCHITECTURE, PYTHONISTA, DBA}
        assert API not in selected
        assert FRONTEND not in selected
        assert sorted(skipped_specialists(selected)) == sorted([API, FRONTEND])

    def test_a_frontend_only_pr_wakes_only_the_frontend_reviewer(self):
        selected = infer_specialists(["src/components/Cart.tsx"], diff="+const [x, setX] = useState(0)\n")

        assert selected == [FRONTEND]
        assert sorted(skipped_specialists(selected)) == sorted([API, BACKEND_ARCHITECTURE, PYTHONISTA, DBA])

    def test_the_esp_common_pr(self):
        """Job 3945783f, for real: pure C, no API surface, not frontend."""
        files = ["src/atecc_hex_utils.c", "src/atecc_utils_crypto.c"]
        diff = "+#define DEV_PRINT 0\n"

        selected = infer_specialists(files, diff)

        assert selected == [BACKEND_ARCHITECTURE]


class TestPrompts:
    def test_every_specialty_has_a_prompt(self):
        """A specialty with no prompt file would fan out into an empty persona."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "prompts" / "reviewers"
        for specialty in (API, BACKEND_ARCHITECTURE, DBA, PYTHONISTA, FRONTEND):
            assert (root / f"{specialty}.md").is_file(), f"missing prompt for {specialty}"

    def test_every_prompt_states_its_verdict_contract(self):
        """Aggregation reads these verdicts; a persona without one cannot vote."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "prompts" / "reviewers"
        for path in root.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            assert "Verdict[" in text, f"{path.name} has no verdict line"
            assert "REQUEST_CHANGES" in text, f"{path.name} cannot request changes"

    def test_backend_architecture_has_an_na_path(self):
        """It rubber-stamped job 3945783f precisely because it had no way to
        say 'nothing in my domain changed' — api already had this."""
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "prompts" / "reviewers" / "backend-architecture.md"
        assert "N/A" in path.read_text(encoding="utf-8")


class TestAggregation:
    """Any REQUEST_CHANGES blocks; N/A abstains; silence never approves."""

    @staticmethod
    def _v(verdicts):
        from minions.reviewers import aggregate_verdicts

        return aggregate_verdicts(verdicts)

    def test_all_approve(self):
        verdict, reason = self._v({API: "approve", BACKEND_ARCHITECTURE: "approve"})

        assert verdict == "approve"
        assert "api" in reason

    def test_one_blocker_beats_four_approvals(self):
        """A DBA objection is not outvoted by reviewers who never looked at SQL."""
        verdict, reason = self._v(
            {API: "approve", BACKEND_ARCHITECTURE: "approve", PYTHONISTA: "approve", FRONTEND: "approve", DBA: "request_changes"}
        )

        assert verdict == "request_changes"
        assert "dba" in reason

    def test_discuss_beats_approve_but_loses_to_blocking(self):
        assert self._v({API: "approve", DBA: "discuss"})[0] == "discuss"
        assert self._v({API: "discuss", DBA: "request_changes"})[0] == "request_changes"

    def test_na_abstains_rather_than_approving(self):
        verdict, reason = self._v({API: "approve", DBA: "n/a"})

        assert verdict == "approve"
        assert "n/a: dba" in reason

    def test_all_na_does_not_approve(self):
        """Nobody reviewed anything — that is not consent to merge."""
        verdict, reason = self._v({API: "n/a", DBA: "n/a"})

        assert verdict == "request_changes"
        assert "nothing was actually reviewed" in reason

    def test_a_missing_verdict_fails_closed(self):
        """Same rule as the single-reviewer path: silence is not approval."""
        verdict, reason = self._v({API: "approve", DBA: None})

        assert verdict == "request_changes"
        assert "dba" in reason

    def test_an_unparseable_verdict_fails_closed(self):
        verdict, _ = self._v({API: "approve", DBA: "lgtm probably"})

        assert verdict == "request_changes"

    def test_no_reviewers_at_all_fails_closed(self):
        assert self._v({})[0] == "request_changes"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("APPROVE", "approve"),
            ("Approved", "approve"),
            ("REQUEST_CHANGES", "request_changes"),
            ("request-changes", "request_changes"),
            ("changes_requested", "request_changes"),
            ("  DISCUSS  ", "discuss"),
            ("N/A", "n/a"),
        ],
    )
    def test_verdict_spellings_the_personas_actually_emit(self, raw, expected):
        from minions.reviewers import normalise_verdict

        assert normalise_verdict(raw) == expected


class TestFanoutCap:
    """Narrowing the panel — `cap_specialists`, set to 2 on 2026-08-20.

    The priority rule is "signal wins, blast radius breaks ties": every
    specialist here fires on a real signal now, so the ranking among
    conditionals reflects how costly missing that lens tends to be — a DBA or
    frontend finding usually outweighs an idiom nit or a contract nit if only
    one slot is left. `backend-architecture` is the anchor: the one lens broad
    enough to never be worth dropping.

    These tests pin the *consequences* of that choice, not just the arithmetic.
    A cap that quietly kept two generalists would satisfy `len(kept) == 2` while
    dropping every lens the diff actually asked for.
    """

    def _cap(self, files, diff="", limit=2):
        from minions.reviewers import cap_specialists

        return cap_specialists(infer_specialists(files, diff), limit)

    def test_python_pr_keeps_the_pythonista(self):
        assert self._cap(["app/crud/play.py"]) == [BACKEND_ARCHITECTURE, PYTHONISTA]

    def test_pure_frontend_pr_wakes_only_frontend_nothing_to_cap(self):
        assert self._cap(["src/App.tsx"]) == [FRONTEND]

    def test_sql_only_pr_keeps_the_dba(self):
        assert self._cap(["db/migrations/001_add_index.sql"]) == [BACKEND_ARCHITECTURE, DBA]

    def test_content_triggered_dba_survives_the_cap(self):
        """The DBA's content trigger is why `reviewers.py` exists at all.

        A lock-taking ALTER TABLE inside ordinary application code has no
        tell-tale path. If the cap dropped it here, the module's stated
        reason for being would be dead code at the default width.
        """
        diff = "+    db.execute('ALTER TABLE plays ADD COLUMN note text')\n"
        kept = self._cap(["app/service.py"], diff)
        assert DBA in kept or PYTHONISTA in kept
        assert API not in kept

    def test_api_yields_to_a_signal_specialist_when_both_fire(self):
        """Even when api genuinely fires (real contract signal), it's still
        the lowest priority among conditionals — a contract nit is the
        easiest of the four to live without for one round.

        Non-.py file on purpose: a .py file would also wake pythonista and
        muddy the api-vs-dba comparison this test is isolating.
        """
        diff = '+    db.Exec("ALTER TABLE plays ADD COLUMN y int")\n'
        wanted = infer_specialists(["app/api/routes.go"], diff)
        assert API in wanted and DBA in wanted and PYTHONISTA not in wanted

        kept = self._cap(["app/api/routes.go"], diff)
        assert DBA in kept
        assert API not in kept

    def test_anchor_is_never_dropped_when_it_fires(self):
        for files, diff in (
            (["app/crud/play.py"], ""),
            (["db/migrations/1.sql"], ""),
            (["README.md"], ""),
        ):
            assert BACKEND_ARCHITECTURE in self._cap(files, diff)

    def test_pure_frontend_correctly_excludes_the_anchor(self):
        """Not a cap drop — backend-architecture never fires on a 100%
        frontend diff, so there's nothing for the cap to preserve."""
        assert BACKEND_ARCHITECTURE not in infer_specialists(["src/App.tsx"])

    def test_cap_binds_at_the_limit(self):
        """A PR that wakes everything still yields exactly `limit` reviewers."""
        files = ["app/crud/play.py", "src/App.tsx", "db/migrations/1.sql", "app/api/handlers.py"]
        assert len(infer_specialists(files)) == 5
        assert len(self._cap(files)) == 2

    @pytest.mark.parametrize("limit", [0, -1])
    def test_non_positive_limit_means_uncapped(self, limit):
        """Matches the cost-ceiling convention, so the cap can be turned off."""
        files = ["app/crud/play.py", "src/App.tsx"]
        assert self._cap(files, limit=limit) == infer_specialists(files)

    def test_slack_limit_returns_the_list_unchanged(self):
        """Raising the cap restores byte-identical behaviour, not a reordering."""
        from minions.reviewers import cap_specialists

        wanted = infer_specialists(["app/crud/play.py", "src/App.tsx"])
        assert cap_specialists(wanted, 99) == wanted
        assert cap_specialists(wanted, len(wanted)) == wanted

    def test_capped_specialists_reports_what_was_dropped(self):
        """The dropped names are the only trace a narrowed gate leaves.

        A reviewer that never ran cannot report what it would have found, so
        `capped=` in the audit event is the sole evidence the panel was smaller
        than the diff called for.
        """
        from minions.reviewers import capped_specialists

        files = ["app/crud/play.py", "src/App.tsx", "app/api/handlers.py"]
        wanted = infer_specialists(files)
        kept = self._cap(files)
        dropped = capped_specialists(wanted, kept)

        assert API in dropped
        assert FRONTEND in dropped
        assert set(kept) & set(dropped) == set()
        assert set(kept) | set(dropped) == set(wanted)

    def test_capped_is_empty_when_the_cap_does_not_bind(self):
        from minions.reviewers import capped_specialists

        wanted = infer_specialists(["README.md"])
        assert capped_specialists(wanted, self._cap(["README.md"])) == []
