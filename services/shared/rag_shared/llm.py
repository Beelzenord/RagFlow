"""Provider-agnostic chat completion wrapper. Supports OpenAI-compatible
(`/v1/chat/completions`) and Anthropic (`/v1/messages`)."""
from __future__ import annotations

import json
from typing import AsyncIterator, Literal

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

    async def stream(
        self, system: str, user: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        """Yield text deltas as they arrive from the upstream provider.

        Each yielded string is a partial chunk of the assistant message; concat
        all chunks to reconstruct the full answer.
        """
        if self._provider == "anthropic":
            async for delta in self._stream_anthropic(system, user, max_tokens):
                yield delta
            return
        async for delta in self._stream_openai(system, user, max_tokens):
            yield delta

    async def _stream_openai(
        self, system: str, user: str, max_tokens: int
    ) -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            await _raise_for_stream_status(resp)
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta

    async def _stream_anthropic(
        self, system: str, user: str, max_tokens: int
    ) -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "stream": True,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        async with self._client.stream("POST", "/v1/messages", json=payload) as resp:
            await _raise_for_stream_status(resp)
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "content_block_delta":
                    continue
                delta_obj = obj.get("delta") or {}
                text = delta_obj.get("text")
                if text:
                    yield text

    async def aclose(self) -> None:
        await self._client.aclose()


async def _raise_for_stream_status(resp: httpx.Response) -> None:
    """Surface upstream errors before iterating the stream body."""
    if resp.status_code >= 400:
        body = await resp.aread()
        raise httpx.HTTPStatusError(
            f"upstream {resp.status_code}: {body.decode(errors='replace')[:500]}",
            request=resp.request,
            response=resp,
        )
