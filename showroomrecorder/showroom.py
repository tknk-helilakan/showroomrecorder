from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from .config import RoomConfig

LOGGER = logging.getLogger(__name__)


@dataclass
class LiveStatus:
    is_live: bool
    title: str = ""
    raw: dict[str, Any] | None = None


class ShowroomClient:
    BASE = "https://www.showroom-live.com"

    def __init__(self, timeout_seconds: int = 15) -> None:
        self.timeout_seconds = timeout_seconds
        self._local = threading.local()
        self._warning_lock = threading.Lock()
        self._last_warning_at: dict[tuple[str, str], float] = {}
        self._failure_counts: dict[tuple[str, str], int] = {}
        self._warning_interval_seconds = 300.0
        self._headers = (
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            }
        )

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.headers.update(self._headers)
            self._local.session = session
        return session

    def reset_session(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
        self._local.session = None

    def ensure_room_id(self, room: RoomConfig) -> int:
        if room.room_id is not None:
            return room.room_id
        room_id = self.resolve_room_id(room.url)
        room.room_id = room_id
        return room_id

    def resolve_room_id(self, url: str) -> int:
        parsed = urlparse(url)
        query_room_id = parse_qs(parsed.query).get("room_id")
        if query_room_id:
            return int(query_room_id[0])

        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        text = response.text
        patterns = [
            r"/room/profile\?room_id=(\d+)",
            r"SrGlobal\.roomId\s*=\s*['\"]?(\d+)",
            r'"room_id"\s*:\s*"?(\d+)"?',
            r"room_id\s*[:=]\s*['\"]?(\d+)",
            r"sr_room_id\s*=\s*['\"]?(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
        raise ValueError(f"Could not resolve SHOWROOM room_id from {url}")

    def get_live_status(self, room: RoomConfig) -> LiveStatus:
        room_id = self.ensure_room_id(room)
        raw: dict[str, Any] = {}

        try:
            profile = self._get_json("/api/room/profile", {"room_id": room_id})
            self._clear_request_failure("profile", room.name)
        except requests.RequestException as exc:
            self._log_request_failure("profile", room.name, exc)
            return LiveStatus(is_live=False, raw={})

        if "is_onlive" not in profile:
            try:
                raw = self._get_json("/api/room/is_live", {"room_id": room_id})
                self._clear_request_failure("is_live", room.name)
            except requests.HTTPError as exc:
                response = exc.response
                if response is None or response.status_code != 404:
                    self._log_request_failure("is_live", room.name, exc)
                else:
                    LOGGER.debug("SHOWROOM is_live endpoint returned 404 for %s; profile fallback is used", room.name)
            except requests.RequestException as exc:
                self._log_request_failure("is_live", room.name, exc)

        is_live = bool(
            profile.get("is_onlive") is True
            or raw.get("is_live")
            or raw.get("is_live_now")
        )
        merged = {**raw, **profile}
        title = ""
        if is_live:
            for key in ("live_title", "room_name", "main_name", "performer_name", "name", "title"):
                value = merged.get(key)
                if isinstance(value, str) and value.strip():
                    title = value.strip()
                    break
            title = title or self.get_live_title(room_id) or room.name
        return LiveStatus(is_live=is_live, title=title, raw=merged)

    def get_live_title(self, room_id: int) -> str:
        for endpoint in ("/api/live/live_info", "/api/room/profile"):
            try:
                data = self._get_json(endpoint, {"room_id": room_id})
            except requests.RequestException:
                continue
            for key in ("live_title", "main_name", "room_name", "name", "title"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def get_streaming_urls(self, room: RoomConfig) -> list[str]:
        room_id = self.ensure_room_id(room)
        data = self._get_json("/api/live/streaming_url", {"room_id": room_id})
        urls: list[str] = []
        self._collect_urls(data, urls)
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
        return deduped

    def get_comment_log(self, room: RoomConfig, timeout_seconds: int | None = None) -> list[dict[str, Any]]:
        room_id = self.ensure_room_id(room)
        data = self._get_json(
            "/api/live/comment_log",
            {"room_id": room_id},
            timeout_seconds=timeout_seconds,
        )
        items = data.get("comment_log")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []

    def _get_json(
        self,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.BASE}{path}"
        last_error: requests.RequestException | None = None
        timeout = timeout_seconds or self.timeout_seconds
        for attempt in range(2):
            try:
                response = self.session.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError(f"Unexpected SHOWROOM API response from {url}: {type(data)!r}")
                return data
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code is not None and status_code < 500:
                    raise
                last_error = exc
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                self.reset_session()
            if attempt == 0:
                time.sleep(1.0)
        assert last_error is not None
        raise last_error

    def _log_request_failure(self, kind: str, room_name: str, exc: requests.RequestException) -> None:
        key = (kind, room_name)
        now = time.monotonic()
        with self._warning_lock:
            failure_count = self._failure_counts.get(key, 0) + 1
            self._failure_counts[key] = failure_count
            last_warning = self._last_warning_at.get(key, 0.0)
            should_warn = failure_count >= 3 and now - last_warning >= self._warning_interval_seconds
            if should_warn:
                self._last_warning_at[key] = now
        message = "SHOWROOM %s request failed for %s after %d consecutive failed poll(s): %s"
        if should_warn:
            LOGGER.warning(message, kind, room_name, failure_count, exc)
        else:
            LOGGER.debug(message, kind, room_name, failure_count, exc)

    def _clear_request_failure(self, kind: str, room_name: str) -> None:
        key = (kind, room_name)
        with self._warning_lock:
            self._failure_counts.pop(key, None)

    def _collect_urls(self, value: Any, output: list[str]) -> None:
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            output.append(value)
            return
        if isinstance(value, dict):
            for key in ("url", "streaming_url", "streaming_url_hls"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    output.append(candidate)
            for item in value.values():
                self._collect_urls(item, output)
            return
        if isinstance(value, list):
            for item in value:
                self._collect_urls(item, output)
