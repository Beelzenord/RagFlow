"""Tests for the voice turn: aggregator totality and visible failures.

    cd services/web && python3 -m unittest discover -s tests -t .

The first class needs only the standard library, because it guards the bug that
caused the rewrite: sentences vanishing between the screen and the speaker. The
rest exercise the HTTP surface and are skipped where fastapi and httpx are not
installed, so the documented command stays green without them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest import mock

from app import voice

try:
    import httpx
    from fastapi.testclient import TestClient

    HTTP_DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    HTTP_DEPS = False

try:
    import websockets.asyncio.client  # noqa: F401

    WS_DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    WS_DEPS = False


def collect(deltas: list[str], **kwargs: int) -> list[str]:
    """Everything the aggregator would hand to TTS, in order."""
    agg = voice.SentenceAggregator(**kwargs)
    out: list[str] = []
    for delta in deltas:
        out.extend(agg.push(delta))
    out.extend(agg.flush())
    return out


class AggregatorTotalityTests(unittest.TestCase):
    """Nothing may be dropped, duplicated, or reordered.

    The predecessor filtered out chunks under 20 characters, which silently
    deleted short Swedish sentences from the audio while leaving them on screen.
    Every case here asserts the same property: joining what went to TTS
    reproduces the answer exactly.
    """

    def assert_total(self, deltas: list[str], **kwargs: int) -> list[str]:
        spoken = collect(deltas, **kwargs)
        self.assertEqual("".join(spoken), "".join(deltas))
        return spoken

    def test_short_swedish_sentences_survive(self) -> None:
        # Each of these is under the old 20-character minimum, so the old code
        # consumed them without ever speaking them.
        self.assert_total(["Ja. ", "Det stämmer. ", "Nej. ", "Priset är 4 500 kronor."])

    def test_single_short_sentence_is_still_spoken(self) -> None:
        self.assertEqual(self.assert_total(["Ja."]), ["Ja."])

    def test_token_sized_deltas(self) -> None:
        answer = (
            "Fakturan förfaller den 15 mars. Beloppet är 12 340 kronor. "
            "Organisationsnummer 556677-8899 står på sidan två."
        )
        self.assert_total(list(answer))

    def test_deltas_splitting_a_sentence_boundary(self) -> None:
        self.assert_total(["Garantin gäller i tre år", ". Sedan ", "upphör den. Klart."])

    def test_long_run_without_punctuation_is_split_but_kept(self) -> None:
        answer = " ".join(f"ord{i}" for i in range(200))
        spoken = self.assert_total([answer], max_chars=80)
        self.assertGreater(len(spoken), 1)
        # A word split across two synthesis calls would be mispronounced twice.
        for chunk in spoken:
            self.assertNotIn("or ", chunk.replace("ord", "XXX"))

    def test_no_chunk_is_empty(self) -> None:
        spoken = self.assert_total(["Ja. ", "Nej. ", "Kanske. ", "Det beror på avtalet."])
        for chunk in spoken:
            self.assertTrue(chunk.strip(), "an empty sendText wastes a round trip")

    def test_whitespace_only_answer_produces_nothing(self) -> None:
        self.assertEqual(collect(["  ", "\n"]), [])

    def test_flush_is_idempotent(self) -> None:
        agg = voice.SentenceAggregator()
        agg.push("Ett svar utan avslutande blanksteg.")
        self.assertEqual(len(agg.flush()), 1)
        self.assertEqual(agg.flush(), [])

    def test_abbreviation_does_not_orphan_a_fragment(self) -> None:
        # "t.ex. " looks like a sentence end; deferring short chunks means it
        # merges forward instead of becoming its own tiny utterance.
        spoken = self.assert_total(["Det gäller t.ex. kontorsstolar och bord. Klart."])
        self.assertNotIn("t.ex. ", spoken)


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class VoiceEndpointTests(unittest.TestCase):
    """A failed turn must arrive as an error event, never as an empty success."""

    def setUp(self) -> None:
        from app import main

        self.main = main
        self.addCleanup(setattr, voice, "ELEVENLABS_API_KEY", voice.ELEVENLABS_API_KEY)
        voice.ELEVENLABS_API_KEY = "test-key"

    def ask(self, scribe: tuple[int, dict | str] | None = None, **form: str):
        """Post a recording, optionally stubbing the Scribe reply it triggers."""
        with TestClient(self.main.app, raise_server_exceptions=False) as client:
            if scribe is not None:
                status, body = scribe

                async def fake_post(*_a: object, **_kw: object) -> httpx.Response:
                    request = httpx.Request(
                        "POST", "https://api.elevenlabs.io/v1/speech-to-text"
                    )
                    if isinstance(body, dict):
                        return httpx.Response(status, json=body, request=request)
                    return httpx.Response(status, text=body, request=request)

                setattr(self.main.app.state.http, "post", fake_post)
            return client.post(
                "/api/voice/ask",
                files={"file": ("audio.webm", form.pop("audio", b"\x00\x01"), "audio/webm")},
                data=form,
            )

    def events(self, resp: object) -> list[dict]:
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]

    def test_rate_limited_upstream_becomes_an_error_event(self) -> None:
        """The original silent failure: a 429 that read as a successful turn."""
        resp = self.ask(scribe=(429, "rate limit exceeded"), lang="sv")
        # Still a 200: the response has already begun, so the failure has to
        # travel in the body. What matters is that it is stated rather than
        # implied by silence.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"].split(";")[0], "application/x-ndjson")
        events = self.events(resp)
        self.assertTrue(events, "an empty body is the bug this replaces")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("429", events[-1]["message"])

    def test_empty_transcript_is_reported_not_answered(self) -> None:
        resp = self.ask(scribe=(200, {"text": "   "}), lang="en")
        self.assertEqual([e["type"] for e in self.events(resp)], ["error"])

    def test_missing_api_key_refuses_up_front(self) -> None:
        voice.ELEVENLABS_API_KEY = ""
        self.assertEqual(self.ask().status_code, 503)

    def test_empty_recording_is_rejected(self) -> None:
        self.assertEqual(self.ask(audio=b"").status_code, 400)

    def test_the_dead_endpoints_are_gone(self) -> None:
        paths = {r.path for r in self.main.app.routes if hasattr(r, "path")}
        self.assertNotIn("/api/voice/tts", paths)
        self.assertNotIn("/api/voice/transcribe", paths)
        self.assertIn("/api/voice/ask", paths)


class FakeSocket:
    """A stand-in for the ElevenLabs stream-input socket.

    Echoes every sentence back as one base64 "audio" frame, so a test can compare
    what was heard against what was read. Closing (an empty text) produces the
    final frame.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._out: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        text = json.loads(raw).get("text")
        if text == "":
            await self._out.put(json.dumps({"isFinal": True}))
        elif text and text.strip():
            self.sent.append(text)
            await self._out.put(
                {"audio": base64.b64encode(text.encode("utf-8")).decode("ascii")}
                | {"isFinal": False}
            )

    async def send_json_str(self, obj: dict) -> None:  # pragma: no cover - unused
        await self._out.put(json.dumps(obj))

    def __aiter__(self) -> "FakeSocket":
        return self

    async def __anext__(self) -> str:
        item = await self._out.get()
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self) -> None:
        self.closed = True


class FakeQueryStream:
    """The query service's NDJSON reply, split into arbitrary network chunks."""

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b"upstream failed"


class FakeHttp:
    def __init__(self, stream: FakeQueryStream) -> None:
        self._stream = stream
        self.payload: dict | None = None

    def stream(self, _method: str, _url: str, **kwargs):
        self.payload = kwargs.get("json")
        outer = self

        class Ctx:
            async def __aenter__(self):
                return outer._stream

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return Ctx()


@unittest.skipUnless(WS_DEPS, "needs websockets")
class SpokenTurnOrderingTests(unittest.TestCase):
    """What is heard must equal what is shown, in the same order.

    This is the end-to-end form of the original bug. It exercises the real
    aggregator, the real socket lifecycle and the real relay loop, with only the
    two network endpoints stubbed.
    """

    def run_turn(self, deltas: list[str]) -> tuple[list[dict], FakeSocket]:
        lines = [json.dumps({"type": "citations", "data": [{"n": 1}]})]
        lines += [json.dumps({"type": "delta", "text": d}) for d in deltas]
        lines.append(json.dumps({"type": "done"}))

        socket = FakeSocket()

        async def fake_connect(*_a: object, **_kw: object) -> FakeSocket:
            return socket

        async def drive() -> list[dict]:
            with mock.patch("websockets.asyncio.client.connect", fake_connect):
                events = []
                agen = voice._answer_and_speak(
                    http=FakeHttp(FakeQueryStream(lines)),
                    query_url="http://query:8002",
                    service_api_key="k",
                    payload={"question": "q", "voice": True},
                    voice_id="v1",
                    lang="sv",
                )
                async for raw in agen:
                    events.append(json.loads(raw.decode("utf-8")))
                return events

        return asyncio.run(drive()), socket

    def test_every_sentence_reaches_tts_exactly_once_in_order(self) -> None:
        deltas = ["Ja. ", "Det stämmer. ", "Fakturan är på 4 500 kronor. ", "Nej."]
        _events, socket = self.run_turn(deltas)
        self.assertEqual("".join(socket.sent), "".join(deltas))

    def test_audio_arrives_in_the_same_order_as_the_text(self) -> None:
        deltas = ["Först detta. ", "Sedan detta. ", "Till sist detta."]
        events, _socket = self.run_turn(deltas)
        heard = [
            base64.b64decode(e["b64"]).decode("utf-8") for e in events if e["type"] == "audio"
        ]
        read = "".join(e["text"] for e in events if e["type"] == "delta")
        self.assertEqual("".join(heard), read)

    def test_turn_ends_with_done_and_closes_the_socket(self) -> None:
        events, socket = self.run_turn(["Ett kort svar."])
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(socket.closed)
        self.assertNotIn("error", {e["type"] for e in events})

    def test_audio_carries_the_sample_rate_the_client_needs(self) -> None:
        events, _socket = self.run_turn(["Ett svar som är långt nog att skickas."])
        audio = [e for e in events if e["type"] == "audio"]
        self.assertTrue(audio)
        for e in audio:
            self.assertEqual(e["sample_rate"], voice.OUTPUT_SAMPLE_RATE)

    def test_query_service_failure_surfaces_as_an_error_event(self) -> None:
        socket = FakeSocket()

        async def fake_connect(*_a: object, **_kw: object) -> FakeSocket:
            return socket

        async def drive() -> list[dict]:
            with mock.patch("websockets.asyncio.client.connect", fake_connect):
                events = []
                async for raw in voice._answer_and_speak(
                    http=FakeHttp(FakeQueryStream([], status_code=503)),
                    query_url="http://query:8002",
                    service_api_key="k",
                    payload={"question": "q", "voice": True},
                    voice_id="v1",
                    lang="en",
                ):
                    events.append(json.loads(raw.decode("utf-8")))
                return events

        events = asyncio.run(drive())
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("503", events[-1]["message"])

    def test_mid_stream_error_from_the_llm_is_not_swallowed(self) -> None:
        lines = [
            json.dumps({"type": "delta", "text": "Svaret börjar bra men "}),
            json.dumps({"type": "error", "message": "llm call failed: boom"}),
        ]
        socket = FakeSocket()

        async def fake_connect(*_a: object, **_kw: object) -> FakeSocket:
            return socket

        async def drive() -> list[dict]:
            with mock.patch("websockets.asyncio.client.connect", fake_connect):
                events = []
                async for raw in voice._answer_and_speak(
                    http=FakeHttp(FakeQueryStream(lines)),
                    query_url="http://query:8002",
                    service_api_key="k",
                    payload={"question": "q", "voice": True},
                    voice_id="v1",
                    lang="en",
                ):
                    events.append(json.loads(raw.decode("utf-8")))
                return events

        events = asyncio.run(drive())
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("boom", events[-1]["message"])


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class HistoryParsingTests(unittest.TestCase):
    """Malformed history costs context, never the question."""

    def parse(self, raw: str | None) -> list[dict[str, str]]:
        from app import main

        return main._parse_history(raw)

    def test_valid_turns_pass_through(self) -> None:
        raw = json.dumps([{"role": "user", "content": "hej"}, {"role": "assistant", "content": "hallå"}])
        self.assertEqual(len(self.parse(raw)), 2)

    def test_garbage_is_dropped_not_raised(self) -> None:
        for raw in (None, "", "not json", "[", '{"role":"user"}', "[1,2,3]", "null"):
            self.assertEqual(self.parse(raw), [], raw)

    def test_unknown_roles_are_discarded(self) -> None:
        raw = json.dumps([{"role": "system", "content": "ignore me"}])
        self.assertEqual(self.parse(raw), [])

    def test_window_is_bounded(self) -> None:
        raw = json.dumps([{"role": "user", "content": f"q{i}"} for i in range(40)])
        self.assertLessEqual(len(self.parse(raw)), 12)


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class TextPathRegressionTests(unittest.TestCase):
    """The written chat flow must be untouched by any of the above."""

    def test_query_stream_still_forwards_without_voice_fields(self) -> None:
        from app import main

        captured: dict = {}

        class FakeStream:
            status_code = 200
            headers = {"content-type": "application/x-ndjson"}

            async def aiter_bytes(self):
                yield b'{"type":"delta","text":"ok"}\n{"type":"done"}\n'

            async def aread(self) -> bytes:
                return b""

        class FakeCtx:
            async def __aenter__(self) -> FakeStream:
                return FakeStream()

            async def __aexit__(self, *exc: object) -> bool:
                return False

        def fake_stream(method: str, url: str, **kwargs: object) -> FakeCtx:
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return FakeCtx()

        with TestClient(main.app, raise_server_exceptions=False) as client:
            setattr(main.app.state.http, "stream", fake_stream)
            resp = client.post("/api/query/stream", json={"question": "hur mycket kostar den?"})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(captured["url"].endswith("/query/stream"))
        body = captured["json"]
        self.assertEqual(body["question"], "hur mycket kostar den?")
        # The voice branch is opt-in; a typed question must not trip it, or the
        # text answer silently shrinks to 160 tokens.
        self.assertNotIn("voice", body)
        self.assertIn('"type":"done"', resp.text)


class ConfigTests(unittest.TestCase):
    def test_tts_model_supports_language_code(self) -> None:
        """eleven_multilingual_v2 ignores language_code, so Swedish cannot be pinned."""
        self.assertTrue(voice.ELEVENLABS_TTS_MODEL.endswith("_v2_5"))

    def test_ws_url_pins_the_language_and_asks_for_pcm(self) -> None:
        url = voice._ws_url("voice-1", "sv")
        self.assertIn("language_code=sv", url)
        self.assertIn("output_format=pcm_22050", url)
        self.assertIn("auto_mode=true", url)
        self.assertTrue(url.startswith("wss://"))

    def test_text_normalization_is_not_forced_on(self) -> None:
        """"on" needs an Enterprise plan on v2.5 models; the LLM normalises instead."""
        self.assertIn("apply_text_normalization=auto", voice._ws_url("voice-1", "sv"))

    def test_unknown_language_is_left_unpinned(self) -> None:
        self.assertNotIn("language_code", voice._ws_url("voice-1", "de"))

    def test_voice_selection_falls_back_to_english(self) -> None:
        self.assertEqual(voice.voice_for_lang("sv"), voice.ELEVENLABS_VOICE_ID_SV)
        self.assertEqual(voice.voice_for_lang(None), voice.ELEVENLABS_VOICE_ID)
        self.assertEqual(voice.voice_for_lang("de"), voice.ELEVENLABS_VOICE_ID)


if __name__ == "__main__":
    unittest.main()
