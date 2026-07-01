from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml

from .config import AppConfig
from .models import LiveSession, SubtitleSegment
from .subtitles import to_bilibili_subtitle_json
from .templating import build_context, render_template

LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
PREUPLOAD_URL = "https://member.bilibili.com/preupload"
ARCHIVE_VIEW_URL = "https://member.bilibili.com/x/client/archive/view"
ADD_URL = "https://member.bilibili.com/x/vu/web/add/v3"
EDIT_URL = "https://member.bilibili.com/x/vu/web/edit"


class BiliupUploader:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._state_lock = threading.Lock()

    def upload(self, session: LiveSession, segments: list[SubtitleSegment] | None = None) -> str | None:
        if not self.config.upload.enabled:
            LOGGER.info("Upload disabled; skipping")
            return None
        if self.config.upload.uploader != "biliup":
            raise ValueError(f"Unsupported upload.uploader: {self.config.upload.uploader}")
        if not session.upload_file:
            raise RuntimeError("No upload_file set for session")

        biliup_cfg = self.config.upload.biliup
        user_cookie = biliup_cfg.get("user_cookie")
        use_small_chunks = self._use_small_chunk_upload()
        bin_name = str(biliup_cfg.get("bin", "biliup"))
        if not use_small_chunks and shutil.which(bin_name) is None:
            raise RuntimeError(f"biliup executable not found in PATH: {bin_name}")

        mode = str(biliup_cfg.get("mode", "upload")).lower()
        context = self._context(session)
        part_title = render_template(
            self.config.naming.part_title_template,
            context,
        )
        subtitle_part_titles: list[str] = []
        prefer_last_part = False

        if mode == "append":
            if use_small_chunks:
                bvid = self._append_with_small_chunks(
                    session=session,
                    user_cookie=user_cookie,
                    part_title=part_title,
                )
            else:
                bvid, _output = self._append_with_biliup(
                    session=session,
                    bin_name=bin_name,
                    user_cookie=user_cookie,
                    part_title=part_title,
                )
            subtitle_part_titles = self._subtitle_part_title_candidates(session, part_title)
            prefer_last_part = True
        elif mode in {"monthly", "auto_monthly", "monthly_append"}:
            monthly_bvid = self._resolve_monthly_vid(session)
            if monthly_bvid:
                if use_small_chunks:
                    bvid = self._append_with_small_chunks(
                        session=session,
                        user_cookie=user_cookie,
                        part_title=part_title,
                        vid=monthly_bvid,
                    )
                else:
                    bvid, _output = self._append_with_biliup(
                        session=session,
                        bin_name=bin_name,
                        user_cookie=user_cookie,
                        part_title=part_title,
                        vid=monthly_bvid,
                    )
                subtitle_part_titles = self._subtitle_part_title_candidates(session, part_title)
                prefer_last_part = True
            else:
                if use_small_chunks:
                    bvid = self._upload_new_with_small_chunks(session=session, user_cookie=user_cookie)
                else:
                    bvid, _output = self._upload_new_with_biliup(
                        session=session,
                        bin_name=bin_name,
                        user_cookie=user_cookie,
                    )
                if not bvid:
                    raise RuntimeError("Biliup upload succeeded but no BVID was detected; cannot remember monthly submission")
                self._remember_monthly_vid(session, bvid)
                subtitle_part_titles = self._subtitle_part_title_candidates(session, None)
                prefer_last_part = True
        elif mode == "upload":
            if use_small_chunks:
                bvid = self._upload_new_with_small_chunks(session=session, user_cookie=user_cookie)
            else:
                bvid, _output = self._upload_new_with_biliup(
                    session=session,
                    bin_name=bin_name,
                    user_cookie=user_cookie,
                )
        else:
            raise ValueError(f"Unsupported upload.biliup.mode: {mode}")

        if bvid:
            LOGGER.info("Biliup output detected BVID: %s", bvid)
            session.bvid = bvid

        if (
            bvid
            and segments
            and bool(biliup_cfg.get("upload_subtitle_draft", False))
            and session.zh_srt_file
        ):
            session.metadata.pop("subtitle_uploaded", None)
            session.metadata.pop("subtitle_upload_error", None)
            session.metadata["subtitle_upload_attempted"] = True
            try:
                SubtitleDraftUploader(
                    cookie_file=self._resolve_config_path(user_cookie),
                    language=str(biliup_cfg.get("subtitle_language", "zh")),
                    trust_env=bool(biliup_cfg.get("trust_env", False)),
                    page_wait_seconds=int(biliup_cfg.get("subtitle_page_wait_seconds") or 900),
                    page_poll_seconds=int(biliup_cfg.get("subtitle_page_poll_seconds") or 30),
                ).upload(
                    bvid,
                    segments,
                    part_titles=subtitle_part_titles,
                    prefer_last=prefer_last_part,
                    allow_unmatched_fallback=_bool_value(
                        biliup_cfg.get("subtitle_allow_unmatched_fallback", False),
                        default=False,
                    ),
                )
                session.metadata["subtitle_uploaded"] = True
            except Exception as exc:  # noqa: BLE001
                session.metadata["subtitle_upload_error"] = str(exc)
                LOGGER.warning("Bilibili subtitle draft upload failed: %s", exc)
                if _bool_value(biliup_cfg.get("subtitle_errors_fatal", False), default=False):
                    raise
        return bvid

    def _use_small_chunk_upload(self) -> bool:
        cfg = self.config.upload.biliup
        nested = cfg.get("small_chunk")
        if isinstance(nested, dict) and "enabled" in nested:
            return _bool_value(nested.get("enabled"), default=True)
        if "small_chunk_upload" in cfg:
            return _bool_value(cfg.get("small_chunk_upload"), default=True)
        if "small_chunk_enabled" in cfg:
            return _bool_value(cfg.get("small_chunk_enabled"), default=True)
        return True

    def _small_chunk_value(self, names: list[str], default: Any) -> Any:
        cfg = self.config.upload.biliup
        nested = cfg.get("small_chunk")
        for name in names:
            if isinstance(nested, dict) and name in nested:
                return nested[name]
            key = f"small_chunk_{name}"
            if key in cfg:
                return cfg[key]
        return default

    def _small_chunk_uploader(self, session: LiveSession, user_cookie: Any) -> "SmallChunkBilibiliUploader":
        if not session.upload_file:
            raise RuntimeError("No upload_file set for session")
        cfg = self.config.upload.biliup
        chunk_mib = int(self._small_chunk_value(["chunk_mib", "chunk_mb", "mib", "mb"], 2))
        workers = int(self._small_chunk_value(["workers"], 3))
        retries = int(self._small_chunk_value(["retries"], 12))
        proxy = self._small_chunk_value(["proxy"], cfg.get("proxy") or None)
        direct_upos = _bool_value(self._small_chunk_value(["direct_upos"], True), default=True)
        line = str(self._small_chunk_value(["line"], cfg.get("line", "bda2")) or "bda2")
        checkpoint = session.work_dir / f"{session.upload_file.name}.small-chunks.json"
        result_file = session.work_dir / "small-chunk-upload-result.json"
        return SmallChunkBilibiliUploader(
            cookie_file=self._resolve_config_path(user_cookie),
            checkpoint=checkpoint,
            result_file=result_file,
            proxy=str(proxy) if proxy else None,
            direct_upos=direct_upos,
            line=line,
            chunk_mib=max(1, chunk_mib),
            workers=max(1, workers),
            retries=max(1, retries),
        )

    def _upload_new_with_small_chunks(
        self,
        *,
        session: LiveSession,
        user_cookie: Any,
    ) -> str | None:
        if not session.upload_file:
            raise RuntimeError("No upload_file set for session")
        self._write_biliup_config(session)
        result = self._small_chunk_uploader(session, user_cookie).upload_new(
            session.upload_file,
            metadata=self._biliup_metadata_item(session),
            part_title=session.upload_file.stem,
        )
        return str(result.get("bvid") or "") or None

    def _append_with_small_chunks(
        self,
        *,
        session: LiveSession,
        user_cookie: Any,
        part_title: str,
        vid: str | None = None,
    ) -> str:
        if not session.upload_file:
            raise RuntimeError("No upload_file set for session")
        vid = vid or self._resolve_append_vid(session)
        result = self._small_chunk_uploader(session, user_cookie).append(
            session.upload_file,
            bvid=vid,
            part_title=session.upload_file.stem,
        )
        return str(result.get("bvid") or vid)

    def _upload_new_with_biliup(
        self,
        *,
        session: LiveSession,
        bin_name: str,
        user_cookie: Any,
    ) -> tuple[str | None, str]:
        biliup_cfg = self.config.upload.biliup
        upload_config = self._write_biliup_config(session)
        command = [bin_name]
        if user_cookie:
            command.extend(["-u", str(self._resolve_config_path(user_cookie))])
        command.extend(["upload", "-c", str(upload_config)])
        command.extend(str(item) for item in biliup_cfg.get("extra_args", []))

        output = self._run(command, session.work_dir / "biliup-upload.log")
        return _extract_bvid(output), output

    def _append_with_biliup(
        self,
        *,
        session: LiveSession,
        bin_name: str,
        user_cookie: Any,
        part_title: str,
        vid: str | None = None,
    ) -> tuple[str, str]:
        cfg = self.config.upload.biliup
        vid = vid or self._resolve_append_vid(session)
        command = [bin_name]
        if user_cookie:
            command.extend(["-u", str(self._resolve_config_path(user_cookie))])
        command.extend(["append", "--vid", vid])
        command.extend(["--line", str(cfg.get("line", "kodo"))])
        command.extend(["--limit", str(int(cfg.get("limit", 3)))])
        command.extend(["--copyright", str(int(cfg.get("copyright", 2)))])
        command.extend(["--source", render_template(str(cfg.get("source_template", "{room_url}")), self._context(session))])
        command.extend(["--tid", str(int(cfg.get("tid", 21)))])
        command.extend(["--cover", str(cfg.get("cover", ""))])
        command.extend(["--title", part_title])
        command.extend(["--desc", render_template(self.config.naming.desc_template, self._context(session))])
        command.extend(["--dynamic", render_template(self.config.naming.dynamic_template, self._context(session))])
        tags = cfg.get("tags", [])
        tag_value = ",".join(str(item) for item in tags) if isinstance(tags, list) else str(tags)
        command.extend(["--tag", tag_value])
        command.extend(str(item) for item in cfg.get("extra_args", []))
        command.append(str(session.upload_file))
        output = self._run(command, session.work_dir / "biliup-append.log")
        return vid, output

    def _resolve_append_vid(self, session: LiveSession) -> str:
        vid = self._resolve_append_vid_optional(session)
        if vid:
            return vid
        raise RuntimeError(
            "upload.biliup.mode is append, but append_vid or append_vids is not configured"
        )

    def _resolve_append_vid_optional(self, session: LiveSession) -> str | None:
        cfg = self.config.upload.biliup
        mappings = cfg.get("append_vids") or {}
        if isinstance(mappings, dict):
            for key in (session.room.name, str(session.room.room_id or "")):
                value = mappings.get(key)
                if value:
                    return str(value)
        vid = cfg.get("append_vid") or cfg.get("vid")
        if vid:
            return str(vid)
        return None

    def _resolve_monthly_vid(self, session: LiveSession) -> str | None:
        manual_vid = self._resolve_append_vid_optional(session)
        if manual_vid:
            return manual_vid
        with self._state_lock:
            state = self._load_monthly_state()
        key = self._monthly_key(session)
        item = (state.get("items") or {}).get(key)
        if isinstance(item, dict) and item.get("bvid"):
            return str(item["bvid"])
        if isinstance(item, str) and item:
            return item
        return None

    def _remember_monthly_vid(self, session: LiveSession, bvid: str) -> None:
        with self._state_lock:
            state = self._load_monthly_state()
            items = state.setdefault("items", {})
            items[self._monthly_key(session)] = {
                "bvid": bvid,
                "streamer": session.room.name,
                "room_id": session.room.room_id,
                "month": f"{session.started_at:%Y%m}",
                "title": render_template(self.config.naming.title_template, self._context(session)),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._save_monthly_state(state)
        LOGGER.info("Remembered monthly BVID for %s: %s", session.room.name, bvid)

    def _monthly_key(self, session: LiveSession) -> str:
        template = str(self.config.upload.biliup.get("monthly_key_template", "{streamer}:{started_at:%Y%m}"))
        return render_template(template, self._context(session))

    def _monthly_state_path(self) -> Path:
        value = self.config.upload.biliup.get("monthly_state_file")
        if value:
            return self._resolve_config_path(value)
        return self.config.paths.data_dir / "biliup-monthly.json"

    def _load_monthly_state(self) -> dict[str, Any]:
        path = self._monthly_state_path()
        if not path.exists():
            return {"version": 1, "items": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse monthly state file: {path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Monthly state file must contain a JSON object: {path}")
        data.setdefault("version", 1)
        data.setdefault("items", {})
        return data

    def _save_monthly_state(self, state: dict[str, Any]) -> None:
        path = self._monthly_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_biliup_config(self, session: LiveSession) -> Path:
        cfg = self.config.upload.biliup
        upload_file = session.upload_file.resolve().as_posix()
        payload = {
            "line": str(cfg.get("line", "kodo")),
            "limit": int(cfg.get("limit", 3)),
            "streamers": {upload_file: self._biliup_metadata_item(session)},
        }
        path = session.work_dir / "biliup-upload.yaml"
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def _biliup_metadata_item(self, session: LiveSession) -> dict[str, Any]:
        cfg = self.config.upload.biliup
        context = self._context(session)
        tags = cfg.get("tags", [])
        if isinstance(tags, list):
            tag_value = ",".join(str(item) for item in tags)
        else:
            tag_value = str(tags)
        open_subtitle = _bool_value(cfg.get("open_subtitle", True), default=True)
        return {
            "copyright": int(cfg.get("copyright", 2)),
            "source": render_template(str(cfg.get("source_template", "{room_url}")), context),
            "tid": int(cfg.get("tid", 21)),
            "cover": str(cfg.get("cover", "")),
            "title": render_template(self.config.naming.title_template, context),
            "desc_format_id": int(cfg.get("desc_format_id", 0)),
            "desc": render_template(self.config.naming.desc_template, context),
            "dynamic": render_template(self.config.naming.dynamic_template, context),
            "tag": tag_value,
            "open_subtitle": open_subtitle,
            "subtitle": {
                "open": 1 if open_subtitle else 0,
                "lan": str(cfg.get("subtitle_language", "zh")),
            },
        }

    def _subtitle_part_title_candidates(self, session: LiveSession, part_title: str | None) -> list[str]:
        candidates: list[str] = []
        if session.upload_file:
            candidates.append(session.upload_file.stem)
        if part_title:
            candidates.append(part_title)
            candidates.append(part_title.replace(" ", "_"))
        return list(dict.fromkeys(item.strip() for item in candidates if item.strip()))

    def _context(self, session: LiveSession) -> dict[str, Any]:
        return build_context(
            streamer=session.room.name,
            room_url=session.room.url,
            room_id=session.room.room_id,
            title=session.live_title,
            started_at=session.started_at,
            ended_at=session.ended_at,
            job_id=session.job_id,
        )

    def _run(self, command: list[str], log_file: Path) -> str:
        LOGGER.info("Starting upload command: %s", " ".join(command))
        output_lines: list[str] = []
        with log_file.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                output_lines.append(line)
                log.write(line)
                log.flush()
                LOGGER.info("[upload] %s", line.rstrip())
            code = process.wait()
        output = "".join(output_lines)
        if code != 0:
            raise RuntimeError(f"Upload command failed with exit code {code}. See log: {log_file}")
        return output

    def _resolve_config_path(self, value: str | os.PathLike | None) -> Path:
        if not value:
            raise ValueError("A cookie file path is required")
        path = Path(value)
        if not path.is_absolute():
            path = self.config.config_path.parent / path
        return path.resolve()


class SmallChunkBilibiliUploader:
    def __init__(
        self,
        *,
        cookie_file: Path,
        checkpoint: Path,
        result_file: Path,
        proxy: str | None = None,
        direct_upos: bool = True,
        line: str = "bda2",
        chunk_mib: int = 2,
        workers: int = 3,
        retries: int = 12,
    ) -> None:
        self.cookie_file = cookie_file
        self.checkpoint = checkpoint
        self.result_file = result_file
        self.proxy = proxy
        self.direct_upos = direct_upos
        self.line = line
        self.chunk_size = max(1, int(chunk_mib)) * 1024 * 1024
        self.workers = max(1, int(workers))
        self.retries = max(1, int(retries))
        self.cookies, self.access_token = _load_biliup_login(cookie_file)
        self.csrf = self.cookies.get("bili_jct")
        if not self.csrf:
            raise RuntimeError("Cookie file does not contain bili_jct")
        if not self.access_token:
            raise RuntimeError("Cookie file does not contain access_token")
        self._checkpoint_lock = threading.Lock()
        self._thread_local = threading.local()
        self.video: Path | None = None

    def upload_new(
        self,
        video: Path,
        *,
        metadata: dict[str, Any],
        part_title: str | None = None,
    ) -> dict[str, Any]:
        return self._run_with_checkpoint_reset(
            video,
            mode="new",
            metadata=dict(metadata),
            bvid=None,
            part_title=part_title,
        )

    def append(
        self,
        video: Path,
        *,
        bvid: str,
        part_title: str | None = None,
    ) -> dict[str, Any]:
        return self._run_with_checkpoint_reset(
            video,
            mode="append",
            metadata=None,
            bvid=bvid,
            part_title=part_title,
        )

    def _run_with_checkpoint_reset(
        self,
        video: Path,
        *,
        mode: str,
        metadata: dict[str, Any] | None,
        bvid: str | None,
        part_title: str | None,
    ) -> dict[str, Any]:
        last_error: RuntimeError | None = None
        for attempt in range(2):
            try:
                return self._run(
                    video,
                    mode=mode,
                    metadata=metadata,
                    bvid=bvid,
                    part_title=part_title,
                )
            except RuntimeError as exc:
                last_error = exc
                if attempt == 0 and self.checkpoint.exists() and _looks_like_expired_upload(exc):
                    LOGGER.warning(
                        "Small chunk upload checkpoint appears expired; recreating upload session: %s",
                        exc,
                    )
                    self.checkpoint.unlink(missing_ok=True)
                    continue
                raise
        assert last_error is not None
        raise last_error

    def _run(
        self,
        video: Path,
        *,
        mode: str,
        metadata: dict[str, Any] | None,
        bvid: str | None,
        part_title: str | None,
    ) -> dict[str, Any]:
        self.video = video.resolve()
        if not self.video.is_file():
            raise FileNotFoundError(self.video)

        state = self._load_or_create_upload()
        completed = {int(item) for item in state.get("completed", [])}
        chunk_count = int(state["chunks"])
        pending = [index for index in range(chunk_count) if index not in completed]
        LOGGER.info(
            "Small chunk upload %s: %d/%d chunks complete, %d pending",
            self.video.name,
            len(completed),
            chunk_count,
            len(pending),
        )
        if pending:
            self._upload_pending(state, completed, pending)

        video_item = self._complete_upload(state, part_title=part_title)
        if mode == "new":
            if metadata is None:
                raise RuntimeError("New Bilibili upload requires metadata")
            payload = dict(metadata)
            payload.pop("videos", None)
            payload["videos"] = [video_item]
            response = self._submit(ADD_URL, payload)
            data = response.get("data") or {}
            submitted_bvid = data.get("bvid") or data.get("BVID")
            if not submitted_bvid:
                raise RuntimeError(f"New submission succeeded but returned no BVID: {response}")
            bvid = str(submitted_bvid)
        elif mode == "append":
            if not bvid:
                raise RuntimeError("Append upload requires a BVID")
            payload = self._load_existing_studio(bvid)
            payload["videos"].append(video_item)
            self._submit(EDIT_URL, payload)
        else:
            raise ValueError(f"Unsupported small chunk upload mode: {mode}")

        result = {
            "bvid": bvid,
            "mode": mode,
            "video": str(self.video),
            "part_title": video_item["title"],
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _write_json_atomic(self.result_file, result)
        LOGGER.info("Small chunk upload completed: %s", json.dumps(result, ensure_ascii=False))
        return result

    def _load_or_create_upload(self) -> dict[str, Any]:
        if self.video is None:
            raise RuntimeError("No video set for upload")
        stat = self.video.stat()
        identity = {
            "video": str(self.video),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "line": self.line,
            "chunk_size": self.chunk_size,
        }
        if self.checkpoint.exists():
            state = json.loads(self.checkpoint.read_text(encoding="utf-8"))
            for key, value in identity.items():
                if state.get(key) != value:
                    raise RuntimeError(
                        f"Checkpoint does not match current upload ({key}): {self.checkpoint}"
                    )
            return state

        session = self._new_session()
        query = _small_chunk_line_query(self.line)
        params = {
            "name": self.video.name,
            "r": "upos",
            "profile": "ugcupos/bup",
            "ssl": 0,
            "version": "2.14.0",
            "build": 2140000,
            "size": stat.st_size,
        }
        response = self._request(
            session,
            "GET",
            PREUPLOAD_URL,
            params={**query, **params},
            timeout=(30, 60),
        )
        bucket = response.json()
        endpoint = str(bucket["endpoint"])
        if endpoint.startswith("//"):
            endpoint = f"https:{endpoint}"
        upos_uri = str(bucket["upos_uri"])
        upos_path = upos_uri[7:] if upos_uri.startswith("upos://") else upos_uri
        upload_url = f"{endpoint.rstrip('/')}/{upos_path}"
        init_response = self._request(
            session,
            "POST",
            upload_url,
            params={"uploads": "", "output": "json"},
            headers={"X-Upos-Auth": str(bucket["auth"])},
            timeout=(30, 60),
        )
        init_data = init_response.json()
        upload_id = init_data.get("upload_id")
        if not upload_id:
            raise RuntimeError(f"Could not create Upos upload: {init_data}")

        state = {
            **identity,
            "endpoint": endpoint,
            "upos_uri": upos_uri,
            "upload_url": upload_url,
            "auth": str(bucket["auth"]),
            "biz_id": int(bucket["biz_id"]),
            "upload_id": str(upload_id),
            "chunks": math.ceil(stat.st_size / self.chunk_size),
            "completed": [],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _write_json_atomic(self.checkpoint, state)
        return state

    def _upload_pending(
        self,
        state: dict[str, Any],
        completed: set[int],
        pending: list[int],
    ) -> None:
        for offset in range(0, len(pending), self.workers):
            batch = pending[offset : offset + self.workers]
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {executor.submit(self._upload_chunk, state, index): index for index in batch}
                for future in as_completed(futures):
                    index = futures[future]
                    future.result()
                    with self._checkpoint_lock:
                        completed.add(index)
                        state["completed"] = sorted(completed)
                        _write_json_atomic(self.checkpoint, state)
                        LOGGER.info(
                            "Small chunk %d/%d complete (%d/%d)",
                            index + 1,
                            state["chunks"],
                            len(completed),
                            state["chunks"],
                        )

    def _upload_chunk(self, state: dict[str, Any], index: int) -> None:
        if self.video is None:
            raise RuntimeError("No video set for upload")
        chunk_size = int(state["chunk_size"])
        total_size = int(state["size"])
        start = index * chunk_size
        size = min(chunk_size, total_size - start)
        with self.video.open("rb") as file:
            file.seek(start)
            data = file.read(size)
        if len(data) != size:
            raise RuntimeError(f"Short read for chunk {index}: expected {size}, got {len(data)}")

        params = {
            "uploadId": state["upload_id"],
            "chunks": state["chunks"],
            "total": total_size,
            "chunk": index,
            "size": size,
            "partNumber": index + 1,
            "start": start,
            "end": start + size,
        }
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._thread_session().put(
                    state["upload_url"],
                    params=params,
                    data=data,
                    headers={
                        "X-Upos-Auth": state["auth"],
                        "Content-Length": str(size),
                    },
                    timeout=(30, 300),
                )
                response.raise_for_status()
                return
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                delay = min(30, 2 ** min(attempt, 5))
                LOGGER.warning(
                    "Small chunk %d attempt %d failed; retrying in %d second(s): %s",
                    index + 1,
                    attempt,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise RuntimeError(
            f"Chunk {index + 1} failed after {self.retries} attempts: {last_error}"
        ) from last_error

    def _complete_upload(self, state: dict[str, Any], *, part_title: str | None) -> dict[str, str]:
        if self.video is None:
            raise RuntimeError("No video set for upload")
        parts = [
            {"partNumber": index + 1, "eTag": "etag"}
            for index in range(int(state["chunks"]))
        ]
        response = self._request(
            self._new_session(use_proxy=not self.direct_upos),
            "POST",
            state["upload_url"],
            params={
                "name": self.video.name,
                "uploadId": state["upload_id"],
                "biz_id": state["biz_id"],
                "output": "json",
                "profile": "ugcupos/bup",
            },
            headers={"X-Upos-Auth": state["auth"]},
            json={"parts": parts},
            timeout=(30, 60),
        )
        payload = response.json()
        if payload.get("OK") != 1:
            raise RuntimeError(f"Upos completion failed: {payload}")
        remote_name = urlparse(state["upos_uri"]).path.rsplit("/", 1)[-1]
        remote_stem = remote_name.rsplit(".", 1)[0]
        title = (part_title or self.video.stem).strip() or self.video.stem
        return {
            "title": title[:80],
            "filename": remote_stem,
            "desc": "",
        }

    def _load_existing_studio(self, bvid: str) -> dict[str, Any]:
        response = self._request(
            self._new_session(),
            "GET",
            ARCHIVE_VIEW_URL,
            params={"access_key": self.access_token, "bvid": bvid},
            timeout=(30, 60),
        )
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise RuntimeError(f"Could not load submission {bvid}: {payload}")
        data = payload["data"]
        archive = data.get("archive") or {}
        allowed = {
            "copyright",
            "source",
            "tid",
            "cover",
            "title",
            "desc_format_id",
            "desc",
            "desc_v2",
            "dynamic",
            "subtitle",
            "tag",
            "dtime",
            "open_subtitle",
            "interactive",
            "mission_id",
            "dolby",
            "lossless_music",
            "no_reprint",
            "is_only_self",
            "charging_pay",
            "aid",
            "up_selection_reply",
            "up_close_reply",
            "up_close_danmu",
        }
        studio = {key: value for key, value in archive.items() if key in allowed}
        studio["videos"] = [
            {
                "title": item.get("title"),
                "filename": item["filename"],
                "desc": item.get("desc") or "",
            }
            for item in data.get("videos") or []
        ]
        if not studio["videos"]:
            raise RuntimeError(f"Submission {bvid} returned no existing videos")
        return studio

    def _submit(self, endpoint: str, studio: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            self._new_session(),
            "POST",
            endpoint,
            params={"t": int(time.time() * 1000), "csrf": self.csrf},
            json=studio,
            timeout=(30, 60),
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"Bilibili submission API failed: {payload}")
        return payload

    def _request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = session.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status is not None and status < 500:
                    break
                if attempt == self.retries:
                    break
                delay = min(30, 2 ** min(attempt, 5))
                LOGGER.warning(
                    "%s %s attempt %d failed; retrying in %d second(s): %s",
                    method,
                    url,
                    attempt,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise RuntimeError(f"{method} {url} failed after retries: {last_error}") from last_error

    def _new_session(self, *, use_proxy: bool = True) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": USER_AGENT, "Referer": "https://member.bilibili.com/"})
        for name, value in self.cookies.items():
            session.cookies.set(name, value, domain=".bilibili.com")
        if self.proxy and use_proxy:
            session.proxies.update({"http": self.proxy, "https": self.proxy})
        return session

    def _thread_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._new_session(use_proxy=not self.direct_upos)
            self._thread_local.session = session
        return session


class SubtitleDraftUploader:
    ENDPOINT = "https://api.bilibili.com/x/v2/dm/subtitle/draft/save"
    PAGELIST = "https://api.bilibili.com/x/player/pagelist"
    VIEW = "https://api.bilibili.com/x/web-interface/view"

    def __init__(
        self,
        cookie_file: Path,
        language: str = "zh",
        trust_env: bool = False,
        page_wait_seconds: int = 900,
        page_poll_seconds: int = 30,
    ) -> None:
        self.cookie_file = cookie_file
        self.language = language
        self.page_wait_seconds = max(0, page_wait_seconds)
        self.page_poll_seconds = max(1, page_poll_seconds)
        self.session = requests.Session()
        self.session.trust_env = trust_env
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
            }
        )
        self._load_cookies()

    def upload(
        self,
        bvid: str,
        segments: list[SubtitleSegment],
        *,
        part_title: str | None = None,
        part_titles: list[str] | None = None,
        prefer_last: bool = False,
        allow_unmatched_fallback: bool = False,
    ) -> None:
        page = self._wait_for_page(
            bvid,
            part_title=part_title,
            part_titles=part_titles,
            prefer_last=prefer_last,
            allow_unmatched_fallback=allow_unmatched_fallback,
        )
        cid = int(page["cid"])
        aid = int(page["aid"])
        csrf = self.session.cookies.get("bili_jct")
        if not csrf:
            raise RuntimeError("Cookie does not contain bili_jct csrf token")
        data = to_bilibili_subtitle_json(segments, max_end=_page_duration(page))
        response = self.session.post(
            self.ENDPOINT,
            data={
                "aid": aid,
                "bvid": bvid,
                "type": 1,
                "oid": cid,
                "lan": self.language,
                "sign": "false",
                "data": json.dumps(data, ensure_ascii=False),
                "submit": "true",
                "csrf": csrf,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"Bilibili subtitle API returned: {payload}")
        LOGGER.info("Bilibili subtitle draft uploaded for %s cid=%s", bvid, cid)

    def _get_cid(
        self,
        bvid: str,
        *,
        part_title: str | None = None,
        part_titles: list[str] | None = None,
        prefer_last: bool = False,
        allow_unmatched_fallback: bool = False,
    ) -> int:
        return int(
            self._wait_for_page(
                bvid,
                part_title=part_title,
                part_titles=part_titles,
                prefer_last=prefer_last,
                allow_unmatched_fallback=allow_unmatched_fallback,
            )["cid"]
        )

    def _wait_for_page(
        self,
        bvid: str,
        *,
        part_title: str | None = None,
        part_titles: list[str] | None = None,
        prefer_last: bool = False,
        allow_unmatched_fallback: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.page_wait_seconds
        last_error: Exception | None = None
        while True:
            try:
                return self._get_page(
                    bvid,
                    part_title=part_title,
                    part_titles=part_titles,
                    prefer_last=prefer_last,
                    allow_unmatched_fallback=allow_unmatched_fallback,
                )
            except (requests.RequestException, RuntimeError) as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    break
                LOGGER.warning(
                    "Bilibili page info for %s is not ready; retrying in %d second(s): %s",
                    bvid,
                    self.page_poll_seconds,
                    exc,
                )
                time.sleep(self.page_poll_seconds)
        assert last_error is not None
        raise RuntimeError(
            f"Could not fetch page info for {bvid} within {self.page_wait_seconds} second(s): {last_error}"
        ) from last_error

    def _get_page(
        self,
        bvid: str,
        *,
        part_title: str | None = None,
        part_titles: list[str] | None = None,
        prefer_last: bool = False,
        allow_unmatched_fallback: bool = False,
    ) -> dict[str, Any]:
        response = self.session.get(self.VIEW, params={"bvid": bvid}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        pages = data.get("pages") or []
        aid = data.get("aid")
        if payload.get("code") != 0 or not pages or not aid:
            raise RuntimeError(f"Could not fetch page info for {bvid}: {payload}")
        title_candidates: list[str] = []
        if part_titles:
            title_candidates.extend(str(item).strip() for item in part_titles if str(item).strip())
        if part_title:
            title_candidates.append(part_title.strip())
        title_candidates = list(dict.fromkeys(title_candidates))
        if title_candidates:
            for title in title_candidates:
                for page in pages:
                    if str(page.get("part", "")).strip() == title:
                        LOGGER.info(
                            "Matched Bilibili part title %r in %s cid=%s",
                            title,
                            bvid,
                            page.get("cid"),
                        )
                        return {**page, "aid": aid}
            if not allow_unmatched_fallback:
                raise RuntimeError(
                    f"Could not find Bilibili part title in {bvid}; candidates={title_candidates}"
                )
            LOGGER.warning(
                "Could not find Bilibili part title in %s; candidates=%s; using %s page",
                bvid,
                title_candidates,
                "last" if prefer_last else "first",
            )
        selected = pages[-1] if prefer_last else pages[0]
        return {**selected, "aid": aid}

    def _load_cookies(self) -> None:
        if not self.cookie_file.exists():
            raise FileNotFoundError(f"Bilibili cookie file not found: {self.cookie_file}")
        text = self.cookie_file.read_text(encoding="utf-8")
        if self.cookie_file.suffix.lower() == ".json":
            data = json.loads(text)
            loaded = self._load_json_cookies(data)
            if loaded:
                return
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                self.session.cookies.set(parts[5], parts[6], domain=parts[0])

    def _load_json_cookies(self, value: Any) -> bool:
        loaded = False
        if isinstance(value, dict) and "name" in value and "value" in value:
            self.session.cookies.set(
                str(value["name"]),
                str(value["value"]),
                domain=str(value.get("domain", ".bilibili.com")),
            )
            return True
        if isinstance(value, dict):
            scalar_items = {
                key: item
                for key, item in value.items()
                if isinstance(item, (str, int, float, bool)) and key not in {"expires", "expirationDate"}
            }
            if scalar_items and len(scalar_items) == len(value):
                for name, item in scalar_items.items():
                    self.session.cookies.set(str(name), str(item), domain=".bilibili.com")
                return True
            for item in value.values():
                loaded = self._load_json_cookies(item) or loaded
            return loaded
        if isinstance(value, list):
            for item in value:
                loaded = self._load_json_cookies(item) or loaded
        return loaded


def _extract_bvid(output: str) -> str | None:
    match = re.search(r"\bBV[0-9A-Za-z]{10,}\b", output)
    return match.group(0) if match else None


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _small_chunk_line_query(line: str) -> dict[str, str]:
    queries = {
        "bldsa": {"zone": "cs", "upcdn": "bldsa", "probe_version": "20221109"},
        "bda2": {"zone": "cs", "upcdn": "bda2", "probe_version": "20221109"},
    }
    key = line.strip().lower()
    try:
        return queries[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported small chunk upload line: {line}") from exc


def _load_biliup_login(path: Path) -> tuple[dict[str, str], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cookies: dict[str, str] = {}
    if isinstance(payload, dict):
        for item in (payload.get("cookie_info") or {}).get("cookies") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name and value is not None:
                cookies[str(name)] = str(value)
        if not cookies:
            cookies.update(_collect_cookie_scalars(payload))
        access_token = (
            (payload.get("token_info") or {}).get("access_token")
            or payload.get("access_token")
            or ""
        )
    else:
        access_token = ""
    return cookies, str(access_token)


def _collect_cookie_scalars(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        if "name" in value and "value" in value:
            name = value.get("name")
            cookie_value = value.get("value")
            if name and cookie_value is not None:
                return {str(name): str(cookie_value)}
        scalar_items = {
            str(key): str(item)
            for key, item in value.items()
            if isinstance(item, (str, int, float, bool)) and key not in {"expires", "expirationDate"}
        }
        if scalar_items and len(scalar_items) == len(value):
            return scalar_items
        cookies: dict[str, str] = {}
        for item in value.values():
            cookies.update(_collect_cookie_scalars(item))
        return cookies
    if isinstance(value, list):
        cookies: dict[str, str] = {}
        for item in value:
            cookies.update(_collect_cookie_scalars(item))
        return cookies
    return {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _looks_like_expired_upload(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "404",
        "403",
        "nosuchupload",
        "no such upload",
        "upload_id",
        "uploadid",
        "expired",
        "expire",
        "invalid upload",
    )
    return any(marker in text for marker in markers)


def _page_duration(page: dict[str, Any]) -> float | None:
    value = page.get("duration")
    if value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None
