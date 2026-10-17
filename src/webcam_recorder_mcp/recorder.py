"""Single-camera recording lifecycle; files never travel through MCP messages."""

import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .config import RecorderConfig


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_id: UUID
    status: Literal["recording", "completed", "failed"] = "recording"
    video_device: str
    audio_device: str | None
    file_path: str
    log_path: str
    started_at: datetime
    finished_at: datetime | None = None
    frame_count: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0, ge=0)
    size_bytes: int = Field(default=0, ge=0)
    error: str | None = None


@dataclass
class _Capture:
    process: asyncio.subprocess.Process
    recording: Recording
    log: BinaryIO
    ready: asyncio.Event
    job: Any = None
    progress: asyncio.Task[None] | None = None


class Recorder:
    def __init__(self, config: RecorderConfig) -> None:
        self.config = config
        self.root = config.output_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._active: _Capture | None = None

    @staticmethod
    def _process_options() -> dict[str, Any]:
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    async def _query(self, executable: str, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **self._process_options(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        return (process.returncode or 0, stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"))

    async def list_devices(self) -> dict[str, Any]:
        _, _, output = await self._query(
            self.config.ffmpeg_path, "-hide_banner", "-list_devices", "true", "-f", "dshow",
            "-i", "dummy",
        )
        devices = [{"name": name, "type": kind} for name, kind in
                   re.findall(r'"([^"\r\n]+)" \((video|audio)\)', output)]
        _, _, options = await self._query(
            self.config.ffmpeg_path, "-hide_banner", "-list_options", "true", "-f", "dshow",
            "-i", f"video={self.config.video_device}",
        )
        return {"devices": devices, "configured_video_device": self.config.video_device,
                "video_options": [line.split("] ", 1)[-1].strip() for line in options.splitlines()
                                  if "vcodec=" in line or "pixel_format=" in line]}

    def _directory(self, recording_id: UUID) -> Path:
        directory = (self.root / str(recording_id)).resolve()
        if directory.parent != self.root:
            raise ValueError("Recording directory escapes output_dir")
        return directory

    def _save(self, recording: Recording) -> None:
        path = self._directory(recording.recording_id) / "recording.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(recording.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    def _command(self, target: Path) -> list[str]:
        config = self.config
        args = [config.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-n",
                "-thread_queue_size", "512", "-f", "dshow", "-rtbufsize", "256M",
                "-video_size", f"{config.width}x{config.height}",
                "-framerate", str(config.framerate)]
        args += (["-pixel_format", "yuyv422"] if config.input_format == "yuyv422"
                 else ["-vcodec", config.input_format])
        source = f"video={config.video_device}"
        if config.audio_device:
            source += f":audio={config.audio_device}"
        args += ["-i", source, "-map", "0:v:0"]
        if config.audio_device:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-an"]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                 "-g", str(config.framerate * 2),
                 "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                 "-frag_duration", "1000000", "-flush_packets", "1",
                 "-progress", "pipe:1", "-stats_period", "0.2", "-nostats", str(target)]
        return args

    async def _read_progress(self, capture: _Capture) -> None:
        assert capture.process.stdout is not None
        while line := await capture.process.stdout.readline():
            key, _, value = line.decode("ascii", "replace").strip().partition("=")
            if key == "frame" and value.strip().isdigit():
                capture.recording.frame_count = int(value)
            elif key == "out_time_us":
                try:
                    capture.recording.duration_seconds = max(0, int(value) / 1_000_000)
                except ValueError:
                    pass  # FFmpeg reports N/A before an output timestamp exists.
            elif key == "progress" and capture.recording.frame_count > 0:
                if not capture.ready.is_set():
                    capture.recording.started_at = datetime.now(UTC) - timedelta(
                        seconds=capture.recording.duration_seconds,
                    )
                    capture.ready.set()

    async def start(self, recording_id: UUID | None = None) -> Recording:
        async with self._lock:
            if self._active is not None:
                if self._active.process.returncode is None:
                    raise ValueError("Camera is already recording; stop the active recording first")
                await self._finish(self._active, "FFmpeg exited unexpectedly")
            recording_id = recording_id or uuid4()
            directory = self._directory(recording_id)
            directory.mkdir(exist_ok=False)
            recording = Recording(recording_id=recording_id, video_device=self.config.video_device,
                                  audio_device=self.config.audio_device,
                                  file_path=str(directory / "video.mp4"),
                                  log_path=str(directory / "ffmpeg.log"),
                                  started_at=datetime.now(UTC))
            self._save(recording)
            log = Path(recording.log_path).open("wb")
            capture = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._command(Path(recording.file_path)), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=log, **self._process_options(),
                )
                capture = _Capture(process, recording, log, asyncio.Event())
                self._active = capture
                if os.name == "nt":
                    from .windows_job import CaptureJob
                    capture.job = CaptureJob(process.pid)
                capture.progress = asyncio.create_task(self._read_progress(capture))
                ready = asyncio.create_task(capture.ready.wait())
                ended = asyncio.create_task(process.wait())
                try:
                    done, _ = await asyncio.wait(
                        {ready, ended}, timeout=self.config.start_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if ended in done:
                        raise RuntimeError("FFmpeg exited before startup; inspect ffmpeg.log")
                    if ready not in done:
                        raise TimeoutError("Camera produced no video frames before startup timeout")
                finally:
                    ready.cancel()
                    ended.cancel()
                    await asyncio.gather(ready, ended, return_exceptions=True)
                self._save(recording)
                return recording.model_copy()
            except BaseException as exc:
                if capture is not None:
                    await self._finish(capture, str(exc) or type(exc).__name__)
                else:
                    log.close()
                    recording.status, recording.error = "failed", str(exc)
                    recording.finished_at = datetime.now(UTC)
                    self._save(recording)
                raise

    async def _finish(self, capture: _Capture, error: str | None = None) -> Recording:
        process, recording = capture.process, capture.recording
        try:
            if process.returncode is None:
                try:
                    assert process.stdin is not None
                    process.stdin.write(b"q\n")
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                try:
                    await asyncio.wait_for(process.wait(), self.config.stop_timeout_seconds)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    error = error or "FFmpeg stop timed out; partial video retained"
            if capture.progress is not None:
                await capture.progress
            if process.returncode != 0:
                error = error or f"FFmpeg exited with code {process.returncode}; inspect ffmpeg.log"
            path = Path(recording.file_path)
            recording.size_bytes = path.stat().st_size if path.is_file() else 0
            if recording.size_bytes:
                code, output, _ = await self._query(
                    self.config.ffprobe_path, "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type", "-of", "json", str(path),
                )
                probe = json.loads(output) if code == 0 else {}
                if not any(stream.get("codec_type") == "video"
                           for stream in probe.get("streams", [])):
                    error = error or "Recorded file has no readable video stream"
                else:
                    recording.duration_seconds = float(probe.get("format", {}).get("duration", 0))
            else:
                error = error or "No video file was written"
            if recording.frame_count == 0 or recording.duration_seconds <= 0:
                error = error or "Recorded file contains no video frames"
        except asyncio.CancelledError:
            error = error or "Video finalization was interrupted; partial video retained"
            raise
        except Exception as exc:
            error = error or f"Video finalization failed: {exc}"
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            capture.log.close()
            if capture.job is not None:
                capture.job.close()
            self._active = None
            recording.status = "failed" if error else "completed"
            recording.error = error
            recording.finished_at = datetime.now(UTC)
            self._save(recording)
        return recording.model_copy()

    async def status(self, recording_id: UUID) -> Recording:
        async with self._lock:
            if self._active is not None and self._active.recording.recording_id == recording_id:
                if self._active.process.returncode is not None:
                    return await self._finish(self._active, "FFmpeg exited unexpectedly")
                result = self._active.recording.model_copy()
                path = Path(result.file_path)
                result.size_bytes = path.stat().st_size if path.is_file() else 0
                return result
            path = self._directory(recording_id) / "recording.json"
            recording = Recording.model_validate_json(path.read_text(encoding="utf-8"))
            if recording.status == "recording":
                recording.status = "failed"
                recording.error = "Recorder was interrupted; partial video retained"
                recording.finished_at = datetime.now(UTC)
                self._save(recording)
            return recording

    async def stop(self, recording_id: UUID) -> Recording:
        async with self._lock:
            if self._active is not None and self._active.recording.recording_id == recording_id:
                error = ("FFmpeg exited unexpectedly" if self._active.process.returncode is not None
                         else None)
                return await self._finish(self._active, error)
        return await self.status(recording_id)

    async def close(self) -> None:
        async with self._lock:
            if self._active is not None:
                await self._finish(self._active)
