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
    data_dir: Path = Path("data")
    log_level: str = "INFO"


@dataclass
class PathsConfig:
    data_dir: Path
    raw_dir: Path
    processed_dir: Path
    subtitles_dir: Path
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


@dataclass
class RecordProxyConfig:
    enabled: bool = False
    mode: str = "auto"
    include_system: bool = True
    urls: list[str] = field(default_factory=list)
    file: Path | None = None
    source_url: str = ""
    cache_file: Path | None = None
    probe_url: str = "https://www.showroom-live.com/api/live/onlives"
    probe_timeout_seconds: float = 5.0
    source_timeout_seconds: float = 15.0
    refresh_seconds: int = 300


@dataclass
class RecordConfig:
    strategy: str = "streamlink"
    yt_dlp_bin: str = "yt-dlp"
    streamlink_bin: str = "streamlink"
    extra_args: list[str] = field(default_factory=list)
    streamlink_extra_args: list[str] = field(default_factory=list)
    cookies_file: Path | None = None
    min_file_size_mb: float = 5
    min_duration_seconds: float = 10
    max_seconds: int | None = None
    ffmpeg_fallback_to_ytdlp: bool = True
    streamlink_fallback_to_ffmpeg: bool = True
    reconnect_delay_seconds: float = 5.0
    live_end_confirmations: int = 4
    live_end_check_interval_seconds: float = 20.0
    hls_concurrent_fragments: int = 2
    hls_fragment_retries: int = 5
    capture_realtime_ratio_warning: float = 0.95
    proxy: RecordProxyConfig = field(default_factory=RecordProxyConfig)


@dataclass
class TranscodeConfig:
    enabled: bool = True
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = ""
    width: int | None = 1920
    height: int | None = 1080
    fps: int | None = None
    scale_mode: str = "fit"
    video_codec: str = "libx264"
    preset: str = "medium"
    crf: int = 20
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    extra_args: list[str] = field(default_factory=list)
    validate_av_sync: bool = True
    max_av_desync_seconds: float = 3.0


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
    burn_in: bool = False


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


def load_config(path: Path) -> AppConfig:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    base_dir = path.parent
    service_raw = dict(raw.get("service") or {})
    for legacy_key in (
        "upload_recovery_enabled",
        "upload_recovery_time",
        "upload_recovery_stale_minutes",
    ):
        service_raw.pop(legacy_key, None)
    service = ServiceConfig(**service_raw)
    service.data_dir = _resolve_path(base_dir, service.data_dir)
    service.poll_interval_seconds = max(1, int(service.poll_interval_seconds))
    service.status_parallelism = max(1, int(service.status_parallelism or 1))
    service.processing_parallelism = max(1, int(service.processing_parallelism or 1))
    service.record_retry_cooldown_seconds = max(0, int(service.record_retry_cooldown_seconds or 0))
    paths = _build_paths(service.data_dir)

    rooms_raw = raw.get("rooms") or []
    rooms = [_parse_room(item, service, base_dir) for item in rooms_raw]
    rooms = [room for room in rooms if room.enabled]
    if not rooms:
        raise ValueError("No enabled rooms configured. Edit rooms in config.yaml.")

    record_raw = dict(raw.get("record") or {})
    proxy_raw = record_raw.pop("proxy", None)
    record = RecordConfig(**record_raw)
    record.proxy = _parse_record_proxy(proxy_raw, base_dir, paths.data_dir)
    record.cookies_file = _optional_path(base_dir, record.cookies_file)
    record.min_file_size_mb = max(0.0, float(record.min_file_size_mb or 0.0))
    record.min_duration_seconds = max(0.0, float(record.min_duration_seconds or 0.0))
    record.reconnect_delay_seconds = max(0.0, float(record.reconnect_delay_seconds or 0.0))
    record.live_end_confirmations = max(1, int(record.live_end_confirmations or 1))
    record.live_end_check_interval_seconds = max(
        1.0,
        float(record.live_end_check_interval_seconds or 1.0),
    )
    record.hls_concurrent_fragments = min(
        10,
        max(1, int(record.hls_concurrent_fragments or 1)),
    )
    record.hls_fragment_retries = max(0, int(record.hls_fragment_retries or 0))
    record.capture_realtime_ratio_warning = min(
        1.0,
        max(0.0, float(record.capture_realtime_ratio_warning or 0.0)),
    )

    transcode = TranscodeConfig(**(raw.get("transcode") or {}))
    transcode.ffprobe_bin = str(transcode.ffprobe_bin or "").strip()
    transcode.max_av_desync_seconds = max(
        0.1,
        float(transcode.max_av_desync_seconds or 3.0),
    )

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

    naming_raw = raw.get("naming") or {}
    naming = NamingConfig(
        filename_template=str(
            naming_raw.get("filename_template")
            or NamingConfig.filename_template
        )
    )

    config = AppConfig(
        config_path=path,
        service=service,
        paths=paths,
        rooms=rooms,
        naming=naming,
        record=record,
        transcode=transcode,
        asr=asr,
        translation=TranslationConfig(**(raw.get("translation") or {})),
        subtitles=SubtitlesConfig(**(raw.get("subtitles") or {})),
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


def _parse_record_proxy(
    raw: dict[str, Any] | None,
    base_dir: Path,
    data_dir: Path,
) -> RecordProxyConfig:
    if raw is None:
        return RecordProxyConfig(cache_file=data_dir / "proxy" / "recording-proxies.cache")
    if not isinstance(raw, dict):
        raise ValueError("record.proxy must be a mapping")

    urls_raw = raw.get("urls") or []
    if isinstance(urls_raw, str):
        urls = [urls_raw.strip()] if urls_raw.strip() else []
    elif isinstance(urls_raw, list):
        urls = [str(value).strip() for value in urls_raw if str(value).strip()]
    else:
        raise ValueError("record.proxy.urls must be a string or list")

    mode = str(raw.get("mode", "auto") or "auto").strip().lower()
    if mode == "fallback":
        mode = "auto"
    if mode not in {"auto", "proxy_only", "off"}:
        raise ValueError("record.proxy.mode must be 'auto', 'proxy_only', or 'off'")

    cache_value = raw.get("cache_file")
    cache_file = (
        _optional_path(base_dir, cache_value)
        if cache_value not in (None, "")
        else data_dir / "proxy" / "recording-proxies.cache"
    )
    return RecordProxyConfig(
        enabled=bool(raw.get("enabled", False)),
        mode=mode,
        include_system=bool(raw.get("include_system", True)),
        urls=urls,
        file=_optional_path(base_dir, raw.get("file")),
        source_url=str(raw.get("source_url", "") or "").strip(),
        cache_file=cache_file,
        probe_url=str(
            raw.get("probe_url", "https://www.showroom-live.com/api/live/onlives")
            or "https://www.showroom-live.com/api/live/onlives"
        ).strip(),
        probe_timeout_seconds=max(0.5, float(raw.get("probe_timeout_seconds", 5.0) or 5.0)),
        source_timeout_seconds=max(1.0, float(raw.get("source_timeout_seconds", 15.0) or 15.0)),
        refresh_seconds=max(0, int(raw.get("refresh_seconds", 300) or 0)),
    )


def _build_paths(data_dir: Path) -> PathsConfig:
    return PathsConfig(
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        processed_dir=data_dir / "processed",
        subtitles_dir=data_dir / "subtitles",
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
