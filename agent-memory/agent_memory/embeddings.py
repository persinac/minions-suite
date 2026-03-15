"""Vendor-agnostic embedding helper — default LiteLLM implementation."""

import logging

logger = logging.getLogger(__name__)


class LiteLLMEmbeddingProvider:
    """EmbeddingProvider implementation using LiteLLM's aembedding()."""

    def __init__(self, model: str = "text-embedding-3-small", dims: int = 1536):
        self._model = model
        self._dims = dims

    @property
    def dimensions(self) -> int:
        return self._dims

    async def embed(self, text: str) -> list[float]:
        import litellm

        response = await litellm.aembedding(model=self._model, input=[text])
        return response.data[0]["embedding"]
