from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from showroomrecorder.config import RecordProxyConfig, load_config
from showroomrecorder.recording_proxy import RecordingProxyResolver, parse_proxy_source
from showroomrecorder.recorder import StreamRecorder


class RecordingProxyConfigTests(unittest.TestCase):
    def test_load_config_resolves_proxy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_file = root / "config.yaml"
            config_file.write_text(
                """
service:
  data_dir: data
rooms:
  - name: test
    url: https://www.showroom-live.com/r/test
record:
  proxy:
    enabled: true
    mode: fallback
    urls: http://127.0.0.1:7897
    file: proxy-list.yaml
    cache_file: data/proxy/cache.txt
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_file)

        self.assertTrue(config.record.proxy.enabled)
        self.assertEqual(config.record.proxy.mode, "auto")
        self.assertEqual(config.record.proxy.urls, ["http://127.0.0.1:7897"])
        self.assertEqual(config.record.proxy.file, (root / "proxy-list.yaml").resolve())
        self.assertEqual(
            config.record.proxy.cache_file,
            (root / "data" / "proxy" / "cache.txt").resolve(),
        )

    def test_proxy_source_supports_yaml_and_base64_endpoint_lists(self) -> None:
        yaml_source = """
proxies:
  - http://127.0.0.1:7897
  - url: https://proxy.example:8443
  - ss://unsupported
"""
        encoded = base64.b64encode(
            b"http://proxy-one.example:8080\nhttp://proxy-two.example:8080"
        ).decode("ascii")

        self.assertEqual(
            parse_proxy_source(yaml_source),
            ["http://127.0.0.1:7897", "https://proxy.example:8443"],
        )
        self.assertEqual(
            parse_proxy_source(encoded),
            ["http://proxy-one.example:8080", "http://proxy-two.example:8080"],
        )


class RecordingProxyResolverTests(unittest.TestCase):
    def test_auto_routes_use_healthy_system_proxy_then_direct_then_project_proxy(self) -> None:
        config = RecordProxyConfig(
            enabled=True,
            mode="auto",
            include_system=True,
            urls=["http://127.0.0.1:7897"],
            refresh_seconds=0,
        )
        resolver = RecordingProxyResolver(config)

        with patch(
            "showroomrecorder.recording_proxy.getproxies",
            return_value={"https": "http://system.example:8080"},
        ), patch.object(resolver, "_probe", return_value=True):
            routes = resolver.routes()

        self.assertEqual(
            routes,
            ["http://system.example:8080", None, "http://127.0.0.1:7897"],
        )

    def test_unhealthy_proxy_is_retained_after_direct_route(self) -> None:
        config = RecordProxyConfig(
            enabled=True,
            mode="auto",
            include_system=False,
            urls=["http://127.0.0.1:7897"],
            refresh_seconds=0,
        )
        resolver = RecordingProxyResolver(config)

        with patch.object(resolver, "_probe", return_value=False):
            routes = resolver.routes()

        self.assertEqual(routes, [None, "http://127.0.0.1:7897"])


class RecordingProxyCommandTests(unittest.TestCase):
    def _recorder(self) -> StreamRecorder:
        recorder = object.__new__(StreamRecorder)
        recorder.config = SimpleNamespace(
            record=SimpleNamespace(
                yt_dlp_bin="yt-dlp",
                cookies_file=None,
                extra_args=[],
                streamlink_bin="streamlink",
                streamlink_extra_args=[],
                hls_concurrent_fragments=8,
                hls_fragment_retries=5,
                max_seconds=None,
            ),
            transcode=SimpleNamespace(ffmpeg_bin="ffmpeg"),
        )
        recorder.showroom = SimpleNamespace(session=SimpleNamespace(cookies=[]))
        return recorder

    def test_proxy_is_added_to_streamlink_and_ffmpeg_commands(self) -> None:
        recorder = self._recorder()
        proxy_url = "http://user:secret@127.0.0.1:7897"
        with patch.object(
            recorder,
            "_streamlink_command_prefix",
            return_value=["showroomrecorder.exe", "--streamlink-worker"],
        ):
            streamlink = recorder._streamlink_record_command(
                "https://cdn.example/live.m3u8",
                Path("recording.ts"),
                proxy_url=proxy_url,
            )
        ffmpeg = recorder._ffmpeg_record_command(
            "https://cdn.example/live.m3u8",
            Path("recording.ts"),
            proxy_url=proxy_url,
        )

        self.assertEqual(streamlink[streamlink.index("--http-proxy") + 1], proxy_url)
        self.assertEqual(ffmpeg[ffmpeg.index("-http_proxy") + 1], proxy_url)
        logged = recorder._format_command_for_log(streamlink)
        self.assertNotIn("user", logged)
        self.assertNotIn("secret", logged)
        self.assertIn("http://127.0.0.1:7897", logged)

    def test_proxy_is_added_to_ytdlp_command(self) -> None:
        recorder = self._recorder()
        recorder.proxy_resolver = SimpleNamespace(routes=lambda: ["http://127.0.0.1:7897"])
        session = SimpleNamespace(
            room=SimpleNamespace(
                name="test-room",
                url="https://www.showroom-live.com/r/test",
                cookies_file=None,
            ),
            job_id="test-job",
        )
        recorded_file = Path("recording-proxy01.ts")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            recorder,
            "_capture_dir",
            return_value=Path(temp_dir),
        ), patch.object(
            recorder,
            "_yt_dlp_command_prefix",
            return_value=["showroomrecorder.exe", "--yt-dlp-worker"],
        ), patch.object(
            recorder,
            "_run_record_command",
            return_value=10.0,
        ) as run_command, patch.object(
            recorder,
            "_find_recorded_file",
            return_value=recorded_file,
        ), patch.object(
            recorder,
            "_write_capture_health_report",
        ):
            result = recorder._record_with_ytdlp(
                session,
                "https://cdn.example/live.m3u8",
            )

        command = run_command.call_args.args[0]
        self.assertEqual(command[command.index("--proxy") + 1], "http://127.0.0.1:7897")
        self.assertEqual(result, recorded_file)

    def test_streamlink_retries_with_project_proxy_after_direct_route_fails(self) -> None:
        recorder = self._recorder()
        recorder.config.paths = SimpleNamespace(raw_dir=Path("unused"))
        recorder.config.record.streamlink_fallback_to_ffmpeg = False
        recorder.config.record.min_file_size_mb = 0
        recorder.proxy_resolver = SimpleNamespace(
            routes=lambda: [None, "http://127.0.0.1:7897"]
        )
        recorder.showroom.get_streaming_urls = lambda _room: [
            "https://cdn.example/live_main_mm.m3u8"
        ]
        session = SimpleNamespace(
            room=SimpleNamespace(name="test-room"),
            job_id="test-job",
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            recorder,
            "_capture_dir",
            return_value=Path(temp_dir),
        ), patch.object(
            recorder,
            "_streamlink_command_prefix",
            return_value=["showroomrecorder.exe", "--streamlink-worker"],
        ), patch.object(
            recorder,
            "_run_record_command",
            side_effect=[RuntimeError("direct failed"), 10.0],
        ) as run_command, patch.object(
            recorder,
            "_write_capture_health_report",
        ), patch.object(
            recorder,
            "_validate_recorded_file",
            side_effect=lambda path: path,
        ):
            result = recorder._record_with_streamlink(session)

        direct_command = run_command.call_args_list[0].args[0]
        proxy_command = run_command.call_args_list[1].args[0]
        self.assertNotIn("--http-proxy", direct_command)
        self.assertEqual(
            proxy_command[proxy_command.index("--http-proxy") + 1],
            "http://127.0.0.1:7897",
        )
        self.assertIn("proxy02", result.name)


if __name__ == "__main__":
    unittest.main()
