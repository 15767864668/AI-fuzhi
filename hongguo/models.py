"""跨模块数据契约。时间均为正文时间轴上的秒数。"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from typing import Any
import copy
import math
import re
import uuid


DEFAULT_SETTINGS: dict[str, Any] = {
    "width": 1080, "height": 1920, "fps": 30,
    "video_bitrate": "6000k", "audio_bitrate": "192k", "sample_rate": 48000,
    "fit": "crop", "subtitle_enabled": True, "subtitle_style": "经典白字",
    "font_name": "Noto Sans CJK SC", "font_size": 64, "subtitle_margin": 180,
    "bgm_volume": 0.15, "voice_volume": 1.0, "ducking": True, "denoise": False,
    "watermark_enabled": False, "watermark_text": "", "watermark_corner": "右上",
    "watermark_font_size": 32,
}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Segment:
    id: str = field(default_factory=new_id)
    text: str = ""
    start: float = 0.0
    end: float = 5.0
    character: str = ""
    prompt: str = ""
    trim_start: float = 0.0
    clip_path: str = ""
    fingerprint: str = ""
    remote_task: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Project:
    id: str = field(default_factory=new_id)
    title: str = "未命名短剧"
    segments: list[Segment] = field(default_factory=list)
    characters: dict[str, str] = field(default_factory=dict)
    bgm: str = ""
    intro: str = ""
    outro: str = ""
    settings: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_SETTINGS))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        if not isinstance(data, dict):
            raise ValueError("项目内容必须是 JSON 对象")
        allowed = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in allowed}
        raw_segments = values.pop("segments", [])
        if not isinstance(raw_segments, list) or len(raw_segments) > 5000:
            raise ValueError("项目台词格式错误或超过 5000 段")
        segment_fields = {f.name for f in fields(Segment)}
        if not all(isinstance(segment, dict) for segment in raw_segments):
            raise ValueError("项目台词必须为对象列表")
        segments = [Segment(**{k: v for k, v in s.items() if k in segment_fields}) for s in raw_segments]
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update(values.pop("settings", {}))
        project = cls(segments=segments, settings=settings, **values)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project.id):
            raise ValueError("无效项目 ID")
        validate_segments(segments, allow_empty=True)
        validate_settings(settings)
        if not isinstance(project.characters, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in project.characters.items()):
            raise ValueError("角色素材格式错误")
        return project


def validate_segments(segments: list[Segment], allow_empty: bool = False) -> None:
    if not segments and not allow_empty:
        raise ValueError("请先录入台词")
    seen: set[str] = set()
    previous_end = 0.0
    for index, segment in enumerate(segments, 1):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(segment.id)) or segment.id in seen:
            raise ValueError(f"第 {index} 段 ID 无效或重复（仅支持字母、数字、下划线、连字符）")
        seen.add(segment.id)
        if not str(segment.text).strip():
            raise ValueError(f"第 {index} 段台词不能为空")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in (segment.start, segment.end, segment.trim_start)):
            raise ValueError(f"第 {index} 段时间必须是有限数字")
        if segment.start < 0 or segment.end <= segment.start or segment.trim_start < 0:
            raise ValueError(f"第 {index} 段需满足：0 ≤ 开始 < 结束，裁剪起点 ≥ 0")
        if segment.start < previous_end - 0.001:
            raise ValueError(f"第 {index} 段与上一段重叠或顺序错误；请按时间递增排列")
        if segment.end > 86400:
            raise ValueError("时间轴不能超过 24 小时")
        previous_end = segment.end


def validate_settings(settings: dict) -> None:
    for key, low, high in [("width", 128, 4096), ("height", 128, 4096), ("fps", 1, 60),
                           ("sample_rate", 8000, 96000), ("font_size", 12, 250),
                           ("subtitle_margin", 0, 2000), ("watermark_font_size", 12, 150)]:
        value = settings.get(key, DEFAULT_SETTINGS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high or int(value) != value:
            raise ValueError(f"{key} 必须是 {low}–{high} 范围内的整数")
    if int(settings["width"]) % 2 or int(settings["height"]) % 2:
        raise ValueError("H.264 分辨率宽高必须为偶数")
    for key, high in [("bgm_volume", 1), ("voice_volume", 2)]:
        if not isinstance(settings[key], (int, float)) or not math.isfinite(settings[key]) or not 0 <= settings[key] <= high:
            raise ValueError(f"{key} 超出允许范围")
    for key in ("video_bitrate", "audio_bitrate"):
        if not re.fullmatch(r"[1-9][0-9]{0,5}[kKmM]", str(settings[key])):
            raise ValueError(f"{key} 需采用 6000k、6M 等格式")
    if settings.get("fit") not in ("crop", "pad"):
        raise ValueError("画面适配方式仅支持 crop / pad")
    if not str(settings.get("font_name", "")).strip() or any(c in str(settings["font_name"]) for c in ",\r\n"):
        raise ValueError("字体名称不能为空或包含逗号、换行")
