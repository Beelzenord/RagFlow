"""Provider-agnostic chat completion wrapper. Supports OpenAI-compatible
(`/v1/chat/completions`) and Anthropic (`/v1/messages`)."""
from typing import Literal
import httpx
from .settings import settings

Role = Literal["system", "user", "assistant"]


class LLMClient:
    def __init__(self) -> None:
        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is not set")
        self._provider = settings.llm_provider.lower()
        self._model = settings.llm_model

        if self._provider == "anthropic":
            self._client = httpx.AsyncClient(
                base_url=settings.llm_base_url.rstrip("/") or "https://api.anthropic.com",
                headers={
                    "x-api-key": settings.llm_api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=120.0,
            )
        else:  # openai-compatible
            self._client = httpx.AsyncClient(
                base_url=settings.llm_base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                timeout=120.0,
            )

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        if self._provider == "anthropic":
            resp = await self._client.post(
                "/v1/messages",
                json={
                    "model": self._model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return "".join(block.get("text", "") for block in data.get("content", []))

        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def aclose(self) -> None:
        await self._client.aclose()
