from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
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
        bin_name = str(biliup_cfg.get("bin", "biliup"))
        if shutil.which(bin_name) is None:
            raise RuntimeError(f"biliup executable not found in PATH: {bin_name}")

        user_cookie = biliup_cfg.get("user_cookie")
        mode = str(biliup_cfg.get("mode", "upload")).lower()
        context = self._context(session)
        part_title = render_template(
            self.config.naming.part_title_template,
            context,
        )
        subtitle_part_titles: list[str] = []
        prefer_last_part = False

        if mode == "append":
            bvid, output = self._append_with_biliup(
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
                bvid, output = self._append_with_biliup(
                    session=session,
                    bin_name=bin_name,
                    user_cookie=user_cookie,
                    part_title=part_title,
                    vid=monthly_bvid,
                )
                subtitle_part_titles = self._subtitle_part_title_candidates(session, part_title)
                prefer_last_part = True
            else:
                bvid, output = self._upload_new_with_biliup(
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
            bvid, output = self._upload_new_with_biliup(
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
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Bilibili subtitle draft upload failed: %s", exc)
                if bool(biliup_cfg.get("subtitle_errors_fatal", True)):
                    raise
        return bvid

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
        context = self._context(session)
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
                "lan": str(cfg.get("subtitle_language", "zh")),
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
    ) -> None:
        page = self._wait_for_page(bvid, part_title=part_title, part_titles=part_titles, prefer_last=prefer_last)
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
    ) -> int:
        return int(
            self._wait_for_page(
                bvid,
                part_title=part_title,
                part_titles=part_titles,
                prefer_last=prefer_last,
            )["cid"]
        )

    def _wait_for_page(
        self,
        bvid: str,
        *,
        part_title: str | None = None,
        part_titles: list[str] | None = None,
        prefer_last: bool = False,
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


def _page_duration(page: dict[str, Any]) -> float | None:
    value = page.get("duration")
    if value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None
