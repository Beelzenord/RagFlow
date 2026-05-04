"""OpenAI-compatible embedding client. Works against OpenAI, Azure OpenAI, or any
provider exposing /v1/embeddings (e.g. Together, Ollama with the openai shim)."""
from typing import Sequence
import httpx
from .settings import settings


class EmbeddingClient:
    def __init__(self) -> None:
        if not settings.embedding_api_key:
            raise RuntimeError("EMBEDDING_API_KEY is not set")
        self._client = httpx.AsyncClient(
            base_url=settings.embedding_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
            timeout=60.0,
        )
        self._model = settings.embedding_model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.post("/embeddings", json={"model": self._model, "input": list(texts)})
        resp.raise_for_status()
        data = resp.json()["data"]
        # API returns items in input order
        return [item["embedding"] for item in data]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()
