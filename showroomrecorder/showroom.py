from __future__ import annotations

import logging
import re
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
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            }
        )

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
            raw = self._get_json("/api/room/is_live", {"room_id": room_id})
        except requests.RequestException as exc:
            LOGGER.warning("SHOWROOM is_live request failed for %s: %s", room.name, exc)

        is_live = bool(raw.get("is_live") or raw.get("is_live_now"))
        profile: dict[str, Any] = {}
        if not is_live:
            try:
                profile = self._get_json("/api/room/profile", {"room_id": room_id})
                is_live = profile.get("is_onlive") is True
            except requests.RequestException as exc:
                LOGGER.warning("SHOWROOM profile request failed for %s: %s", room.name, exc)

        merged = {**profile, **raw}
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
        return urls

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.BASE}{path}"
        response = self.session.get(url, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected SHOWROOM API response from {url}: {type(data)!r}")
        return data

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
