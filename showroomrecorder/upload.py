from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests
import yaml

from .config import AppConfig
from .models import LiveSession, SubtitleSegment
from .subtitles import to_bilibili_subtitle_json
from .templating import build_context, render_template

LOGGER = logging.getLogger(__name__)


class BiliupUploader:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def upload(self, session: LiveSession, segments: list[SubtitleSegment] | None = None) -> str | None:
        if not self.config.upload.enabled:
            LOGGER.info("Upload disabled; skipping")
            return None
        if self.config.upload.uploader != "biliup":
            raise ValueError(f"Unsupported upload.uploader: {self.config.upload.uploader}")
        if not session.upload_file:
            raise RuntimeError("No upload_file set for session")

        biliup_cfg = self.config.upload.biliup
        bin_name = str(biliup_cfg.get("bin", "biliup"))
        if shutil.which(bin_name) is None:
            raise RuntimeError(f"biliup executable not found in PATH: {bin_name}")

        upload_config = self._write_biliup_config(session)
        command = [bin_name]
        user_cookie = biliup_cfg.get("user_cookie")
        if user_cookie:
            command.extend(["-u", str(self._resolve_config_path(user_cookie))])
        command.extend(["upload", "-c", str(upload_config)])
        command.extend(str(item) for item in biliup_cfg.get("extra_args", []))

        output = self._run(command, session.work_dir / "biliup-upload.log")
        bvid = _extract_bvid(output)
        if bvid:
            LOGGER.info("Biliup output detected BVID: %s", bvid)
            session.bvid = bvid

        if (
            bvid
            and segments
            and bool(biliup_cfg.get("upload_subtitle_draft", False))
            and session.zh_srt_file
        ):
            try:
                SubtitleDraftUploader(
                    cookie_file=self._resolve_config_path(user_cookie),
                    language=str(biliup_cfg.get("subtitle_language", "zh-CN")),
                ).upload(bvid, segments)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Bilibili subtitle draft upload failed: %s", exc)
        return bvid

    def _write_biliup_config(self, session: LiveSession) -> Path:
        cfg = self.config.upload.biliup
        context = build_context(
            streamer=session.room.name,
            room_url=session.room.url,
            room_id=session.room.room_id,
            title=session.live_title,
            started_at=session.started_at,
            ended_at=session.ended_at,
            job_id=session.job_id,
        )
        title = render_template(self.config.naming.title_template, context)
        desc = render_template(self.config.naming.desc_template, context)
        dynamic = render_template(self.config.naming.dynamic_template, context)
        source = render_template(str(cfg.get("source_template", "{room_url}")), context)
        tags = cfg.get("tags", [])
        if isinstance(tags, list):
            tag_value = ",".join(str(item) for item in tags)
        else:
            tag_value = str(tags)

        upload_file = session.upload_file.resolve().as_posix()
        item: dict[str, Any] = {
            "copyright": int(cfg.get("copyright", 2)),
            "source": source,
            "tid": int(cfg.get("tid", 21)),
            "cover": str(cfg.get("cover", "")),
            "title": title,
            "desc_format_id": int(cfg.get("desc_format_id", 0)),
            "desc": desc,
            "dynamic": dynamic,
            "tag": tag_value,
            "open_subtitle": bool(cfg.get("open_subtitle", True)),
            "subtitle": {
                "open": 1 if cfg.get("open_subtitle", True) else 0,
                "lan": str(cfg.get("subtitle_language", "zh-CN")),
            },
        }
        payload = {
            "line": str(cfg.get("line", "kodo")),
            "limit": int(cfg.get("limit", 3)),
            "streamers": {upload_file: item},
        }
        path = session.work_dir / "biliup-upload.yaml"
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

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


class SubtitleDraftUploader:
    ENDPOINT = "https://api.bilibili.com/x/v2/dm/subtitle/draft/save"
    PAGELIST = "https://api.bilibili.com/x/player/pagelist"

    def __init__(self, cookie_file: Path, language: str = "zh-CN") -> None:
        self.cookie_file = cookie_file
        self.language = language
        self.session = requests.Session()
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

    def upload(self, bvid: str, segments: list[SubtitleSegment]) -> None:
        cid = self._get_cid(bvid)
        csrf = self.session.cookies.get("bili_jct")
        if not csrf:
            raise RuntimeError("Cookie does not contain bili_jct csrf token")
        data = to_bilibili_subtitle_json(segments)
        response = self.session.post(
            self.ENDPOINT,
            data={
                "type": 1,
                "oid": cid,
                "lan": self.language,
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

    def _get_cid(self, bvid: str) -> int:
        response = self.session.get(self.PAGELIST, params={"bvid": bvid}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise RuntimeError(f"Could not fetch cid for {bvid}: {payload}")
        return int(payload["data"][0]["cid"])

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
