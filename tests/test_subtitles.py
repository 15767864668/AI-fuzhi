from pathlib import Path
from types import SimpleNamespace
import pytest

from hongguo.subtitles import write_ass, escape_text, _centiseconds, _ass_time


def segment(text="台词", start=0, end=2):
    return SimpleNamespace(text=text, start=start, end=end)


def test_ass_rounding_and_intro_offset(tmp_path):
    assert _ass_time(_centiseconds("59.995")) == "0:01:00.00"
    result = write_ass([segment(start=1.25, end=2.5)], tmp_path / "test.ass", 1080, 1920, offset=3)
    content = result.read_text(encoding="utf-8-sig")
    assert "0:00:04.25,0:00:05.50" in content
    assert "PlayResX: 1080" in content


def test_ass_text_injection_and_real_newlines(tmp_path):
    content = write_ass([segment(r"{\pos(0,0)}hello\N" + "\n下一行")], tmp_path / "safe.ass", 1080, 1920).read_text(encoding="utf-8-sig")
    event = [line for line in content.splitlines() if line.startswith("Dialogue:")][0]
    assert r"{\pos" not in event
    assert "｛＼pos(0,0)｝hello＼N" in event
    assert r"\N下一行" in event
    assert escape_text("a\x00b\tc") == "ab c"


def test_watermark_starts_at_movie_zero(tmp_path):
    path = write_ass([], tmp_path / "watermark.ass", 1080, 1920, offset=5, watermark={"text": "作品", "duration": 12.3, "corner": "左下", "font_size": 30})
    content = path.read_text(encoding="utf-8-sig")
    assert "Dialogue: 1,0:00:00.00,0:00:12.30,Watermark" in content
    assert "Style: Watermark" in content


@pytest.mark.parametrize("start,end", [(float("nan"), 2), (0, float("inf")), (1, 1), (-1, 2)])
def test_invalid_times(tmp_path, start, end):
    with pytest.raises(ValueError):
        write_ass([segment(start=start, end=end)], tmp_path / "bad.ass", 1080, 1920)


def test_cjk_wrap_does_not_change_time(tmp_path):
    text = "这是一个很长很长的中文短剧对白，需要在画面宽度内换行。" * 3
    content = write_ass([segment(text)], tmp_path / "wrap.ass", 720, 1280, font_size=64).read_text(encoding="utf-8-sig")
    assert r"\N" in content
    assert "0:00:00.00,0:00:02.00" in content
