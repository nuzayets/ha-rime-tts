"""Preserve streamed text for a fallback without cancelling its producer."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator


class RecordedText(AsyncIterator[str]):
    """Keep text until Rime produces audio, including any pending next chunk."""

    def __init__(self, source: AsyncGenerator[str]) -> None:
        self.source = source
        self.pending: asyncio.Task[str] | None = None
        self.parts: list[str] = []
        self.recording = True

    async def __anext__(self) -> str:
        if self.pending is None:
            self.pending = asyncio.create_task(anext(self.source))
        try:
            chunk = await asyncio.shield(self.pending)
        except StopAsyncIteration:
            self.pending = None
            raise
        self.pending = None
        if self.recording:
            self.parts.append(chunk)
        return chunk

    async def complete(self) -> str:
        async for _ in self:
            pass
        return "".join(self.parts)

    def stop_recording(self) -> None:
        self.recording = False
        self.parts.clear()

    async def aclose(self) -> None:
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
        await self.source.aclose()
