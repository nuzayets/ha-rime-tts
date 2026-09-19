"""Small asynchronous client for Rime's JSON WebSocket API."""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import suppress
from datetime import date

import aiohttp

from .const import (
    DEFAULT_REGION,
    DEFAULT_SPEED,
    HTTP_URLS,
    LANGUAGES,
    MAX_SPEED,
    MIN_SPEED,
    MODELS,
    USAGE_URL,
    WS_URLS,
)

type VoiceCatalog = dict[str, dict[str, list[str]]]


class RimeError(Exception):
    """Rime could not complete the request."""


class RimeAuthError(RimeError):
    """Rime rejected the API key."""


def validate_speed(value: object) -> float:
    """Accept finite playback multipliers supported by Rime."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not MIN_SPEED <= value <= MAX_SPEED
    ):
        raise ValueError(f"Speaking speed must be between {MIN_SPEED} and {MAX_SPEED}")
    return value


def parse_catalog(data: object) -> VoiceCatalog:
    """Validate the public catalog and normalize language codes."""
    catalog: VoiceCatalog = {}
    if isinstance(data, dict):
        for model in MODELS:
            languages = data.get(model)
            if not isinstance(languages, dict):
                continue
            for language, voices in languages.items():
                if not isinstance(language, str) or not isinstance(voices, list):
                    continue
                valid = sorted({v for v in voices if isinstance(v, str) and v})
                if valid:
                    catalog.setdefault(model, {})[LANGUAGES.get(language, language)] = (
                        valid
                    )
    if not catalog:
        raise RimeError("Rime returned an empty or invalid voice catalog")
    return catalog


class RimeClient:
    """Use Home Assistant's shared HTTP session without owning its lifecycle."""

    def __init__(
        self, session: aiohttp.ClientSession, api_key: str, region: str = DEFAULT_REGION
    ) -> None:
        if region not in WS_URLS:
            raise RimeError("Select us-east or us-west as the Rime region")
        self.region = region
        self.session = session
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def voices(self) -> VoiceCatalog:
        """Fetch the public catalog; this does not validate credentials."""
        try:
            async with self.session.get(
                HTTP_URLS[self.region] + "/data/voices/all-v2.json",
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                response.raise_for_status()
                return parse_catalog(await response.json())
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise RimeError("Unable to load Rime voices") from err

    async def usage(self, start: date, end: date) -> dict[date, int]:
        """Read account-wide character counts, as in Rime's official CLI."""
        try:
            async with self.session.get(
                USAGE_URL,
                params={"startDate": start.isoformat(), "endDate": end.isoformat()},
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise RimeError("Unable to load Rime account usage") from err
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RimeError("Invalid Rime usage response")
        totals: dict[date, int] = {}
        for row in payload["data"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("day"), str)
                or not isinstance(row.get("breakdown"), list)
            ):
                raise RimeError("Invalid Rime usage day")
            try:
                day = date.fromisoformat(row["day"])
            except ValueError as err:
                raise RimeError("Invalid Rime usage date") from err
            if not start <= day <= end:
                continue
            if day in totals:
                raise RimeError("Duplicate Rime usage day")
            total = 0
            for record in row["breakdown"]:
                if not isinstance(record, dict):
                    raise RimeError("Invalid Rime usage record")
                count = record.get("charCount")
                if type(count) is not int or count < 0:
                    raise RimeError("Invalid Rime character count")
                total += count
            totals[day] = total
        return totals

    def _connect(
        self, model: str, language: str, voice: str, speed: float = DEFAULT_SPEED
    ):
        return self.session.ws_connect(
            WS_URLS[self.region],
            headers=self._headers,
            params={
                "modelId": model,
                "lang": language,
                "speaker": voice,
                "audioFormat": "mp3",
                "samplingRate": "24000",
                "segment": "bySentence",
                "timeScaleFactor": str(1.0 / validate_speed(speed)),
            },
            timeout=aiohttp.ClientWSTimeout(ws_receive=60, ws_close=5),
        )

    async def validate(self, model: str, language: str, voice: str) -> None:
        """Authenticate a WebSocket without synthesizing billable speech."""
        try:
            async with asyncio.timeout(20), self._connect(model, language, voice):
                pass
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise RimeAuthError("Rime rejected the API key") from err
            raise RimeError(f"Rime connection failed (HTTP {err.status})") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RimeError("Unable to connect to Rime") from err

    async def stream(
        self,
        text: AsyncIterable[str],
        model: str,
        language: str,
        voice: str,
        speed: float = DEFAULT_SPEED,
    ) -> AsyncGenerator[bytes]:
        """Send incremental text while receiving MP3 audio concurrently."""
        try:
            async with self._connect(model, language, voice, speed) as ws:
                eos_sent = False
                has_text = False
                has_audio = False
                batch_done = False
                sender_error: Exception | None = None

                async def send() -> None:
                    nonlocal eos_sent, has_text, batch_done, sender_error
                    try:
                        async for chunk in text:
                            has_text = has_text or bool(chunk.strip())
                            for offset in range(0, len(chunk), 1000):
                                batch_done = False
                                await ws.send_json(
                                    {"text": chunk[offset : offset + 1000]}
                                )
                        await ws.send_json({"operation": "eos"})
                        eos_sent = True
                    except Exception as err:
                        sender_error = err
                        await ws.close()

                sender = asyncio.create_task(send(), name="rime_tts_send")
                try:
                    async for frame in ws:
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            raise RimeError("Unexpected Rime WebSocket frame")
                        try:
                            event = json.loads(frame.data)
                        except ValueError as err:
                            raise RimeError("Invalid Rime WebSocket response") from err
                        if not isinstance(event, dict):
                            raise RimeError("Invalid Rime WebSocket event")
                        kind = event.get("type")
                        if kind == "error":
                            raise RimeError("Rime rejected the synthesis request")
                        if kind == "done":
                            batch_done = True
                        elif kind == "chunk":
                            encoded = event.get("data")
                            if not isinstance(encoded, str):
                                raise RimeError("Invalid Rime audio chunk")
                            try:
                                audio = base64.b64decode(encoded, validate=True)
                            except (ValueError, binascii.Error) as err:
                                raise RimeError("Invalid Rime audio encoding") from err
                            if audio:
                                has_audio = True
                                batch_done = False
                                yield audio
                    if sender_error is not None:
                        raise sender_error
                    if not eos_sent or (has_text and not has_audio):
                        raise RimeError("Rime closed before completing synthesis")
                    # Rime may close after EOS without a WebSocket close frame.
                    if has_text and ws.close_code != 1000 and not batch_done:
                        raise RimeError("Rime audio stream ended unexpectedly")
                finally:
                    sender.cancel()
                    with suppress(asyncio.CancelledError):
                        await sender
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise RimeAuthError("Rime rejected the API key") from err
            raise RimeError(f"Rime connection failed (HTTP {err.status})") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RimeError("Rime audio connection failed") from err
