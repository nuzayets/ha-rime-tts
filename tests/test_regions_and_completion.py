"""Regional routing and end-of-stream semantics."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web

from custom_components.rime_tts import api
from custom_components.rime_tts.api import RimeClient, RimeError
from custom_components.rime_tts.const import HTTP_URLS, WS_URLS


@pytest.mark.parametrize("region", ["us-east", "us-west"])
async def test_region_routes_catalog_and_auth(region):
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(return_value={"coda": {"eng": ["astra"]}})
    client = RimeClient(session, "test-key", region)
    assert await client.voices() == {"coda": {"en": ["astra"]}}
    await client.validate("coda", "en", "astra")
    assert (
        session.get.call_args.args[0] == HTTP_URLS[region] + "/data/voices/all-v2.json"
    )
    assert session.ws_connect.call_args.args[0] == WS_URLS[region]
    assert "headers" not in session.get.call_args.kwargs


def test_unknown_region():
    with pytest.raises(RimeError):
        RimeClient(MagicMock(), "test-key", "untrusted-server")


@pytest.mark.parametrize(
    ("done", "empty"), [(True, False), (False, False), (False, True)]
)
async def test_eos_without_websocket_close_frame(
    aiohttp_server, socket_enabled, monkeypatch, done, empty
):
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.json().get("operation") == "eos":
                if not empty:
                    await ws.send_json({"type": "chunk", "data": "YWJj"})
                if done:
                    await ws.send_json({"type": "done"})
                request.transport.close()
                break
        return ws

    app = web.Application()
    app.router.add_get("/ws3", handler)
    server = await aiohttp_server(app)
    monkeypatch.setitem(api.WS_URLS, "us-east", str(server.make_url("/ws3")))

    async def text():
        if not empty:
            yield "Hello."

    async with aiohttp.ClientSession() as session:

        async def collect():
            return [
                chunk
                async for chunk in RimeClient(session, "test-key", "us-east").stream(
                    text(), "coda", "en", "astra"
                )
            ]

        async with asyncio.timeout(2):
            if done or empty:
                assert await collect() == ([] if empty else [b"abc"])
            else:
                with pytest.raises(RimeError, match="ended unexpectedly"):
                    await collect()
