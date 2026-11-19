"""HTTP adapter regression tests: no live credentials or paid calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from hongguo.backends import (
    BackendError, OFFICIAL_BASE, build_payload, download_video,
    generate_clip, normalize_response,
)
from hongguo.models import Segment

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 80


class BackendTests(unittest.TestCase):
    def test_official_v2_shape_and_reference(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "person.png"
            Image.new("RGB", (256, 256)).save(path)
            payload = build_payload(Segment(text="你好", start=0, end=2.2), [str(path)], {})
        self.assertEqual(payload["model"], "MiniMax-H3")
        self.assertEqual(payload["duration"], 4)
        self.assertEqual(payload["ratio"], "9:16")
        item = payload["content"][1]
        self.assertEqual(item["role"], "reference_image")
        self.assertTrue(item["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("audio", payload)

    def test_payload_validation_before_submission(self):
        with self.assertRaisesRegex(BackendError, "15 秒"):
            build_payload(Segment(text="长", end=15, trim_start=1), [], {})
        with self.assertRaisesRegex(BackendError, "不能覆盖"):
            build_payload(Segment(text="台词"), [], {"extra_json": {"model": "bad"}})
        with self.assertRaisesRegex(BackendError, "不支持"):
            build_payload(Segment(text="台词"), [], {"model": "MiniMax-H3-Lite", "resolution": "2K"})
        with self.assertRaisesRegex(BackendError, "JSON"):
            build_payload(Segment(text="台词"), [], {"extra_json": "{"})

    def test_official_poll_and_download_without_leaking_api_key(self):
        requests = []
        checkpoints = []
        def handler(request):
            requests.append(request)
            if request.method == "POST":
                self.assertEqual(request.url.path, "/v2/video_generation")
                self.assertEqual(request.headers["Authorization"], "Bearer SECRET")
                return httpx.Response(200, json={"task_id": "123"})
            if request.url.host == "api.minimax.cn":
                return httpx.Response(200, json={"task": {"id": "123", "status": "succeeded", "content": {"url": "https://cdn.example/clip.mp4?sig=private"}}})
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=MP4)
        segment = Segment(text="测试")
        with TemporaryDirectory() as tmp:
            result = generate_clip(segment, [], {"api_key": "SECRET", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None, checkpoints.append)
            self.assertEqual(Path(result["local_path"]).read_bytes(), MP4)
        self.assertEqual(result["task_id"], "123")
        self.assertTrue(result["video_url"].startswith("https://cdn.example/"))
        self.assertEqual(sum(r.method == "POST" for r in requests), 1)
        self.assertEqual(checkpoints[0]["status"], "submission_unknown")
        self.assertEqual(checkpoints[1]["task_id"], "123")
        self.assertNotIn("SECRET", json.dumps(checkpoints))

    def test_gpu_sync_and_async_contract(self):
        for asynchronous in (False, True):
            requests = []
            def handler(request):
                requests.append(request)
                if request.method == "POST":
                    payload = json.loads(request.content)
                    self.assertEqual(payload["dialogue"], "测试")
                    self.assertEqual(payload["reference_images"], [])
                    return httpx.Response(202, json={"task_id": "gpu-1", "status": "queued"}) if asynchronous else httpx.Response(200, json={"video_url": "http://gpu.local/result.mp4"})
                if request.url.path.startswith("/tasks/"):
                    return httpx.Response(200, json={"task_id": "gpu-1", "status": "succeeded", "video_url": "http://gpu.local/result.mp4"})
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(200, content=MP4)
            with TemporaryDirectory() as tmp:
                result = generate_clip(Segment(text="测试"), [], {"backend": "gpu", "base_url": "http://gpu.local", "api_key": "GPU-SECRET", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None)
                self.assertTrue(Path(result["local_path"]).exists())
            self.assertEqual(sum(r.method == "POST" for r in requests), 1)

    def test_resume_saved_task_does_not_submit(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            self.assertEqual(request.method, "GET")
            if request.url.host == "api.minimax.cn":
                return httpx.Response(200, json={"task": {"id": "recover", "status": "succeeded", "content": {"url": "https://cdn.example/recovered.mp4"}}})
            return httpx.Response(200, content=MP4)
        segment = Segment(text="恢复", remote_task={"backend": "official", "base_url": OFFICIAL_BASE, "task_id": "recover", "status": "running"})
        with TemporaryDirectory() as tmp:
            generate_clip(segment, [], {"api_key": "key", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None)
        self.assertNotIn("POST", calls)

    def test_corrected_poll_route_overrides_saved_route_without_paid_post(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.method, "GET")
            if request.url.host == "gpu.local":
                self.assertEqual(request.url.path, "/correct/tasks/existing")
                return httpx.Response(200, json={"task_id": "existing", "status": "succeeded", "video_url": "https://cdn.example/existing.mp4"})
            return httpx.Response(200, content=MP4)
        segment = Segment(text="恢复", remote_task={"backend": "gpu", "base_url": "http://gpu.local", "task_id": "existing", "status": "running", "query_path": "/broken/{task_id}"})
        with TemporaryDirectory() as tmp:
            generate_clip(segment, [], {"backend": "gpu", "base_url": "http://gpu.local", "query_path": "/correct/tasks/{task_id}", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None)
        self.assertEqual(segment.remote_task["query_path"], "/correct/tasks/{task_id}")
        self.assertFalse(any(request.method == "POST" for request in calls))

    def test_omitted_poll_route_uses_saved_custom_route(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            if request.url.host == "gpu.local":
                self.assertEqual(request.url.path, "/custom/existing")
                return httpx.Response(200, json={"task_id": "existing", "status": "succeeded", "video_url": "https://cdn.example/existing.mp4"})
            return httpx.Response(200, content=MP4)
        segment = Segment(text="恢复", remote_task={"backend": "gpu", "base_url": "http://gpu.local", "task_id": "existing", "status": "running", "query_path": "/custom/{task_id}"})
        with TemporaryDirectory() as tmp:
            generate_clip(segment, [], {"backend": "gpu", "base_url": "http://gpu.local", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None)
        self.assertEqual(segment.remote_task["query_path"], "/custom/{task_id}")

    def test_uncertain_paid_post_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("key MUST NOT APPEAR", request=request)
        segment = Segment(text="测试")
        config = {"api_key": "key", "_transport": httpx.MockTransport(handler)}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.mp4"
            with self.assertRaises(BackendError) as caught:
                generate_clip(segment, [], config, path, lambda _: None)
            self.assertNotIn("MUST NOT APPEAR", str(caught.exception))
            self.assertEqual(segment.remote_task["status"], "submission_unknown")
            with self.assertRaisesRegex(BackendError, "不确定"):
                generate_clip(segment, [], config, path, lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_poll_failure_retains_remote_id(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"task_id": "saved"})
            raise httpx.ConnectError("offline", request=request)
        segment = Segment(text="测试")
        with TemporaryDirectory() as tmp, patch("hongguo.backends.time.sleep"):
            with self.assertRaises(BackendError):
                generate_clip(segment, [], {"api_key": "key", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None)
        self.assertEqual(segment.remote_task["task_id"], "saved")

    def test_bad_or_oversize_download_is_not_committed(self):
        for response in (httpx.Response(200, content=b"not mp4"), httpx.Response(200, headers={"Content-Length": "2000000000"}, content=b"")):
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "video.mp4"
                with self.assertRaises(BackendError):
                    download_video("https://cdn.example/video.mp4", path, transport=httpx.MockTransport(lambda _: response))
                self.assertFalse(path.exists())
                self.assertFalse(path.with_name(path.name + ".part").exists())

    def test_service_error_redacts_token_and_signed_urls(self):
        def handler(request):
            return httpx.Response(401, json={"error": {"message": "Bearer TOPSECRET https://example.com/?signed=private"}})
        with TemporaryDirectory() as tmp:
            with self.assertRaises(BackendError) as caught:
                generate_clip(Segment(text="测试"), [], {"api_key": "TOPSECRET", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None)
        self.assertNotIn("TOPSECRET", str(caught.exception))
        self.assertNotIn("signed=private", str(caught.exception))

    def test_failed_task_is_reported(self):
        def handler(request):
            return httpx.Response(200, json={"task_id": "fail"}) if request.method == "POST" else httpx.Response(200, json={"task": {"id": "fail", "status": "failed"}})
        segment = Segment(text="测试")
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(BackendError, "失败"):
                generate_clip(segment, [], {"api_key": "key", "_transport": httpx.MockTransport(handler)}, Path(tmp) / "out.mp4", lambda _: None)
        self.assertEqual(segment.remote_task["status"], "failed")


if __name__ == "__main__":
    unittest.main()
