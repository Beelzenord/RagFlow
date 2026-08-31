"""Server-side orchestration for one spoken turn.

The browser records audio, posts it once, and reads back a single NDJSON stream
carrying the transcript, the citations, the answer text and the audio. Everything
between those two points happens here.

Why it is shaped this way. The previous implementation chunked the answer in the
browser and fired a separate TTS request per sentence, then tried to play the
responses back in dispatch order. That design needs a correct resequencer, correct
error propagation through a streaming response, and correct sentence segmentation,
and it got two of the three wrong in ways that were invisible at runtime: short
sentences were dropped by a minimum-length filter, and requests rejected for
exceeding the account's concurrency limit arrived as empty successes.

Both failures are structural here rather than guarded against:

  * One WebSocket to ElevenLabs per turn. It returns a single ordered audio
    stream, relayed in arrival order and played in arrival order, so there is no
    sequence number to get wrong and no concurrency limit to exceed.
  * The response is NDJSON, never audio, so a failure is an `error` event. The
    "200 with an empty body" path that hid the last set of failures cannot exist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:  # `from __future__ import annotations` makes this type-only.
    import httpx

log = logging.getLogger("web.voice")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_VOICE_ID_SV = os.environ.get("ELEVENLABS_VOICE_ID_SV", "kPdGSxhZAqy4bmPAf9iJ")
# Only the *_v2_5 models accept language_code; eleven_multilingual_v2 ignores it
# and infers the language from the text, which is how a Swedish answer ends up
# read with English vowels.
ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v1")
ELEVENLABS_BASE_URL = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/")
ELEVENLABS_WS_URL = os.environ.get(
    "ELEVENLABS_WS_URL", "wss://api.elevenlabs.io"
).rstrip("/")

ELEVENLABS_VOICES: dict[str, str] = {
    "en": ELEVENLABS_VOICE_ID,
    "sv": ELEVENLABS_VOICE_ID_SV,
}

# Raw 16-bit signed little-endian mono. Chosen over mp3 because MediaSource
# support for streamed mp3 is uneven across browsers, while PCM can be handed
# straight to Web Audio and scheduled back to back. 22.05kHz also stays under the
# 44.1kHz tier gate.
OUTPUT_FORMAT = "pcm_22050"
OUTPUT_SAMPLE_RATE = 22050

# The socket opens before retrieval runs, so it sits idle for as long as the
# rewrite, embedding and vector search take. The ceiling the API allows.
WS_INACTIVITY_TIMEOUT = 180

# A spoken answer is capped at ~40 words, so anything approaching this is a stall
# rather than a long answer. Bounds a wedged turn instead of holding the
# browser's request open until the socket's own idle timer fires.
TURN_TIMEOUT = float(os.environ.get("VOICE_TURN_TIMEOUT", "90"))


def enabled() -> bool:
    return bool(ELEVENLABS_API_KEY)


def voice_for_lang(lang: str | None) -> str:
    return ELEVENLABS_VOICES.get((lang or "").lower()) or ELEVENLABS_VOICE_ID


# A sentence ends at .!?… possibly followed by a closing quote or bracket, and
# then whitespace. Requiring the whitespace means the last sentence of a stream
# never matches, which is correct: flush() owns the tail.
_SENTENCE_END = re.compile(r"[.!?\u2026]['\")\]\u201d\u2019]*\s")


class SentenceAggregator:
    """Group a token stream into speakable chunks without losing a character.

    Totality is the whole point. The predecessor advanced its cursor only when a
    chunk cleared a 20-character minimum, so anything shorter was consumed by the
    regex and never spoken - which in Swedish quietly deleted "Ja.", "Nej." and
    "Det stämmer." from the audio while leaving them on screen. That gap between
    the text and the audio is part of what the old mode sounded wrong for.

    So a short sentence is *deferred*, never dropped: if a candidate chunk would
    fall below `min_chars` it is held and merged into the next one. Concatenating
    everything push() and flush() return reproduces the input exactly, and that is
    the property the tests assert. The single exception is an answer that is only
    whitespace, which yields nothing rather than an empty synthesis request.
    """

    def __init__(self, min_chars: int = 24, max_chars: int = 220) -> None:
        self._buf = ""
        self._min_chars = min_chars
        self._max_chars = max_chars

    def push(self, text: str) -> list[str]:
        if not text:
            return []
        self._buf += text
        out: list[str] = []
        while (cut := self._find_cut()) is not None:
            out.append(self._buf[:cut])
            self._buf = self._buf[cut:]
        return out

    def flush(self) -> list[str]:
        rest, self._buf = self._buf, ""
        return [rest] if rest.strip() else []

    def _find_cut(self) -> int | None:
        start = 0
        while (m := _SENTENCE_END.search(self._buf, start)) is not None:
            if m.end() >= self._min_chars:
                return m.end()
            # Too short to speak on its own; keep scanning so it merges forward.
            start = m.end()
        if len(self._buf) >= self._max_chars:
            # A long run with no sentence break. Split on a word boundary so no
            # word is ever cut across two synthesis calls.
            space = self._buf.rfind(" ", 0, self._max_chars)
            return space + 1 if space > 0 else self._max_chars
        return None


def ndjson(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


async def transcribe(
    http: httpx.AsyncClient,
    *,
    audio: bytes,
    filename: str,
    content_type: str,
    lang: str | None,
) -> tuple[str, str | None]:
    """Send one recording to Scribe and return (transcript, detected language).

    `lang` is forwarded whenever the caller supplies one - the old endpoint only
    forwarded it for languages that happened to have a configured TTS voice,
    which silently ignored the parameter for everything else.
    """
    files = {"file": (filename or "audio.webm", audio, content_type or "audio/webm")}
    data: dict[str, str] = {"model_id": ELEVENLABS_STT_MODEL}
    if lang:
        data["language_code"] = lang.lower()

    resp = await http.post(
        f"{ELEVENLABS_BASE_URL}/v1/speech-to-text",
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        files=files,
        data=data,
    )
    if resp.status_code >= 400:
        detail = resp.text[:300] or f"HTTP {resp.status_code}"
        raise RuntimeError(f"speech-to-text failed ({resp.status_code}): {detail}")

    body = resp.json()
    if not isinstance(body, dict):
        raise RuntimeError("speech-to-text returned an unexpected payload")
    text = (body.get("text") or body.get("transcript") or "").strip()
    detected = body.get("language_code") or body.get("language")
    return text, (detected if isinstance(detected, str) else None)


def _ws_url(voice_id: str, lang: str | None) -> str:
    params = [
        f"model_id={ELEVENLABS_TTS_MODEL}",
        f"output_format={OUTPUT_FORMAT}",
        # Full sentences are all we ever send, so the server-side chunk scheduler
        # is pure added latency.
        "auto_mode=true",
        # Not "on": ElevenLabs gates normalisation on v2.5 models behind an
        # Enterprise plan, so forcing it would either 422 or be ignored - the
        # same class of plan-limit failure this rebuild exists to remove. The
        # documented alternative for low-latency models is to normalise upstream,
        # which SYSTEM_PROMPT_VOICE in the query service does: it asks for
        # numbers, prices and reference numbers already written as they are said.
        "apply_text_normalization=auto",
        f"inactivity_timeout={WS_INACTIVITY_TIMEOUT}",
    ]
    if lang and lang.lower() in ELEVENLABS_VOICES:
        params.append(f"language_code={lang.lower()}")
    return f"{ELEVENLABS_WS_URL}/v1/text-to-speech/{voice_id}/stream-input?" + "&".join(params)


async def run_turn(
    *,
    http: httpx.AsyncClient,
    query_url: str,
    service_api_key: str,
    audio: bytes,
    filename: str,
    content_type: str,
    lang: str | None,
    document_id: str | None,
    history: list[dict[str, str]] | None,
) -> AsyncIterator[bytes]:
    """Drive one spoken turn, yielding NDJSON lines.

    Wire format (a superset of /query/stream, so the browser reuses the same
    reader):
      {"type":"transcript","text":"...","lang":"sv"}
      {"type":"rewrite","text":"..."}
      {"type":"citations","data":[{...}, ...]}
      {"type":"delta","text":"..."}
      {"type":"audio","b64":"...","sample_rate":22050}
      {"type":"done"}
      {"type":"error","message":"..."}
    """
    if not enabled():
        yield ndjson({"type": "error", "message": "voice is not configured on this server"})
        return

    try:
        transcript, detected = await transcribe(
            http, audio=audio, filename=filename, content_type=content_type, lang=lang
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("transcription failed: %s", exc)
        yield ndjson({"type": "error", "message": str(exc)})
        return

    if not transcript:
        yield ndjson({"type": "error", "message": "I didn't catch that - try again?"})
        return

    # Scribe reports what it heard, but not always as the two-letter code the
    # voice map is keyed on, so an unrecognised value is treated as unknown
    # rather than passed on as if it were a language we can pin.
    if detected and detected.lower() not in ELEVENLABS_VOICES:
        detected = None
    speak_lang = (lang or detected or "").lower() or None
    yield ndjson({"type": "transcript", "text": transcript, "lang": speak_lang})

    payload: dict[str, Any] = {"question": transcript, "voice": True}
    if speak_lang in ELEVENLABS_VOICES:
        payload["lang"] = speak_lang
    if document_id:
        payload["document_id"] = document_id
    if history:
        payload["history"] = history

    async for line in _answer_and_speak(
        http=http,
        query_url=query_url,
        service_api_key=service_api_key,
        payload=payload,
        voice_id=voice_for_lang(speak_lang),
        lang=speak_lang,
    ):
        yield line


async def _answer_and_speak(
    *,
    http: httpx.AsyncClient,
    query_url: str,
    service_api_key: str,
    payload: dict[str, Any],
    voice_id: str,
    lang: str | None,
) -> AsyncIterator[bytes]:
    """Run the query stream and the TTS socket side by side.

    Text events reach the browser as soon as the LLM produces them, and audio as
    soon as ElevenLabs returns it, so neither waits on the other. The two
    producers share one queue; audio keeps its order because a single socket is
    read sequentially by a single task.
    """
    # Imported here rather than at module scope so SentenceAggregator - the piece
    # carrying the ordering guarantee - stays importable and testable with the
    # standard library alone.
    from websockets.asyncio.client import connect as ws_connect

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    failed: list[str] = []

    try:
        socket = await ws_connect(
            _ws_url(voice_id, lang),
            additional_headers={"xi-api-key": ELEVENLABS_API_KEY},
            max_size=None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("elevenlabs websocket refused: %s", exc)
        yield ndjson({"type": "error", "message": f"text-to-speech unavailable: {exc}"})
        return

    async def pump_text() -> None:
        """Stream the answer, forwarding text down and sentences into the socket."""
        aggregator = SentenceAggregator()
        try:
            # Opens the socket's generation; ElevenLabs treats the first message
            # as the session initialiser.
            await socket.send(json.dumps({"text": " "}))
            async with http.stream(
                "POST",
                f"{query_url}/query/stream",
                headers={"x-api-key": service_api_key, "content-type": "application/json"},
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    detail = body.decode(errors="replace")[:300] or f"HTTP {resp.status_code}"
                    raise RuntimeError(f"query service failed ({resp.status_code}): {detail}")
                async for raw in resp.aiter_lines():
                    if not raw.strip():
                        continue
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    kind = evt.get("type")
                    if kind == "delta":
                        await queue.put(ndjson(evt))
                        for sentence in aggregator.push(evt.get("text") or ""):
                            await socket.send(json.dumps({"text": sentence}))
                    elif kind in ("citations", "rewrite"):
                        await queue.put(ndjson(evt))
                    elif kind == "error":
                        raise RuntimeError(evt.get("message") or "query service error")
                    elif kind == "done":
                        break
            for sentence in aggregator.flush():
                await socket.send(json.dumps({"text": sentence}))
        except Exception as exc:  # noqa: BLE001
            log.warning("voice answer failed: %s", exc)
            failed.append(str(exc))
        finally:
            # Empty text flushes and closes the generation, which is what makes
            # the socket emit its final frame and let pump_audio finish.
            try:
                await socket.send(json.dumps({"text": ""}))
            except Exception:  # noqa: BLE001
                pass

    async def pump_audio() -> None:
        """Relay audio frames in the order the socket produces them."""
        try:
            async for message in socket:
                try:
                    frame = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue
                if frame.get("error"):
                    raise RuntimeError(str(frame.get("message") or frame.get("error")))
                chunk = frame.get("audio")
                if chunk:
                    # Already base64 on the wire; forwarded as-is.
                    await queue.put(
                        ndjson(
                            {
                                "type": "audio",
                                "b64": chunk,
                                "sample_rate": OUTPUT_SAMPLE_RATE,
                            }
                        )
                    )
                if frame.get("isFinal"):
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("voice audio stream failed: %s", exc)
            failed.append(str(exc))

    async def drive() -> None:
        try:
            # A socket that stops sending without ever sending isFinal would
            # leave pump_audio waiting on its own inactivity timeout, holding the
            # browser's request open with nothing to show for it.
            await asyncio.wait_for(
                asyncio.gather(pump_text(), pump_audio()), timeout=TURN_TIMEOUT
            )
        except asyncio.TimeoutError:
            failed.append("the answer took too long")
        finally:
            await socket.close()
            await queue.put(None)

    driver = asyncio.create_task(drive())
    try:
        while (item := await queue.get()) is not None:
            yield item
        await driver
    finally:
        if not driver.done():
            driver.cancel()

    if failed:
        yield ndjson({"type": "error", "message": failed[0]})
    else:
        yield ndjson({"type": "done"})
