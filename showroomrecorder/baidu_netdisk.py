from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from .config import AppConfig, BaiduNetdiskConfig
from .models import LiveSession
from .templating import build_context, render_template

LOGGER = logging.getLogger(__name__)

OAUTH_BASE_URL = "https://openapi.baidu.com"
PAN_BASE_URL = "https://pan.baidu.com"
PCS_BASE_URL = "https://d.pcs.baidu.com"
USER_AGENT = "showroomrecorder/0.3"
INVALID_REMOTE_CHARS = re.compile(r'[\\:*?"<>|]')
RETRYABLE_ERRNOS = {-1, 111, 31023, 31034, 31326, 31363, -9999}


class BaiduNetdiskAPIError(RuntimeError):
    def __init__(self, operation: str, code: int | str, message: str = "") -> None:
        self.operation = operation
        self.code = code
        self.message = message
        details = f"Baidu Netdisk {operation} failed: code={code}"
        if message:
            details += f" message={message}"
        super().__init__(details)


@dataclass(frozen=True)
class BaiduUploadResult:
    fs_id: int | None
    path: str
    size: int
    md5: str
    rapid_upload: bool = False
    resumed: bool = False


class BaiduNetdiskClient:
    def __init__(
        self,
        config: AppConfig | BaiduNetdiskConfig,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = getattr(config, "baidu_netdisk", config)
        self.session = session or requests.Session()
        self.session.trust_env = bool(self.config.trust_env)
        self.session.headers.update({"User-Agent": USER_AGENT})

    def authorize_device(self, output: Callable[[str], None] = print) -> dict[str, Any]:
        self._require_credentials()
        device = self._request_json(
            "GET",
            f"{OAUTH_BASE_URL}/oauth/2.0/device/code",
            operation="request device code",
            params={
                "response_type": "device_code",
                "client_id": self.config.app_key,
                "scope": "basic,netdisk",
            },
            timeout=self.config.request_timeout_seconds,
        )
        device_code = str(device.get("device_code") or "")
        user_code = str(device.get("user_code") or "")
        verification_url = str(device.get("verification_url") or "")
        qrcode_url = str(device.get("qrcode_url") or "")
        if not device_code or not user_code or not verification_url:
            raise RuntimeError(f"Baidu Netdisk device-code response is incomplete: {device}")

        output(f"Open this URL to authorize: {verification_url}")
        output(f"User code: {user_code}")
        if qrcode_url:
            output(f"QR code URL: {qrcode_url}")
        output("Waiting for authorization...")

        expires_in = max(1, int(device.get("expires_in") or 300))
        interval = max(1, int(device.get("interval") or 5))
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            token = self._request_json(
                "GET",
                f"{OAUTH_BASE_URL}/oauth/2.0/token",
                operation="exchange device code",
                params={
                    "grant_type": "device_token",
                    "code": device_code,
                    "client_id": self.config.app_key,
                    "client_secret": self.config.secret_key,
                },
                timeout=self.config.request_timeout_seconds,
                allow_oauth_error=True,
            )
            error = str(token.get("error") or "")
            if not error and token.get("access_token"):
                saved = self._save_token(token)
                output(f"Authorization succeeded. Token saved to: {self.config.token_file}")
                return saved
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            raise BaiduNetdiskAPIError(
                "exchange device code",
                error or token.get("error_code") or "unknown",
                str(token.get("error_description") or token.get("error_msg") or ""),
            )
        raise TimeoutError("Baidu Netdisk device authorization expired; run --baidu-auth again")

    def list_directories(self, remote_dir: str = "/") -> list[dict[str, Any]]:
        remote_dir = _normalize_remote_path(remote_dir, allow_root=True)
        start = 0
        limit = 1000
        entries: list[dict[str, Any]] = []
        while True:
            payload = self._retry(
                "list directory",
                lambda: self._authorized_request(
                    "GET",
                    f"{PAN_BASE_URL}/rest/2.0/xpan/file",
                    operation="list directory",
                    params={
                        "method": "list",
                        "dir": remote_dir,
                        "order": "name",
                        "desc": 0,
                        "start": start,
                        "limit": limit,
                    },
                    timeout=self.config.request_timeout_seconds,
                ),
            )
            page = payload.get("list") or []
            if not isinstance(page, list):
                raise RuntimeError("Baidu Netdisk directory response does not contain a list")
            directories = [item for item in page if isinstance(item, dict) and int(item.get("isdir") or 0) == 1]
            entries.extend(directories)
            if len(page) < limit:
                break
            start += len(page)
            if start >= 100_000:
                raise RuntimeError("Baidu Netdisk directory listing exceeded 100000 entries")
        return entries

    def list_directory_tree(self, remote_dir: str = "/", max_depth: int = 3) -> list[dict[str, Any]]:
        root = _normalize_remote_path(remote_dir, allow_root=True)
        depth_limit = max(0, int(max_depth))
        found: list[dict[str, Any]] = []
        pending: list[tuple[str, int]] = [(root, 0)]
        while pending:
            parent, depth = pending.pop(0)
            if depth >= depth_limit:
                continue
            for item in self.list_directories(parent):
                path = _normalize_remote_path(str(item.get("path") or ""), allow_root=False)
                row = dict(item)
                row["depth"] = depth + 1
                found.append(row)
                pending.append((path, depth + 1))
                if len(found) >= 10_000:
                    raise RuntimeError("Baidu Netdisk directory tree exceeded 10000 folders")
        return found

    def upload_recording(self, session: LiveSession) -> BaiduUploadResult | None:
        if not self.config.enabled:
            LOGGER.info("Baidu Netdisk upload disabled; skipping")
            return None
        if not session.raw_file:
            raise RuntimeError("Baidu Netdisk upload requires session.raw_file")
        local_file = session.raw_file.resolve()
        if local_file.name.lower() != "recording-merged.mkv":
            raise RuntimeError(
                "Baidu Netdisk only uploads the merged source file named recording-merged.mkv"
            )
        if not local_file.is_file() or local_file.stat().st_size <= 0:
            raise FileNotFoundError(f"Merged recording is missing or empty: {local_file}")
        if not self.config.remote_root:
            raise RuntimeError(
                "baidu_netdisk.remote_root is empty; choose a directory before enabling automatic upload"
            )
        self._require_credentials()

        remote_path = self.remote_path_for(session)
        state_file = self.state_file_for(session)
        fingerprint = self._fingerprint(local_file, remote_path)
        state = self._load_upload_state(state_file)
        if not self._state_matches(state, fingerprint):
            state = {
                "version": 1,
                "job_id": session.job_id,
                **fingerprint,
                "status": "hashing",
                "created_at": int(time.time()),
                "uploaded_parts": {},
            }
            self._write_json_atomic(state_file, state)

        if state.get("status") == "complete" and isinstance(state.get("result"), dict):
            result = self._result_from_state(state["result"], resumed=True)
            LOGGER.info("Baidu Netdisk upload already complete: %s", result.path)
            return result

        try:
            block_md5s = state.get("block_md5s")
            if not isinstance(block_md5s, list) or not block_md5s:
                LOGGER.info("Computing Baidu upload block hashes: %s", local_file)
                block_md5s = self._block_md5s(local_file, int(fingerprint["chunk_size"]))
                state["block_md5s"] = block_md5s
                state["status"] = "precreating"
                self._write_json_atomic(state_file, state)

            self._ensure_remote_directory(posixpath.dirname(remote_path))
            if not state.get("upload_id"):
                precreate = self._precreate(remote_path, int(fingerprint["size"]), block_md5s)
                if int(precreate.get("return_type") or 0) == 2:
                    result = BaiduUploadResult(
                        fs_id=None,
                        path=remote_path,
                        size=int(fingerprint["size"]),
                        md5="",
                        rapid_upload=True,
                    )
                    self._mark_upload_complete(state_file, state, result)
                    LOGGER.info("Baidu Netdisk rapid upload completed: %s", remote_path)
                    return result
                upload_id = str(precreate.get("uploadid") or "")
                if not upload_id:
                    raise RuntimeError(f"Baidu Netdisk precreate returned no uploadid: {precreate}")
                needed_parts = precreate.get("block_list")
                if not isinstance(needed_parts, list):
                    needed_parts = list(range(len(block_md5s)))
                state["upload_id"] = upload_id
                state["needed_parts"] = [int(index) for index in needed_parts]
                state["status"] = "uploading"
                self._write_json_atomic(state_file, state)

            upload_id = str(state["upload_id"])
            needed_parts = [int(index) for index in state.get("needed_parts") or []]
            uploaded_parts = state.get("uploaded_parts")
            if not isinstance(uploaded_parts, dict):
                uploaded_parts = {}
                state["uploaded_parts"] = uploaded_parts
            for completed, part_index in enumerate(needed_parts, start=1):
                key = str(part_index)
                if key in uploaded_parts:
                    continue
                md5 = self._upload_part(
                    local_file,
                    remote_path,
                    upload_id,
                    part_index,
                    int(fingerprint["chunk_size"]),
                    int(fingerprint["size"]),
                )
                uploaded_parts[key] = md5
                state["updated_at"] = int(time.time())
                self._write_json_atomic(state_file, state)
                LOGGER.info(
                    "Baidu Netdisk uploaded part %d/%d for %s",
                    completed,
                    len(needed_parts),
                    local_file.name,
                )

            create_md5s = [str(uploaded_parts[str(index)]) for index in needed_parts]
            if not create_md5s:
                create_md5s = [str(value) for value in block_md5s]
            created = self._create_file(
                remote_path,
                int(fingerprint["size"]),
                upload_id,
                create_md5s,
            )
            result = BaiduUploadResult(
                fs_id=_optional_int(created.get("fs_id")),
                path=str(created.get("path") or remote_path),
                size=int(created.get("size") or fingerprint["size"]),
                md5=str(created.get("md5") or ""),
            )
            self._mark_upload_complete(state_file, state, result)
            LOGGER.info("Baidu Netdisk upload completed: %s", result.path)
            return result
        except Exception as exc:
            state["status"] = "failed"
            state["updated_at"] = int(time.time())
            state["error"] = str(exc)
            self._write_json_atomic(state_file, state)
            raise

    def remote_path_for(self, session: LiveSession) -> str:
        context = build_context(
            streamer=session.room.name,
            room_url=session.room.url,
            room_id=session.room.room_id,
            title=session.live_title,
            started_at=session.started_at,
            ended_at=session.ended_at,
            job_id=session.job_id,
        )
        relative = render_template(self.config.remote_path_template, context)
        relative = _sanitize_relative_remote_path(relative)
        root = _normalize_remote_path(self.config.remote_root, allow_root=True)
        return _normalize_remote_path(posixpath.join(root, relative), allow_root=False)

    def state_file_for(self, session: LiveSession) -> Path:
        safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session.job_id).strip("._") or "job"
        return self.config.state_dir / f"{safe_job_id}.json"

    def _precreate(self, remote_path: str, size: int, block_md5s: list[str]) -> dict[str, Any]:
        return self._retry(
            "precreate upload",
            lambda: self._authorized_request(
                "POST",
                f"{PAN_BASE_URL}/rest/2.0/xpan/file",
                operation="precreate upload",
                params={"method": "precreate"},
                data={
                    "path": remote_path,
                    "size": size,
                    "isdir": 0,
                    "autoinit": 1,
                    "rtype": self._rtype(),
                    "block_list": json.dumps(block_md5s, separators=(",", ":")),
                },
                timeout=self.config.request_timeout_seconds,
            ),
        )

    def _upload_part(
        self,
        local_file: Path,
        remote_path: str,
        upload_id: str,
        part_index: int,
        chunk_size: int,
        file_size: int,
    ) -> str:
        offset = part_index * chunk_size
        size = min(chunk_size, file_size - offset)
        if size <= 0:
            raise RuntimeError(f"Invalid Baidu upload part index: {part_index}")

        def upload() -> dict[str, Any]:
            with local_file.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size)
            if len(chunk) != size:
                raise OSError(
                    f"Could not read upload part {part_index}: expected {size} bytes, got {len(chunk)}"
                )
            return self._authorized_request(
                "POST",
                f"{PCS_BASE_URL}/rest/2.0/pcs/superfile2",
                operation=f"upload part {part_index}",
                params={
                    "method": "upload",
                    "type": "tmpfile",
                    "path": remote_path,
                    "uploadid": upload_id,
                    "partseq": part_index,
                },
                files={"file": ("file", chunk, "application/octet-stream")},
                timeout=self.config.upload_timeout_seconds,
            )

        payload = self._retry(f"upload part {part_index}", upload)
        md5 = str(payload.get("md5") or "")
        if not md5:
            raise RuntimeError(f"Baidu Netdisk upload part {part_index} returned no md5")
        return md5

    def _create_file(
        self,
        remote_path: str,
        size: int,
        upload_id: str,
        block_md5s: list[str],
    ) -> dict[str, Any]:
        return self._retry(
            "create uploaded file",
            lambda: self._authorized_request(
                "POST",
                f"{PAN_BASE_URL}/rest/2.0/xpan/file",
                operation="create uploaded file",
                params={"method": "create"},
                data={
                    "path": remote_path,
                    "size": size,
                    "isdir": 0,
                    "uploadid": upload_id,
                    "rtype": self._rtype(),
                    "block_list": json.dumps(block_md5s, separators=(",", ":")),
                },
                timeout=self.config.request_timeout_seconds,
            ),
        )

    def _ensure_remote_directory(self, remote_dir: str) -> None:
        remote_dir = _normalize_remote_path(remote_dir, allow_root=True)
        if remote_dir == "/":
            return
        current = ""
        for component in remote_dir.strip("/").split("/"):
            current += "/" + component
            try:
                self._retry(
                    "create directory",
                    lambda path=current: self._authorized_request(
                        "POST",
                        f"{PAN_BASE_URL}/rest/2.0/xpan/file",
                        operation="create directory",
                        params={"method": "create"},
                        data={"path": path, "isdir": 1, "rtype": 0},
                        timeout=self.config.request_timeout_seconds,
                    ),
                )
            except BaiduNetdiskAPIError as exc:
                if exc.code != -8:
                    raise

    def _authorized_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        operation = str(kwargs.get("operation") or "request")
        params = dict(kwargs.pop("params", None) or {})
        params["access_token"] = self._access_token()
        try:
            return self._request_json(method, url, params=params, **kwargs)
        except BaiduNetdiskAPIError as exc:
            if exc.code != -6:
                raise
            LOGGER.warning("Baidu Netdisk token was rejected; refreshing it once")
            params["access_token"] = self._refresh_access_token()
            kwargs["operation"] = operation
            return self._request_json(method, url, params=params, **kwargs)

    def _access_token(self) -> str:
        token = self._load_token()
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise RuntimeError("Baidu Netdisk is not authorized; run --baidu-auth first")
        expires_at = float(token.get("expires_at") or 0)
        if expires_at and time.time() >= expires_at - 300:
            return self._refresh_access_token(token)
        return access_token

    def _refresh_access_token(self, token: dict[str, Any] | None = None) -> str:
        self._require_credentials()
        token = token or self._load_token()
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise RuntimeError("Baidu Netdisk token cannot be refreshed; run --baidu-auth again")
        payload = self._request_json(
            "GET",
            f"{OAUTH_BASE_URL}/oauth/2.0/token",
            operation="refresh token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.app_key,
                "client_secret": self.config.secret_key,
            },
            timeout=self.config.request_timeout_seconds,
        )
        if not payload.get("refresh_token"):
            payload["refresh_token"] = refresh_token
        saved = self._save_token(payload)
        access_token = str(saved.get("access_token") or "")
        if not access_token:
            raise RuntimeError("Baidu Netdisk refresh response contains no access_token")
        LOGGER.info("Baidu Netdisk access token refreshed")
        return access_token

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        allow_oauth_error: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise requests.RequestException(
                f"Baidu Netdisk {operation} request failed"
            ) from exc
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            self._raise_for_status(response, operation)
            raise RuntimeError(f"Baidu Netdisk {operation} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Baidu Netdisk {operation} returned a non-object response")
        if allow_oauth_error and payload.get("error"):
            return payload
        error = str(payload.get("error") or "")
        if error:
            raise BaiduNetdiskAPIError(
                operation,
                error,
                str(payload.get("error_description") or ""),
            )
        code = payload.get("errno")
        if code in (None, 0, "0"):
            code = payload.get("error_code") or payload.get("error_no")
        if code not in (None, 0, "0"):
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = str(code)
            raise BaiduNetdiskAPIError(
                operation,
                code,
                str(payload.get("errmsg") or payload.get("error_msg") or ""),
            )
        self._raise_for_status(response, operation)
        return payload

    @staticmethod
    def _raise_for_status(response: requests.Response, operation: str) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            status_code = getattr(response, "status_code", None)
            suffix = f" (HTTP {status_code})" if status_code else ""
            raise requests.RequestException(
                f"Baidu Netdisk {operation} request failed{suffix}"
            ) from exc

    def _retry(self, operation: str, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        attempts = max(1, int(self.config.retries))
        for attempt in range(1, attempts + 1):
            try:
                return callback()
            except (requests.RequestException, OSError) as exc:
                retryable = True
                error = exc
            except BaiduNetdiskAPIError as exc:
                retryable = exc.code in RETRYABLE_ERRNOS
                error = exc
            if not retryable or attempt >= attempts:
                raise error
            delay = min(60.0, float(self.config.retry_seconds) * (2 ** (attempt - 1)))
            LOGGER.warning(
                "Baidu Netdisk %s attempt %d/%d failed; retrying in %.1f second(s): %s",
                operation,
                attempt,
                attempts,
                delay,
                error,
            )
            if delay:
                time.sleep(delay)
        raise RuntimeError(f"Baidu Netdisk {operation} failed")

    def _rtype(self) -> int:
        return {"fail": 0, "rename": 3, "overwrite": 2}[self.config.conflict_policy]

    def _require_credentials(self) -> None:
        if not self.config.app_key or not self.config.secret_key:
            raise RuntimeError(
                "Set baidu_netdisk.app_key and baidu_netdisk.secret_key in config.yaml first"
            )

    def _load_token(self) -> dict[str, Any]:
        path = self.config.token_file
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read Baidu Netdisk token file: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Baidu Netdisk token file must contain a JSON object: {path}")
        return payload

    def _save_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = dict(payload)
        now = int(time.time())
        token["obtained_at"] = now
        token["expires_at"] = now + max(0, int(token.get("expires_in") or 0))
        self._write_json_atomic(self.config.token_file, token)
        try:
            os.chmod(self.config.token_file, 0o600)
        except OSError:
            pass
        return token

    def _fingerprint(self, local_file: Path, remote_path: str) -> dict[str, Any]:
        stat = local_file.stat()
        return {
            "local_file": str(local_file),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "chunk_size": int(self.config.chunk_size_mb) * 1024 * 1024,
            "remote_path": remote_path,
        }

    def _state_matches(self, state: dict[str, Any], fingerprint: dict[str, Any]) -> bool:
        return bool(state) and all(state.get(key) == value for key, value in fingerprint.items())

    def _load_upload_state(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Ignoring invalid Baidu upload state: %s", path)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _block_md5s(self, path: Path, chunk_size: int) -> list[str]:
        hashes: list[str] = []
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                hashes.append(hashlib.md5(chunk).hexdigest())  # noqa: S324 - required by Baidu API
        if not hashes:
            raise RuntimeError(f"Cannot upload an empty file: {path}")
        return hashes

    def _mark_upload_complete(
        self,
        state_file: Path,
        state: dict[str, Any],
        result: BaiduUploadResult,
    ) -> None:
        state["status"] = "complete"
        state["completed_at"] = int(time.time())
        state.pop("error", None)
        state["result"] = {
            "fs_id": result.fs_id,
            "path": result.path,
            "size": result.size,
            "md5": result.md5,
            "rapid_upload": result.rapid_upload,
        }
        self._write_json_atomic(state_file, state)

    def _result_from_state(self, payload: dict[str, Any], *, resumed: bool) -> BaiduUploadResult:
        return BaiduUploadResult(
            fs_id=_optional_int(payload.get("fs_id")),
            path=str(payload.get("path") or ""),
            size=int(payload.get("size") or 0),
            md5=str(payload.get("md5") or ""),
            rapid_upload=bool(payload.get("rapid_upload")),
            resumed=resumed,
        )

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)


def _normalize_remote_path(value: str, *, allow_root: bool) -> str:
    value = str(value or "").strip().replace("\\", "/")
    if not value:
        if allow_root:
            return "/"
        raise ValueError("Baidu Netdisk path must not be empty")
    if not value.startswith("/"):
        value = "/" + value
    value = posixpath.normpath(value)
    if value == "/":
        if allow_root:
            return value
        raise ValueError("Baidu Netdisk file path must not be root")
    if any(part in {"", ".", ".."} for part in value.split("/")[1:]):
        raise ValueError(f"Invalid Baidu Netdisk path: {value}")
    return value


def _sanitize_relative_remote_path(value: str) -> str:
    value = str(value or "").strip().replace("\\", "/").strip("/")
    parts: list[str] = []
    for raw_part in value.split("/"):
        part = INVALID_REMOTE_CHARS.sub("_", raw_part).strip(". ")
        part = re.sub(r"\s+", "_", part)
        if not part or part in {".", ".."}:
            raise ValueError(f"Invalid baidu_netdisk.remote_path_template result: {value!r}")
        parts.append(part[:255])
    return "/".join(parts)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
