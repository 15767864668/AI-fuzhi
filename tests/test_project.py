"""项目导入、持久化与恢复的行为测试。"""
import json
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from hongguo.models import Segment, validate_segments
from hongguo.script import parse_script_text, rows_from_segments
from hongguo.service import StudioService


def test_timed_plain_script_and_invalid_overlap():
    segments = parse_script_text("# 注释\n[00:00:00-00:00:04] 女主|你好。|近景\n下一句。", 5)
    assert [(s.start, s.end, s.text) for s in segments] == [(0, 4, "你好。"), (4, 9, "下一句。")]
    assert segments[0].character == "女主"
    with pytest.raises(ValueError, match="重叠"):
        parse_script_text("[0-4] 甲\n[3-5] 乙")
    with pytest.raises(ValueError, match="有限"):
        validate_segments([Segment(text="你好", start=float("nan"))])


def test_portable_bundle_and_autosave(tmp_path):
    service = StudioService(tmp_path / "data")
    project = service.new_project("回家的路")
    project = service.update(project, project.title, rows_from_segments(parse_script_text("女主|你好。")), {"backend_config": {"api_key": "NEVER_SAVE_THIS", "backend": "gpu"}})
    image = tmp_path / "actor.png"
    Image.new("RGB", (256, 256)).save(image)
    project = service.add_asset(project, str(image), "characters", "女主")
    bundle = service.save(project)
    with zipfile.ZipFile(bundle) as archive:
        manifest = archive.read("project.json").decode()
        assert "NEVER_SAVE_THIS" not in manifest
        assert str(tmp_path) not in manifest
    restored = service.load(bundle)
    assert restored.id != project.id
    assert restored.title == project.title
    assert restored.segments[0].text == "你好。"
    assert Path(restored.characters["女主"]).is_file()
    assert service.load_autosave(restored.id).characters == restored.characters
    assert any(identifier == restored.id for label, identifier in service.list_autosaves())
    assert Path(service.export_dialogue(restored)).read_text(encoding="utf-8") == "女主：你好。\n"


def test_bundle_rejects_traversal(tmp_path):
    service = StudioService(tmp_path / "data")
    bundle = tmp_path / "bad.hgproj"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    with pytest.raises(ValueError, match="不安全"):
        service.load(str(bundle))
    assert not (tmp_path / "escape.txt").exists()


def test_autosave_rejects_unmanaged_paths(tmp_path):
    service = StudioService(tmp_path / "data")
    project = service.new_project()
    project.bgm = str(tmp_path / "external.mp3")
    service.storage.checkpoint(project)
    with pytest.raises(ValueError, match="超出"):
        service.load_autosave(project.id)


def test_draft_and_generation_fingerprint(tmp_path):
    service = StudioService(tmp_path / "data")
    project = service.update(service.new_project(), "草稿", [], {})
    assert not project.segments
    segment = Segment(text="你好")
    sparse = {"backend": "gpu", "base_url": "http://gpu.local/"}
    full = {**sparse, "base_url": "http://gpu.local", "model": "MiniMax-H3", "resolution": "768P", "ratio": "9:16", "create_path": "/generate", "query_path": "/tasks/{task_id}", "extra_json": "{}"}
    assert service.fingerprint(project, segment, sparse) == service.fingerprint(project, segment, full)
