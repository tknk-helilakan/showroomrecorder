# Showroom Recorder Pipeline

一个本地常驻服务，用来监听配置的 SHOWROOM 直播间，开播后自动录制、转码、生成日语识别字幕、翻译中文字幕，并按配置调用 `biliup` 上传到 Bilibili。

## 功能

- 轮询 SHOWROOM 直播间开播状态。
- 开播后调用 `yt-dlp` 录制直播流到本地。
- 调用 `ffmpeg` 保留源时间戳并转码为指定分辨率的 MP4。
- 可选捕获 SHOWROOM 弹幕，保存 `.danmaku.ass` / `.danmaku.jsonl`，并在转码时同步烧进画面。
- 调用 OpenAI Audio transcription API 做日语语音识别，生成 `.ja.srt`。
- 支持多种翻译后端生成 `.zh.srt`：
  - `openai_responses`：OpenAI Responses API，默认准确率优先。
  - `openai_compatible`：OpenAI、DeepSeek、SiliconFlow、Ollama/LM Studio 等兼容接口。
  - `deepl`：DeepL API。
  - `argos`：本地 Argos Translate。
  - `external`：你自己的翻译命令。
  - `none`：不翻译，只保留日语字幕。
- 支持把中文字幕硬压进 MP4 后上传，或只上传无硬字幕 MP4 并保留字幕文件。
- 上传通过 `biliup` 命令行完成，并可选尝试调用 Bilibili 字幕草稿接口上传字幕。
- 支持按月合集投稿：本月第一次自动新投稿，后续直播自动追加分 P。
- 可配置上传成功后只保留最终上传用 MP4 和字幕文件，清理中间产物。

请只录制和上传你有权处理的直播内容，并遵守 SHOWROOM 与 Bilibili 的平台规则。

## 环境要求

- Python 3.12
- FFmpeg 可执行文件在 `PATH` 中。
- `yt-dlp`，通过本项目依赖安装。
- `biliup`，用于 Bilibili 投稿。可以按 `biliup` 官方文档安装并登录。

## 版本包和打包策略

Release 会提供一份默认的 Windows x64 CPU 版 zip：`showroomrecorder-windows-x64-cpu.zip`。

这份包只包含程序运行时和示例配置，不包含：

- 本地 ASR 模型
- 本地翻译模型
- `config.yaml`
- Bilibili cookie
- `biliup.exe`
- FFmpeg
- 录制和输出数据

默认 Release 包面向“本地模型 + CPU 计算”的使用方式。使用者需要自己把模型下载到 `models/asr/` 和 `models/translation/`，并复制 `config.local-model.example.yaml` 为 `config.yaml` 后修改房间、模型路径、上传配置。

如果要用 NVIDIA GPU/CUDA、本机特定版本的 PyTorch，或只使用 OpenAI 在线服务，建议在自己的环境里从源码运行或重新执行 `build.ps1` 打包。这样 exe 会按本机安装的依赖和 CUDA/CPU 运行时生成。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

安装并登录 `biliup` 后，建议把 cookie 文件放在 `data\biliup-cookies.json`：

```powershell
New-Item -ItemType Directory -Force data
biliup -u data\biliup-cookies.json login
```

## 配置

复制示例配置：

```powershell
Copy-Item config.example.yaml config.yaml
```

然后编辑 `config.yaml`：

- `rooms`：填 SHOWROOM 房间名、`room_id` 和直播间 URL。
- `transcode`：设置输出分辨率、帧率、码率/CRF。
- `asr`：设置转写接口、模型、音频切片。
- `translation`：设置翻译后端。
- `upload`：设置 B 站标题、分区、标签、cookie 文件等。

## 运行

```powershell
.\.venv\Scripts\Activate.ps1
python -m showroomrecorder --config config.yaml
```

默认会在 `data\` 下生成：

- `raw`：同一直播任务的原始分段、FFconcat 清单和合并后的录制文件。
- `processed`：转码后的 MP4。
- `subtitles`：日语和中文字幕。
- `danmaku`：弹幕 JSONL 和 ASS 文件。
- `upload`：最终上传用视频。
- `jobs.jsonl`：任务流水日志。

## 运行流程

服务启动后会为 `rooms` 中每个启用的直播间建立一个监听任务。多个直播间可以同时监听；如果多个房间同时开播，也可以同时录制。

一次直播只对应一个逻辑任务。Streamlink/FFmpeg 因 403、超时或暂时没有新分片而退出时，服务会重新查询房间状态和最新播放地址；只要房间仍在线，就在同一个 `job_id` 下继续写入下一个分段。连续达到 `record.live_end_confirmations` 次下线确认后，才结束弹幕捕获并结算任务。

直播确认结束后，任务会进入处理队列，按顺序执行：

- 按录制顺序合并原始分段并重建连续时间戳
- 转码 MP4
- 用 `ffprobe` 校验音视频流结束时间，异常成品保留在本地但不上传
- 如果启用弹幕烧录，转码时把 `.danmaku.ass` 同步显示在画面中
- 日语语音识别
- 翻译中文字幕并生成字幕文件
- 准备上传视频
- 按配置上传 Bilibili

默认 `service.processing_parallelism: 1`，所以识别、翻译、压字幕、上传会一个一个完成。你可以监听和录制多个直播间，但后处理队列保持串行，避免同时跑多个大模型或上传任务。

## 常见配置说明

### 断流重连和直播结束确认

```yaml
record:
  reconnect_delay_seconds: 5
  live_end_confirmations: 4
  live_end_check_interval_seconds: 20
  hls_concurrent_fragments: 2
```

默认在录制进程退出后等待 5 秒重连。房间首次显示下线后，每 20 秒复查一次，连续 4 次确认下线（总计约 60 秒）才结束同一逻辑任务，因此短暂断流或主播快速重连不会产生多个独立上传。各段保存在 `raw/<主播>/<job_id>/segments/`，随后逐段重建从零开始的音视频 PTS，再通过 FFmpeg concat filter 合并为连续时间轴。`hls_concurrent_fragments` 默认使用 2，避免对 SHOWROOM CDN 发起过多并行分片请求。

弹幕 JSONL 保留原始墙钟时间；生成 ASS 时会按每段录制的实际媒体时长映射到合并后的视频时间轴。断流期间没有对应画面的评论不会烧录到后续片段上。

### 转码时间戳和音画校验

```yaml
transcode:
  fps:
  ffprobe_bin: ffprobe
  validate_av_sync: true
  max_av_desync_seconds: 3
```

SHOWROOM 的 MPEG-TS 元数据可能把实际约 30fps 的画面标成 20fps。`fps` 留空时程序按源 PTS 做 VFR 编码，不会使用名义帧率重算每一帧的时间。合并、转码、硬字幕输出以及上传恢复前都会比较视频流和音频流的结束时间；差值超过阈值时任务失败并停止上传，原始分段、合并文件、成品和 FFmpeg 日志都会保留用于检查。

### 录制代理回退

`record.proxy` 只作用于直播流下载，会同时传给 Streamlink、FFmpeg 和 yt-dlp，不会改变 Bilibili 上传线路。`mode: auto` 的顺序是：Windows 显式系统代理、当前系统/TUN 路由、项目代理。这样系统代理或 TUN 正常时保持原线路，直连录制失败后才使用项目代理。

```yaml
record:
  proxy:
    enabled: true
    mode: "auto"
    include_system: true
    urls:
      - "http://127.0.0.1:7897"
    file: ""
    source_url: ""
```

`urls`、`file` 和 `source_url` 可提供一个或多个 HTTP 代理端点。文件和远程内容支持 YAML、JSON、逐行文本以及 Base64 编码的端点列表；程序会探测并优先使用可用端点，远程列表的最后一次有效内容会写入 `cache_file`。

当前只接受 `http://` 或 `https://` 代理端点，不直接解析 `ss://`、`vmess://`、`trojan://` 等节点订阅。使用 Clash/Mihomo 时应让核心保持运行，并把它的本地 mixed-port（例如 `http://127.0.0.1:7897`）填入 `urls`；关闭系统代理或 TUN 开关不会影响该本地端口。

### OpenAI 转写和翻译

默认配置已经使用 OpenAI：

- 日语转写：`gpt-4o-transcribe-diarize`
- 日译中：`gpt-5.5`，`reasoning_effort: high`

先设置 API Key：

```powershell
$env:OPENAI_API_KEY="你的 API Key"
```

长视频会先用 FFmpeg 抽取音频并切成小段，再逐段上传到 OpenAI 转写接口。`gpt-4o-transcribe-diarize` 会返回分段时间，适合生成 SRT 字幕；如果改成 `gpt-4o-transcribe`，转写文本准确，但官方接口不返回字幕级时间戳，本项目会用近似时间切分。

### 翻译到中文字幕

使用 OpenAI Responses API：

```yaml
translation:
  provider: openai_responses
  openai_responses:
    base_url: "https://api.openai.com/v1"
    api_key_env: "OPENAI_API_KEY"
    model: "gpt-5.5"
    reasoning_effort: "high"
```

如果你用 Ollama 或 LM Studio 这类兼容接口，可以把 `translation.provider` 改回 `openai_compatible`。

### 第三方 OpenAI 兼容接口

可以把 `base_url` 和 `api_key_env` 改成第三方兼容端点，例如：

```yaml
asr:
  provider: openai_compatible
  base_url: "https://your-provider.example.com/v1"
  api_key_env: "THIRD_PARTY_API_KEY"
  model: "gpt-4o-transcribe-diarize"

translation:
  provider: openai_responses
  openai_responses:
    base_url: "https://your-provider.example.com/v1"
    api_key_env: "THIRD_PARTY_API_KEY"
    model: "gpt-5.5"
```

如果供应商只兼容 `/chat/completions`，把翻译改成：

```yaml
translation:
  provider: openai_compatible
  openai_compatible:
    base_url: "https://your-provider.example.com/v1"
    api_key_env: "THIRD_PARTY_API_KEY"
    model: "供应商文档中的模型名"
```

限制：只有第三方真实代理或提供对应模型别名时，才能填 OpenAI 模型名并消耗第三方 token。普通第三方 API Key 不能直接调用 OpenAI 托管的 `gpt-5.5` 或 `gpt-4o-transcribe-diarize`。音频转写还要求供应商兼容 `/audio/transcriptions`，并支持 `diarized_json` 或返回 `segments`，否则字幕时间轴只能近似生成。

### 本地下载模型

可以完全不用 API Key，改成本地模型：

- 日语识别：`faster_whisper`
- 日译中：`transformers_seq2seq`，指向本地 NLLB/M2M100 等 seq2seq 翻译模型目录

安装可选本地模型依赖：

```powershell
pip install -r requirements-local.txt
```

复制本地模型配置：

```powershell
Copy-Item config.local-model.example.yaml config.yaml
```

然后把模型路径改成你本机下载的位置：

```yaml
asr:
  provider: faster_whisper
  model: "models/asr/faster-whisper-large-v3"

translation:
  provider: transformers_seq2seq
  transformers:
    model_path: "models/translation/nllb-200-distilled-600M"
    source_lang: "jpn_Jpan"
    target_lang: "zho_Hans"
```

运行命令不变：

```powershell
python -m showroomrecorder --config config.yaml
```

说明：`faster_whisper` 的本地路径建议使用 CTranslate2/faster-whisper 格式模型目录；NLLB/M2M100 翻译模型使用 Hugging Face Transformers 格式目录。CPU 也能跑，但速度会比较慢；有 NVIDIA 显卡时建议 `device: cuda`。

Release 默认 CPU 配置建议：

```yaml
asr:
  provider: faster_whisper
  device: "cpu"
  compute_type: "int8"

translation:
  provider: transformers_seq2seq
  transformers:
    device: "cpu"
    torch_dtype: "float32"
    batch_size: 1
```

GPU 用户可以改成：

```yaml
asr:
  device: "cuda"
  compute_type: "float16"

translation:
  transformers:
    device: "cuda"
    torch_dtype: "float16"
```

### 弹幕捕获和烧录

启用后，录制直播的同时会轮询 SHOWROOM 的 `comment_log`，实时保存原始弹幕 JSONL，并在录制结束后生成可被 FFmpeg 使用的 ASS 弹幕文件：

```yaml
danmaku:
  enabled: true
  burn_in: true
  poll_seconds: 2
  display_seconds: 14
  lane_count: 12
  font_size: 32
  font_opacity: 0.6
  max_user_name_chars: 16
  include_user_name: true
  include_system_messages: false
```

`burn_in: true` 时，弹幕会在转码阶段直接烧进 `processed` MP4；最终 `upload` 目录也会保留同名 `.danmaku.ass` 和 `.danmaku.jsonl` sidecar，方便之后检查或重新处理。

### 上传 Bilibili

上传使用 `biliup`，配置示例：

```yaml
upload:
  enabled: true
  uploader: biliup
  subtitle_mode: sidecar
  cleanup_after_success: true
  keep_latest_upload_per_room: true
  biliup:
    mode: monthly
    bin: biliup
    user_cookie: "data/biliup-cookies.json"
    subtitle_language: zh
    upload_subtitle_draft: true
    subtitle_page_wait_seconds: 900
    subtitle_page_poll_seconds: 30
```

默认 `subtitle_mode: hard_subbed`，会先生成一个硬字幕 MP4 再投稿，避免 B 站字幕上传接口变化导致字幕不可见。生成的 `.zh.srt` 仍会保留在本地。

`biliup.mode` 可选：

- `upload`：每次都新建投稿。
- `append`：追加到 `append_vid` 或 `append_vids` 指定的已有投稿。
- `monthly`：按 `monthly_key_template` 区分主播和月份；第一次新建投稿并把 BVID 写入 `data/biliup-monthly.json`，之后自动追加分 P。

按月合集可以这样命名：

```yaml
naming:
  title_template: "【高嶺のなでしこ-{streamer}】{started_at:%Y%m} showroom 直播合集"
  part_title_template: "{started_at:%Y%m%d} showroom 直播"
```

如果想尝试上传 B 站字幕草稿：

```yaml
upload:
  biliup:
    upload_subtitle_draft: true
    subtitle_language: zh
    subtitle_page_wait_seconds: 900
    subtitle_page_poll_seconds: 30
    subtitle_save_attempts: 3
    subtitle_save_retry_seconds: 5
    subtitle_errors_fatal: true
```

这一步依赖 Bilibili 未公开接口，可能因为账号、审核、接口变化而失败。默认 `subtitle_errors_fatal: true`，字幕上传失败会让本次任务标记为失败并保留中间文件，方便重新生成或重传；如果只想视频投稿成功即可，可以改成 `false`。

## 本地打包

如果你要按自己的环境生成 exe，先安装对应依赖，再运行：

```powershell
.\build.ps1
```

打包结果在：

```text
dist/showroomrecorder/showroomrecorder.exe
```

常见选择：

- CPU 本地模型：安装普通 `torch` CPU 版和 `requirements-local.txt`。
- GPU 本地模型：先安装匹配自己 CUDA 版本的 PyTorch，再安装 `requirements-local.txt`。
- OpenAI 在线服务：只需要基础依赖和 PyInstaller，模型目录可以为空。

## GitHub 自动发布

仓库里的 `.github/workflows/release.yml` 会在推送 `v*` tag 时自动构建 Windows x64 CPU 版 zip，并上传到 GitHub Release。

发布一个版本：

```powershell
git status
git add .gitignore .github/workflows/release.yml README.md config.local-model.example.yaml showroomrecorder
git commit -m "Prepare v0.1.0 release"
git tag -a v0.1.0 -m "v0.1.0"
git push origin main
git push origin v0.1.0
```

推送 tag 后，到 GitHub 的 Actions 页面等 `Build Windows CPU Release` 完成。成功后，Release 页面会出现 `showroomrecorder-windows-x64-cpu.zip`。

如果只是想生成 Actions Artifact 而不发 tag，可以在 GitHub Actions 页面手动运行 `workflow_dispatch`。
