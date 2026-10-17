"""Configuration belongs to this service, independently of its MCP host."""

import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecorderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_device: str = Field(min_length=1)
    audio_device: str | None = Field(default=None, min_length=1)
    input_format: Literal["mjpeg", "h264", "yuyv422"] = "mjpeg"
    width: int = Field(default=1920, ge=160, le=7680)
    height: int = Field(default=1080, ge=120, le=4320)
    framerate: int = Field(default=30, ge=1, le=120)
    output_dir: Path = Path("data/recordings")
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    start_timeout_seconds: float = Field(default=20, gt=0, le=120)
    stop_timeout_seconds: float = Field(default=15, gt=0, le=60)


def load_config(path: Path) -> RecorderConfig:
    config = RecorderConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))
    config.output_dir = (path.resolve().parent / config.output_dir).resolve()
    for field in ("ffmpeg_path", "ffprobe_path"):
        value = getattr(config, field)
        if "/" in value or "\\" in value:
            value = str((path.resolve().parent / value).resolve())
        resolved = shutil.which(value)
        if resolved is None:
            raise ValueError(f"Executable not found: {field}")
        setattr(config, field, resolved)
    return config
