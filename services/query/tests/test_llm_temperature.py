"""Streamed answers carry the temperature they are given, for both providers,
and leave the provider's default when given none.

    cd services/query && python3 -m unittest discover -s tests -t .

The HTTP side is a stand-in transport, so no key or network is needed.
"""
from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from rag_shared.llm import LLMClient

STREAM_BODY = {
    "openai": 'data: {"choices": [{"delta": {"content": "Hej"}}]}\n\ndata: [DONE]\n\n',
    "anthropic": 'data: {"type": "content_block_delta", "delta": {"text": "Hej"}}\n\n',
}


def client(provider: str, sent: list[dict]) -> LLMClient:
    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=STREAM_BODY[provider])

    llm = LLMClient.__new__(LLMClient)  # skip __init__: it wants a real key
    llm._provider = provider
    llm._model = "test-model"
    llm._client = httpx.AsyncClient(base_url="http://llm.test", transport=httpx.MockTransport(handle))
    return llm


async def collect(llm: LLMClient, **kwargs) -> str:
    try:
        return "".join([d async for d in llm.stream("system", "question", **kwargs)])
    finally:
        await llm.aclose()


class StreamTemperatureTests(unittest.TestCase):
    def test_a_given_temperature_is_sent(self) -> None:
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                sent: list[dict] = []
                text = asyncio.run(collect(client(provider, sent), temperature=0.0))
                self.assertEqual(text, "Hej")
                self.assertEqual(sent[0]["temperature"], 0.0)

    def test_none_leaves_the_provider_default(self) -> None:
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                sent: list[dict] = []
                asyncio.run(collect(client(provider, sent)))
                self.assertNotIn("temperature", sent[0])


if __name__ == "__main__":
    unittest.main()
