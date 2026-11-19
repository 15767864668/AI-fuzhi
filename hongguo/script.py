"""TXT 剧本解析及表格转换。不自动猜测已有时间轴。"""
from __future__ import annotations

import re
from pathlib import Path
from .models import Segment, new_id, validate_segments


def parse_time(value: str) -> float:
    """接受秒数或 HH:MM:SS.mmm / MM:SS.mmm。"""
    parts = value.strip().replace(",", ".").split(":")
    if len(parts) > 3:
        raise ValueError("时间格式错误")
    result = 0.0
    for item in parts:
        result = result * 60 + float(item)
    return result


def parse_script_text(text: str, default_duration: float = 5) -> list[Segment]:
    """每行一句；定时格式：[0-5] 角色|台词|画面提示词，角色/提示词可省。"""
    if not 0 < float(default_duration) <= 3600:
        raise ValueError("默认每段时长需大于 0 且不超过 3600 秒")
    segments: list[Segment] = []
    cursor = 0.0
    for line_number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        start, end = cursor, cursor + float(default_duration)
        if line.startswith("["):
            match = re.match(r"^\[\s*([^\]]+?)\s*(?:-->|–|-)\s*([^\]]+?)\s*\]\s*(.*)$", line)
            if not match:
                raise ValueError(f"第 {line_number} 行时间格式错误；例：[0-5] 女主|台词")
            start, end = parse_time(match[1]), parse_time(match[2])
            line = match[3]
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) == 1:
            character, dialogue, prompt = "", parts[0], ""
        else:
            character, dialogue = parts[:2]
            prompt = parts[2] if len(parts) > 2 else ""
        segments.append(Segment(text=dialogue, start=start, end=end, character=character, prompt=prompt))
        cursor = end
    validate_segments(segments)
    return segments


def read_script(path: str, default_duration: float = 5) -> list[Segment]:
    raw = Path(path).read_bytes()
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("TXT 剧本不能超过 5MB")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return parse_script_text(raw.decode(encoding), default_duration)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码，请将 TXT 保存为 UTF-8")


def rows_from_segments(segments: list[Segment]) -> list[list]:
    return [[s.id, s.text, s.start, s.end, s.character, s.prompt, s.trim_start] for s in segments]


def segments_from_rows(rows, previous: list[Segment] | None = None) -> list[Segment]:
    if hasattr(rows, "values"):
        rows = rows.values.tolist()
    old = {s.id: s for s in previous or []}
    segments = []
    for index, row in enumerate(rows or [], 1):
        row = list(row) + [""] * max(0, 7 - len(row))
        if not any(str(v).strip() for v in row if v is not None):
            continue
        segment_id = str(row[0] or "").strip() or new_id()
        try:
            segment = Segment(id=segment_id, text=str(row[1] or "").strip(), start=float(row[2]),
                              end=float(row[3]), character=str(row[4] or "").strip(),
                              prompt=str(row[5] or "").strip(), trim_start=float(row[6] or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 行起止时间或裁剪起点无效") from exc
        if segment_id in old:
            prior = old[segment_id]
            segment.clip_path, segment.fingerprint, segment.remote_task = prior.clip_path, prior.fingerprint, dict(prior.remote_task)
        segments.append(segment)
    validate_segments(segments)
    return segments
