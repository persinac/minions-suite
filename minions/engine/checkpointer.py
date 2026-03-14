"""LangGraph checkpointer factory — selects backend based on DB_BACKEND config."""

import logging

from ..config import Config

logger = logging.getLogger(__name__)


async def create_checkpointer(config: Config | None = None):
    """Return an async LangGraph checkpointer matching the configured DB backend.

    - PostgreSQL: AsyncPostgresSaver with the existing POSTGRES_URL
    - SQLite: AsyncSqliteSaver with the existing DB_PATH

    Note: The returned checkpointer manages its own connection pool.
    The caller is responsible for keeping a reference alive.
    """
    if config is None:
        config = Config.from_env()

    if config.db_backend == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer = AsyncPostgresSaver.from_conn_string(config.postgres_url)
        # from_conn_string returns an async context manager; enter it to get the saver
        saver = await checkpointer.__aenter__()
        await saver.setup()
        logger.info("LangGraph checkpointer: PostgreSQL")
        return saver

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    checkpointer = AsyncSqliteSaver.from_conn_string(config.db_path)
    saver = await checkpointer.__aenter__()
    await saver.setup()
    logger.info("LangGraph checkpointer: SQLite (%s)", config.db_path)
    return saver
