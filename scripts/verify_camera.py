"""Opt-in hardware acceptance through real MCP stdio, never part of pytest."""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from webcam_recorder_mcp.config import load_config


async def verify(config_path: Path, seconds: float) -> None:
    config = load_config(config_path)
    evidence = config.output_dir.parent / "verification"
    evidence.mkdir(parents=True, exist_ok=True)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "webcam_recorder_mcp", "--config", str(config_path.resolve())],
        env={"PYTHONUTF8": "1"},
    )
    recording_id = str(uuid4())
    with (evidence / "mcp-stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = [tool.name for tool in (await session.list_tools()).tools]
                assert set(names) == {"list_devices", "start_recording", "stop_recording",
                                      "get_recording_status"}
                devices = await session.call_tool("list_devices", {})
                assert not devices.isError, devices.content
                invalid = await session.call_tool("start_recording", {"recording_id": "../bad"})
                assert invalid.isError
                start = await session.call_tool("start_recording", {"recording_id": recording_id})
                assert not start.isError, start.content
                try:
                    conflict = await session.call_tool("start_recording", {})
                    assert conflict.isError
                    await asyncio.sleep(seconds)
                    status = await session.call_tool("get_recording_status",
                                                     {"recording_id": recording_id})
                    assert not status.isError, status.content
                finally:
                    stop = await session.call_tool("stop_recording", {"recording_id": recording_id})
                assert not stop.isError, stop.content
                result = stop.structuredContent
                assert result and result["status"] == "completed", result
                repeat = await session.call_tool("stop_recording", {"recording_id": recording_id})
                assert repeat.structuredContent == result
    target = Path(result["file_path"])
    decoded = subprocess.run([config.ffmpeg_path, "-v", "error", "-i", str(target),
                              "-f", "null", "-"], capture_output=True, timeout=60)
    assert decoded.returncode == 0, decoded.stderr.decode("utf-8", "replace")
    probe = subprocess.run([config.ffprobe_path, "-v", "error", "-show_streams",
                            "-show_format", "-of", "json", str(target)],
                           capture_output=True, check=True, timeout=15)
    report = {"tools": names, "recording": result, "probe": json.loads(probe.stdout),
              "decode_exit_code": decoded.returncode}
    (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(json.dumps({"status": result["status"], "file_path": str(target),
                      "duration_seconds": result["duration_seconds"],
                      "size_bytes": result["size_bytes"], "evidence": str(evidence)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 30:
        parser.error("--seconds must be between 1 and 30")
    asyncio.run(verify(args.config, args.seconds))
