from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from showroomrecorder.baidu_netdisk import BaiduNetdiskClient, _normalize_remote_path
from showroomrecorder.config import BaiduNetdiskConfig, load_config


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error for url: "
                "https://example.test/token?client_secret=secret-key",
                response=self,
            )

    def json(self) -> dict:
        return self.payload


class _DeviceAuthSession:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.headers: dict[str, str] = {}
        self.trust_env = False
        self.requests: list[dict] = []

    def request(self, _method: str, _url: str, **_kwargs) -> _Response:
        self.requests.append(_kwargs)
        payload = dict(self.payloads.pop(0))
        status_code = int(payload.pop("_status_code", 200))
        return _Response(payload, status_code)


class _FakeUploadClient(BaiduNetdiskClient):
    def __init__(self, config: BaiduNetdiskConfig, *, fail_part: int | None = None) -> None:
        super().__init__(config)
        self.fail_part = fail_part
        self.precreate_calls = 0
        self.uploaded_parts: list[int] = []
        self.created_with: list[str] = []

    def _ensure_remote_directory(self, _remote_dir: str) -> None:
        return None

    def _precreate(self, _remote_path: str, _size: int, block_md5s: list[str]) -> dict:
        self.precreate_calls += 1
        return {"uploadid": "upload-1", "block_list": list(range(len(block_md5s)))}

    def _upload_part(
        self,
        _local_file: Path,
        _remote_path: str,
        _upload_id: str,
        part_index: int,
        _chunk_size: int,
        _file_size: int,
    ) -> str:
        if self.fail_part == part_index:
            raise OSError("temporary upload failure")
        self.uploaded_parts.append(part_index)
        return f"server-md5-{part_index}"

    def _create_file(
        self,
        remote_path: str,
        size: int,
        _upload_id: str,
        block_md5s: list[str],
    ) -> dict:
        self.created_with = list(block_md5s)
        return {"fs_id": 123, "path": remote_path, "size": size, "md5": "file-md5"}


def _session(raw_file: Path) -> SimpleNamespace:
    return SimpleNamespace(
        room=SimpleNamespace(name="测试房间", url="https://example.test/room", room_id=123),
        job_id="20260808_120000_test-room",
        started_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc),
        live_title="test live",
        raw_file=raw_file,
    )


class BaiduNetdiskConfigTests(unittest.TestCase):
    def test_remote_root_path_is_valid_only_when_allowed(self) -> None:
        self.assertEqual(_normalize_remote_path("/", allow_root=True), "/")
        with self.assertRaises(ValueError):
            _normalize_remote_path("/", allow_root=False)

    def test_load_config_resolves_baidu_paths_and_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_file = root / "config.yaml"
            config_file.write_text(
                """
service:
  data_dir: data
rooms:
  - name: test
    url: https://example.test/room
record:
  live_end_grace_seconds: 180
baidu_netdisk:
  token_file: private/token.json
  state_dir: private/uploads
  conflict_policy: overwrite
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_file)

            self.assertEqual(config.record.live_end_grace_seconds, 180)
            self.assertEqual(config.baidu_netdisk.token_file, (root / "private/token.json").resolve())
            self.assertEqual(config.baidu_netdisk.state_dir, (root / "private/uploads").resolve())


class BaiduNetdiskAuthTests(unittest.TestCase):
    def test_device_auth_polls_and_persists_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "token.json"
            config = BaiduNetdiskConfig(
                app_key="app-key",
                secret_key="secret-key",
                token_file=token_file,
                state_dir=Path(temp_dir) / "uploads",
                retries=1,
            )
            http = _DeviceAuthSession(
                [
                    {
                        "device_code": "device-code",
                        "user_code": "ABCD-EFGH",
                        "verification_url": "https://example.test/auth",
                        "expires_in": 60,
                        "interval": 1,
                    },
                    {"error": "authorization_pending", "_status_code": 400},
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_in": 3600,
                    },
                ]
            )
            client = BaiduNetdiskClient(config, session=http)
            output: list[str] = []

            with patch("showroomrecorder.baidu_netdisk.time.sleep"):
                token = client.authorize_device(output.append)

            saved = json.loads(token_file.read_text(encoding="utf-8"))
            self.assertEqual(token["access_token"], "access-token")
            self.assertEqual(saved["refresh_token"], "refresh-token")
            self.assertGreater(saved["expires_at"], saved["obtained_at"])
            self.assertTrue(any("ABCD-EFGH" in line for line in output))

    def test_http_error_does_not_expose_request_url(self) -> None:
        config = BaiduNetdiskConfig(
            app_key="app-key",
            secret_key="secret-key",
            retries=1,
        )
        http = _DeviceAuthSession([{"message": "failure", "_status_code": 500}])
        client = BaiduNetdiskClient(config, session=http)

        with self.assertRaises(requests.RequestException) as raised:
            client._request_json(
                "GET",
                "https://example.test/token",
                operation="test request",
                params={"client_secret": "secret-key"},
            )

        self.assertNotIn("secret-key", str(raised.exception))
        self.assertNotIn("example.test", str(raised.exception))
        self.assertIn("HTTP 500", str(raised.exception))


class BaiduNetdiskDirectoryTests(unittest.TestCase):
    def test_list_directories_filters_locally_without_folder_parameter(self) -> None:
        config = BaiduNetdiskConfig(retries=1)
        http = _DeviceAuthSession(
            [
                {
                    "errno": 0,
                    "list": [
                        {"server_filename": "folder", "path": "/folder", "isdir": 1},
                        {"server_filename": "file.mkv", "path": "/file.mkv", "isdir": 0},
                    ],
                }
            ]
        )
        client = BaiduNetdiskClient(config, session=http)

        with patch.object(client, "_access_token", return_value="access-token"):
            directories = client.list_directories("/")

        self.assertEqual([item["path"] for item in directories], ["/folder"])
        self.assertNotIn("folder", http.requests[0]["params"])


class BaiduNetdiskUploadTests(unittest.TestCase):
    def _config(self, root: Path) -> BaiduNetdiskConfig:
        return BaiduNetdiskConfig(
            enabled=True,
            app_key="app-key",
            secret_key="secret-key",
            token_file=root / "token.json",
            state_dir=root / "uploads",
            remote_root="/apps/showroom",
            remote_path_template=(
                "{streamer_slug}/{streamer_slug}_{started_at:%Y%m%d_%H%M%S}_recording-merged.mkv"
            ),
            chunk_size_mb=1,
            retries=1,
        )

    def test_upload_is_idempotent_after_complete_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_file = root / "recording-merged.mkv"
            raw_file.write_bytes(b"a" * (1024 * 1024 + 10))
            client = _FakeUploadClient(self._config(root))
            session = _session(raw_file)

            first = client.upload_recording(session)
            second = client.upload_recording(session)

            self.assertEqual(client.precreate_calls, 1)
            self.assertEqual(client.uploaded_parts, [0, 1])
            self.assertEqual(client.created_with, ["server-md5-0", "server-md5-1"])
            self.assertEqual(first.path, "/apps/showroom/测试房间/测试房间_20260808_120000_recording-merged.mkv")
            self.assertTrue(second.resumed)

    def test_failed_upload_resumes_without_reuploading_completed_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_file = root / "recording-merged.mkv"
            raw_file.write_bytes(b"b" * (1024 * 1024 + 10))
            config = self._config(root)
            session = _session(raw_file)
            first = _FakeUploadClient(config, fail_part=1)

            with self.assertRaises(OSError):
                first.upload_recording(session)

            resumed = _FakeUploadClient(config)
            result = resumed.upload_recording(session)

            self.assertEqual(first.uploaded_parts, [0])
            self.assertEqual(resumed.precreate_calls, 0)
            self.assertEqual(resumed.uploaded_parts, [1])
            self.assertEqual(result.fs_id, 123)


if __name__ == "__main__":
    unittest.main()
