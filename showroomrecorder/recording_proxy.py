from __future__ import annotations

import base64
import binascii
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import getproxies

import requests
import yaml

from .config import RecordProxyConfig


LOGGER = logging.getLogger(__name__)
SUPPORTED_PROXY_SCHEMES = {"http", "https"}
MAX_SOURCE_BYTES = 2 * 1024 * 1024


class RecordingProxyResolver:
    def __init__(self, config: RecordProxyConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_routes: list[str | None] | None = None

    def routes(self) -> list[str | None]:
        cfg = self.config
        if not cfg.enabled or cfg.mode == "off":
            return [None]

        now = time.monotonic()
        with self._lock:
            if (
                self._cached_routes is not None
                and cfg.refresh_seconds > 0
                and now - self._cached_at < cfg.refresh_seconds
            ):
                return list(self._cached_routes)

            system = self._system_proxy_urls() if cfg.include_system else []
            project = self._project_proxy_urls()
            project = [url for url in project if url not in system]
            healthy_system, unhealthy_system = self._partition_by_health(system)
            healthy_project, unhealthy_project = self._partition_by_health(project)

            proxies = [
                *healthy_system,
                *healthy_project,
                *unhealthy_system,
                *unhealthy_project,
            ]
            if cfg.mode == "proxy_only":
                if not proxies:
                    raise RuntimeError(
                        "record.proxy.mode is proxy_only, but no proxy endpoint is configured"
                    )
                routes: list[str | None] = proxies
            else:
                routes = [
                    *healthy_system,
                    None,
                    *healthy_project,
                    *unhealthy_system,
                    *unhealthy_project,
                ]

            routes = _dedupe(routes)
            self._cached_routes = routes
            self._cached_at = now
            LOGGER.info(
                "Recording network routes resolved: %s",
                ", ".join("system route" if item is None else describe_proxy(item) for item in routes),
            )
            return list(routes)

    def _system_proxy_urls(self) -> list[str]:
        values = getproxies()
        candidates = [
            values.get("https"),
            values.get("http"),
            values.get("all"),
        ]
        return _normalize_urls(candidates, source="system proxy")

    def _project_proxy_urls(self) -> list[str]:
        candidates: list[str] = list(self.config.urls)
        if self.config.file:
            candidates.extend(self._read_proxy_file(self.config.file, label="proxy file"))
        if self.config.source_url:
            downloaded = self._download_proxy_source()
            if downloaded:
                candidates.extend(downloaded)
            elif self.config.cache_file:
                candidates.extend(
                    self._read_proxy_file(self.config.cache_file, label="proxy source cache")
                )
        return _normalize_urls(candidates, source="record.proxy")

    def _download_proxy_source(self) -> list[str]:
        source_label = _source_label(self.config.source_url)
        try:
            session = requests.Session()
            session.trust_env = True
            response = session.get(
                self.config.source_url,
                headers={"User-Agent": "showroomrecorder/recording-proxy"},
                timeout=self.config.source_timeout_seconds,
            )
            response.raise_for_status()
            content = response.content
            if len(content) > MAX_SOURCE_BYTES:
                raise RuntimeError(f"proxy source exceeds {MAX_SOURCE_BYTES} bytes")
            text = content.decode("utf-8-sig")
            proxies = parse_proxy_source(text)
            if not proxies:
                LOGGER.warning("Proxy source %s contains no supported HTTP proxy URLs", source_label)
                return []
            if self.config.cache_file:
                self.config.cache_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file = self.config.cache_file.with_name(f"{self.config.cache_file.name}.tmp")
                temp_file.write_text(text, encoding="utf-8")
                temp_file.replace(self.config.cache_file)
            LOGGER.info("Loaded %d recording proxy endpoint(s) from %s", len(proxies), source_label)
            return proxies
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Could not refresh recording proxy source %s (%s); using cache if available",
                source_label,
                type(exc).__name__,
            )
            return []

    def _read_proxy_file(self, path: Path, *, label: str) -> list[str]:
        if not path.exists():
            LOGGER.warning("Configured %s does not exist: %s", label, path)
            return []
        try:
            return parse_proxy_source(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            LOGGER.warning("Could not read %s %s: %s", label, path, type(exc).__name__)
            return []

    def _partition_by_health(self, proxies: list[str]) -> tuple[list[str], list[str]]:
        healthy: list[str] = []
        unhealthy: list[str] = []
        for proxy_url in proxies:
            if self._probe(proxy_url):
                healthy.append(proxy_url)
            else:
                unhealthy.append(proxy_url)
        return healthy, unhealthy

    def _probe(self, proxy_url: str) -> bool:
        if not self.config.probe_url:
            return True
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(
                self.config.probe_url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=(self.config.probe_timeout_seconds, self.config.probe_timeout_seconds),
                stream=True,
            )
            response.close()
            if 200 <= response.status_code < 400:
                LOGGER.info("Recording proxy is available: %s", describe_proxy(proxy_url))
                return True
            LOGGER.warning(
                "Recording proxy probe returned HTTP %d: %s",
                response.status_code,
                describe_proxy(proxy_url),
            )
        except requests.RequestException as exc:
            LOGGER.warning(
                "Recording proxy probe failed for %s: %s",
                describe_proxy(proxy_url),
                type(exc).__name__,
            )
        return False


def parse_proxy_source(text: str, *, _allow_base64: bool = True) -> list[str]:
    values: list[str] = []
    try:
        structured = yaml.safe_load(text)
    except yaml.YAMLError:
        structured = None

    if isinstance(structured, dict):
        for key in ("proxies", "proxy_urls", "urls", "endpoints"):
            if key in structured:
                values.extend(_endpoint_values(structured[key]))
    elif isinstance(structured, list):
        values.extend(_endpoint_values(structured))
    elif isinstance(structured, str):
        values.extend(_endpoint_values(structured))

    for line in text.splitlines():
        candidate = line.strip().strip("'\"")
        if (
            candidate
            and not candidate.startswith("#")
            and ("://" in candidate or candidate.rsplit(":", 1)[-1].isdigit())
        ):
            values.extend(_endpoint_values(candidate))

    normalized = _normalize_urls(values, source="proxy source", log_invalid=False)
    if normalized or not _allow_base64:
        return normalized

    compact = "".join(text.split())
    if not compact:
        return []
    try:
        padding = "=" * (-len(compact) % 4)
        decoded = base64.b64decode(compact + padding).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeError):
        return []
    return parse_proxy_source(decoded, _allow_base64=False)


def describe_proxy(proxy_url: str) -> str:
    parsed = urlsplit(proxy_url)
    host = parsed.hostname or "unknown"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
    port = f":{parsed_port}" if parsed_port else ""
    return f"{parsed.scheme}://{host}{port}"


def _endpoint_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_endpoint_values(item))
        return values
    if isinstance(value, dict):
        for key in ("url", "proxy", "endpoint"):
            if key in value:
                return _endpoint_values(value[key])
    return []


def _normalize_urls(
    values: list[str | None],
    *,
    source: str,
    log_invalid: bool = True,
) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        if "://" not in candidate:
            if not candidate.rsplit(":", 1)[-1].isdigit():
                if log_invalid:
                    LOGGER.warning("Ignoring unsupported %s endpoint", source)
                continue
            candidate = f"http://{candidate}"
        parsed = urlsplit(candidate)
        try:
            parsed.port
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES
            or not parsed.hostname
        ):
            if log_invalid:
                LOGGER.warning("Ignoring unsupported %s endpoint", source)
            continue
        normalized.append(candidate)
    return _dedupe(normalized)


def _source_label(source_url: str) -> str:
    parsed = urlsplit(source_url)
    return parsed.hostname or "configured URL"


def _dedupe(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(values))
