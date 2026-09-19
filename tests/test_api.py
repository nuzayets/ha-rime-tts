"""Exercise the streaming protocol against a local WebSocket server."""

import asyncio
import base64
from contextlib import aclosing

import aiohttp
import pytest
from aiohttp import web

from custom_components.rime_tts import api
from custom_components.rime_tts.api import (
    RimeAuthError,
    RimeClient,
    RimeError,
    parse_catalog,
)


@pytest.fixture
async def connect(aiohttp_server, socket_enabled, monkeypatch):
    async def start(handler):
        app = web.Application()
        app.router.add_get("/ws3", handler)
        server = await aiohttp_server(app)
        monkeypatch.setitem(api.WS_URLS, "us-west", str(server.make_url("/ws3")))
        return RimeClient

    return start


async def text(*chunks):
    for chunk in chunks:
        yield chunk


def test_catalog():
    assert parse_catalog(
        {"coda": {"eng": ["astra", "astra", None], "jpn": ["ren"]}, "unknown": {}}
    ) == {"coda": {"en": ["astra"], "ja": ["ren"]}}


@pytest.mark.parametrize("value", [None, [], {}, {"coda": {"eng": []}}])
def test_bad_catalog(value):
    with pytest.raises(RimeError):
        parse_catalog(value)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_connection_errors(connect, status):
    async def handler(request):
        return web.Response(status=status)

    await connect(handler)
    async with aiohttp.ClientSession() as session:
        client = RimeClient(session, "test-key")
        expected = RimeAuthError if status in (401, 403) else RimeError
        with pytest.raises(expected):
            await client.validate("coda", "en", "astra")
        with pytest.raises(expected):
            await anext(client.stream(text("Hello."), "coda", "en", "astra"))


async def test_authentication_without_synthesis(connect):
    messages = []

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            messages.append(message)
        return ws

    await connect(handler)
    async with aiohttp.ClientSession() as session:
        await RimeClient(session, "test-key").validate("coda", "en", "astra")
    assert not messages


async def test_streams_before_input_finishes_and_ignores_batch_done(connect):
    release = asyncio.Event()
    sent = []

    async def input_text():
        yield "First sentence."
        await release.wait()
        yield "Second sentence."

    async def handler(request):
        assert request.query["modelId"] == "coda"
        assert request.query["audioFormat"] == "mp3"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for frame in ws:
            event = frame.json()
            sent.append(event)
            if "text" in event:
                await ws.send_json({"type": "timestamps", "word_timestamps": {}})
                await ws.send_json(
                    {
                        "type": "chunk",
                        "data": base64.b64encode(event["text"].encode()).decode(),
                    }
                )
                await ws.send_json({"type": "done"})
            else:
                await ws.close()
        return ws

    await connect(handler)
    async with aiohttp.ClientSession() as session:
        async with aclosing(
            RimeClient(session, "test-key").stream(input_text(), "coda", "en", "astra")
        ) as audio:
            assert await asyncio.wait_for(anext(audio), 2) == b"First sentence."
            assert not release.is_set()
            release.set()
            assert b"".join([chunk async for chunk in audio]) == b"Second sentence."
    assert sent[-1] == {"operation": "eos"}


async def test_cancel_closes_socket_and_sender(connect):
    sender_closed = asyncio.Event()
    server_closed = asyncio.Event()

    async def input_text():
        try:
            yield "Hello."
            await asyncio.Event().wait()
        finally:
            sender_closed.set()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await ws.send_json({"type": "chunk", "data": "YWJj"})
        async for _ in ws:
            pass
        server_closed.set()
        return ws

    await connect(handler)
    async with aiohttp.ClientSession() as session:
        stream = RimeClient(session, "test-key").stream(
            input_text(), "coda", "en", "astra"
        )
        assert await anext(stream) == b"abc"
        await stream.aclose()
        await asyncio.wait_for(server_closed.wait(), 2)
        assert sender_closed.is_set()


@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "message": "sensitive upstream details"},
        {"type": "chunk", "data": "%%%"},
        {"type": "chunk", "data": None},
        ["not an object"],
    ],
)
async def test_bad_events(connect, event):
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await ws.send_json(event)
        async for _ in ws:
            pass
        return ws

    await connect(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(RimeError) as error:
            async for _ in RimeClient(session, "test-key").stream(
                text("Hello."), "coda", "en", "astra"
            ):
                pass
        assert "sensitive" not in str(error.value)


async def test_input_failure_does_not_hang(connect):
    async def input_text():
        yield "Hello."
        raise ValueError("Input failed")

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    await connect(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ValueError, match="Input failed"):
            async with asyncio.timeout(2):
                async for _ in RimeClient(session, "test-key").stream(
                    input_text(), "coda", "en", "astra"
                ):
                    pass


async def test_long_text_and_unpunctuated_tail(connect):
    received = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for frame in ws:
            event = frame.json()
            if "text" in event:
                received.append(event["text"])
            else:
                await ws.send_json({"type": "chunk", "data": "YWJj"})
                await ws.send_json({"type": "done"})
                await ws.close()
        return ws

    await connect(handler)
    message = "a" * 2501
    async with aiohttp.ClientSession() as session:
        result = [
            chunk
            async for chunk in RimeClient(session, "test-key").stream(
                text(message), "coda", "en", "astra"
            )
        ]
    assert result == [b"abc"]
    assert "".join(received) == message
    assert max(map(len, received)) <= 1000


async def test_premature_close(connect):
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    await connect(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(RimeError):
            async for _ in RimeClient(session, "test-key").stream(
                text("Hello."), "coda", "en", "astra"
            ):
                pass
