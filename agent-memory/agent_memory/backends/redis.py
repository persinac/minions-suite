"""Redis backend for the L2 TupleSpace — RedisJSON + RediSearch."""

import json
import logging

logger = logging.getLogger(__name__)

# Lua script for atomic pop: search → get → delete in one round-trip
_ATOMIC_POP_LUA = """
local results = redis.call('FT.SEARCH', KEYS[1], ARGV[1], 'LIMIT', '0', '1')
if results[1] == 0 then
    return nil
end
local doc_key = results[2]
local doc = redis.call('JSON.GET', doc_key)
redis.call('DEL', doc_key)
return doc
"""


class RedisTupleSpaceBackend:
    """TupleSpaceBackend implementation using redis.asyncio + RedisJSON + RediSearch."""

    def __init__(self, url: str = "redis://localhost:6379", password: str | None = None):
        self._url = url
        self._password = password
        self._client = None
        self._pop_sha: str | None = None

    async def connect(self) -> None:
        import redis.asyncio as aioredis

        kwargs = {"decode_responses": True}
        if self._password:
            kwargs["password"] = self._password
        self._client = aioredis.from_url(self._url, **kwargs)
        # Pre-load the atomic pop Lua script
        self._pop_sha = await self._client.script_load(_ATOMIC_POP_LUA)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def put(self, key: str, doc: dict, ttl: int | None = None) -> None:
        await self._client.json().set(key, "$", doc)
        if ttl is not None:
            await self._client.expire(key, ttl)

    async def get(self, key: str) -> dict | None:
        result = await self._client.json().get(key)
        return result

    async def delete(self, key: str) -> bool:
        return bool(await self._client.delete(key))

    async def search(self, index: str, query: str, limit: int = 20) -> list[dict]:
        try:
            result = await self._client.ft(index).search(query)
        except Exception:
            return []

        docs = []
        for doc in result.docs:
            try:
                raw = doc.json if hasattr(doc, "json") else doc.__dict__.get("json", "{}")
                if isinstance(raw, str):
                    parsed = json.loads(raw)
                else:
                    parsed = raw
                docs.append(parsed)
            except json.JSONDecodeError, AttributeError:
                # Try to build from individual fields
                d = {}
                for field in ("project", "category", "key", "value", "tags", "agent_role", "job_id", "timestamp"):
                    val = getattr(doc, field, None)
                    if val is not None:
                        d[field] = val
                if d:
                    docs.append(d)
        return docs[:limit]

    async def atomic_pop(self, index: str, query: str) -> dict | None:
        try:
            result = await self._client.evalsha(self._pop_sha, 1, index, query)
        except Exception:
            # Fallback: search + get + delete (not fully atomic without Lua)
            results = await self.search(index, query, limit=1)
            if not results:
                return None
            # We need the key to delete it — search again to get keys
            try:
                search_result = await self._client.ft(index).search(query)
                if not search_result.docs:
                    return None
                doc_key = search_result.docs[0].id
                doc = await self.get(doc_key)
                await self.delete(doc_key)
                return doc
            except Exception:
                return None
        else:
            if result is None:
                return None
            if isinstance(result, str):
                return json.loads(result)
            return result

    async def keys(self, pattern: str) -> list[str]:
        result = []
        async for key in self._client.scan_iter(match=pattern):
            result.append(key)
        return result

    async def create_index(self, name: str, schema: dict) -> None:
        """Create a RediSearch index if it doesn't already exist."""
        from redis.commands.search.field import NumericField, TagField, TextField

        field_map = {
            "TAG": TagField,
            "TEXT": TextField,
            "NUMERIC SORTABLE": lambda n: NumericField(n, sortable=True),
        }

        fields = []
        for field_name, field_type in schema.items():
            factory = field_map.get(field_type)
            if factory:
                fields.append(factory(f"$.{field_name}", as_name=field_name))

        try:
            await self._client.ft(name).info()
            logger.debug("Index '%s' already exists", name)
        except Exception:
            from redis.commands.search.indexDefinition import IndexDefinition, IndexType

            definition = IndexDefinition(prefix=["fact:"], index_type=IndexType.JSON)
            await self._client.ft(name).create_index(fields, definition=definition)
            logger.info("Created RediSearch index '%s'", name)
