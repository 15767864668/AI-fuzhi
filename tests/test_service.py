"""Real local media + mocked GPU HTTP integration tests, without paid requests."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import httpx
from PIL import Image
import pytest

from hongguo.backends import BackendError
from hongguo.media import probe
from hongguo.models import Project, Segment
from hongguo.script import rows_from_segments
from hongguo.service import StudioService

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="需要本机 FFmpeg")


@pytest.fixture
def tiny_mp4(tmp_path):
    path = tmp_path / "native-audio.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x288:r=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000", "-t", "2",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ], capture_output=True, check=True)
    return path


class GPU:
    """A deterministic HTTP transport; each POST represents a billed job."""
    def __init__(self, video: bytes):
        self.video = video
        self.posts: list[dict] = []
        self.fail_submit = False
        self.fail_poll = False
        self.async_mode = False
        self.downloads = 0
        self.polls = 0

    def __call__(self, request):
        if request.method == "POST":
            assert request.url.host == "gpu.local"
            assert request.headers.get("authorization") == "Bearer test-only-secret"
            self.posts.append(json.loads(request.content))
            if self.fail_submit:
                raise httpx.ReadTimeout("uncertain submission", request=request)
            if self.async_mode:
                return httpx.Response(202, json={"task_id": f"task-{len(self.posts)}", "status": "queued"})
            return httpx.Response(200, json={"video_url": f"https://cdn.local/{len(self.posts)}.mp4"})
        if request.url.host == "gpu.local":
            self.polls += 1
            if self.fail_poll:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json={"task_id": request.url.path.rsplit("/", 1)[-1], "status": "succeeded", "video_url": "https://cdn.local/recovered.mp4"})
        assert request.url.host == "cdn.local"
        assert "authorization" not in request.headers
        self.downloads += 1
        return httpx.Response(200, content=self.video)


def config_for(gpu: GPU):
    # Mirrors all defaults emitted by the UI, including parsed extra JSON.
    return {"backend": "gpu", "base_url": "http://gpu.local", "model": "MiniMax-H3",
            "resolution": "768P", "ratio": "9:16", "create_path": "/generate",
            "query_path": "/tasks/{task_id}", "extra_json": {}, "timeout": 60,
            "poll_interval": .01, "max_wait": 5, "api_key": "test-only-secret",
            "_transport": httpx.MockTransport(gpu)}


def make_project(service: StudioService, tmp_path: Path, *, one_row=False):
    project = service.new_project("测试短剧")
    project.settings.update(width=160, height=288, fps=10, video_bitrate="200k",
                            audio_bitrate="64k", font_size=20, subtitle_margin=30,
                            watermark_enabled=True, watermark_text="本地测试", watermark_font_size=12)
    project.segments = [Segment(id="first", text="第一句。", start=0, end=.5, character="女主")]
    if not one_row:
        project.segments.append(Segment(id="second", text="第二句。", start=.5, end=1, character="女主"))
    reference = tmp_path / "女主.png"
    Image.new("RGB", (256, 256), color="pink").save(reference)
    project = service.add_asset(project, str(reference), "characters", "女主")
    return project


def run_last(service, project, config, mode="all", selected_id=""):
    events = list(service.run(project, config, mode=mode, selected_id=selected_id))
    assert events and "任务完成" in events[-1]["log"]
    return events[-1]


def test_full_pipeline_cache_selected_regeneration_and_changed_line(tmp_path, tiny_mp4):
    service = StudioService(tmp_path / "data")
    project = make_project(service, tmp_path)
    gpu = GPU(tiny_mp4.read_bytes())
    cfg = config_for(gpu)

    first = run_last(service, project, cfg)
    project = first["project"]
    assert len(gpu.posts) == 2
    assert [post["dialogue"] for post in gpu.posts] == ["第一句。", "第二句。"]
    assert all(post["reference_images"][0].startswith("data:image/png;base64,") for post in gpu.posts)
    assert Path(first["output"]).is_file()
    info = probe(first["output"])
    assert abs(float(info["format"]["duration"]) - 1) < .15
    assert any(stream["codec_type"] == "audio" for stream in info["streams"])
    assert any(Path(path).suffix == ".ass" for path in first["files"])
    assert any("全部台词" in path for path in first["files"])
    assert all(Path(segment.clip_path).is_file() for segment in project.segments)
    assert "_transport" not in project.settings["backend_config"]
    assert "api_key" not in project.settings["backend_config"]
    assert "_transport" in cfg  # Saving settings must not mutate live request config.

    cached = run_last(service, project, cfg)
    project = cached["project"]
    assert len(gpu.posts) == 2 and gpu.downloads == 2
    assert cached["output"] != first["output"]
    assert "复用已生成片段" in cached["log"]
    clip_paths = [segment.clip_path for segment in project.segments]

    regenerated = run_last(service, project, cfg, "regenerate", "second")
    project = regenerated["project"]
    assert len(gpu.posts) == 3 and gpu.posts[-1]["dialogue"] == "第二句。"
    assert project.segments[0].clip_path == clip_paths[0]
    assert project.segments[1].clip_path != clip_paths[1]
    second_clip = project.segments[1].clip_path

    rows = rows_from_segments(project.segments)
    rows[0][1] = "第一句修改。"
    project = service.update(project, project.title, rows, project.settings)
    changed = run_last(service, project, cfg, "generate")
    assert len(gpu.posts) == 4 and gpu.posts[-1]["dialogue"] == "第一句修改。"
    assert changed["project"].segments[1].clip_path == second_clip


def test_portable_project_moves_assets_and_keeps_cache_fingerprints(tmp_path, tiny_mp4):
    original = StudioService(tmp_path / "original-data")
    project = make_project(original, tmp_path)
    project = original.add_asset(project, str(tiny_mp4), "bgm")
    gpu = GPU(tiny_mp4.read_bytes())
    cfg = config_for(gpu)
    project = run_last(original, project, cfg, "generate")["project"]
    bundle = Path(original.save(project))
    portable = tmp_path / "portable.hgproj"
    shutil.copyfile(bundle, portable)
    with zipfile.ZipFile(portable) as archive:
        document = archive.read("project.json").decode()
        assert "test-only-secret" not in document
        assert '"_transport"' not in document
        stored = json.loads(document)["project"]
        assert all(not Path(seg["clip_path"]).is_absolute() for seg in stored["segments"])
        assert not Path(stored["bgm"]).is_absolute()

    restored = StudioService(tmp_path / "restored-data")
    loaded = restored.load(str(portable))
    assert loaded.id != project.id
    assert loaded.characters["女主"] != project.characters["女主"]
    assert loaded.bgm != project.bgm and Path(loaded.bgm).is_file()
    assert all(Path(seg.clip_path).is_file() for seg in loaded.segments)
    assert [seg.fingerprint for seg in loaded.segments] == [seg.fingerprint for seg in project.segments]
    for segment in loaded.segments:
        assert restored.fingerprint(loaded, segment, cfg) == segment.fingerprint
    shutil.rmtree(original.storage.root)

    # Simulate UI hydration: defaults + saved backend configuration + session-only credentials.
    hydrated = {**cfg, **loaded.settings["backend_config"]}
    hydrated["extra_json"] = json.loads(json.dumps(hydrated["extra_json"], ensure_ascii=False, indent=2))
    loaded = restored.update(loaded, loaded.title, rows_from_segments(loaded.segments), copy.deepcopy(loaded.settings))
    final = run_last(restored, loaded, hydrated)
    assert len(gpu.posts) == 2 and gpu.downloads == 2
    assert Path(final["output"]).is_file()


def test_pending_task_resumes_after_poll_failure_without_another_post(tmp_path, tiny_mp4, monkeypatch):
    service = StudioService(tmp_path / "data")
    project = make_project(service, tmp_path, one_row=True)
    gpu = GPU(tiny_mp4.read_bytes())
    gpu.async_mode = gpu.fail_poll = True
    cfg = config_for(gpu)
    monkeypatch.setattr("hongguo.backends.time.sleep", lambda _: None)
    with pytest.raises(BackendError, match="网络请求失败"):
        list(service.run(project, cfg, "generate"))
    saved = service.storage.folder(project, "projects") / "autosave.json"
    recovered = Project.from_dict(json.loads(saved.read_text()))
    assert recovered.segments[0].fingerprint == service.fingerprint(recovered, recovered.segments[0], cfg)
    assert recovered.segments[0].remote_task["task_id"] == "task-1"
    assert len(gpu.posts) == 1
    gpu.fail_poll = False
    recovered = run_last(service, recovered, cfg, "generate")["project"]
    assert len(gpu.posts) == 1 and Path(recovered.segments[0].clip_path).is_file()


def test_unknown_submission_requires_explicit_regeneration(tmp_path, tiny_mp4):
    service = StudioService(tmp_path / "data")
    project = make_project(service, tmp_path, one_row=True)
    gpu = GPU(tiny_mp4.read_bytes())
    gpu.fail_submit = True
    cfg = config_for(gpu)
    with pytest.raises(BackendError, match="创建请求不会自动重发"):
        list(service.run(project, cfg, "generate"))
    saved = service.storage.folder(project, "projects") / "autosave.json"
    recovered = Project.from_dict(json.loads(saved.read_text()))
    assert recovered.segments[0].remote_task["status"] == "submission_unknown"
    gpu.fail_submit = False
    with pytest.raises(BackendError, match="结果不确定"):
        list(service.run(recovered, cfg, "generate"))
    assert len(gpu.posts) == 1
    final = run_last(service, recovered, cfg, "regenerate", "first")
    assert len(gpu.posts) == 2
    assert Path(final["project"].segments[0].clip_path).is_file()


def test_effective_backend_defaults_produce_stable_fingerprint(tmp_path):
    service = StudioService(tmp_path / "data")
    project = Project(segments=[Segment(text="台词")])
    sparse = {"backend": "gpu", "base_url": "http://gpu.local/"}
    hydrated = {"backend": "gpu", "base_url": "http://gpu.local", "model": "MiniMax-H3",
                "resolution": "768P", "ratio": "9:16", "create_path": "/generate",
                "query_path": "/tasks/{task_id}", "extra_json": {}}
    assert service.fingerprint(project, project.segments[0], sparse) == service.fingerprint(project, project.segments[0], hydrated)


def test_correcting_query_route_reuses_existing_billed_task(tmp_path, tiny_mp4):
    service = StudioService(tmp_path / "data")
    project = make_project(service, tmp_path, one_row=True)
    gpu = GPU(tiny_mp4.read_bytes())
    gpu.async_mode = True

    def handler(request):
        if request.method == "GET" and request.url.host == "gpu.local" and request.url.path.startswith("/broken/"):
            return httpx.Response(404, json={"error": {"message": "unknown query route"}})
        return gpu(request)

    cfg = config_for(gpu)
    cfg["_transport"] = httpx.MockTransport(handler)
    cfg["query_path"] = "/broken/{task_id}"
    with pytest.raises(BackendError, match="HTTP 404"):
        list(service.run(project, cfg, "generate"))
    saved = service.storage.folder(project, "projects") / "autosave.json"
    recovered = Project.from_dict(json.loads(saved.read_text()))
    assert recovered.segments[0].remote_task["task_id"] == "task-1"
    assert len(gpu.posts) == 1
    cfg["query_path"] = "/tasks/{task_id}"
    final = run_last(service, recovered, cfg, "generate")
    assert len(gpu.posts) == 1
    assert final["project"].segments[0].remote_task["query_path"] == "/tasks/{task_id}"
    assert Path(final["project"].segments[0].clip_path).is_file()


def test_batch_continues_after_invalid_project(tmp_path, tiny_mp4):
    service = StudioService(tmp_path / "data")
    project = make_project(service, tmp_path, one_row=True)
    gpu = GPU(tiny_mp4.read_bytes())
    cfg = config_for(gpu)
    project = run_last(service, project, cfg, "generate")["project"]
    valid = service.save(project)
    broken = tmp_path / "broken.hgproj"
    broken.write_bytes(b"not a zip")
    events = list(service.batch([str(broken), valid], cfg))
    assert "失败" in events[0]["log"]
    assert "2/2" in events[-1]["log"] and "任务完成" in events[-1]["log"]
    assert Path(events[-1]["output"]).is_file()
    assert len(gpu.posts) == 1  # Later valid bundle reused its cached clip.
