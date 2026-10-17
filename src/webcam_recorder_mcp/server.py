"""Standard MCP tools; stdout is reserved for the protocol."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import RecorderConfig
from .recorder import Recorder, Recording


def create_server(config: RecorderConfig) -> FastMCP:
    recorder = Recorder(config)

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await recorder.close()

    server = FastMCP("Webcam Recorder MCP", lifespan=lifespan, log_level="WARNING")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def list_devices() -> dict:
        """List Windows video/audio devices and the configured camera's supported modes."""
        return await recorder.list_devices()

    @server.tool(annotations=ToolAnnotations(destructiveHint=False))
    async def start_recording(recording_id: UUID | None = None) -> Recording:
        """Start recording; return only after receiving actual video frames. UUID is optional."""
        return await recorder.start(recording_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def get_recording_status(recording_id: UUID) -> Recording:
        """Read recording status, including persisted results after the session was closed."""
        return await recorder.status(recording_id)

    @server.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
    async def stop_recording(recording_id: UUID) -> Recording:
        """Stop, wait for FFmpeg, validate the file, and return metadata. Safe to repeat."""
        return await recorder.stop(recording_id)

    return server
