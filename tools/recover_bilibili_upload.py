from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
PREUPLOAD_URL = "https://member.bilibili.com/preupload"
ARCHIVE_VIEW_URL = "https://member.bilibili.com/x/client/archive/view"
ADD_URL = "https://member.bilibili.com/x/vu/web/add/v3"
EDIT_URL = "https://member.bilibili.com/x/vu/web/edit"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover a Bilibili upload with smaller resumable Upos chunks."
    )
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--metadata-yaml", type=Path)
    parser.add_argument("--cookie", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("new", "append"))
    parser.add_argument("--bvid", help="Existing submission for append mode")
    parser.add_argument("--proxy")
    parser.add_argument(
        "--direct-upos",
        action="store_true",
        help="Use the proxy for Bilibili APIs but upload video chunks directly.",
    )
    parser.add_argument("--line", default="bda2")
    parser.add_argument("--chunk-mib", type=int, default=2)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=12)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if args.mode == "append" and not args.bvid:
        parser.error("--bvid is required in append mode")
    if args.mode == "new" and not args.metadata_yaml:
        parser.error("--metadata-yaml is required in new mode")
    if args.chunk_mib < 1:
        parser.error("--chunk-mib must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


class RecoverUploader:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.video = args.video.resolve()
        self.metadata_yaml = args.metadata_yaml.resolve() if args.metadata_yaml else None
        self.cookie_file = args.cookie.resolve()
        self.checkpoint = (
            args.checkpoint.resolve()
            if args.checkpoint
            else self.video.with_suffix(self.video.suffix + ".small-chunks.json")
        )
        self.result_file = (
            args.result.resolve()
            if args.result
            else self.video.with_suffix(self.video.suffix + ".upload-result.json")
        )
        self.proxy = args.proxy
        self.cookies, self.access_token = load_login(self.cookie_file)
        self.csrf = self.cookies.get("bili_jct")
        if not self.csrf:
            raise RuntimeError("Cookie file does not contain bili_jct")
        if not self.access_token:
            raise RuntimeError("Cookie file does not contain access_token")
        self._checkpoint_lock = threading.Lock()
        self._thread_local = threading.local()

    def run(self) -> dict[str, Any]:
        if not self.video.is_file():
            raise FileNotFoundError(self.video)
        if self.metadata_yaml is not None and not self.metadata_yaml.is_file():
            raise FileNotFoundError(self.metadata_yaml)

        state = self._load_or_create_upload()
        completed = {int(item) for item in state.get("completed", [])}
        chunk_count = int(state["chunks"])
        pending = [index for index in range(chunk_count) if index not in completed]
        print(
            f"Uploading {self.video.name}: {len(completed)}/{chunk_count} chunks complete, "
            f"{len(pending)} pending",
            flush=True,
        )
        if pending:
            self._upload_pending(state, completed, pending)

        video = self._complete_upload(state)
        if self.args.mode == "new":
            assert self.metadata_yaml is not None
            metadata = load_metadata(self.metadata_yaml, self.video)
            payload = metadata
            payload["videos"] = [video]
            response = self._submit(ADD_URL, payload)
            data = response.get("data") or {}
            bvid = data.get("bvid") or data.get("BVID")
            if not bvid:
                raise RuntimeError(f"New submission succeeded but returned no BVID: {response}")
        else:
            bvid = str(self.args.bvid)
            payload = self._load_existing_studio(bvid)
            payload["videos"].append(video)
            self._submit(EDIT_URL, payload)

        result = {
            "bvid": bvid,
            "mode": self.args.mode,
            "video": str(self.video),
            "part_title": video["title"],
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        write_json_atomic(self.result_file, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    def _load_or_create_upload(self) -> dict[str, Any]:
        stat = self.video.stat()
        identity = {
            "video": str(self.video),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "line": self.args.line,
            "chunk_size": self.args.chunk_mib * 1024 * 1024,
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
        query = line_query(self.args.line)
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

        chunk_size = int(identity["chunk_size"])
        state = {
            **identity,
            "endpoint": endpoint,
            "upos_uri": upos_uri,
            "upload_url": upload_url,
            "auth": str(bucket["auth"]),
            "biz_id": int(bucket["biz_id"]),
            "upload_id": str(upload_id),
            "chunks": math.ceil(stat.st_size / chunk_size),
            "completed": [],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        write_json_atomic(self.checkpoint, state)
        return state

    def _upload_pending(
        self,
        state: dict[str, Any],
        completed: set[int],
        pending: list[int],
    ) -> None:
        for offset in range(0, len(pending), self.args.workers):
            batch = pending[offset : offset + self.args.workers]
            with ThreadPoolExecutor(max_workers=self.args.workers) as executor:
                futures = {
                    executor.submit(self._upload_chunk, state, index): index for index in batch
                }
                for future in as_completed(futures):
                    index = futures[future]
                    future.result()
                    with self._checkpoint_lock:
                        completed.add(index)
                        state["completed"] = sorted(completed)
                        write_json_atomic(self.checkpoint, state)
                        print(
                            f"chunk {index + 1}/{state['chunks']} complete "
                            f"({len(completed)}/{state['chunks']})",
                            flush=True,
                        )

    def _upload_chunk(self, state: dict[str, Any], index: int) -> None:
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
        for attempt in range(1, self.args.retries + 1):
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
                if attempt == self.args.retries:
                    break
                delay = min(30, 2 ** min(attempt, 5))
                print(
                    f"chunk {index + 1} attempt {attempt} failed; retrying in {delay}s: {exc}",
                    flush=True,
                )
                time.sleep(delay)
        raise RuntimeError(
            f"Chunk {index + 1} failed after {self.args.retries} attempts"
        ) from last_error

    def _complete_upload(self, state: dict[str, Any]) -> dict[str, str]:
        parts = [
            {"partNumber": index + 1, "eTag": "etag"}
            for index in range(int(state["chunks"]))
        ]
        response = self._request(
            self._new_session(use_proxy=not self.args.direct_upos),
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
        return {
            "title": self.video.stem[:80],
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
        for attempt in range(1, self.args.retries + 1):
            try:
                response = session.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status is not None and status < 500:
                    break
                if attempt == self.args.retries:
                    break
                delay = min(30, 2 ** min(attempt, 5))
                print(
                    f"{method} {url} attempt {attempt} failed; retrying in {delay}s: {exc}",
                    flush=True,
                )
                time.sleep(delay)
        raise RuntimeError(f"{method} {url} failed after retries") from last_error

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
            session = self._new_session(use_proxy=not self.args.direct_upos)
            self._thread_local.session = session
        return session


def line_query(line: str) -> dict[str, str]:
    queries = {
        "bldsa": {"zone": "cs", "upcdn": "bldsa", "probe_version": "20221109"},
        "bda2": {"zone": "cs", "upcdn": "bda2", "probe_version": "20221109"},
    }
    try:
        return queries[line]
    except KeyError as exc:
        raise ValueError(f"Unsupported line: {line}") from exc


def load_login(path: Path) -> tuple[dict[str, str], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cookies: dict[str, str] = {}
    for item in (payload.get("cookie_info") or {}).get("cookies") or []:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            cookies[str(name)] = str(value)
    access_token = str((payload.get("token_info") or {}).get("access_token") or "")
    return cookies, access_token


def load_metadata(path: Path, video: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    streamers = payload.get("streamers") or {}
    if len(streamers) != 1:
        raise RuntimeError(f"Expected one streamer entry in {path}")
    metadata = dict(next(iter(streamers.values())))
    metadata.pop("videos", None)
    return metadata


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    RecoverUploader(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
