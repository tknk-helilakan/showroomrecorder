from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ServiceConfig:
    timezone: str = "Asia/Shanghai"
    poll_interval_seconds: int = 30
    status_parallelism: int = 2
    processing_parallelism: int = 1
    record_retry_cooldown_seconds: int = 180
    upload_recovery_enabled: bool = True
    upload_recovery_time: str = "03:00"
    upload_recovery_stale_minutes: int = 120
    data_dir: Path = Path("data")
    log_level: str = "INFO"


@dataclass
class PathsConfig:
    data_dir: Path
    raw_dir: Path
    processed_dir: Path
    subtitles_dir: Path
    danmaku_dir: Path
    upload_dir: Path
    work_dir: Path
    logs_dir: Path
    jobs_log: Path


@dataclass
class RoomConfig:
    name: str
    url: str
    room_id: int | None = None
    enabled: bool = True
    poll_interval_seconds: int | None = None
    cookies_file: Path | None = None


@dataclass
class NamingConfig:
    filename_template: str = "{streamer}_{started_at:%Y%m%d_%H%M%S}_{title_slug}"
    part_title_template: str = "{started_at:%Y%m%d} showroom 直播"
    title_template: str = "【{streamer}】SHOWROOM直播录像 {started_at:%Y-%m-%d %H:%M}"
    desc_template: str = "自动录制的 SHOWROOM 直播录像。"
    dynamic_template: str = "{streamer} SHOWROOM直播录像"


@dataclass
class RecordConfig:
    strategy: str = "yt_dlp"
    yt_dlp_bin: str = "yt-dlp"
    extra_args: list[str] = field(default_factory=list)
    cookies_file: Path | None = None
    min_file_size_mb: float = 5
    min_duration_seconds: float = 10
    max_seconds: int | None = None
    ffmpeg_fallback_to_ytdlp: bool = True


@dataclass
class TranscodeConfig:
    enabled: bool = True
    ffmpeg_bin: str = "ffmpeg"
    width: int | None = 1920
    height: int | None = 1080
    fps: int | None = 30
    scale_mode: str = "fit"
    video_codec: str = "libx264"
    preset: str = "medium"
    crf: int = 20
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AsrConfig:
    enabled: bool = True
    provider: str = "openai"
    model: str = "gpt-4o-transcribe-diarize"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    organization_env: str = "OPENAI_ORG_ID"
    project_env: str = "OPENAI_PROJECT_ID"
    trust_env: bool = False
    timeout_seconds: int = 300
    retries: int = 3
    chunk_seconds: int = 180
    max_file_size_mb: float = 24
    audio_format: str = "mp3"
    audio_bitrate: str = "64k"
    response_format: str = "diarized_json"
    chunking_strategy: str = "auto"
    prompt: str = ""
    device: str = "auto"
    compute_type: str = "auto"
    language: str = "ja"
    task: str = "transcribe"
    beam_size: int = 5
    vad_filter: bool = True
    vad_parameters: dict[str, Any] = field(default_factory=dict)
    condition_on_previous_text: bool = True
    temperature: float | list[float] | None = None
    no_speech_threshold: float | None = None
    log_prob_threshold: float | None = None
    compression_ratio_threshold: float | None = None
    word_timestamps: bool = False
    hallucination_silence_threshold: float | None = None
    initial_prompt: str = ""
    log_progress: bool = False
    normalize_audio: bool = True


@dataclass
class TranslationConfig:
    enabled: bool = True
    provider: str = "openai_responses"
    batch_size: int = 20
    retries: int = 3
    openai_responses: dict[str, Any] = field(default_factory=dict)
    openai_compatible: dict[str, Any] = field(default_factory=dict)
    transformers: dict[str, Any] = field(default_factory=dict)
    deepl: dict[str, Any] = field(default_factory=dict)
    argos: dict[str, Any] = field(default_factory=dict)
    external: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubtitlesConfig:
    max_line_chars: int = 24
    bilingual: bool = False


@dataclass
class DanmakuConfig:
    enabled: bool = False
    burn_in: bool = True
    poll_seconds: float = 2.0
    request_timeout_seconds: int = 10
    display_seconds: float = 8.0
    max_entries: int = 20000
    max_text_chars: int = 80
    max_user_name_chars: int = 16
    lane_count: int = 12
    font_name: str = "Microsoft YaHei"
    font_size: int = 28
    font_opacity: float = 0.6
    top_margin: int = 24
    bottom_margin: int = 140
    include_user_name: bool = True
    include_system_messages: bool = False


@dataclass
class UploadConfig:
    enabled: bool = False
    uploader: str = "biliup"
    subtitle_mode: str = "hard_subbed"
    cleanup_after_success: bool = False
    keep_latest_upload_per_room: bool = False
    biliup: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    config_path: Path
    service: ServiceConfig
    paths: PathsConfig
    rooms: list[RoomConfig]
    naming: NamingConfig
    record: RecordConfig
    transcode: TranscodeConfig
    asr: AsrConfig
    translation: TranslationConfig
    subtitles: SubtitlesConfig
    danmaku: DanmakuConfig
    upload: UploadConfig


def load_config(path: Path) -> AppConfig:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    base_dir = path.parent
    service = ServiceConfig(**(raw.get("service") or {}))
    service.data_dir = _resolve_path(base_dir, service.data_dir)
    service.poll_interval_seconds = max(1, int(service.poll_interval_seconds))
    service.status_parallelism = max(1, int(service.status_parallelism or 1))
    service.processing_parallelism = max(1, int(service.processing_parallelism or 1))
    service.record_retry_cooldown_seconds = max(0, int(service.record_retry_cooldown_seconds or 0))
    service.upload_recovery_stale_minutes = max(1, int(service.upload_recovery_stale_minutes or 1))
    paths = _build_paths(service.data_dir)

    rooms_raw = raw.get("rooms") or []
    rooms = [_parse_room(item, service, base_dir) for item in rooms_raw]
    rooms = [room for room in rooms if room.enabled]
    if not rooms:
        raise ValueError("No enabled rooms configured. Edit rooms in config.yaml.")

    record = RecordConfig(**(raw.get("record") or {}))
    record.cookies_file = _optional_path(base_dir, record.cookies_file)
    record.min_file_size_mb = max(0.0, float(record.min_file_size_mb or 0.0))
    record.min_duration_seconds = max(0.0, float(record.min_duration_seconds or 0.0))

    asr = AsrConfig(**(raw.get("asr") or {}))
    asr.task = str(asr.task or "transcribe").lower()
    if asr.task not in {"transcribe", "translate"}:
        raise ValueError("asr.task must be 'transcribe' or 'translate'")
    asr.beam_size = max(1, int(asr.beam_size or 1))
    if isinstance(asr.temperature, list):
        asr.temperature = [float(value) for value in asr.temperature]
    elif asr.temperature is not None:
        asr.temperature = float(asr.temperature)
    for field_name in (
        "no_speech_threshold",
        "log_prob_threshold",
        "compression_ratio_threshold",
        "hallucination_silence_threshold",
    ):
        value = getattr(asr, field_name)
        if value is not None:
            setattr(asr, field_name, float(value))

    danmaku = DanmakuConfig(**(raw.get("danmaku") or {}))
    danmaku.poll_seconds = max(0.5, float(danmaku.poll_seconds or 2.0))
    danmaku.request_timeout_seconds = max(1, int(danmaku.request_timeout_seconds or 10))
    danmaku.display_seconds = max(1.0, float(danmaku.display_seconds or 8.0))
    danmaku.max_entries = max(0, int(danmaku.max_entries or 0))
    danmaku.max_text_chars = max(1, int(danmaku.max_text_chars or 80))
    danmaku.max_user_name_chars = max(1, int(danmaku.max_user_name_chars or 16))
    danmaku.lane_count = max(1, int(danmaku.lane_count or 1))
    danmaku.font_size = max(8, int(danmaku.font_size or 28))
    danmaku.font_opacity = min(1.0, max(0.0, float(danmaku.font_opacity)))
    danmaku.top_margin = max(0, int(danmaku.top_margin or 0))
    danmaku.bottom_margin = max(0, int(danmaku.bottom_margin or 0))

    config = AppConfig(
        config_path=path,
        service=service,
        paths=paths,
        rooms=rooms,
        naming=NamingConfig(**(raw.get("naming") or {})),
        record=record,
        transcode=TranscodeConfig(**(raw.get("transcode") or {})),
        asr=asr,
        translation=TranslationConfig(**(raw.get("translation") or {})),
        subtitles=SubtitlesConfig(**(raw.get("subtitles") or {})),
        danmaku=danmaku,
        upload=UploadConfig(**(raw.get("upload") or {})),
    )
    _ensure_dirs(config.paths)
    return config


def _parse_room(raw: dict[str, Any], service: ServiceConfig, base_dir: Path) -> RoomConfig:
    if "name" not in raw or "url" not in raw:
        raise ValueError("Each room must include name and url.")
    room = RoomConfig(
        name=str(raw["name"]),
        url=str(raw["url"]),
        room_id=raw.get("room_id"),
        enabled=bool(raw.get("enabled", True)),
        poll_interval_seconds=raw.get("poll_interval_seconds") or service.poll_interval_seconds,
        cookies_file=_optional_path(base_dir, raw.get("cookies_file")),
    )
    if room.room_id is not None:
        room.room_id = int(room.room_id)
    return room


def _build_paths(data_dir: Path) -> PathsConfig:
    return PathsConfig(
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        processed_dir=data_dir / "processed",
        subtitles_dir=data_dir / "subtitles",
        danmaku_dir=data_dir / "danmaku",
        upload_dir=data_dir / "upload",
        work_dir=data_dir / "work",
        logs_dir=data_dir / "logs",
        jobs_log=data_dir / "jobs.jsonl",
    )


def _ensure_dirs(paths: PathsConfig) -> None:
    for item in (
        paths.data_dir,
        paths.raw_dir,
        paths.processed_dir,
        paths.subtitles_dir,
        paths.danmaku_dir,
        paths.upload_dir,
        paths.work_dir,
        paths.logs_dir,
    ):
        item.mkdir(parents=True, exist_ok=True)


def _resolve_path(base_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _optional_path(base_dir: Path, value: str | Path | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    return _resolve_path(base_dir, value)
