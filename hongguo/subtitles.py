"""ASS 字幕与角落水印。

所有时间均为正文时间轴的秒数；``offset`` 只用于将正文整体后移到片头后。
用户文字中的反斜线和大括号显示为相似的全角字符，阻止 ASS 标签/换行注入。
本模块不生成语音，也不推测语音出现时间。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from pathlib import Path
import re
import unicodedata
from typing import Iterable, Any


SUBTITLE_STYLES: dict[str, dict[str, Any]] = {
    "经典白字": {
        "font_size": 64, "primary": "&H00FFFFFF", "outline": "&H00131313",
        "back": "&H90000000", "outline_width": 4, "shadow": 2, "bold": -1,
    },
    "高亮黄字": {
        "font_size": 68, "primary": "&H0000EFFF", "outline": "&H00101010",
        "back": "&H90000000", "outline_width": 5, "shadow": 3, "bold": -1,
    },
    "清爽蓝字": {
        "font_size": 62, "primary": "&H00FFF1D2", "outline": "&H002F1B08",
        "back": "&H90000000", "outline_width": 4, "shadow": 2, "bold": -1,
    },
    "红果粗黑描边": {
        "font_size": 72, "primary": "&H00FFFFFF", "outline": "&H00000000",
        "back": "&HA0000000", "outline_width": 6, "shadow": 4, "bold": -1,
    },
}


def _finite_number(value: Any, field: str, *, minimum: float = 0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是数字") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{field}必须是大于等于 {minimum:g} 的有限数字")
    return value


def _centiseconds(seconds: Any) -> int:
    """使用十进制半入舍入，避免浮点 59.995 秒的进位错误。"""
    _finite_number(seconds, "字幕时间")
    try:
        return int((Decimal(str(seconds)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("无效的字幕时间") from exc


def _ass_time(centiseconds: int) -> str:
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def escape_text(text: str) -> str:
    """保留正常换行，消除可被 ASS 解释为指令的字符。

    ASS 的字面反斜线转义在播放器之间不一致，使用全角字形可以安全导出。
    """
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(char for char in text if char == "\n" or not unicodedata.category(char).startswith("C"))
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _wrap_text(text: str, max_units: int) -> str:
    """按字形宽度软换行；只改变换行，不改变字幕的显示时段。"""
    lines: list[str] = []
    for explicit_line in escape_text(text).split("\n"):
        current = ""
        units = 0
        for char in explicit_line:
            char_units = _char_width(char)
            if current and units + char_units > max_units:
                # 英文优先在最近空格处断行；中文允许字间换行。
                last_space = current.rfind(" ")
                if last_space >= max(1, len(current) // 2):
                    lines.append(current[:last_space].rstrip())
                    current = current[last_space + 1:]
                    units = sum(_char_width(c) for c in current)
                else:
                    lines.append(current.rstrip())
                    current = ""
                    units = 0
            current += char
            units += char_units
        lines.append(current.rstrip())
    return r"\N".join(lines)


def _safe_font(font_name: str) -> str:
    # Style 字段以逗号分隔，禁止换行和标签进入字段。
    font_name = re.sub(r"[,\r\n{}\\]", " ", str(font_name)).strip()
    font_name = "".join(c for c in font_name if not unicodedata.category(c).startswith("C"))
    return font_name[:120] or "Noto Sans CJK SC"


def write_ass(
    segments: Iterable[Any],
    destination: str | Path,
    width: int,
    height: int,
    style: str = "经典白字",
    font_name: str = "Noto Sans CJK SC",
    font_size: int | float = 64,
    margin: int | float = 180,
    offset: int | float = 0,
    watermark: dict[str, Any] | None = None,
) -> Path:
    """将台词写为 UTF-8 ASS 文件，返回目标路径。

    ``watermark`` 可指定 text、corner、font_size 和 duration；水印从成片的
    0 秒开始，不受正文 offset 影响。字幕边距和字号采用输出分辨率的像素单位。
    ``segments`` 可以为空（只输出水印）。
    """
    width = int(_finite_number(width, "画面宽度", minimum=16))
    height = int(_finite_number(height, "画面高度", minimum=16))
    font_size = _finite_number(font_size, "字幕字号", minimum=8)
    margin = _finite_number(margin, "字幕底部边距")
    offset = _finite_number(offset, "片头偏移")
    if font_size > height or margin >= height:
        raise ValueError("字幕字号/底部边距超出画面范围")
    if style not in SUBTITLE_STYLES:
        raise ValueError(f"未知字幕样式：{style}")
    preset = SUBTITLE_STYLES[style]
    font = _safe_font(font_name)
    horizontal_margin = max(12, round(width * 0.06))
    max_units = max(4, int((width - horizontal_margin * 2) / (font_size * 0.53)))
    headers = [
        "[Script Info]", "Title: Hongguo Studio", "ScriptType: v4.00+",
        f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Dialogue,{font},{font_size:g},{preset['primary']},{preset['primary']},{preset['outline']},{preset['back']},{preset['bold']},0,0,0,100,100,0,0,1,{preset['outline_width']},{preset['shadow']},2,{horizontal_margin},{horizontal_margin},{round(margin)},1",
    ]
    events: list[str] = []
    for segment in segments:
        start = _finite_number(segment.start, "台词开始时间")
        end = _finite_number(segment.end, "台词结束时间")
        if end <= start:
            raise ValueError("台词结束时间必须晚于开始时间")
        start_cs = _centiseconds(start + offset)
        end_cs = max(start_cs + 1, _centiseconds(end + offset))
        text = _wrap_text(segment.text, max_units)
        events.append(f"Dialogue: 0,{_ass_time(start_cs)},{_ass_time(end_cs)},Dialogue,,0,0,0,,{text}")
    if watermark and str(watermark.get("text", "")).strip():
        size = _finite_number(watermark.get("font_size", 30), "水印字号", minimum=8)
        if size > height:
            raise ValueError("水印字号超出画面范围")
        duration = _finite_number(watermark.get("duration", 0), "水印时长", minimum=0.01)
        corners = {"左上": 7, "右上": 9, "左下": 1, "右下": 3}
        corner = watermark.get("corner", "右上")
        if corner not in corners:
            raise ValueError("水印位置必须是左上、右上、左下或右下")
        inset = max(12, round(width * 0.025))
        headers.append(f"Style: Watermark,{font},{size:g},&H50FFFFFF,&H50FFFFFF,&H70000000,&HA0000000,-1,0,0,0,100,100,0,0,1,1.5,1,{corners[corner]},{inset},{inset},{inset},1")
        max_watermark_units = max(4, int((width - 2 * inset) / (size * 0.53)))
        text = _wrap_text(watermark["text"], max_watermark_units)
        events.append(f"Dialogue: 1,0:00:00.00,{_ass_time(_centiseconds(duration))},Watermark,,0,0,0,,{text}")
    headers.extend(["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"])
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(headers + events) + "\n", encoding="utf-8-sig")
    return destination
