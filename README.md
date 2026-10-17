# Webcam Recorder MCP

Windows 摄像头视频录制 MCP，用于自动化测试取证和复盘。标准 stdio；底层使用 FFmpeg DirectShow。已在 HD Pro Webcam C920 上验证 1920×1080 / 30fps。默认只录视频，可选麦克风。

## 安装

需要 Windows 10/11、Python 3.12/3.13、支持 DirectShow 和 libx264 的 FFmpeg，以及 ffprobe。无需 OBS 或 OpenCV。

```powershell
git clone https://github.com/FasenChen/webcam_recorder_mcp.git
cd webcam_recorder_mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
Copy-Item config.example.json config.json
.\.venv\Scripts\python.exe -m webcam_recorder_mcp --config config.json --check-config
```

锁文件含开发工具；只安装运行依赖也可用 `pip install -e .`。配置检查不代表摄像头已能实际录制，真实设备验证见下文。

## 配置与 MCP 接入

见 `config.example.json`。输出目录和带路径的可执行文件相对于配置文件目录解析；仅填 `ffmpeg` / `ffprobe` 则查 PATH。MCP 客户端可能继承不同的 PATH，接入时建议填写绝对路径。修改后重建 MCP 会话。

默认 C920、MJPEG 输入、1080p/30fps；先调用 `list_devices` 核对支持的模式，不自动换摄像头或降低画质。C920 的原始 YUY2 在 1080p 下只报告 5fps，30fps 请用 MJPEG 或 H.264 输入。

`audio_device=null` 不录音；需要时填音频设备名称，例如 `Microphone (HD Pro Webcam C920)`。输出为 H.264 及可选 AAC 的 MP4。对焦、曝光、条纹和镜头位置需要在电视工位校准。

将 `mcp.example.json` 的 `<REPO>` 替换为本仓库绝对路径，合并到客户端配置。服务不经过 shell、不监听网络端口，stdout 仅用于 MCP，诊断写入 stderr。

| 工具 | 参数 | 行为 |
|---|---|---|
| `list_devices` | 无 | 返回视频/音频设备名称和所配置摄像头支持的模式 |
| `start_recording` | 可选 UUID `recording_id` | 等待真实视频帧后返回；拒绝并发录制和覆盖已有 ID |
| `get_recording_status` | UUID `recording_id` | 查询活动录制或读取已持久化的历史结果 |
| `stop_recording` | UUID `recording_id` | 等待 FFmpeg 收尾并检查视频；重复停止返回保存结果 |

ID 可使用 TestRun UUID。返回结构化元数据和 JSON 文本，视频文件不会通过 MCP 消息传输。字段包括 `recording_id`、`status`、`video_device`、`audio_device`、`file_path`、`log_path`、`started_at`、`finished_at`、`frame_count`、`duration_seconds`、`size_bytes`、`error`。时间为 UTC；视频起点根据首个 FFmpeg 进度与媒体时间估算，适合事件对照，不适用于精密延迟测量。

每段文件位于 `<output_dir>/<UUID>/`：`video.mp4`、`recording.json`、`ffmpeg.log`。不覆盖、不自动删除。启动失败也保留日志和元数据；结束失败返回 `status="failed"`，调用方必须检查状态，不能仅凭工具调用成功判断录像完整。

会话关闭时停止录制；服务被强制结束时，Windows Job Object 回收 FFmpeg。MP4 每秒分片，尽量保留已写入内容，断电仍可能损失末尾内容。重启后查询未正常结束的旧录制会标记失败。单实例只录一段；不同实例争用设备由驱动处理。

## testing-react-agent

放在宿主 `mcp_servers/webcam_recorder_mcp/`，本地配置设置 `output_dir="../../data/recordings"`，根 `mcp.json` 注册服务名 `webcam`，宿主 `.env` 设置 `TEST_AGENT_RECORDING_MCP_SERVER=webcam`，随后重启宿主。宿主启动入口会运行 Alembic 0002 增量迁移，保留既有记录。

后端在第一步前开始录制，通过、失败、异常和取消后收尾；控制工具不交给模型。录制失败记为 BLOCKED，保留已完成的任务结果和可用的部分视频。视频属于运行，原截图属于任务；报告支持播放、Range 拖动和下载。JSON 仅含元数据；HTML 离线回放需同时下载 `<artifact_id>.mp4` 放在同一目录。

MCP 原文件保留在 `data/recordings`，宿主另存证据副本到 `data/artifacts`，会占两份视频空间，需自行管理磁盘。独立服务仓库只包含 MCP；宿主接入代码在 testing-react-agent 中。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe scripts/verify_camera.py --config config.json --seconds 5
```

pytest 使用子进程替身，不启动摄像头，并验证 Windows 强制退出的子进程回收。最后一个命令会真实录制，验证四个 MCP 工具、非法参数、互斥、状态、停止及重复停止，再完整解码并用 ffprobe 检查；结果写在输出目录旁的 `verification/`。配置、视频、日志和验证产物均忽略，不提交 Git。

首版仅 Windows DirectShow；长时间录制、音画同步、拔插以及电视拍屏效果仍需在目标工位验收。不提供直播、云上传或视频识别。
