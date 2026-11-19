"""H3 HTTP adapters: submit once, resume polling, normalize, then download.

The official V2 API returns ``task.content.url``.  Both adapters expose a single
``video_url`` field to the application.  This module never runs a model or TTS.
"""
from __future__ import annotations

import base64
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .models import Segment

Log = Callable[[str], None]
OFFICIAL_BASE = "https://api.minimax.cn"
OFFICIAL_CREATE = "/v2/video_generation"
OFFICIAL_QUERY = "/v2/query/video_generation/{task_id}"
SUCCESS = {"succeeded", "success", "completed", "done"}
FAILED = {"failed", "failure", "error", "cancelled", "canceled"}
RETRYABLE = {408, 425, 429, 500, 502, 503, 504, 529}
MAX_JSON_BYTES = 64 * 1024 * 1024


class BackendError(RuntimeError):
    """A user-facing generation or download failure with secrets removed."""


def _safe_message(value: Any, secret: str = "") -> str:
    """Keep short service messages, without credentials, payloads or signed URLs."""
    import re
    text = str(value)
    if secret:
        text = text.replace(secret, "[隐藏]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [隐藏]", text)
    text = re.sub(r"https?://\S+|data:[^\s]+", "[URL已隐藏]", text)
    return text[:400]


def _url(value: str, label: str) -> str:
    parsed = urlsplit(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BackendError(f"{label}必须是 HTTP/HTTPS 地址。")
    if parsed.username or parsed.password:
        raise BackendError(f"{label}不能在 URL 中包含用户名或密码，请使用 API Key 字段。")
    return value


def _endpoint(base: str, path: str) -> str:
    # Endpoint paths deliberately stay relative to the configured service.
    if "://" in path or path.startswith("//"):
        raise BackendError("接口路径必须是相对路径，不能指向其他主机。")
    return base.rstrip("/") + "/" + path.lstrip("/")


def _delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        try:
            return min(30.0, max(0.0, float(response.headers.get("Retry-After", ""))))
        except ValueError:
            pass
    return min(8.0, 2.0 ** attempt)


def request_json(client: httpx.Client, method: str, url: str, *,
                 payload: dict | None = None, api_key: str = "", retries: int = 2,
                 log: Log = lambda _: None) -> dict:
    """Single encapsulated API request function.

    GET retries transient failures. POST is sent exactly once: retrying a paid
    creation request after a timeout could submit another billable generation.
    TLS verification remains enabled. Credentials are never sent to a CDN.
    """
    method = method.upper()
    attempts = retries + 1 if method == "GET" else 1
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for attempt in range(attempts):
        response = None
        try:
            response = client.request(method, url, json=payload, headers=headers,
                                      follow_redirects=False)
            if response.status_code in RETRYABLE and attempt + 1 < attempts:
                log(f"查询暂时失败（HTTP {response.status_code}），正在重试…")
                time.sleep(_delay(response, attempt))
                continue
            if response.is_redirect:
                raise BackendError("API 返回重定向，请在后端设置中填写最终 API 域名。")
            try:
                data = response.json()
            except ValueError:
                raise BackendError(f"接口返回非 JSON 响应（HTTP {response.status_code}）。") from None
            if not response.is_success:
                error = data.get("error", {}) if isinstance(data, dict) else {}
                detail = error.get("message", "") if isinstance(error, dict) else error
                raise BackendError(f"接口请求失败（HTTP {response.status_code}）: "
                                   f"{_safe_message(detail, api_key)}")
            if not isinstance(data, dict):
                raise BackendError("接口响应应为 JSON 对象。")
            if data.get("error") or data.get("type") == "error":
                error = data.get("error", {})
                detail = error.get("message", "服务端错误") if isinstance(error, dict) else error
                raise BackendError("接口返回错误: " + _safe_message(detail, api_key))
            return data
        except httpx.RequestError as exc:
            if attempt + 1 < attempts:
                log("查询网络异常，正在重试…")
                time.sleep(_delay(response, attempt))
                continue
            suffix = "创建请求不会自动重发，请先核实服务端任务。" if method == "POST" else "已保留任务 ID，可继续查询。"
            raise BackendError(f"网络请求失败（{type(exc).__name__}）。{suffix}") from None
    raise BackendError("接口请求失败。")


def _reference_data(path: str, *, official: bool) -> str:
    ref = Path(path)
    if not ref.is_file():
        raise BackendError(f"参考图文件不存在：{ref.name}")
    if ref.stat().st_size > 30 * 1024 * 1024:
        raise BackendError(f"参考图超过 30 MB：{ref.name}")
    try:
        with Image.open(ref) as im:
            width, height = im.size
            image_format = (im.format or "").lower()
            im.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, SyntaxError):
        raise BackendError(f"参考图损坏或格式不支持：{ref.name}") from None
    mime_format = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(image_format)
    if not mime_format:
        raise BackendError("参考图请使用 JPG、PNG 或 WEBP 格式。")
    if official and (not 256 <= width <= 5760 or not 256 <= height <= 5760
                     or not 0.4 <= width / height <= 2.5):
        raise BackendError(f"参考图 {ref.name} 的边长须为 256–5760 像素，宽高比须为 0.4–2.5。")
    return f"data:image/{mime_format};base64," + base64.b64encode(ref.read_bytes()).decode("ascii")


def build_payload(segment: Segment, references: list[str], config: dict) -> dict:
    """Build the official V2 payload or the documented GPU adapter contract."""
    backend = config.get("backend", "official")
    if backend not in {"official", "gpu"}:
        raise BackendError("未知后端，请选择 MiniMax 官方或自建 GPU。")
    model = str(config.get("model") or "MiniMax-H3")
    needed = float(segment.duration) + float(segment.trim_start)
    if not math.isfinite(needed) or needed <= 0 or float(segment.trim_start) < 0:
        raise BackendError("片段时长须大于 0，裁剪起点须为非负数。")
    duration = max(5 if model == "MiniMax-H3-Max" else 4, math.ceil(needed - 1e-9))
    if duration > 15:
        raise BackendError(f"片段 {segment.id} 所需时长为 {duration} 秒，超过 H3 的 15 秒上限；请拆分台词或缩短裁剪起点。")
    resolution = str(config.get("resolution") or "768P")
    ratio = str(config.get("ratio") or "9:16")
    if backend == "official":
        supported = {"MiniMax-H3": {"768P", "2K"},
                     "MiniMax-H3-Highspeed": {"480P", "768P"},
                     "MiniMax-H3-Lite": {"480P", "768P"},
                     "MiniMax-H3-Max": {"480P", "768P"}}
        if model not in supported:
            raise BackendError("官方 H3 模型名称无效，请检查配置。")
        if resolution not in supported[model]:
            raise BackendError(f"{model} 不支持分辨率 {resolution}。")
        if ratio not in {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
            raise BackendError("官方 H3 宽高比无效。")
        if ratio == "adaptive" and (not references or model == "MiniMax-H3-Lite"):
            raise BackendError("当前输入或模型需要显式宽高比，例如 9:16。")
    if len(references) > 9:
        raise BackendError("每个片段最多传入 9 张角色参考图。")
    character = segment.character.strip() or "角色"
    prompt = (f"{segment.prompt.strip()}\n{character}自然说出以下中文台词：『{segment.text.strip()}』。"
              "保留清晰的原生人声音频，嘴型与台词同步。人物外观遵循参考图。"
              "画面不添加字幕、不添加文字。不要配乐。")
    if len(prompt) > 7000:
        raise BackendError("画面提示词和台词合计过长，H3 文本上限为 7000 字符。")
    images = [_reference_data(p, official=backend == "official") for p in references]
    if backend == "official":
        payload = {"model": model,
                   "content": [{"type": "text", "text": prompt}] + [
                       {"type": "image_url", "image_url": {"url": data}, "role": "reference_image"}
                       for data in images],
                   "resolution": resolution, "duration": duration, "ratio": ratio}
    else:
        payload = {"model": model, "prompt": prompt, "dialogue": segment.text,
                   "character": segment.character, "duration": duration,
                   "resolution": resolution, "ratio": ratio,
                   "reference_images": images}
    extra = config.get("extra_json") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            raise BackendError("额外参数必须是合法 JSON 对象。") from None
    if not isinstance(extra, dict):
        raise BackendError("额外参数必须是 JSON 对象。")
    forbidden = set(payload) | {"api_key", "authorization", "token"}
    if any(str(key).lower() in forbidden for key in extra):
        raise BackendError("额外参数不能覆盖已显示的请求字段或包含密钥。")
    payload.update(extra)
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise BackendError("请求体超过 64 MB，请减少或压缩参考图片。")
    return payload


def normalize_response(data: dict, backend: str) -> dict:
    """Normalize service-specific field names to a tiny internal contract."""
    def text_field(value: Any, label: str, limit: int = 10000) -> str:
        if value is None:
            return ""
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise BackendError(f"接口响应 {label} 字段格式错误。")
        text = str(value)
        if len(text) > limit or any(ord(c) < 32 for c in text):
            raise BackendError(f"接口响应 {label} 字段格式错误。")
        return text
    if backend == "official":
        task = data.get("task") or {}
        if not isinstance(task, dict):
            raise BackendError("官方响应 task 字段格式错误。")
        content = task.get("content") or {}
        if not isinstance(content, dict):
            raise BackendError("官方响应 content 字段格式错误。")
        return {"task_id": text_field(data.get("task_id") or task.get("id") or "", "task_id", 256),
                "status": text_field(task.get("status") or "queued", "status", 40).lower(),
                "video_url": text_field(content.get("url") or "", "video_url")}
    return {"task_id": text_field(data.get("task_id") or "", "task_id", 256),
            "status": text_field(data.get("status") or ("succeeded" if data.get("video_url") else "queued"), "status", 40).lower(),
            "video_url": text_field(data.get("video_url") or "", "video_url")}


def download_video(video_url: str, destination: Path, *, timeout: float = 60,
                   max_bytes: int = 1024 * 1024 * 1024, log: Log = lambda _: None,
                   transport: httpx.BaseTransport | None = None) -> Path:
    """Download MP4 atomically, with bounded retries and no API Authorization."""
    _url(video_url, "视频下载地址")
    if max_bytes <= 0:
        raise BackendError("视频下载大小上限必须大于零。")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    for attempt in range(3):
        response = None
        try:
            partial.unlink(missing_ok=True)
            # A fresh client intentionally has no API key/default auth headers.
            with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport) as client:
                with client.stream("GET", video_url) as response:
                    if response.status_code in RETRYABLE and attempt < 2:
                        log(f"下载暂时失败（HTTP {response.status_code}），正在重试…")
                        time.sleep(_delay(response, attempt))
                        continue
                    if not response.is_success:
                        raise BackendError(f"视频下载失败（HTTP {response.status_code}），任务信息已保存。")
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise BackendError("视频超过下载大小上限。")
                    total = 0
                    next_report = 20 * 1024 * 1024
                    with partial.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise BackendError("视频超过下载大小上限。")
                            handle.write(chunk)
                            if total >= next_report:
                                log(f"已下载 {total / 1024 / 1024:.0f} MB…")
                                next_report += 20 * 1024 * 1024
            with partial.open("rb") as handle:
                header = handle.read(64)
            if total < 32 or b"ftyp" not in header:
                raise BackendError("下载内容不是有效的 MP4 容器；请检查 video_url，任务信息已保存。")
            partial.replace(destination)
            log(f"片段下载完成：{destination.name}（{total / 1024 / 1024:.1f} MB）")
            return destination
        except httpx.RequestError as exc:
            if attempt < 2:
                log("视频下载网络中断，正在重试…")
                time.sleep(_delay(response, attempt))
                continue
            raise BackendError(f"视频下载失败（{type(exc).__name__}）；再次执行可复用已保存任务。") from None
        except OSError as exc:
            raise BackendError(f"本地视频保存失败：{type(exc).__name__}，请检查磁盘空间及文件权限。") from None
        finally:
            partial.unlink(missing_ok=True)
    raise BackendError("视频下载失败，任务信息已保存。")


def generate_clip(segment: Segment, references: list[str], config: dict,
                  destination: Path, log: Log,
                  checkpoint: Callable[[dict], None] | None = None) -> dict:
    """Submit/resume one H3 generation, returning video_url/local_path/task_id.

    Checkpoint metadata is JSON-safe and excludes the API key.  A timeout while
    submitting without a returned ID is explicitly marked ambiguous; a later
    invocation will not silently create another potentially billable task.
    """
    backend = config.get("backend", "official")
    if backend not in {"official", "gpu"}:
        raise BackendError("未知 H3 后端。")
    base = _url(str(config.get("base_url") or (OFFICIAL_BASE if backend == "official" else "http://127.0.0.1:8000")), "后端地址").rstrip("/")
    api_key = str(config.get("api_key") or os.getenv("MINIMAX_API_KEY" if backend == "official" else "H3_GPU_API_KEY", ""))
    if backend == "official" and not api_key:
        raise BackendError("请在界面或 MINIMAX_API_KEY 环境变量中填写官方 API Key。")
    if backend == "official" and not base.startswith("https://"):
        raise BackendError("官方 API 必须使用 HTTPS。")
    metadata = dict(segment.remote_task or {})
    create_path = str(config.get("create_path") or (OFFICIAL_CREATE if backend == "official" else "/generate"))
    # A corrected polling route must reuse the saved task rather than trigger a
    # second paid POST. Explicit current settings win; omitted settings retain a
    # previously configured custom route, then fall back to backend defaults.
    query_path = str(config.get("query_path") or metadata.get("query_path")
                     or (OFFICIAL_QUERY if backend == "official" else "/tasks/{task_id}"))
    timeout = float(config.get("timeout", 60))
    interval = float(config.get("poll_interval", 5))
    max_wait = float(config.get("max_wait", 1800))
    if not all(math.isfinite(v) and v > 0 for v in (timeout, interval, max_wait)):
        raise BackendError("请求超时、查询间隔及最长等待时间必须为正数。")
    _endpoint(base, create_path)
    _endpoint(base, query_path)
    if metadata and (metadata.get("backend") != backend or metadata.get("base_url", "").rstrip("/") != base):
        raise BackendError("当前片段保存了其他后端的远程任务；请先明确重生成该片段后再切换服务。")
    if metadata.get("status") == "submission_unknown":
        raise BackendError("上次创建请求的结果不确定。请先在服务端核实任务；明确重新生成会创建新的计费任务。")
    if metadata.get("status") in FAILED:
        raise BackendError("该远程任务已失败或取消，请选择明确重生成以创建新任务。")

    def save(**updates: Any) -> None:
        metadata.update(updates)
        segment.remote_task = dict(metadata)
        if checkpoint:
            checkpoint(dict(metadata))

    transport = config.get("_transport")  # Test injection; never persisted in projects.
    with httpx.Client(timeout=timeout, transport=transport) as client:
        if not metadata.get("task_id") and not metadata.get("video_url"):
            payload = build_payload(segment, references, config)
            save(backend=backend, base_url=base, query_path=query_path, status="submission_unknown")
            log(f"正在提交片段 {segment.id}（仅提交一次）…")
            try:
                data = request_json(client, "POST", _endpoint(base, create_path),
                                    payload=payload, api_key=api_key, log=log)
            except BackendError:
                # Keep ambiguity marker: even a malformed response can hide a created task.
                raise
            result = normalize_response(data, backend)
            if not result["task_id"] and not result["video_url"]:
                raise BackendError("创建响应缺少 task_id/video_url，结果不确定；请核实服务端任务。")
            save(**result)
        else:
            if metadata.get("query_path") != query_path:
                save(query_path=query_path)
            log(f"复用片段 {segment.id} 已保存的远程任务，无需重新提交。")
        started = time.monotonic()
        while not metadata.get("video_url"):
            status = str(metadata.get("status", "queued")).lower()
            if status in FAILED:
                raise BackendError(f"H3 任务 {metadata.get('task_id')} 已失败或取消，请在服务端查看原因。")
            if time.monotonic() - started >= max_wait:
                raise BackendError("等待 H3 超时；任务 ID 已保存，之后继续执行可恢复查询。")
            if "{task_id}" not in query_path:
                raise BackendError("异步查询路径必须包含 {task_id} 占位符。")
            task_path = query_path.replace("{task_id}", quote(str(metadata["task_id"]), safe=""))
            data = request_json(client, "GET", _endpoint(base, task_path), api_key=api_key, log=log)
            result = normalize_response(data, backend)
            if not result["task_id"]:
                result["task_id"] = metadata["task_id"]
            save(**result)
            status = result["status"]
            log(f"H3 任务 {metadata['task_id']}：{status}")
            if status in FAILED:
                raise BackendError(f"H3 任务 {metadata['task_id']} 已失败或取消，请在服务端查看原因。")
            if status in SUCCESS and not result["video_url"]:
                raise BackendError("H3 任务已成功但响应没有视频 URL，请检查后端返回协议。")
            if not result["video_url"]:
                remaining = max_wait - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(min(interval, remaining))
        if str(metadata.get("status", "")).lower() in FAILED:
            raise BackendError("H3 任务已失败或取消，未下载返回的视频地址。")
        local = download_video(metadata["video_url"], destination, timeout=timeout,
                               max_bytes=int(config.get("max_download_bytes", 1024 ** 3)),
                               log=log, transport=transport)
        save(status="succeeded")
        return {"video_url": metadata["video_url"], "local_path": str(local),
                "task_id": str(metadata.get("task_id", ""))}
