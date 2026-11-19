"""FFmpeg 成片流水线：本模块只使用本机 ffmpeg / ffprobe，不下载二进制。

剧本 start/end 是不含片头的成片绝对时间。每行占据一段时间，空隙补黑场；
trim_start 是该行源视频的裁剪起点。统一规格后用 concat 拼接，最后一次烧字幕、
混音和编码。所有外部命令均传参数列表，字幕滤镜只使用暂存目录内固定文件名。
"""
from __future__ import annotations

from array import array
from collections import deque
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

from .subtitles import write_ass

Log = Callable[[str], None]
EXPORT_PRESETS = {
    "红果竖屏·1080P 30帧（建议）": {"width": 1080, "height": 1920, "fps": 30, "video_bitrate": "6000k", "audio_bitrate": "192k", "sample_rate": 48000},
    "红果竖屏·720P 30帧（轻量）": {"width": 720, "height": 1280, "fps": 30, "video_bitrate": "3000k", "audio_bitrate": "128k", "sample_rate": 48000},
    "红果竖屏·1080P 25帧": {"width": 1080, "height": 1920, "fps": 25, "video_bitrate": "6000k", "audio_bitrate": "192k", "sample_rate": 48000},
    "红果横屏·1080P 30帧（建议）": {"width": 1920, "height": 1080, "fps": 30, "video_bitrate": "6000k", "audio_bitrate": "192k", "sample_rate": 48000},
    "红果横屏·720P 30帧（轻量）": {"width": 1280, "height": 720, "fps": 30, "video_bitrate": "3000k", "audio_bitrate": "128k", "sample_rate": 48000},
    "红果横屏·1080P 25帧": {"width": 1920, "height": 1080, "fps": 25, "video_bitrate": "6000k", "audio_bitrate": "192k", "sample_rate": 48000},
}


class MediaError(RuntimeError):
    """可直接向用户展示的媒体处理异常。"""


def _bin(name: str) -> str:
    return os.environ.get("FFMPEG_BIN" if name == "ffmpeg" else "FFPROBE_BIN", name)


def check_dependencies() -> list[str]:
    """检查可执行程序与必需滤镜；返回问题列表，空列表表示检查通过。"""
    problems = []
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(_bin(name)):
            problems.append(f"找不到 {name}：请安装到 PATH，或设置对应 *_BIN 环境变量。")
    if problems:
        return problems
    try:
        filters = subprocess.run([_bin("ffmpeg"), "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15, check=True).stdout
        for required in ("subtitles", "sidechaincompress", "afftdn", "alimiter"):
            if not re.search(r"\b" + required + r"\s", filters):
                problems.append(f"当前 FFmpeg 缺少 {required} 滤镜，请更换带 libass 的完整构建。")
        encoders = subprocess.run([_bin("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15, check=True).stdout
        for required in ("libx264", "aac"):
            if not re.search(r"\b" + required + r"\s", encoders):
                problems.append(f"当前 FFmpeg 缺少 {required} 编码器。")
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"FFmpeg 自检失败：{type(exc).__name__}")
    return problems


def probe(path: str | Path) -> dict:
    """读取完整 ffprobe JSON；上层可直接查看 streams / format。"""
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"媒体文件不存在：{path.name}")
    try:
        result = subprocess.run([_bin("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path.resolve())], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except FileNotFoundError as exc:
        raise MediaError("找不到 ffprobe，请先安装 FFmpeg 并配置 PATH。") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"读取媒体超时：{path.name}") from exc
    if result.returncode:
        raise MediaError(f"无法读取媒体 {path.name}：{result.stderr[-1500:]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe 返回无效 JSON：{path.name}") from exc
    if not data.get("streams"):
        raise MediaError(f"文件没有可用音视频流：{path.name}")
    return data


def _stream(info: dict, kind: str) -> dict:
    return next((x for x in info.get("streams", []) if x.get("codec_type") == kind), {})


def _duration(info: dict, kind: str = "video") -> float:
    stream = _stream(info, kind)
    for value in (stream.get("duration"), info.get("format", {}).get("duration")):
        try:
            duration = float(value)
            if math.isfinite(duration) and duration > 0:
                return duration
        except (TypeError, ValueError):
            pass
    raise MediaError("媒体时长不可读，不能安全裁剪。")


def _fraction(value: str) -> float:
    try:
        nums = str(value).split("/")
        return float(nums[0]) / float(nums[1]) if len(nums) == 2 else float(nums[0])
    except (ValueError, ZeroDivisionError):
        return 0


def validate_specs(path: str | Path, settings: dict) -> list[str]:
    """按当前导出目标检查规格；这是本地预检，并非平台审核认证。"""
    info = probe(path)
    video, audio = _stream(info, "video"), _stream(info, "audio")
    warnings = []
    name = Path(path).name
    if not video:
        warnings.append(f"{name}：缺少视频流。")
    else:
        target = (int(settings.get("width", 1080)), int(settings.get("height", 1920)))
        actual = (video.get("width"), video.get("height"))
        if actual != target:
            warnings.append(f"{name}：分辨率 {actual[0]}×{actual[1]} 与导出目标 {target[0]}×{target[1]} 不同，将统一缩放/裁切。")
        fps = _fraction(video.get("avg_frame_rate", "0"))
        if abs(fps - float(settings.get("fps", 30))) > .05:
            warnings.append(f"{name}：帧率 {fps:.3f} 与目标 {settings.get('fps', 30)} 不同，将统一帧率。")
        if video.get("codec_name") != "h264":
            warnings.append(f"{name}：视频编码为 {video.get('codec_name')}，导出会转换为 H.264。")
        if video.get("pix_fmt") != "yuv420p":
            warnings.append(f"{name}：像素格式为 {video.get('pix_fmt')}，导出会转换为 yuv420p。")
    if not audio:
        warnings.append(f"{name}：缺少音轨。片头片尾会补静音；H3 正片片段必须带人声音轨。")
    else:
        target_rate = int(settings.get("sample_rate", 48000))
        if int(audio.get("sample_rate", 0)) != target_rate:
            warnings.append(f"{name}：音频采样率 {audio.get('sample_rate')} Hz 与目标 {target_rate} Hz 不同，将重采样。")
        if audio.get("codec_name") != "aac":
            warnings.append(f"{name}：音频编码为 {audio.get('codec_name')}，导出会转换为 AAC。")
        if audio.get("channels") != 2:
            warnings.append(f"{name}：音频声道数为 {audio.get('channels')}，导出会统一为双声道。")
    return warnings


def _run(args: list[str], cwd: Path, log: Log, label: str) -> None:
    """读取 FFmpeg 合并输出，持续报告进度，保留有限错误尾部避免日志无限增长。"""
    command = [_bin("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-threads", "2", "-filter_threads", "1", "-filter_complex_threads", "1", "-progress", "pipe:1", "-nostats", *args]
    tail: deque[str] = deque(maxlen=45)
    previous = -1
    log(label)
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise MediaError("找不到 ffmpeg，请安装并加入 PATH。") from exc
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            if line.startswith("out_time_us="):
                try:
                    current = int(line.split("=", 1)[1]) // 1_000_000
                    if current >= previous + 3:
                        previous = current
                        log(f"{label} · 已处理 {current} 秒")
                except ValueError:
                    pass
            elif "=" not in line or "error" in line.lower():
                log(line[:1000])
        code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    if code:
        raise MediaError(f"{label}失败（FFmpeg 返回 {code}）：\n" + "\n".join(tail)[-6000:])


def analyze_audio(path: str | Path, log: Log = lambda _: None) -> list[str]:
    """流式检测低于 -50 dBFS 的连续静音和接近满幅的疑似削波。

    使用 0.1 秒窗口，静音至少 0.6 秒；峰值判据不能判断失真原因，只作告警。
    保留源采样率与各声道分别取样，避免下混抵消或降采样掩盖满幅采样。
    不把完整音频载入内存，stderr 暂存文件避免管道堵塞。
    """
    info = probe(path)
    if not _stream(info, "audio"):
        return [f"{Path(path).name}：没有音轨。"]
    audio_stream = _stream(info, "audio")
    rate = int(audio_stream.get("sample_rate", 48000))
    channels = int(audio_stream.get("channels", 2))
    if not 1000 <= rate <= 768000 or not 1 <= channels <= 64:
        raise MediaError("音频采样率或声道数量异常，无法安全检测。")
    window = max(1, rate // 10)
    warnings = []
    silence_start = clip_start = None
    elapsed = 0.0
    log(f"音频检测：{Path(path).name}")
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen([_bin("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-i", str(Path(path).resolve()), "-vn", "-map", "0:a:0", "-ac", str(channels), "-ar", str(rate), "-f", "f32le", "pipe:1"], stdout=subprocess.PIPE, stderr=errors)
        except FileNotFoundError as exc:
            raise MediaError("找不到 ffmpeg，无法检测音频。") from exc
        assert proc.stdout is not None
        try:
            while True:
                raw = proc.stdout.read(window * channels * 4)
                if not raw:
                    break
                samples = array("f")
                samples.frombytes(raw[:len(raw) // 4 * 4])
                if sys.byteorder != "little":
                    samples.byteswap()
                if not samples:
                    break
                step = len(samples) / (rate * channels)
                rms = math.sqrt(sum(float(x) * float(x) for x in samples) / len(samples))
                silent = rms <= 10 ** (-50 / 20)
                # 至少 3 个采样点接近满幅，排除单个数值异常；报告为“疑似”。
                clipped = sum(abs(x) >= .995 for x in samples) >= 3
                if silent and silence_start is None:
                    silence_start = elapsed
                if not silent and silence_start is not None:
                    if elapsed - silence_start >= .599:
                        warnings.append(f"静音：{silence_start:.2f}–{elapsed:.2f} 秒（{Path(path).name}，≤ -50 dBFS）")
                    silence_start = None
                if clipped and clip_start is None:
                    clip_start = elapsed
                if not clipped and clip_start is not None:
                    warnings.append(f"疑似爆音/削波：{clip_start:.2f}–{elapsed:.2f} 秒（{Path(path).name}，近满幅采样）")
                    clip_start = None
                elapsed += step
            code = proc.wait()
            if code:
                errors.seek(0)
                raise MediaError("音频解码失败：" + errors.read().decode("utf-8", "replace")[-1500:])
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
        finally:
            proc.stdout.close()
    if silence_start is not None and elapsed - silence_start >= .599:
        warnings.append(f"静音：{silence_start:.2f}–{elapsed:.2f} 秒（{Path(path).name}，≤ -50 dBFS）")
    if clip_start is not None:
        warnings.append(f"疑似爆音/削波：{clip_start:.2f}–{elapsed:.2f} 秒（{Path(path).name}，近满幅采样）")
    for warning in warnings:
        log("告警：" + warning)
    return warnings


def _validated(settings: dict) -> dict:
    settings = dict(settings)
    for key, default, low, high in (("width", 1080, 2, 7680), ("height", 1920, 2, 7680), ("fps", 30, 1, 60), ("sample_rate", 48000, 8000, 192000)):
        value = float(settings.get(key, default))
        if not math.isfinite(value) or int(value) != value or not low <= value <= high:
            raise MediaError(f"参数 {key} 必须是 {low}–{high} 的整数。")
        settings[key] = int(value)
    if settings["width"] % 2 or settings["height"] % 2:
        raise MediaError("H.264 yuv420p 输出宽高必须是偶数。")
    for key, default in (("video_bitrate", "6000k"), ("audio_bitrate", "192k")):
        value = str(settings.get(key, default))
        if not re.fullmatch(r"[1-9]\d{0,7}[kKmM]?", value):
            raise MediaError(f"{key} 格式错误，例如 6000k、192k。")
        settings[key] = value
    for key, default, high in (("bgm_volume", .15, 1), ("voice_volume", 1, 2)):
        value = float(settings.get(key, default))
        if not math.isfinite(value) or not 0 <= value <= high:
            raise MediaError(f"{key} 必须在 0–{high} 范围。")
        settings[key] = value
    if settings.get("fit", "crop") not in ("pad", "crop"):
        raise MediaError("画面适配模式只能为 crop 或 pad。")
    return settings


def _video_filter(settings: dict) -> str:
    width, height = settings["width"], settings["height"]
    if settings.get("fit", "crop") == "pad":
        fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    else:
        fit = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    return f"{fit},setsar=1,fps={settings['fps']},format=yuv420p,setpts=PTS-STARTPTS"


def _normalize(source: str, target: str, duration: float, trim: float, settings: dict, stage: Path, log: Log, *, require_audio: bool = False) -> None:
    info = probe(source)
    if not _stream(info, "video"):
        raise MediaError(f"不是视频文件：{Path(source).name}")
    available = _duration(info)
    if available + .08 < trim + duration:
        raise MediaError(f"片段 {Path(source).name} 时长不足：需从 {trim:.3f}s 裁出 {duration:.3f}s，源视频只有 {available:.3f}s。请调整时间轴/裁剪起点或重生成。")
    has_audio = bool(_stream(info, "audio"))
    if require_audio and not has_audio:
        raise MediaError(f"H3 片段 {Path(source).name} 没有音轨，请检查后端是否生成原生人声。")
    args = ["-ss", f"{trim:.6f}", "-i", str(Path(source).resolve())]
    if not has_audio:
        args += ["-f", "lavfi", "-i", f"anullsrc=r={settings['sample_rate']}:cl=stereo"]
    args += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf", _video_filter(settings), "-af", f"aresample={settings['sample_rate']}:async=1:first_pts=0,apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS", "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "2", "-c:a", "pcm_s16le", "-ac", "2", "-ar", str(settings["sample_rate"]), target]
    _run(args, stage, log, f"统一片段 {Path(source).name}")


def _black(target: str, duration: float, settings: dict, stage: Path, log: Log) -> None:
    _run(["-f", "lavfi", "-i", f"color=c=black:s={settings['width']}x{settings['height']}:r={settings['fps']}", "-f", "lavfi", "-i", f"anullsrc=r={settings['sample_rate']}:cl=stereo", "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ac", "2", target], stage, log, "补齐时间轴黑场")


def render_project(project, output_path: str | Path, log: Log = lambda _: None) -> dict:
    """执行完整流水线，成功后原子发布 MP4 / ASS，失败保留原有成片。

    返回 output/subtitle 路径和全部告警。ASS 位于成片同名旁边，上层可移到字幕分类目录。
    """
    problems = check_dependencies()
    if problems:
        raise MediaError("\n".join(problems))
    settings = _validated(project.settings)
    if not project.segments:
        raise MediaError("剧本没有台词片段。")
    segments = sorted(project.segments, key=lambda s: float(s.start))
    cursor = 0.0
    warnings: list[str] = []
    for segment in segments:
        if not all(math.isfinite(float(v)) for v in (segment.start, segment.end, segment.trim_start)) or segment.start < 0 or segment.end <= segment.start or segment.trim_start < 0:
            raise MediaError(f"片段 {segment.id} 的起止时间或裁剪起点无效。")
        if segment.start < cursor - .000001:
            raise MediaError(f"片段 {segment.id} 与前一片段时间重叠。")
        if not segment.clip_path:
            raise MediaError(f"片段 {segment.id} 尚未生成或未关联本地视频。")
        cursor = segment.end
    if settings["width"] >= settings["height"]:
        warnings.append("当前导出不是竖屏，请确认符合目标平台当前要求。")
    if settings["sample_rate"] not in (44100, 48000):
        warnings.append("当前采样率不是常用 44.1/48 kHz，上传兼容性需自行确认。")
    sources = [s.clip_path for s in segments] + [x for x in (project.intro, project.outro) if x]
    for source in dict.fromkeys(sources):
        warnings.extend(validate_specs(source, settings))
        warnings.extend(analyze_audio(source, log))
    for segment in segments:
        source_info = probe(segment.clip_path)
        if _stream(source_info, "audio") and _duration(source_info, "audio") + .08 < segment.trim_start + segment.end - segment.start:
            warnings.append(f"片段 {segment.id}：音轨短于所选裁剪区间，末尾将补静音，请核查台词是否完整。")
    if any(abs(round(float(t) * settings["fps"]) / settings["fps"] - float(t)) > .001 for s in segments for t in (s.start, s.end)):
        warnings.append(f"时间轴将对齐至 {settings['fps']} fps 的最近画面帧，最大调整不超过半帧；字幕保留输入时间。")
    for warning in warnings:
        log("导出前告警：" + warning)
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".render_", dir=output_path.parent) as temporary:
        stage = Path(temporary)
        fps = settings["fps"]
        frames = lambda value: int(round(float(value) * fps))
        parts: list[str] = []
        intro_duration = 0.0
        total_frames = 0

        def add_source(source: str, length_frames: int, trim: float = 0, required: bool = False):
            nonlocal total_frames
            if length_frames < 1:
                raise MediaError("片段不足一帧，请调整时间轴。")
            name = f"part_{len(parts):05d}.mkv"
            _normalize(source, name, length_frames / fps, trim, settings, stage, log, require_audio=required)
            parts.append(name)
            total_frames += length_frames

        if project.intro:
            intro_frames = frames(_duration(probe(project.intro)))
            add_source(project.intro, intro_frames)
            intro_duration = intro_frames / fps
        body_cursor = 0
        for segment in segments:
            start_frame, end_frame = frames(segment.start), frames(segment.end)
            if start_frame < body_cursor:
                raise MediaError("时间轴量化到输出帧率后发生重叠，请调整起止时间。")
            if start_frame > body_cursor:
                gap = start_frame - body_cursor
                name = f"part_{len(parts):05d}.mkv"
                _black(name, gap / fps, settings, stage, log)
                parts.append(name)
                total_frames += gap
            add_source(segment.clip_path, end_frame - start_frame, float(segment.trim_start), True)
            body_cursor = end_frame
        if project.outro:
            add_source(project.outro, frames(_duration(probe(project.outro))))
        duration = total_frames / fps
        (stage / "timeline.ffconcat").write_text("ffconcat version 1.0\n" + "".join(f"file '{name}'\n" for name in parts), encoding="ascii")
        watermark = None
        if settings.get("watermark_enabled") and str(settings.get("watermark_text", "")).strip():
            watermark = {"text": settings["watermark_text"], "corner": settings.get("watermark_corner", "右上"), "font_size": int(settings.get("watermark_font_size", 30)), "duration": duration}
        subtitle_enabled = bool(settings.get("subtitle_enabled", True))
        ass = write_ass(segments if subtitle_enabled else [], stage / "subtitles.ass", settings["width"], settings["height"], style=settings.get("subtitle_style", "经典白字"), font_name=settings.get("font_name", "Noto Sans CJK SC"), font_size=int(settings.get("font_size", 64)), margin=int(settings.get("subtitle_margin", 180)), offset=intro_duration, watermark=watermark)
        args = ["-f", "concat", "-safe", "1", "-i", "timeline.ffconcat"]
        with_bgm = bool(project.bgm)
        if with_bgm:
            bgm_info = probe(project.bgm)
            if not _stream(bgm_info, "audio"):
                raise MediaError("BGM 文件没有音轨。")
            args += ["-stream_loop", "-1", "-i", str(Path(project.bgm).resolve())]
        filters = []
        video = "subtitles=filename=subtitles.ass" if subtitle_enabled or watermark else "null"
        filters.append(f"[0:v]{video},format=yuv420p[v]")
        voice = f"aresample={settings['sample_rate']},aformat=channel_layouts=stereo,volume={settings['voice_volume']:.6f}"
        if settings.get("denoise", False):
            voice += ",afftdn=nf=-25"
        if with_bgm:
            filters.append(f"[0:a]{voice}[voice]")
            filters.append(f"[1:a]aresample={settings['sample_rate']},aformat=channel_layouts=stereo,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,volume={settings['bgm_volume']:.6f}[music]")
            if settings.get("ducking", True):
                filters += ["[voice]asplit=2[spoken][sidechain]", "[music][sidechain]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=250[ducked]", "[spoken][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:level=0[a]"]
            else:
                filters.append("[voice][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:level=0[a]")
        else:
            filters.append(f"[0:a]{voice},alimiter=limit=0.95:level=0[a]")
        args += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]", "-t", f"{duration:.6f}", "-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-b:v", settings["video_bitrate"], "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "aac", "-b:a", settings["audio_bitrate"], "-ar", str(settings["sample_rate"]), "-ac", "2", "-movflags", "+faststart", "rendered.mp4"]
        _run(args, stage, log, "烧录字幕 / 混音 / 导出")
        result_path = stage / "rendered.mp4"
        final_warnings = validate_specs(result_path, settings)
        warnings.extend(final_warnings)
        warnings.extend(analyze_audio(result_path, log))
        os.replace(result_path, output_path)
        subtitle_path = output_path.with_suffix(".ass")
        shutil.copy2(ass, subtitle_path)
    log(f"成片完成：{output_path.name}")
    return {"output": str(output_path), "subtitle": str(subtitle_path), "warnings": list(dict.fromkeys(warnings))}
