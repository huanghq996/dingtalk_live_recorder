# 钉钉群直播自动录制器

Windows 下的钉钉群直播监控与自动录制项目。程序启动后会自动连接或启动钉钉，按照配置的群聊优先级扫描直播状态，并在发现直播后开始录制。

## 功能

- 自动查找并启动钉钉。
- 每轮扫描完成后按 `scan_interval_seconds` 等待；默认 15 秒。
- 支持配置多个目标群聊，列表顺序即直播选择优先级。
- 同一时间只录制一个直播。
- 录制期间暂停定时扫描，不会并发打开或录制其他直播。
- 直播结束后自动生成 MKV 文件，然后恢复定时扫描。
- 自动创建录制结果目录和日志目录。
- 日志按天轮转，并根据配置清理过期日志。
- 使用 WASAPI 回环录制系统播放声音，不录制麦克风。

## 环境要求

- Windows 10 或 Windows 11。
- Python 3.12～3.14。
- [uv](https://docs.astral.sh/uv/)。
- 钉钉 Windows 桌面客户端，并已登录账号。
- FFmpeg 和 FFprobe，且二者均已加入 `PATH`。
- 至少一个可用的 Windows 音频播放端点，例如扬声器、耳机、HDMI 音频或虚拟声卡。

检查运行环境：

```powershell
uv --version
ffmpeg -version
ffprobe -version
```

## 安装依赖

进入项目目录：

```powershell
cd D:\workspace\huanghq\dingtalk_live_recorder
uv sync
```

`uv` 会根据 `pyproject.toml` 和 `uv.lock` 创建 `.venv` 并安装锁定版本的依赖。

## 配置

程序读取项目根目录下的 `config.yaml`：

```yaml
# 录制结果目录。相对路径以 config.yaml 所在目录为基准。
# 未配置或留空时使用 recordings。
output_dir: recordings

# 目标群聊。顺序即直播选择优先级。
target_groups:
  - 小品种
  - Lcai学员群14


# 每轮扫描完成后的等待时间，单位为秒；默认 15。
scan_interval_seconds: 15

# 视频编码器：auto 优先使用 NVIDIA GPU（NVENC），不可用时回退到 CPU。
# 也可设为 libx264 或 h264_nvenc。
recording:
  video_encoder: auto
log:
  # 日志目录。相对路径以 config.yaml 所在目录为基准。
  # 未配置或留空时使用 logs。
  directory: logs

  # 日志保存天数，必须是大于 0 的整数。
  retention_days: 30
```

### 录制目录

`output_dir` 支持相对路径和绝对路径。

未配置时默认为：

```text
项目目录\recordings
```

示例：

```yaml
output_dir: D:\recordings\dingtalk
```

目录不存在时会自动创建。目录创建失败时，程序异常退出。

### 扫描间隔

`scan_interval_seconds` 是每轮扫描**完成后**的等待秒数，必须是正整数，默认 `15`。例如设为 `30` 后，程序会在每轮扫描结束或录制结束后等待 30 秒再继续扫描。

### 视频编码器

`recording.video_encoder` 可取：

- `auto`（默认）：启动时以一帧实际编码测试 NVIDIA NVENC；成功则使用 GPU，失败时记录原因并回退到 CPU `libx264`。
- `libx264`：强制使用 CPU 软件编码。
- `h264_nvenc`：强制使用 NVIDIA NVENC；驱动、显卡或 FFmpeg 不可用时，启动失败而不会静默降级。

GPU 仅负责 H.264 编码。当前截图、Python 帧传输、OCR 前后处理和 WASAPI 音频采集仍在 CPU；详细录制路径请以日志中的 `H.264/h264_nvenc` 或 `H.264/libx264` 为准。

### 目标群聊和优先级

`target_groups` 必须是非空字符串列表：

```yaml
target_groups:
  - 第一优先级群聊
  - 第二优先级群聊
  - 第三优先级群聊
```

程序按照从上到下的顺序检查群聊。当多个群聊同时直播时，只打开并录制第一个检测到的直播。

如果本轮扫描能够识别至少一个目标群聊，但没有发现直播，程序会继续定时扫描。如果配置中的群聊全部无法识别，程序会记录错误并退出。

目标群聊需要出现在钉钉当前可见的会话列表区域内。群聊被折叠、隐藏或未显示在当前列表中时，OCR 可能无法识别。

### 日志

日志默认写入：

```text
项目目录\logs\dingtalk-live-recorder.log
```

日志每天轮转。程序启动时也会删除超过 `log.retention_days` 天的历史轮转日志。

## 运行

```powershell
cd D:\workspace\huanghq\dingtalk_live_recorder
uv run dingtalk-live-recorder
```

也可以通过 Python 模块运行：

```powershell
uv run python -m dingtalk_live_recorder
```

按 `Ctrl+C` 停止程序。如果正在录制，程序会先停止音视频编码并尝试完成当前录制文件。

## 后台运行

后台启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-recorder.ps1
```

停止后台进程：

```powershell
powershell -ExecutionPolicy Bypass -File .\stop-recorder.ps1
```

启动脚本将进程 ID 写入 `logs\dingtalk-live-recorder.pid.json`，并拒绝重复启动。停止脚本会终止该进程及其子进程；若正在录制，当前未完成的录制无法保证合并为最终 MKV 文件。

## 运行流程

1. 读取并校验 `config.yaml`。
2. 创建录制结果目录和日志目录。
3. 查找已经运行的钉钉主窗口；未找到时自动启动钉钉。
4. 初始化 MaaFramework OCR 和窗口控制器。
5. 按照 `target_groups` 顺序检查群聊。
6. 没有直播时按 `scan_interval_seconds` 等待，然后开始下一轮扫描。
7. 发现直播时打开直播窗口并暂停后续扫描。
8. 同步录制视频和系统播放音频。
9. 检测到“直播已结束”或直播窗口关闭后停止录制。
10. 合并音视频、校验录制文件，然后恢复定时扫描。

程序使用单线程扫描循环控制录制状态，因此不会同时录制两个直播，也不会在录制过程中积压定时扫描任务。

## 录制结果

默认输出文件名：

```text
群聊名称_YYYYMMDD_HHMMSS.mkv
```

例如：

```text
小品种_20260824_133651.mkv
```

录制参数：

- 容器：Matroska（MKV）。
- 视频：H.264、1280×720、30 FPS。
- 音频：AAC、128 kbps、48 kHz。
- 音频来源：默认 Windows 播放设备的 WASAPI 回环端点。
- 音量处理：FFmpeg `loudnorm`，目标综合响度约为 -16 LUFS。

录制完成后会使用 FFprobe 校验视频编码、音频编码、分辨率和帧率。校验成功后才删除中间音视频文件。

## 音频注意事项

程序不要求连接物理音箱，但 Windows 必须存在有效的播放端点，例如：

- 扬声器或耳机。
- HDMI/显示器音频。
- USB 或蓝牙音频设备。
- 虚拟声卡或虚拟音频线。

系统完全没有可用播放端点时，WASAPI 回环初始化会失败，无法录制声音。

不同声卡和驱动对系统主音量、静音与回环录音的处理可能不同。为避免录到静音：

- 不要将钉钉的应用音量设为 0。
- 录制期间保持默认播放设备启用。
- 不要在录制期间切换默认播放设备。
- 对目标电脑先进行一次短时直播录制验证。

## 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 正常停止，包括收到 `Ctrl+C` |
| `1` | FFmpeg、录音、OCR 或其他未预期运行错误 |
| `2` | 配置错误、录制目录创建失败或日志目录创建失败 |
| `3` | 找不到钉钉、钉钉启动失败或钉钉自动化初始化失败 |
| `4` | `target_groups` 中所有群聊均无法识别 |

## 常见问题

### 程序提示未找到 FFmpeg

确认 `ffmpeg.exe` 和 `ffprobe.exe` 已加入当前终端的 `PATH`：

```powershell
ffmpeg -version
ffprobe -version
```

修改环境变量后需要重新打开终端。

### 程序找不到钉钉

程序会检查：

- 当前正在运行的钉钉进程。
- `PATH` 中的 `DingTalk.exe`。
- `%LOCALAPPDATA%`、`%ProgramFiles%` 和 `%ProgramFiles(x86)%` 下的常见安装目录。
- 当前项目运行电脑使用的 `D:\install\DingDing` 安装目录。

如果钉钉安装在其他自定义目录，可以把 `DingTalk.exe` 所在目录加入 `PATH`。

### 群聊全部无法识别

检查以下条件：

- `target_groups` 中的名称与钉钉显示名称一致。
- 目标群聊位于当前可见的会话列表中。
- 钉钉主窗口没有被其他窗口遮挡或缩放到异常尺寸。
- Windows 显示缩放和钉钉界面没有在运行期间发生变化。

### 录制文件没有声音

检查：

- Windows 是否存在已启用的默认播放设备。
- 钉钉和系统音量混合器是否静音。
- 录制过程中是否切换了默认播放设备。
- 声卡驱动是否允许在静音状态下进行 WASAPI 回环录音。

### 直播期间为什么不再扫描其他群聊

这是预期行为。当前录制调用会阻塞扫描循环，直到直播结束并完成音视频合并。这样可以保证任意时刻最多只有一个直播录制任务。

## 测试

运行行为测试：

```powershell
uv run pytest
```

测试覆盖配置默认值、群聊优先级、录制互斥、目录创建、日志清理以及主要异常退出路径。
