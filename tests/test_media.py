"""真实 FFmpeg 集成测试，覆盖时间轴、无声片头、混音、规格及失败路径。"""
from __future__ import annotations

import array
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace as NS
import wave

import pytest

from hongguo.media import MediaError, analyze_audio, probe, render_project, validate_specs

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="需要本机 FFmpeg")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "2", *args], check=True, capture_output=True)


@pytest.fixture
def assets(tmp_path):
    clip = tmp_path / "带人声片段.mp4"
    intro = tmp_path / "无声片头.mp4"
    music = tmp_path / "背景音乐.wav"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=200x300:r=25", "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=24000", "-t", "2", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(clip))
    ffmpeg("-f", "lavfi", "-i", "color=c=green:s=200x300:r=25", "-t", "0.4", "-c:v", "libx264", "-threads", "2", str(intro))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=100:sample_rate=44100", "-t", "0.3", str(music))
    return clip, intro, music


def make_project(assets):
    clip, intro, music = assets
    segment = NS(id="one", text="你好世界", start=.2, end=1.2, trim_start=.2, clip_path=str(clip))
    settings = {"width": 160, "height": 284, "fps": 25, "font_size": 16, "subtitle_margin": 25, "video_bitrate": "300k", "audio_bitrate": "64k", "sample_rate": 48000, "denoise": True, "watermark_enabled": True, "watermark_text": "测试", "watermark_font_size": 10, "fit": "pad"}
    return NS(segments=[segment], settings=settings, intro=str(intro), outro=str(intro), bgm=str(music))


def test_render_mixed_sources_and_subtitle_offset(tmp_path, assets):
    project = make_project(assets)
    result = render_project(project, tmp_path / "成片.mp4")
    info = probe(result["output"])
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"], video["codec_name"]) == (160, 284, "h264")
    assert audio["sample_rate"] == "48000" and audio["channels"] == 2
    # 0.4 片头 + 0.2 黑场 + 1.0 片段 + 0.4 片尾。
    assert abs(float(info["format"]["duration"]) - 2) < .08
    ass = Path(result["subtitle"]).read_text(encoding="utf-8-sig")
    assert "0:00:00.60,0:00:01.60" in ass
    assert "Watermark" in ass
    assert any("24000" in warning for warning in result["warnings"])
    assert not validate_specs(result["output"], project.settings)


def test_plain_export_without_bgm_or_subtitles(tmp_path, assets):
    project = make_project(assets)
    project.bgm = project.intro = project.outro = ""
    project.settings.update(subtitle_enabled=False, watermark_enabled=False, denoise=False)
    result = render_project(project, tmp_path / "plain.mp4")
    assert Path(result["output"]).is_file()
    assert "Dialogue: " not in Path(result["subtitle"]).read_text(encoding="utf-8-sig")


def test_refuses_short_clip_and_keeps_old_export(tmp_path, assets):
    project = make_project(assets)
    project.segments[0].end = 4
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"prior-export")
    with pytest.raises(MediaError, match="时长不足"):
        render_project(project, output)
    assert output.read_bytes() == b"prior-export"


def test_requires_h3_native_audio(tmp_path, assets):
    project = make_project(assets)
    project.intro = project.outro = project.bgm = ""
    project.segments[0].clip_path = str(assets[1])
    project.segments[0].start, project.segments[0].end = 0, .2
    project.segments[0].trim_start = 0
    with pytest.raises(MediaError, match="没有音轨"):
        render_project(project, tmp_path / "invalid.mp4")


def test_streaming_audio_silence_and_clipping(tmp_path):
    path = tmp_path / "defects.wav"
    samples = array.array("h", [0] * 16000 + [32767, -32768] * 1600)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    warnings = analyze_audio(path)
    assert any("静音：0.00–1.00" in w for w in warnings)
    assert any("疑似爆音/削波" in w for w in warnings)


def test_odd_output_dimension_rejected(tmp_path, assets):
    project = make_project(assets)
    project.settings["width"] = 161
    with pytest.raises(MediaError, match="偶数"):
        render_project(project, tmp_path / "bad.mp4")


def test_opposite_phase_stereo_does_not_cancel_detection(tmp_path):
    path = tmp_path / "opposite_stereo.wav"
    # 同一帧左右声道反相，单声道下混会抵消，但两个声道都接近满幅。
    samples = array.array("h", [32767, -32767] * 4000)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    warnings = analyze_audio(path)
    assert any("疑似爆音/削波" in w for w in warnings)
    assert not any("静音" in w for w in warnings)
