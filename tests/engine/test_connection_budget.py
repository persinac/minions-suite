"""The connection budget must fit the server, and startup must not spend it.

The shared Postgres runs max_connections=25 (~22 usable after superuser
slots), and three processes load this config — engine, dashboard,
input-sources — so pool_max is a per-process cap. 3 x 10 = 30 did not fit by
construction; the engine poll loop ate a 30-second PoolTimeout on 2026-08-23
to prove it, and a peer session misdiagnosed that error into a whole-file
rewrite and an unbuilt release. The budget is now 3 x 6 = 18.

The other half of the spend was pure ceremony: startup built an
AsyncPostgresSaver — its own pool — ran setup(), and looped every active job
through a checkpoint lookup that could never succeed, because the graph is
compiled with checkpointer=None and no checkpoint was ever written. The pool
was never closed. The DB-status-driven poll loop is the real resume and has
carried every restart since the graph engine shipped.
"""

import inspect

from minions.config import Config
from minions.engine.job_engine import JobEngine

# Keep in sync with the comment in settings.toml [default.database]: three
# processes x pool_max must stay under ~22 usable server connections.
PROCESSES = 3
USABLE_SERVER_CONNECTIONS = 22


class TestPoolBudget:
    def test_the_default_budget_fits_the_server(self):
        config = Config.from_env()

        total = PROCESSES * config.postgres_pool_max
        assert total <= USABLE_SERVER_CONNECTIONS, (
            f"{PROCESSES} processes x pool_max={config.postgres_pool_max} = {total} "
            f"exceeds the ~{USABLE_SERVER_CONNECTIONS} usable connections on the shared server"
        )

    def test_pool_min_leaves_room_to_grow(self):
        config = Config.from_env()

        assert config.postgres_pool_min < config.postgres_pool_max


class TestStartupSpendsNoConnectionsOnCeremony:
    def test_startup_cleanup_builds_no_checkpointer(self):
        """The resume ceremony opened a second pool per boot and leaked it —
        for a lookup that could never succeed. It must not come back without
        the graph actually being compiled with a checkpointer."""
        source = inspect.getsource(JobEngine._startup_cleanup)

        assert "create_checkpointer" not in source
        assert "resume_from_checkpoint" not in source

    def test_the_checkpointer_module_is_gone(self):
        import importlib.util

        assert importlib.util.find_spec("minions.engine.checkpointer") is None, (
            "checkpointer.py was dead code with a connection-pool cost; a revival must come with a caller that closes its pool"
        )
