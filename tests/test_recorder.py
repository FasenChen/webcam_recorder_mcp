import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from webcam_recorder_mcp.config import RecorderConfig, load_config
from webcam_recorder_mcp.recorder import Recorder


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    capture = tmp_path / "capture.py"
    capture.write_text('''import sys, time
from pathlib import Path
mode, target = sys.argv[1:]
if mode == "exit":
    sys.exit(3)
Path(target).write_bytes(b"test-video")
if mode not in ("empty", "ignore-stop"):
    print("frame=30\\nout_time_us=1000000\\nprogress=continue", flush=True)
if mode == "crash":
    time.sleep(0.5)
    sys.exit(4)
if mode == "ignore-stop":
    time.sleep(60)
else:
    sys.stdin.readline()
''', encoding="utf-8")
    config = RecorderConfig(video_device="ok", output_dir=tmp_path / "recordings",
                            start_timeout_seconds=2, stop_timeout_seconds=0.2)
    result = Recorder(config)
    monkeypatch.setattr(result, "_command", lambda target: [
        sys.executable, str(capture), config.video_device, str(target),
    ])

    async def probe(*args):
        return 0, json.dumps({"streams": [{"codec_type": "video"}],
                              "format": {"duration": "1.0"}}), ""

    monkeypatch.setattr(result, "_query", probe)
    return result


async def test_recording_exclusion_stop_and_persisted_readback(recorder: Recorder) -> None:
    recording = await recorder.start()
    assert recording.frame_count == 30 and recording.status == "recording"
    assert (await recorder.status(recording.recording_id)).size_bytes > 0
    with pytest.raises(ValueError, match="already recording"):
        await recorder.start()
    stopped = await recorder.stop(recording.recording_id)
    assert stopped.status == "completed" and stopped.duration_seconds == 1
    assert stopped.finished_at is not None and stopped.error is None
    assert await recorder.stop(recording.recording_id) == stopped
    assert await Recorder(recorder.config).status(recording.recording_id) == stopped
    with pytest.raises(FileExistsError):
        await recorder.start(recording.recording_id)


@pytest.mark.parametrize("mode", ["exit", "empty", "ignore-stop"])
async def test_failed_start_cleans_process_and_preserves_metadata(
    recorder: Recorder, mode: str,
) -> None:
    recorder.config.video_device = mode
    recorder.config.start_timeout_seconds = 0.3
    recording_id = uuid4()
    with pytest.raises((RuntimeError, TimeoutError)):
        await recorder.start(recording_id)
    result = await recorder.status(recording_id)
    assert result.status == "failed" and result.error
    assert recorder._active is None
    recorder.config.video_device = "ok"
    await recorder.start()
    await recorder.close()


async def test_cancelled_start_releases_camera(recorder: Recorder) -> None:
    recorder.config.video_device = "empty"
    recording_id = uuid4()
    task = asyncio.create_task(recorder.start(recording_id))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder._active is None
    assert (await recorder.status(recording_id)).status == "failed"


async def test_crashed_capture_is_not_completed(recorder: Recorder) -> None:
    recorder.config.video_device = "crash"
    recording = await recorder.start()
    assert recorder._active is not None
    await recorder._active.process.wait()
    result = await recorder.stop(recording.recording_id)
    assert result.status == "failed" and "unexpectedly" in (result.error or "")
    assert Path(result.file_path).is_file()


async def test_invalid_video_is_retained_and_marked_failed(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def invalid_probe(*args):
        return 1, "", "invalid input"
    monkeypatch.setattr(recorder, "_query", invalid_probe)
    recording = await recorder.start()
    result = await recorder.stop(recording.recording_id)
    assert result.status == "failed" and "readable video" in (result.error or "")
    assert Path(result.file_path).read_bytes() == b"test-video"


async def test_cancelled_finalization_never_claims_complete_video(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probing = asyncio.Event()

    async def wait_probe(*args):
        probing.set()
        await asyncio.Event().wait()

    recording = await recorder.start()
    monkeypatch.setattr(recorder, "_query", wait_probe)
    stop = asyncio.create_task(recorder.stop(recording.recording_id))
    await asyncio.wait_for(probing.wait(), 2)
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert recorder._active is None
    assert (await recorder.status(recording.recording_id)).status == "failed"


async def test_close_stops_recording_and_restart_detects_interruption(recorder: Recorder) -> None:
    recording = await recorder.start()
    await recorder.close()
    assert (await recorder.status(recording.recording_id)).status == "completed"
    path = Path(recording.file_path).with_name("recording.json")
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["status"] = "recording"
    path.write_text(json.dumps(stale), encoding="utf-8")
    assert (await Recorder(recorder.config).status(recording.recording_id)).status == "failed"


def test_config_paths_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"video_device": "test", "ffmpeg_path": sys.executable,
                                "ffprobe_path": sys.executable}), encoding="utf-8-sig")
    assert load_config(path).output_dir == tmp_path / "data/recordings"
    with pytest.raises(ValidationError):
        RecorderConfig(video_device="", framerate=0, unknown=True)
    with pytest.raises(ValueError):
        uuid4().__class__("../../outside")


def test_command_uses_configured_input_and_fragmented_mp4(recorder: Recorder) -> None:
    args = Recorder(recorder.config)._command(Path("output.mp4"))
    assert args[args.index("-vcodec") + 1] == "mjpeg"
    assert args[args.index("-video_size") + 1] == "1920x1080"
    assert "-n" in args and "-an" in args
    assert "frag_keyframe" in args[args.index("-movflags") + 1]
    recorder.config.audio_device = "Mic"
    args = Recorder(recorder.config)._command(Path("output.mp4"))
    assert "video=ok:audio=Mic" in args and "-c:a" in args
