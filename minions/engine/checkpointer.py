"""LangGraph checkpointer factory — always uses PostgreSQL."""

import logging

from ..config import Config

logger = logging.getLogger(__name__)


async def create_checkpointer(config: Config | None = None):
    """Return an async LangGraph checkpointer backed by PostgreSQL.

    Note: The returned checkpointer manages its own connection pool.
    The caller is responsible for keeping a reference alive.
    """
    if config is None:
        config = Config.from_env()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    checkpointer = AsyncPostgresSaver.from_conn_string(config.postgres_url)
    saver = await checkpointer.__aenter__()
    await saver.setup()
    logger.info("LangGraph checkpointer: PostgreSQL")
    return saver
